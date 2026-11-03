"""產生個股頁面的資料：site/data/stocks/<代號>.json 與 site/data/stocks.json（個股清單）

由 report.py 在產生報告時呼叫，每次全部重建、不進 git（GitHub Actions 發布網頁時才產生）。

上市、上櫃股票都有：每個資料集把上市的與對應的上櫃資料集（tpex_ 開頭，欄位相同）合在一起，
上櫃轉上市的股票會有完整歷史。market 是最後交易日所在的市場（上市 / 上櫃）。

每檔一個 JSON，欄位依日期對齊成陣列（d 是日期，其他陣列的第 i 個值對應 d[i]，沒有資料為 null）：
    o h l c   原始開高低收（還原權息價由網頁用 ex 計算，跟 build_db.py 的算法相同）
    chg       證交所、櫃買中心公布的漲跌價差（除權息日是跟參考價比）
    v         成交量（張）          val  成交金額（百萬元）
    fi tr dl  外資、投信、自營商買賣超（張）
    mf ms     融資、融券餘額（張）       ml  融資限額（張）
    pe dy pb  本益比、殖利率(%)、股價淨值比
    ex        除權息紀錄 [[日期, 權/息, 權值+息值, 除權息前收盤價, 除權息參考價], ...]
    rev       月營收 [[年月, 營收(千元), 年增率(%), 月增率(%), 累計營收(千元), 累計年增率(%)], ...]
    nt        注意股紀錄 [[日期, 注意交易資訊], ...]
    dp        處置紀錄 [[開始日, 結束日, 處置措施, 處置條件], ...]
    dtr       當沖比率（%）：當沖成交股數 / 成交股數；那天有當沖資料但不在名單上為 0
    sb        借券賣出餘額（張）
    sh        集保股權分散（每週）[[資料日期, 千張以上持股 %, 400 張以上持股 %, 股東人數, 千張以上人數], ...]
    fin       季報（單季，千元）[[季度, 營收, 營業毛利, 營業利益, 歸屬母公司淨利, 每股盈餘], ...]；
              公開資訊觀測站是當年累計，這裡減去前一季累計換算成單季，前一季沒有資料時為 null

另外每檔產生一份精簡摘要 site/data/summary/<代號>.json（約 1～2 KB），給排程任務直接讀最新狀態，
不必下載完整歷史；每一檔都有，所以看不出使用者追蹤哪些股票。欄位見 summary()。
"""
import datetime as dt
import json
import math
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


def roc_date(s):
    """民國日期 115/10/30 -> 2026-10-30"""
    y, m, d = (int(x) for x in s.strip().split("/"))
    return f"{y + 1911}-{m:02d}-{d:02d}"


def lots(shares):
    return None if shares is None else round(shares / 1000)


def rows_by_code(con, table, code_col, code):
    con.row_factory = sqlite3.Row
    try:
        return {r["_key"]: dict(r) for r in con.execute(f'SELECT * FROM "{table}" WHERE "{code_col}" = ?', (code,))}
    finally:
        con.row_factory = None


def by_code(con, have, table, code_col, code):
    """上市資料集與對應的上櫃資料集（tpex_ 開頭，欄位相同）合在一起，{日期或年月: row}；
    上櫃轉上市的股票兩邊都有，日期不會重疊"""
    out = {}
    for t in (table, f"tpex_{table}"):
        if t in have:
            out.update(rows_by_code(con, t, code_col, code))
    return out


def query_both(con, have, table, sql, args):
    """同一個 SQL 查上市、上櫃兩個資料表（sql 裡用 {t} 代表表名）"""
    return [r for t in (table, f"tpex_{table}") if t in have for r in con.execute(sql.format(t=f'"{t}"'), args)]


# 季報欄位：一般業、金融業等欄位名稱不同，依序找第一個有值的（金融業沒有營收、毛利，會是 null）
FIN_COLS = (("營業收入",), ("營業毛利（毛損）淨額", "營業毛利（毛損）"), ("營業利益（損失）",),
            ("淨利（淨損）歸屬於母公司業主", "本期淨利（淨損）", "本期稅後淨利（淨損）"), ("基本每股盈餘（元）",))


