"""Import named places from a UTF-8 CSV into the local SQLite memory.

Columns: name,lat,lon,category,address,province,phone,website,notes,source,source_id,status
Usage: python tools/import_csv.py data/my_places.csv --db data/atlas.sqlite3
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server.store import session, init_db, save_place


def main():
    parser=argparse.ArgumentParser(description='Import CSV places into Vietnam Atlas')
    parser.add_argument('csvfile',help='UTF-8/UTF-8-BOM CSV, one row per place')
    parser.add_argument('--db',default=str(Path(__file__).resolve().parents[1]/'data'/'atlas.sqlite3'))
    parser.add_argument('--max-errors',type=int,default=30)
    args=parser.parse_args()
    init_db(args.db)
    created=updated=invalid=0
    with open(args.csvfile,'r',encoding='utf-8-sig',newline='') as infile:
        reader=csv.DictReader(infile)
        mandatory={'name','lat','lon'}
        if not reader.fieldnames or not mandatory.issubset(reader.fieldnames):
            raise SystemExit('CSV cần tối thiểu cột: name,lat,lon')
        with session(args.db) as con:
            for rownum,row in enumerate(reader,2):
                try:
                    # Keep successful rows even if later entries are invalid.
                    sp='entry_'+str(rownum)
                    con.execute('SAVEPOINT '+sp)
                    payload={k:v for k,v in row.items() if k and v is not None}
                    _,action=save_place(con,payload)
                    con.execute('RELEASE SAVEPOINT '+sp)
                    if action=='create':created+=1
                    else:updated+=1
                except Exception as exc:
                    con.execute('ROLLBACK TO SAVEPOINT '+sp)
                    con.execute('RELEASE SAVEPOINT '+sp)
                    invalid+=1
                    if invalid<=args.max_errors:
                        print(f'Row {rownum}: {exc}',file=sys.stderr)
    print(f'DONE created={created} updated={updated} invalid={invalid} db={args.db}')

if __name__=='__main__':main()
