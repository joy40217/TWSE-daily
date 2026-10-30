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

另外每檔產生一份精簡摘要 site/data/summary/<代號>.json（約 1～2 KB），給排程任務直接讀最新狀態，
不必下載完整歷史；每一檔都有，所以看不出使用者追蹤哪些股票。欄位見 summary()。
"""
import datetime as dt
import json
import sqlite3
from itertools import groupby

import build_db

ROOT = build_db.ROOT
OUT_DIR = ROOT / "site" / "data" / "stocks"
SUMMARY_DIR = ROOT / "site" / "data" / "summary"
RECENT = 20   # 摘要裡列最近幾天的走勢
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
    """days: [(日期, 名稱, 開, 高, 低, 收, 量, 金額, 還原收盤), ...]"""
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


def last_index(values):
    """最後一個有值的位置，沒有回傳 -1"""
    return next((i for i in range(len(values) - 1, -1, -1) if values[i] is not None), -1)


def streak(values):
    """從最後一個有資料的日子往回數，連續買超幾天（正數）或賣超幾天（負數）；遇到沒資料或方向改變就停"""
    j = last_index(values)
    if j < 0 or not values[j]:
        return 0
    sign = 1 if values[j] > 0 else -1
    n = 0
    while j >= 0 and values[j] is not None and values[j] * sign > 0:
        n, j = n + 1, j - 1
    return n * sign


def summary(con, have, data, adj, latest, forecast, generated_at):
    """個股最新狀態摘要：行情、均線、法人（含連買連賣）、融資融券、本益比、月營收、注意處置、除權息預告"""
    d, i = data["d"], len(data["d"]) - 1
    col = lambda k, j: data[k][j] if data.get(k) and 0 <= j < len(data[k]) else None  # noqa: E731
    pct = lambda j: (round(col("chg", j) / (col("c", j) - col("chg", j)) * 100, 2)  # noqa: E731
                     if None not in (col("c", j), col("chg", j)) and col("c", j) != col("chg", j) else None)
    closes = [x for x in adj if x is not None]
    ma = {f"ma{n}": round(sum(closes[-n:]) / n, 2) for n in (5, 20, 60, 100) if len(closes) >= n}

    out = {
        "code": data["code"], "name": data["name"], "industry": data.get("industry", ""), "market": "上市",
        "date": d[i],                    # 這檔股票最後一個交易日；跟 latest_market_date 不同表示停止交易或已下市
        "latest_market_date": latest,
        "generated_at": generated_at,
        "price": {"open": col("o", i), "high": col("h", i), "low": col("l", i), "close": col("c", i),
                  "change": col("chg", i), "pct": pct(i), "volume": col("v", i), "value": col("val", i)},
        "units": {"volume": "張", "value": "百萬元", "institutional": "張", "margin": "張", "revenue": "千元"},
        "ma": ma,                        # 均線、60 日高低點都用還原權息收盤價，可以直接跟最新收盤價比較
        "high_60": round(max(closes[-60:]), 2) if closes else None,
        "low_60": round(min(closes[-60:]), 2) if closes else None,
    }

    j = last_index(data.get("fi") or [])
    if j >= 0:
        net = {k: col(src, j) for k, src in (("foreign", "fi"), ("trust", "tr"), ("dealer", "dl"))}
        out["institutional"] = {"date": d[j], **net, "total": sum(v or 0 for v in net.values())}
        out["streak"] = {k: streak(data[src]) for k, src in (("foreign", "fi"), ("trust", "tr"), ("dealer", "dl"))}
        for n in (5, 20):
            out[f"sum_{n}d"] = {k: sum(v for v in data[src][max(0, j - n + 1):j + 1] if v is not None)
                                for k, src in (("foreign", "fi"), ("trust", "tr"), ("dealer", "dl"))}
    else:
        out["institutional"] = None

    j = last_index(data.get("mf") or [])
    out["margin"] = None if j < 0 else {
        "date": d[j], "financing": col("mf", j), "financing_change": col("mf", j) - (col("mf", j - 1) or 0),
        "short": col("ms", j), "short_change": col("ms", j) - (col("ms", j - 1) or 0)}

    j = max(last_index(data.get(k) or []) for k in ("pe", "dy", "pb"))
    out["valuation"] = None if j < 0 else {"date": d[j], "pe": col("pe", j), "dy": col("dy", j), "pb": col("pb", j)}

    if data["rev"]:
        month, revenue, yoy = data["rev"][-1]
        out["revenue"] = {"month": month, "revenue": revenue, "yoy": None if yoy is None else round(yoy, 2)}
    else:
        out["revenue"] = None

    # 注意股：最近 5 個交易日被列入的紀錄；處置股：最新交易日在處置期間內
    code = data["code"]
    notice = rows_by_code(con, "notice", "證券代號", code) if "notice" in have else {}
    out["notice"] = [{"date": day, "count": notice[day]["累計次數"], "info": notice[day]["注意交易資訊"]}
                     for day in d[-5:] if day in notice]
    disp = []
    if "disposition" in have:
        disp = con.execute('SELECT "處置起迄時間", "處置措施", "處置條件" FROM disposition '
                           'WHERE "證券代號" = ? AND _key = ? ORDER BY "處置起迄時間"', (code, latest)).fetchall()
    # 前後兩次處置期間重疊時會有兩筆，取期間較晚的那筆
    out["disposition"] = dict(zip(("period", "measure", "condition"), disp[-1])) if disp else None
    out["ex_rights_upcoming"] = forecast.get(code, [])

    start = max(0, i - RECENT + 1)
    out["recent"] = {
        "fields": ["date", "close", "pct", "volume", "foreign", "trust", "dealer", "financing"],
        "rows": [[d[j], col("c", j), pct(j), col("v", j), col("fi", j), col("tr", j), col("dl", j), col("mf", j)]
                 for j in range(start, i + 1)],
    }
    return out


def dividend(r):
    """除權息預告的股利欄位：數字轉成數值，「待公告實際收益分配金額」這類文字保留原樣"""
    cash = num(r["現金股利"])
    return {"cash_dividend": r["現金股利"] if cash is None else cash, "stock_dividend_rate": num(r["無償配股率"])}


def load_forecast(con, have, latest):
    """除權息預告：{代號: [{'date', 'type', 'cash_dividend', 'stock_dividend_rate'}, ...]}，只取最新交易日（含）之後的"""
    out = {}
    if "ex_rights_forecast" not in have:
        return out
    con.row_factory = sqlite3.Row
    try:
        for r in con.execute("SELECT * FROM ex_rights_forecast ORDER BY 除權除息日期"):
            if r["除權除息日期"] >= latest:
                out.setdefault(r["股票代號"], []).append({"date": r["除權除息日期"], "type": r["除權息"], **dividend(r)})
    finally:
        con.row_factory = None
    return out


def build(con, have):
    """產生全部個股 JSON 與個股清單，回傳產生的檔數"""
    for table, col in (("stock_day_all", "Code"), ("institutional", "證券代號"), ("margin", "股票代號"), ("bwibbu_all", "Code"),
                       ("ex_rights", "股票代號"), ("monthly_revenue", "公司代號"),
                       ("notice", "證券代號"), ("disposition", "證券代號")):
        if table in have:
            con.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_code" ON "{table}" ("{col}")')
    for folder in (OUT_DIR, SUMMARY_DIR):
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.json"):
            old.unlink()

    latest = con.execute("SELECT MAX(date) FROM stock_day_adj").fetchone()[0]
    forecast = load_forecast(con, have, latest)
    generated_at = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")
    listing = []
    sql = ("SELECT code, date, name, open, high, low, close, volume, value, adj_close "
           "FROM stock_day_adj ORDER BY code, date")
    for code, group in groupby(con.execute(sql), key=lambda r: r[0]):
        days = [r[1:] for r in group]
        data = stock_json(con, have, code, days)
        (OUT_DIR / f"{code}.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                                              encoding="utf-8")
        brief = summary(con, have, data, [x[8] for x in days], latest, forecast, generated_at)
        (SUMMARY_DIR / f"{code}.json").write_text(json.dumps(brief, ensure_ascii=False, separators=(",", ":")),
                                                  encoding="utf-8")
        # 個股清單：代號、名稱、產業、最後交易日是否為最新交易日（已下市的股票仍可查歷史）
        listing.append([code, data["name"], data.get("industry", ""), days[-1][0] == latest])
    (ROOT / "site" / "data" / "stocks.json").write_text(json.dumps(listing, ensure_ascii=False, separators=(",", ":")),
                                                          encoding="utf-8")
    return len(listing)
