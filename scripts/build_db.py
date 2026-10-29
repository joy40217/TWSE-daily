"""把 data/ 底下所有 CSV 匯入 SQLite，並計算還原權息日 K（在本機執行，不進 git）

用法：python scripts/build_db.py  ->  產生 twse.db

原始資料表：每個資料集一張，欄位一律存文字（避免 0050 變成 50），多一欄 _key（日期或年月）。
stock_day_adj：整理好的日 K，價格已轉數字，並附還原權息後的開高低收：
    date, code, name, open, high, low, close, volume, value, transactions,
    adj_factor, adj_open, adj_high, adj_low, adj_close
還原方式：每次除權息的因子 = 除權息參考價 / 除權息前收盤價，
除權息日「之前」的所有價格乘上之後各次因子的連乘積（最新價格維持不變）。
"""
import bisect
import csv
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB = ROOT / "twse.db"


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def num(v):
    """'1,234.5' -> 1234.5；空字串、'--'、'X' 等 -> None"""
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def load_tables(con):
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


def columns(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({q(table)})")]


def pick(cols, exact, contains=None, exclude=()):
    if exact in cols:
        return exact
    for c in cols:
        if contains and contains in c and not any(x in c for x in exclude):
            return c
    return None


def load_ex_events(con):
    """回傳 {股票代號: [(除權息日, 因子), ...]}，依日期排序"""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "ex_rights" not in tables:
        print("沒有除權息資料，還原價格等於原始價格")
        return {}
    cols = columns(con, "ex_rights")
    c_code = pick(cols, "股票代號", "代號")
    c_before = pick(cols, "除權息前收盤價", "前收盤價")
    c_ref = pick(cols, "除權息參考價", "參考價", exclude=("減除", "開盤"))
    if not all((c_code, c_before, c_ref)):
        print(f"除權息欄位對不上，略過還原計算。實際欄位：{cols}")
        return {}
    events = defaultdict(list)
    sql = f"SELECT _key, {q(c_code)}, {q(c_before)}, {q(c_ref)} FROM ex_rights"
    for day, code, before, ref in con.execute(sql):
        b, r = num(before), num(ref)
        if b and r and b > 0:
            events[str(code).strip()].append((day, r / b))
    for v in events.values():
        v.sort()
    return events


def build_adjusted(con):
    if "stock_day_all" not in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return
    events = load_ex_events(con)
    # 每檔股票的「之後所有因子連乘積」：suffix[i] = 第 i 次（含）之後所有因子的乘積
    suffix = {}
    for code, ev in events.items():
        prods, p = [], 1.0
        for _, f in reversed(ev):
            p *= f
            prods.append(p)
        suffix[code] = ([d for d, _ in ev], list(reversed(prods)))

    con.execute("""CREATE TABLE stock_day_adj (
        date TEXT, code TEXT, name TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, value REAL, transactions REAL, adj_factor REAL,
        adj_open REAL, adj_high REAL, adj_low REAL, adj_close REAL)""")
    out = []
    sql = ('SELECT _key, Code, Name, OpeningPrice, HighestPrice, LowestPrice, ClosingPrice, '
           'TradeVolume, TradeValue, "Transaction" FROM stock_day_all')
    for day, code, name, o, h, lo, c, vol, val, tx in con.execute(sql):
        code = str(code).strip()
        factor = 1.0
        if code in suffix:
            dates, prods = suffix[code]
            i = bisect.bisect_right(dates, day)  # 嚴格晚於當天的第一次除權息
            if i < len(dates):
                factor = prods[i]
        o, h, lo, c = num(o), num(h), num(lo), num(c)
        adj = lambda x: round(x * factor, 4) if x is not None else None
        out.append((day, code, name, o, h, lo, c, num(vol), num(val), num(tx),
                    factor, adj(o), adj(h), adj(lo), adj(c)))
    con.executemany(f"INSERT INTO stock_day_adj VALUES ({','.join('?' * 15)})", out)
    con.execute("CREATE INDEX idx_adj ON stock_day_adj (code, date)")
    print(f"stock_day_adj: {len(out)} 筆，{len(events)} 檔有除權息紀錄")


def main():
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(DB)
    load_tables(con)
    build_adjusted(con)
    con.commit()
    con.close()
    print(f"完成：{DB}")


if __name__ == "__main__":
    main()
