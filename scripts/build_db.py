"""把 data/ 底下所有 CSV 匯入 SQLite，並計算還原權息日 K（在本機執行，不進 git）

用法：python scripts/build_db.py  ->  產生 twse.db

原始資料表：每個資料集一張，欄位一律存文字（避免 0050 變成 50），多一欄 _key（日期或年月）。
stock_day_adj：整理好的上市（stock_day_all）、上櫃（tpex_stock_day_all）日 K，價格已轉數字，並附還原權息後的開高低收：
    date, code, name, open, high, low, close, volume, value, transactions,
    adj_factor, adj_open, adj_high, adj_low, adj_close, market（上市 / 上櫃）
還原方式：每次除權息的因子 = 除權息參考價 / 除權息前收盤價，
除權息日「之前」的所有價格乘上之後各次因子的連乘積（最新價格維持不變）。
上櫃轉上市的股票代號不變，兩邊的日 K 與除權息合在一起算。
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
                reader = csv.DictReader(fh)
                cols.update(dict.fromkeys(reader.fieldnames or []))
                for r in reader:
                    r["_key"] = f.stem
                    rows.append(r)
        cols["_key"] = None
        cols = list(cols)
        if cols == ["_key"]:
            continue
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
    """回傳 {股票代號: [(除權息日, 因子), ...]}，依日期排序（上市、上櫃合在一起）"""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    events = defaultdict(list)
    for table in ("ex_rights", "tpex_ex_rights"):
        if table not in tables:
            print(f"沒有 {table}，這個市場的還原價格等於原始價格")
            continue
        cols = columns(con, table)
        c_code, c_before, c_ref = ex_columns(cols)
        if not all((c_code, c_before, c_ref)):
            print(f"{table} 欄位對不上，略過還原計算。實際欄位：{cols}")
            continue
        sql = f"SELECT _key, {q(c_code)}, {q(c_before)}, {q(c_ref)} FROM {q(table)}"
        for day, code, before, ref in con.execute(sql):
            f = ex_factor(before, ref)
            if f is not None:
                events[str(code).strip()].append((day, f))
    for v in events.values():
        v.sort()
    return events


def ex_columns(cols):
    """除權息結果表的（代號、除權息前收盤價、除權息參考價）欄位名稱；上市、上櫃、各年份名稱略有不同"""
    return (pick(cols, "股票代號", "代號"), pick(cols, "除權息前收盤價", "前收盤價"),
            pick(cols, "除權息參考價", "參考價", exclude=("減除", "開盤")))


def ex_factor(before, ref):
    """一次除權息的還原因子 = 除權息參考價 / 除權息前收盤價；資料不全回傳 None"""
    b, r = num(before), num(ref)
    return r / b if b and r and b > 0 else None


def suffix_factors(events):
    """[(除權息日, 因子), ...]（依日期排序）-> (日期列表, 第 i 次（含）之後所有因子的連乘積)"""
    prods, p = [], 1.0
    for _, f in reversed(events):
        p *= f
        prods.append(p)
    return [d for d, _ in events], list(reversed(prods))


def factor_on(suffix, day):
    """某一天的還原因子：嚴格晚於當天的所有除權息因子連乘積（沒有就是 1）"""
    dates, prods = suffix
    i = bisect.bisect_right(dates, day)
    return prods[i] if i < len(dates) else 1.0


def adjust_factors(events, days):
    """events: [(除權息日, 因子), ...]；days: 日期列表 -> 每一天的還原因子（還原價 = 原始價 × 因子）。
    build_adjusted() 與 signals.py 共用，兩邊的還原價算法一定相同"""
    suffix = suffix_factors(events)
    return [factor_on(suffix, d) for d in days]


MARKETS = (("stock_day_all", "上市"), ("tpex_stock_day_all", "上櫃"))


def build_adjusted(con):
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "stock_day_all" not in tables:
        return
    events = load_ex_events(con)
    # 每檔股票的「之後所有因子連乘積」
    suffix = {code: suffix_factors(ev) for code, ev in events.items()}

    con.execute("""CREATE TABLE stock_day_adj (
        date TEXT, code TEXT, name TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, value REAL, transactions REAL, adj_factor REAL,
        adj_open REAL, adj_high REAL, adj_low REAL, adj_close REAL, market TEXT)""")
    out = []
    for table, market in MARKETS:
        if table not in tables:
            continue
        sql = ('SELECT _key, Code, Name, OpeningPrice, HighestPrice, LowestPrice, ClosingPrice, '
               f'TradeVolume, TradeValue, "Transaction" FROM {q(table)}')
        for day, code, name, o, h, lo, c, vol, val, tx in con.execute(sql):
            code = str(code).strip()
            factor = factor_on(suffix[code], day) if code in suffix else 1.0
            o, h, lo, c = num(o), num(h), num(lo), num(c)
            adj = lambda x: round(x * factor, 4) if x is not None else None
            out.append((day, code, name, o, h, lo, c, num(vol), num(val), num(tx),
                        factor, adj(o), adj(h), adj(lo), adj(c), market))
    con.executemany(f"INSERT INTO stock_day_adj VALUES ({','.join('?' * 16)})", out)
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
