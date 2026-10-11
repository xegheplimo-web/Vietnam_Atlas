#!/usr/bin/env python3
"""Import only legally reusable Vietnamese business-register CSV into separate SQLite.

It does NOT claim to access national official registry automatically, and it
never fabricates latitude/longitude from addresses.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def normalize(s):
    return str(s or "").strip()


def read_rows(path: Path):
    # --csv-file path is chosen by the local operator running this tool, not untrusted input.
    with path.open(encoding="utf-8-sig", newline="") as f:  # NOSONAR(S8707)
        reader = csv.DictReader(f)
        if not reader.fieldnames or not {"enterprise_id", "legal_name", "registered_address"}.issubset(set(reader.fieldnames)):
            raise ValueError("CSV cần enterprise_id,legal_name,registered_address; có thể thêm tax_code,status,observed_at")
        for row in reader:
            yield {k: normalize(v) for k, v in row.items() if k is not None}


def init_registry(db):
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)  # NOSONAR(S8706)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=45000")
    con.execute("""CREATE TABLE IF NOT EXISTS legal_entities (
          source_name TEXT NOT NULL, enterprise_id TEXT NOT NULL,
          legal_name TEXT NOT NULL, registered_address TEXT NOT NULL,
          tax_code TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
          observed_at TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL,
          rights_basis TEXT NOT NULL, content_hash TEXT NOT NULL,
          first_imported TEXT NOT NULL, last_imported TEXT NOT NULL,
          PRIMARY KEY(source_name, enterprise_id))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_legal_entities_tax_code ON legal_entities(tax_code)")
    con.commit()
    return con


def import_file(path, db, source_name, source_url, rights_basis):
    if not source_name.strip() or not rights_basis.strip():
        raise ValueError("Bắt buộc có tên nguồn và căn cứ quyền sử dụng dữ liệu")
    url = urlsplit(source_url)
    if url.scheme != "https" or not url.hostname:
        raise ValueError("--source-url phải là URL HTTPS của nguồn hợp pháp")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con = init_registry(db)
    counts = {"created": 0, "changed": 0, "unchanged": 0, "invalid": 0}
    try:
        con.execute("BEGIN IMMEDIATE")
        for row in read_rows(Path(path)):
            identifier = row.get("enterprise_id", "")
            name = row.get("legal_name", "")
            address = row.get("registered_address", "")
            if not identifier or not name or not address or max(map(len, (identifier, name, address))) > 2000:
                counts["invalid"] += 1
                continue
            vals = [row.get(x, "") for x in ("tax_code", "status", "observed_at")]
            content = json.dumps([name, address, *vals], ensure_ascii=False)
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            before = con.execute(
                "SELECT content_hash, source_url, rights_basis FROM legal_entities WHERE source_name=? AND enterprise_id=?",
                (source_name, identifier),
            ).fetchone()
            if before and before[0] == content_hash:
                if before[1] != source_url or before[2] != rights_basis:
                    # Same business content but corrected/renewed provenance: refresh metadata, count as changed.
                    con.execute(
                        "UPDATE legal_entities SET source_url=?, rights_basis=?, last_imported=? WHERE source_name=? AND enterprise_id=?",
                        (source_url, rights_basis, stamp, source_name, identifier),
                    )
                    counts["changed"] += 1
                else:
                    counts["unchanged"] += 1
                continue
            con.execute(
                """INSERT INTO legal_entities (source_name,enterprise_id,legal_name,registered_address,tax_code,status,observed_at,source_url,rights_basis,content_hash,first_imported,last_imported)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_name,enterprise_id) DO UPDATE SET
                legal_name=excluded.legal_name,registered_address=excluded.registered_address,
                tax_code=excluded.tax_code,status=excluded.status,observed_at=excluded.observed_at,
                source_url=excluded.source_url,rights_basis=excluded.rights_basis,
                content_hash=excluded.content_hash,last_imported=excluded.last_imported""",
                (source_name, identifier, name, address, *vals, source_url, rights_basis, content_hash, stamp, stamp),
            )
            counts["changed" if before else "created"] += 1
        con.commit()
        counts.update(
            {"database": str(db), "source_name": source_name, "note": "Legal records; not proven to be geocoded physical business locations"}
        )
        return counts
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def main():
    p = argparse.ArgumentParser(description="Import legally licensed business registry CSV (separate from OSM POIs)")
    p.add_argument("csv_file", type=Path)
    p.add_argument("--db", type=Path, default=Path("data/enterprise_registry.sqlite3"))
    p.add_argument("--source-name", required=True)
    p.add_argument("--source-url", required=True)
    p.add_argument("--rights-basis", required=True, help="Lawful reuse permission/reference. Do not invent.")
    a = p.parse_args()
    try:
        print(json.dumps(import_file(a.csv_file, a.db, a.source_name, a.source_url, a.rights_basis), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as e:
        print("ERROR: " + str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
