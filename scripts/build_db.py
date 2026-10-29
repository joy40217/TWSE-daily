"""把 data/ 底下所有 CSV 匯入一個 SQLite 檔（在本機執行，不進 git）

用法：python scripts/build_db.py  ->  產生 twse.db
每個資料集一張表，多一欄 _key（交易日或資料年月）。
欄位一律存成文字，避免股票代號 0050 被轉成 50；計算時用 CAST，
例如 CAST(REPLACE(ClosingPrice, ',', '') AS REAL)
"""
import csv
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB = ROOT / "twse.db"


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def main():
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    for ds in sorted(p for p in DATA_DIR.iterdir() if p.is_dir()):
        files = sorted(ds.rglob("*.csv"))
        if not files:
            continue
        rows, cols = [], {}
        for f in files:
            with f.open(encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    r["_key"] = f.stem
                    rows.append(r)
                    cols.update(dict.fromkeys(r))
        cols = list(cols)
        con.execute(f"CREATE TABLE {q(ds.name)} ({', '.join(q(c) + ' TEXT' for c in cols)})")
        con.executemany(
            f"INSERT INTO {q(ds.name)} VALUES ({', '.join('?' * len(cols))})",
            ([r.get(c) for c in cols] for r in rows),
        )
        con.execute(f"CREATE INDEX {q('idx_' + ds.name)} ON {q(ds.name)} (_key)")
        print(f"{ds.name}: {len(files)} 個檔案，{len(rows)} 筆")
    con.commit()
    con.close()
    print(f"完成：{DB}")


if __name__ == "__main__":
    main()
