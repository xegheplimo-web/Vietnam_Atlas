"""Migrate existing place.province names from the 63 legacy units to the 34 new names.

Dry run by default. Creates a SQLite online backup before --apply.
Original address is preserved; update is logged per place.
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from server.store import session, init_db, normalize, save_place


def main():
    ap=argparse.ArgumentParser(description='Migration of old Vietnamese provincial place labels')
    ap.add_argument('--db',default=str(ROOT/'data'/'atlas.sqlite3'))
    ap.add_argument('--apply',action='store_true',help='execute migration after automatic backup')
    args=ap.parse_args()
    if not Path(args.db).exists():raise SystemExit('SQLite database not found. Start the application first.')
    mergers=json.loads((ROOT/'data'/'province_mergers_2025.json').read_text(encoding='utf-8'))
    mapping={}
    for new,olds in mergers['new_to_old'].items():
        for old in olds:
            mapping[normalize(old)]=new
    for old,new in mergers['historical_aliases'].items():mapping[normalize(old)]=new
    to_update=[]
    with session(args.db) as con:
        rows=con.execute("SELECT * FROM places WHERE province<>''").fetchall()
        for row in rows:
            old=row['province']
            new=mapping.get(normalize(old))
            if new and new!=old:
                to_update.append((dict(row),new))
    print(f'Found {len(to_update)} places with old/alias province labels out of {len(rows)} labeled places.')
    for p,new in to_update[:20]:print(f"- {p['id']}: {p['province']} → {new}")
    if not args.apply:
        print('Dry-run only. Pass --apply to back up DB and update records.')
        return
    source=Path(args.db)
    directory=ROOT/'backups';directory.mkdir(exist_ok=True)
    dest=directory/('before-province-migration-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.sqlite3')
    with sqlite3.connect(source) as original,sqlite3.connect(dest) as copied:original.backup(copied)
    print(f'Backup completed: {dest}')
    with session(args.db) as con:
        for row,new in to_update:
            row['province']=new
            save_place(con,row,row['id'])
    print(f'Migrated {len(to_update)} records; original address retained.')

if __name__=='__main__':main()