def quarterly(rows):
    """{'2026-Q2': row, ...}（當年累計）-> [[季度, 營收, 毛利, 營業利益, 淨利, EPS], ...]（單季）"""
    ytd = {k: [first(r, *names) for names in FIN_COLS] for k, r in rows.items()}
    out = []
    for k in sorted(ytd):
        year, q = k.split("-Q")
        prev = ytd.get(f"{year}-Q{int(q) - 1}") if q != "1" else [0] * len(FIN_COLS)
        if prev is None:
            out.append([k] + [None] * len(FIN_COLS))
            continue
        out.append([k] + [None if a is None or b is None else round(a - b, 2) for a, b in zip(ytd[k], prev)])
    return out


def holders(rows):
    """一檔股票各週的股權分散 rows -> [[日期, 千張以上 %, 400 張以上 %, 股東人數, 千張以上人數], ...]
    持股分級 12～15 是 400 張以上，15 是千張以上，17 是合計"""
    weeks = {}
    for r in rows:
        weeks.setdefault(r["_key"], {})[r["持股分級"]] = r
    out = []
    for day, lv in sorted(weeks.items()):
        pct = lambda *ks: round(sum(num(lv[k]["占集保庫存數比例%"]) or 0 for k in ks if k in lv), 2)  # noqa: E731
        people = lambda k: int(num(lv[k]["人數"]) or 0) if k in lv else None  # noqa: E731
        out.append([day, pct("15"), pct("12", "13", "14", "15"), people("17"), people("15")])
    return out


def stock_json(con, have, code, days, dt_days):
    """days: [(日期, 名稱, 開, 高, 低, 收, 量, 金額, 還原收盤, 市場), ...]；dt_days：{市場: 有當沖資料的日子}"""
    d = [x[0] for x in days]
    out = {"code": code, "name": days[-1][1], "market": days[-1][9], "d": d}
    for i, k in enumerate(("o", "h", "l", "c"), start=2):
        out[k] = [x[i] for x in days]
    official = by_code(con, have, "stock_day_all", "Code", code)
    out["chg"] = [num(official[day]["Change"]) if day in official else None for day in d]
    out["v"] = [lots(x[6]) for x in days]
    out["val"] = [None if x[7] is None else round(x[7] / 1e6, 1) for x in days]

    inst = by_code(con, have, "institutional", "證券代號", code)
    get = lambda day, *names: first(inst[day], *names) if day in inst else None  # noqa: E731
    out["fi"] = [None if day not in inst else lots(
        (get(day, "外陸資買賣超股數(不含外資自營商)", "外資買賣超股數") or 0) + (get(day, "外資自營商買賣超股數") or 0))
        for day in d]
    out["tr"] = [lots(get(day, "投信買賣超股數")) if day in inst else None for day in d]
    out["dl"] = [lots(get(day, "自營商買賣超股數")) if day in inst else None for day in d]

    margin = by_code(con, have, "margin", "股票代號", code)
    balance = lambda day, col: int(num(margin[day][col]) or 0) if day in margin else None  # noqa: E731
    out["mf"] = [balance(day, "融資今日餘額") for day in d]
    out["ms"] = [balance(day, "融券今日餘額") for day in d]
    out["ml"] = [balance(day, "融資限額") for day in d]

    bw = by_code(con, have, "bwibbu_all", "Code", code)
    for k, col in (("pe", "PEratio"), ("dy", "DividendYield"), ("pb", "PBratio")):
        out[k] = [num(bw[day][col]) if day in bw else None for day in d]

    ex = by_code(con, have, "ex_rights", "股票代號", code)
    out["ex"] = [[day, r["權/息"], num(r["權值+息值"]), num(r["除權息前收盤價"]), num(r["除權息參考價"])]
                 for day, r in sorted(ex.items())]

    rev = by_code(con, have, "monthly_revenue", "公司代號", code)
    out["rev"] = [[ym, num(r["營業收入-當月營收"]), num(r["營業收入-去年同月增減(%)"]), num(r["營業收入-上月比較增減(%)"]),
                   num(r["累計營業收入-當月累計營收"]), num(r["累計營業收入-前期比較增減(%)"])] for ym, r in sorted(rev.items())]
    if rev:
        out["industry"] = rev[max(rev)]["產業別"]

    notice = by_code(con, have, "notice", "證券代號", code)
    out["nt"] = [[day, r["注意交易資訊"]] for day, r in sorted(notice.items())]   # 上市的累計次數一律是 1，不放
    # 處置股每天都會列出處置中的股票，同一次處置會重複出現，用期間去重
    disp = set(query_both(con, have, "disposition",
                          'SELECT "處置起迄時間", "處置措施", "處置條件" FROM {t} WHERE "證券代號" = ?', (code,)))
    out["dp"] = sorted([*map(roc_date, period.split("～")), measure, cond] for period, measure, cond in disp)

    trade = by_code(con, have, "day_trading", "證券代號", code)
    out["dtr"] = [None if day not in dt_days[x[9]] or not x[6] else
                  round((num(trade[day]["當日沖銷交易成交股數"]) or 0) / x[6] * 100, 2) if day in trade else 0
                  for day, x in zip(d, days)]
    sbl = by_code(con, have, "sbl_short", "代號", code)
    out["sb"] = [lots(num(sbl[day]["借券賣出當日餘額"]) or 0) if day in sbl else None for day in d]
    if "shareholding" in have:
        con.row_factory = sqlite3.Row
        try:
            out["sh"] = holders(con.execute('SELECT * FROM shareholding WHERE "證券代號" = ?', (code,)).fetchall())
        finally:
            con.row_factory = None
    else:
        out["sh"] = []
    out["fin"] = quarterly(by_code(con, have, "income_statement", "公司代號", code))

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


