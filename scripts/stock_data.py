"""產生個股頁面的資料：site/data/stocks/<代號>.json 與 site/data/stocks.json（個股清單）

由 report.py 在產生報告時呼叫，每次全部重建、不進 git（GitHub Actions 發布網頁時才產生）。

每檔一個 JSON，欄位依日期對齊成陣列（d 是日期，其他陣列的第 i 個值對應 d[i]，沒有資料為 null）：
    o h l c   原始開高低收（還原權息價由網頁用 ex 計算，跟 build_db.py 的算法相同）
    chg       證交所公布的漲跌價差（除權息日是跟參考價比）
    v         成交量（張）          val  成交金額（百萬元）
    fi tr dl  外資、投信、自營商買賣超（張）
    mf ms     融資、融券餘額（張）
    pe dy pb  本益比、殖利率(%)、股價淨值比
    ex        除權息紀錄 [[日期, 權/息, 權值+息值, 除權息前收盤價, 除權息參考價], ...]
    rev       月營收 [[年月, 營收(千元), 年增率(%)], ...]
"""
import json
import sqlite3
from itertools import groupby

import build_db

ROOT = build_db.ROOT
OUT_DIR = ROOT / "site" / "data" / "stocks"
num = build_db.num


def first(row, *names):
    """依序找第一個有值的欄位（不同年份的欄位名稱不同）"""
    for n in names:
        v = num(row.get(n))
        if v is not None:
            return v
    return None


def lots(shares):
    return None if shares is None else round(shares / 1000)


def rows_by_code(con, table, code_col, code):
    con.row_factory = sqlite3.Row
    try:
        return {r["_key"]: dict(r) for r in con.execute(f'SELECT * FROM "{table}" WHERE "{code_col}" = ?', (code,))}
    finally:
        con.row_factory = None


def stock_json(con, have, code, days):
    """days: [(日期, 名稱, 開, 高, 低, 收, 量, 金額), ...]"""
    d = [x[0] for x in days]
    out = {"code": code, "name": days[-1][1], "d": d}
    for i, k in enumerate(("o", "h", "l", "c"), start=2):
        out[k] = [x[i] for x in days]
    official = rows_by_code(con, "stock_day_all", "Code", code)
    out["chg"] = [num(official[day]["Change"]) if day in official else None for day in d]
    out["v"] = [lots(x[6]) for x in days]
    out["val"] = [None if x[7] is None else round(x[7] / 1e6, 1) for x in days]

    inst = rows_by_code(con, "institutional", "證券代號", code) if "institutional" in have else {}
    get = lambda day, *names: first(inst[day], *names) if day in inst else None  # noqa: E731
    out["fi"] = [None if day not in inst else lots(
        (get(day, "外陸資買賣超股數(不含外資自營商)", "外資買賣超股數") or 0) + (get(day, "外資自營商買賣超股數") or 0))
        for day in d]
    out["tr"] = [lots(get(day, "投信買賣超股數")) if day in inst else None for day in d]
    out["dl"] = [lots(get(day, "自營商買賣超股數")) if day in inst else None for day in d]

    margin = rows_by_code(con, "margin", "股票代號", code) if "margin" in have else {}
    balance = lambda day, col: int(num(margin[day][col]) or 0) if day in margin else None  # noqa: E731
    out["mf"] = [balance(day, "融資今日餘額") for day in d]
    out["ms"] = [balance(day, "融券今日餘額") for day in d]

    bw = rows_by_code(con, "bwibbu_all", "Code", code) if "bwibbu_all" in have else {}
    for k, col in (("pe", "PEratio"), ("dy", "DividendYield"), ("pb", "PBratio")):
        out[k] = [num(bw[day][col]) if day in bw else None for day in d]

    ex = rows_by_code(con, "ex_rights", "股票代號", code) if "ex_rights" in have else {}
    out["ex"] = [[day, r["權/息"], num(r["權值+息值"]), num(r["除權息前收盤價"]), num(r["除權息參考價"])]
                 for day, r in sorted(ex.items())]

    rev = rows_by_code(con, "monthly_revenue", "公司代號", code) if "monthly_revenue" in have else {}
    out["rev"] = [[ym, num(r["營業收入-當月營收"]), num(r["營業收入-去年同月增減(%)"])] for ym, r in sorted(rev.items())]
    if rev:
        out["industry"] = rev[max(rev)]["產業別"]

    # 去掉整欄都沒資料的欄位（例如 ETF 沒有本益比），減少檔案大小
    for k in [k for k, v in out.items() if isinstance(v, list) and v and all(x is None for x in v)]:
        out[k] = []
    return out


def build(con, have):
    """產生全部個股 JSON 與個股清單，回傳產生的檔數"""
    for table, col in (("stock_day_all", "Code"), ("institutional", "證券代號"), ("margin", "股票代號"), ("bwibbu_all", "Code"),
                       ("ex_rights", "股票代號"), ("monthly_revenue", "公司代號")):
        if table in have:
            con.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_code" ON "{table}" ("{col}")')
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("*.json"):
        old.unlink()

    latest = con.execute("SELECT MAX(date) FROM stock_day_adj").fetchone()[0]
    listing = []
    sql = ("SELECT code, date, name, open, high, low, close, volume, value "
           "FROM stock_day_adj ORDER BY code, date")
    for code, group in groupby(con.execute(sql), key=lambda r: r[0]):
        days = [r[1:] for r in group]
        data = stock_json(con, have, code, days)
        (OUT_DIR / f"{code}.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                                              encoding="utf-8")
        # 個股清單：代號、名稱、產業、最後交易日是否為最新交易日（已下市的股票仍可查歷史）
        listing.append([code, data["name"], data.get("industry", ""), days[-1][0] == latest])
    (ROOT / "site" / "data" / "stocks.json").write_text(json.dumps(listing, ensure_ascii=False, separators=(",", ":")),
                                                          encoding="utf-8")
    return len(listing)
