"""V1-05 drill: backup -> restore -> integrity -> smoke, on a copy (never the master).

Run:  python tools/restore_drill.py --backup-dir backups/corpus-lock-20261017 --out reports/v1-05-drill.json
Exit code 0 only when every gate passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TABLES = ["places", "changes", "metadata", "memory_records", "place_media", "search_aliases"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rowhash(path: Path) -> dict[str, list]:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        out = {}
        for t in TABLES:
            rows = con.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall()
            h = hashlib.sha256()
            for r in rows:
                h.update(repr(r).encode())
            out[t] = [len(rows), h.hexdigest()[:16]]
        return out
    finally:
        con.close()


def integrity(path: Path) -> dict:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        checks = {
            "integrity_check": con.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_violations": len(con.execute("PRAGMA foreign_key_check").fetchall()),
            "journal_mode": con.execute("PRAGMA journal_mode").fetchone()[0],
        }
        try:
            con.execute("SELECT count(*) FROM places_fts").fetchone()
            checks["fts5_ok"] = True
        except sqlite3.Error as exc:  # FTS may be absent in a stripped build
            checks["fts5_ok"] = f"error: {exc}"
        return checks
    finally:
        con.close()


def free_port(preferred: int = 8799) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free port")


def http_json(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        body = r.read()
        return r.status, json.loads(body) if body[:1] in (b"{", b"[") else body[:200].decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup-dir", default="backups/corpus-lock-20261017")
    ap.add_argument("--out", default="reports/v1-05-drill.json")
    ap.add_argument("--keep", action="store_true", help="keep the restore directory")
    args = ap.parse_args()

    backup_dir = (REPO / args.backup_dir).resolve()
    atlas_backups = sorted(backup_dir.glob("atlas-*.sqlite3"))
    gaz_backups = sorted(set(backup_dir.glob("gazetteer-*.sqlite3")) | set(backup_dir.glob("gaz-*.sqlite3")))
    if not atlas_backups or not gaz_backups:
        print("FAIL: backup set incomplete", backup_dir)
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    restore_dir = Path(os.environ.get("TMPDIR", "/tmp")) / f"atlas-v105-{stamp}"
    restore_dir.mkdir(parents=True, exist_ok=True)
    atlas_restore = restore_dir / "atlas.sqlite3"
    gaz_restore = restore_dir / "gazetteer.sqlite3"

    t0 = time.perf_counter()
    shutil.copy2(atlas_backups[-1], atlas_restore)
    shutil.copy2(gaz_backups[-1], gaz_restore)
    restore_seconds = round(time.perf_counter() - t0, 2)

    master = REPO / "data" / "atlas.sqlite3"
    report = {
        "drill": "V1-05 backup->restore->smoke",
        "executed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "backup": {
            "atlas": atlas_backups[-1].name,
            "gazetteer": gaz_backups[-1].name,
            "atlas_sha256": sha256(atlas_backups[-1]),
            "gazetteer_sha256": sha256(gaz_backups[-1]),
        },
        "restore": {
            "dir": str(restore_dir),
            "seconds": restore_seconds,
            "atlas_sha256": sha256(atlas_restore),
            "gazetteer_sha256": sha256(gaz_restore),
        },
        "gates": {},
    }
    report["gates"]["restore_byte_identical"] = (
        report["backup"]["atlas_sha256"] == report["restore"]["atlas_sha256"]
        and report["backup"]["gazetteer_sha256"] == report["restore"]["gazetteer_sha256"]
    )

    rep_atlas = integrity(atlas_restore)
    rep_gaz = integrity(gaz_restore)
    report["integrity"] = {"atlas": rep_atlas, "gazetteer": rep_gaz}
    report["gates"]["integrity_ok"] = (
        rep_atlas["integrity_check"] == "ok"
        and rep_gaz["integrity_check"] == "ok"
        and rep_atlas["foreign_key_violations"] == 0
        and rep_gaz["foreign_key_violations"] == 0
    )

    restored_hash = rowhash(atlas_restore)
    master_hash = rowhash(master) if master.exists() else {}
    report["rowhash"] = {"restored": restored_hash, "live_master": master_hash}
    report["gates"]["content_matches_master"] = bool(master_hash) and restored_hash == master_hash

    port = free_port()
    t1 = time.perf_counter()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", ATLAS_GAZETTEER_DB=str(gaz_restore))
    log = (restore_dir / "server.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "server.app", "--db", str(atlas_restore), "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
    )
    health = None
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                status, payload = http_json(f"http://127.0.0.1:{port}/api/health", timeout=2)
                if status == 200:
                    health = payload
                    break
            except Exception:
                time.sleep(0.3)
        startup_seconds = round(time.perf_counter() - t1, 2)
        report["smoke"] = {"port": port, "startup_seconds": startup_seconds, "health": health, "endpoints": {}}
        if health is not None:
            base = f"http://127.0.0.1:{port}"
            probes = {
                "stats": "/api/stats",
                "search_pho": "/api/places?search=ph%E1%BB%9F&limit=3",
                "admin_resolve": "/api/admin/resolve?q=b%E1%BB%87nh%20vi%E1%BB%87n%20Tr%E1%BA%A3ng%20Bom",
                "provinces": "/api/provinces",
            }
            for name, path in probes.items():
                try:
                    status, payload = http_json(base + path)
                    summary = payload
                    if isinstance(payload, dict):
                        summary = {k: payload[k] for k in list(payload)[:6]}
                    report["smoke"]["endpoints"][name] = {"status": status, "body": summary}
                except Exception as exc:
                    report["smoke"]["endpoints"][name] = {"error": repr(exc)}
            try:
                with urllib.request.urlopen(base + "/", timeout=10) as r:
                    html = r.read(400).decode("utf-8", "replace")
                report["smoke"]["endpoints"]["ui"] = {"status": 200, "has_leaflet": "leaflet" in html.lower()}
            except Exception as exc:
                report["smoke"]["endpoints"]["ui"] = {"error": repr(exc)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    ends = report.get("smoke", {}).get("endpoints", {})
    report["gates"]["health_ok"] = bool(health) and health.get("status") in (None, "ok")
    report["gates"]["smoke_ok"] = all(ends.get(k, {}).get("status") == 200 for k in ("stats", "search_pho", "admin_resolve", "provinces", "ui"))
    report["gates"]["smoke_rto_seconds"] = report.get("smoke", {}).get("startup_seconds")

    report["passed"] = all(
        report["gates"].get(k) for k in ("restore_byte_identical", "integrity_ok", "content_matches_master", "health_ok", "smoke_ok")
    )

    out = REPO / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({k: report["gates"][k] for k in report["gates"]}, ensure_ascii=False, indent=2))
    print("PASSED:", report["passed"])
    print("stats:", json.dumps(ends.get("stats", {}).get("body", {}), ensure_ascii=False))
    print("search_pho:", json.dumps(ends.get("search_pho", {}).get("body", {}), ensure_ascii=False)[:300])
    print("out:", out)

    if not args.keep:
        shutil.rmtree(restore_dir, ignore_errors=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