INDEX_NAME = {"上市": "加權指數", "上櫃": "櫃買指數"}


def risk(data, adj, taiex):
    """風險指標，以還原權息價計算（除權息不會被當成下跌）：
    ATR14（Wilder 平滑）、20 日年化波動率、距最近 250 日最高收盤的回檔、
    最近 20 / 60 日相對大盤（上市比加權指數、上櫃比櫃買指數，taiex 傳入對應的指數）的漲跌（百分點）"""
    idx = [i for i, x in enumerate(adj) if x is not None]
    if len(idx) < 21:
        return None
    d, c = data["d"], data["c"]
    tr = []
    for a, b in zip(idx, idx[1:]):
        f = adj[b] / c[b]
        h, lo = (data["h"][b] or c[b]) * f, (data["l"][b] or c[b]) * f
        tr.append(max(h - lo, abs(h - adj[a]), abs(lo - adj[a])))
    atr = sum(tr[:14]) / 14
    for x in tr[14:]:
        atr = (atr * 13 + x) / 14
    rets = [math.log(adj[b] / adj[a]) for a, b in zip(idx[-21:], idx[-20:])]
    mean = sum(rets) / len(rets)
    last = adj[idx[-1]]
    out = {"atr14": round(atr, 2), "atr_pct": round(atr / last * 100, 2),
           "hv20": round(math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) * 252) * 100, 1),
           "drawdown_250": round((last / max(adj[i] for i in idx[-250:]) - 1) * 100, 2),
           "rs_index": INDEX_NAME[data["market"]]}
    for n in (20, 60):
        if len(idx) > n and taiex.get(d[idx[-1]]) and taiex.get(d[idx[-1 - n]]):
            market = taiex[d[idx[-1]]] / taiex[d[idx[-1 - n]]] - 1
            out[f"rs_{n}d"] = round((last / adj[idx[-1 - n]] - 1 - market) * 100, 2)
    return out


def summary(con, have, data, adj, latest, forecast, generated_at, taiex):
    """個股最新狀態摘要：行情、均線、法人（含連買連賣）、融資融券、本益比、月營收、注意處置、除權息預告"""
    d, i = data["d"], len(data["d"]) - 1
    col = lambda k, j: data[k][j] if data.get(k) and 0 <= j < len(data[k]) else None  # noqa: E731
    pct = lambda j: (round(col("chg", j) / (col("c", j) - col("chg", j)) * 100, 2)  # noqa: E731
                     if None not in (col("c", j), col("chg", j)) and col("c", j) != col("chg", j) else None)
    closes = [x for x in adj if x is not None]
    traded = [j for j in range(len(d)) if data["c"][j] is not None]
    raw = [data["c"][j] for j in traded]
    ma = {f"ma{n}": round(sum(closes[-n:]) / n, 2) for n in (5, 20, 60, 100) if len(closes) >= n}
    ma_raw = {f"ma{n}": round(sum(raw[-n:]) / n, 2) for n in (5, 20, 60, 100) if len(raw) >= n}
    w = traded[-60:]
    hi = max(w, key=lambda j: data["h"][j] or 0, default=None)
    lo = min(w, key=lambda j: data["l"][j] or math.inf, default=None)

    out = {
        "code": data["code"], "name": data["name"], "industry": data.get("industry", ""), "market": data["market"],
        "date": d[i],                    # 這檔股票最後一個交易日；跟 latest_market_date 不同表示停止交易或已下市
        "latest_market_date": latest,
        "generated_at": generated_at,
        "price": {"open": col("o", i), "high": col("h", i), "low": col("l", i), "close": col("c", i),
                  "change": col("chg", i), "pct": pct(i), "volume": col("v", i), "value": col("val", i)},
        "units": {"volume": "張", "value": "百萬元", "institutional": "張", "margin": "張", "revenue": "千元",
                  "sbl_short": "張", "financials": "千元", "day_trading": "%"},
        # 給讀摘要的任務看的說明：哪些欄位用還原權息價（跟看盤軟體的數字不同）
        "basis": "ma、high_60、low_60、risk 用還原權息收盤價（除權息前的價格往下調，最新一天等於實際價格）；"
                 "ma_raw 用原始收盤價（跟一般看盤軟體相同）；range_60 是最近 60 個交易日原始盤中最高、最低價",
        "ma": ma,
        "ma_raw": ma_raw,
        "high_60": round(max(closes[-60:]), 2) if closes else None,
        "low_60": round(min(closes[-60:]), 2) if closes else None,
        "range_60": None if hi is None else {"high": data["h"][hi], "high_date": d[hi], "low": data["l"][lo], "low_date": d[lo]},
        "risk": risk(data, adj, taiex),
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
        "financing_limit": col("ml", j),
        "financing_usage": round(col("mf", j) / col("ml", j) * 100, 2) if col("ml", j) else None,   # 融資使用率 %
        "short": col("ms", j), "short_change": col("ms", j) - (col("ms", j - 1) or 0)}

    j = max(last_index(data.get(k) or []) for k in ("pe", "dy", "pb"))
    out["valuation"] = None if j < 0 else {"date": d[j], "pe": col("pe", j), "dy": col("dy", j), "pb": col("pb", j)}

    if data["rev"]:
        month, revenue, yoy, mom, cum, cum_yoy = data["rev"][-1]
        r2 = lambda v: None if v is None else round(v, 2)  # noqa: E731
        out["revenue"] = {"month": month, "revenue": revenue, "yoy": r2(yoy), "mom": r2(mom),
                          "cumulative": cum, "cumulative_yoy": r2(cum_yoy)}
    else:
        out["revenue"] = None

    # 當沖比率（%）：最新一天與 5 日平均；借券賣出餘額（張）
    j = last_index(data.get("dtr") or [])
    recent_dtr = [v for v in (data.get("dtr") or [])[max(0, j - 4):j + 1] if v is not None]
    out["day_trading"] = None if j < 0 else {"date": d[j], "ratio": col("dtr", j),
                                             "avg_5d": round(sum(recent_dtr) / len(recent_dtr), 2)}
    j = last_index(data.get("sb") or [])
    out["sbl_short"] = None if j < 0 else {"date": d[j], "balance": col("sb", j),
                                           "change": None if col("sb", j - 1) is None else col("sb", j) - col("sb", j - 1)}

    # 集保股權分散（每週）：最新一週與前一週比較
    sh = data.get("sh") or []
    if sh:
        day, big, over400, people, big_people = sh[-1]
        prev = sh[-2] if len(sh) > 1 else None
        out["shareholding"] = {"date": day, "over_1000_lots_pct": big, "over_400_lots_pct": over400,
                               "holders": people, "over_1000_lots_holders": big_people,
                               "over_1000_lots_pct_change": None if prev is None else round(big - prev[1], 2),
                               "holders_change": None if prev is None or prev[3] is None else people - prev[3]}
    else:
        out["shareholding"] = None

    # 季報：最新一季（單季）與近四季 EPS 合計
    fin = [q for q in data.get("fin") or [] if q[5] is not None]
    if fin:
        quarter, revenue, gross, op, net, eps = fin[-1]
        margin = lambda v: None if v is None or not revenue else round(v / revenue * 100, 2)  # noqa: E731
        out["financials"] = {"quarter": quarter, "revenue": revenue, "eps": eps, "gross_margin": margin(gross),
                             "operating_margin": margin(op), "net_margin": margin(net),
                             "eps_4q": round(sum(q[5] for q in fin[-4:]), 2) if len(fin) >= 4 else None}
    else:
        out["financials"] = None

    # 注意股：最近 5 個交易日被列入的紀錄；處置股：最新交易日在處置期間內
    code = data["code"]
    notice = by_code(con, have, "notice", "證券代號", code)
    out["notice"] = [{"date": day, "count": notice[day]["累計次數"], "info": notice[day]["注意交易資訊"]}
                     for day in d[-5:] if day in notice]
    disp = sorted(query_both(con, have, "disposition", 'SELECT "處置起迄時間", "處置措施", "處置條件" FROM {t} '
                             'WHERE "證券代號" = ? AND _key = ?', (code, latest)))
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
    """除權息預告（上市、上櫃）：{代號: [{'date', 'type', 'cash_dividend', 'stock_dividend_rate'}, ...]}，
    只取最新交易日（含）之後的"""
    out = {}
    con.row_factory = sqlite3.Row
    try:
        for table in ("ex_rights_forecast", "tpex_ex_rights_forecast"):
            if table not in have:
                continue
            for r in con.execute(f"SELECT * FROM {table} ORDER BY 除權除息日期"):
                if r["除權除息日期"] >= latest:
                    out.setdefault(r["股票代號"], []).append({"date": r["除權除息日期"], "type": r["除權息"], **dividend(r)})
    finally:
        con.row_factory = None
    return out


def build(con, have):
    """產生全部個股 JSON 與個股清單，回傳產生的檔數"""
    for table, col in (("stock_day_all", "Code"), ("institutional", "證券代號"), ("margin", "股票代號"), ("bwibbu_all", "Code"),
                       ("ex_rights", "股票代號"), ("monthly_revenue", "公司代號"),
                       ("notice", "證券代號"), ("disposition", "證券代號"), ("day_trading", "證券代號"),
                       ("sbl_short", "代號"), ("shareholding", "證券代號"), ("income_statement", "公司代號")):
        for t in (table, f"tpex_{table}"):
            if t in have:
                con.execute(f'CREATE INDEX IF NOT EXISTS "idx_{t}_code" ON "{t}" ("{col}")')
    for folder in (OUT_DIR, SUMMARY_DIR):
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.json"):
            old.unlink()

    latest = con.execute("SELECT MAX(date) FROM stock_day_adj").fetchone()[0]
    # 各市場的大盤指數（算相對強弱）與有當沖資料的日子（那天有資料但股票不在名單上，當沖比率是 0）
    index, dt_days = {}, {}
    for market, prefix, table in (("上市", "", "taiex"), ("上櫃", "tpex_", "tpex_index")):
        index[market] = {day: num(v) for day, v in con.execute(f"SELECT _key, ClosingIndex FROM {table}")} if table in have else {}
        trade = f"{prefix}day_trading"
        dt_days[market] = {r[0] for r in con.execute(f"SELECT DISTINCT _key FROM {trade}")} if trade in have else set()
    forecast = load_forecast(con, have, latest)
    generated_at = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")
    listing = []
    sql = ("SELECT code, date, name, open, high, low, close, volume, value, adj_close, market "
           "FROM stock_day_adj ORDER BY code, date")
    for code, group in groupby(con.execute(sql), key=lambda r: r[0]):
        days = [r[1:] for r in group]
        data = stock_json(con, have, code, days, dt_days)
        (OUT_DIR / f"{code}.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                                              encoding="utf-8")
        brief = summary(con, have, data, [x[8] for x in days], latest, forecast, generated_at, index[data["market"]])
        (SUMMARY_DIR / f"{code}.json").write_text(json.dumps(brief, ensure_ascii=False, separators=(",", ":")),
                                                  encoding="utf-8")
        # 個股清單：代號、名稱、產業、最後交易日是否為最新交易日（已下市的股票仍可查歷史）、市場
        listing.append([code, data["name"], data.get("industry", ""), days[-1][0] == latest, data["market"]])
    (ROOT / "site" / "data" / "stocks.json").write_text(json.dumps(listing, ensure_ascii=False, separators=(",", ":")),
                                                          encoding="utf-8")
    return len(listing)
