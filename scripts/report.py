"""產生收盤後報告：GitHub Pages 互動網頁的資料，以及 Discord 摘要

用法：
  python scripts/report.py                    產生報告資料
  python scripts/report.py --notify 日期      把該日報告送到 Discord（需要環境變數 DISCORD_WEBHOOK_URL）

產生報告資料時：
  1. 執行 build_db.py 建立 twse.db（含還原權息日 K）
  2. 為還沒有報告、當時資料不齊（融資融券、三大法人等晚公布），或最近 REFRESH_DAYS 個交易日
     產生 site/data/days/<年>/<日期>.json（會 commit 進 repo）
  3. 重新產生 site/data/index.json（每個交易日的大盤摘要，給網頁圖表用；每次重建，不進 git）
  4. 重新產生個股頁面資料 site/data/stocks/ 與個股摘要 site/data/summary/（見 stock_data.py；不進 git）
  5. 重新產生 site/data/latest.json：最新交易日、產生時間、各資料是否已公布、除權息預告（不進 git）
  6. 最新交易日的報告第一次有融資融券時（當天最後公布的資料），在 GitHub Actions 輸出 new_day=<日期>，
     workflow 會在網頁發布後再送 Discord，避免通知先到、網頁還沒更新；下午先產生的報告不會通知

大盤三大法人金額用證交所「三大法人買賣金額統計表」的實際金額；沒有這份資料的日子（還沒回補）
才用買賣超股數 × 當日收盤價估算，並標記 estimated。個股的法人買賣超金額一律是估算值。
"""
import argparse
import datetime as dt
import io
import json
import os
import sqlite3
import sys
from collections import defaultdict, deque
from pathlib import Path

import requests

import build_db
import stock_data

ROOT = build_db.ROOT
SITE = ROOT / "site"
DAYS_DIR = SITE / "data" / "days"
TOP = 20                 # 排行榜列幾檔（網頁先顯示前 10 檔，可展開）
SIGNAL_TOP = 20          # 每種技術訊號最多列幾檔
SIGNAL_KEYS = ("new_high", "new_low", "golden", "death", "volume_up", "volume_down", "volume")
SIGNAL_MIN_VALUE = 1e8   # 當日成交金額低於 1 億的股票不列入技術訊號，避免冷門股雜訊
YI = 1e8                 # 億
REPORT_VERSION = 4       # 報告格式改變時加 1，舊的報告會在下次執行時全部重新產生
REFRESH_DAYS = 2         # 最近幾個交易日每次都重新產生（資料公布後可能更正、注意股等可能晚點才公布）
# 類股指數中的大分類（底下還有細分類股），列最強、最弱類股時不算
SECTOR_GROUPS = {"水泥窯製類", "塑膠化工類", "機電類", "化學生技醫療類", "電子工業類"}
num = build_db.num
first = stock_data.first


def is_stock(code: str) -> bool:
    """一般股票（4 位數代號、不是 0 開頭的 ETF）"""
    return len(code) == 4 and code.isdigit() and not code.startswith("0")


def tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def query(con, sql, *args):
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.row_factory = None


# ---- 技術訊號與市場寬度：依股票、日期依序掃過一次還原日 K ----

def limit_pct(day):
    """漲跌停幅度：2015-06-01 起 10%（之前 7%）；收盤價四捨五入到升降單位，所以門檻放寬一點"""
    return 9.5 if day >= "2015-06-01" else 6.5


def scan_market(con, targets):
    """報告只有上市股票（上櫃的日 K 只用在個股頁），只算一般股票（不含 ETF）。回傳
    signals：targets 那幾天的技術訊號 {日期: {'new_high': [代號...], 'new_low', 'golden', 'death', 'volume'}}
             （排除成交金額不到 SIGNAL_MIN_VALUE 的股票）
    breadth：每一天的市場寬度 {日期: {'stocks', 'above_ma20', 'above_ma60'（%）, 'new_high', 'new_low',
             'up', 'down', 'limit_up', 'limit_down'}}；新高、新低跟訊號的條件相同但不看成交金額，
             漲跌停是收在最高（最低）價且漲跌幅達 limit_pct()"""
    signals = defaultdict(lambda: defaultdict(list))
    counts = defaultdict(lambda: defaultdict(int))
    sql = ("SELECT code, date, adj_close, adj_high, adj_low, volume, value FROM stock_day_adj "
           "WHERE market = '上市' ORDER BY code, date")
    code_now, closes, vols, prev_ma5, prev_ma20 = None, None, None, None, None
    for code, day, close, high, low, volume, value in con.execute(sql):
        if not is_stock(code) or close is None:
            continue
        if code != code_now:
            code_now, closes, vols, prev_ma5, prev_ma20 = code, deque(maxlen=60), deque(maxlen=20), None, None
        prior = list(closes)[-59:]   # 前 59 天收盤
        prev_high, prev_low = (max(prior), min(prior)) if len(prior) == 59 else (None, None)
        prev = closes[-1] if closes else None
        closes.append(close)
        window = list(closes)
        ma5 = sum(window[-5:]) / 5 if len(window) >= 5 else None
        ma20 = sum(window[-20:]) / 20 if len(window) >= 20 else None
        ma60 = sum(window) / 60 if len(window) == 60 else None

        c = counts[day]
        c["stocks"] += 1
        if prev:
            pct = (close / prev - 1) * 100
            c["up"] += close > prev
            c["down"] += close < prev
            c["limit_up"] += pct >= limit_pct(day) and high is not None and close >= high
            c["limit_down"] += pct <= -limit_pct(day) and low is not None and close <= low
        for n, ma in ((20, ma20), (60, ma60)):
            if ma is not None:
                c[f"n{n}"] += 1
                c[f"above{n}"] += close > ma
        if prev_high is not None:
            c["new_high"] += close > prev_high
            c["new_low"] += close < prev_low

        if day in targets and (value or 0) >= SIGNAL_MIN_VALUE:
            s = signals[day]
            if prev_high is not None and close > prev_high:
                s["new_high"].append(code)
            if prev_low is not None and close < prev_low:
                s["new_low"].append(code)
            if None not in (prev_ma5, prev_ma20, ma5, ma20):
                if prev_ma5 <= prev_ma20 and ma5 > ma20:
                    s["golden"].append(code)
                elif prev_ma5 >= prev_ma20 and ma5 < ma20:
                    s["death"].append(code)
            if len(vols) == 20 and sum(vols) > 0 and (volume or 0) >= 3 * sum(vols) / 20:
                s["volume"].append(code)
        vols.append(volume or 0)
        prev_ma5, prev_ma20 = ma5, ma20

    pct = lambda a, n: round(a / n * 100, 1) if n else None  # noqa: E731
    breadth = {day: {"stocks": c["stocks"], "above_ma20": pct(c["above20"], c["n20"]), "above_ma60": pct(c["above60"], c["n60"]),
                     **{k: c[k] for k in ("new_high", "new_low", "up", "down", "limit_up", "limit_down")}}
               for day, c in counts.items()}
    return signals, breadth


def scan_streaks(con, have, days, targets):
    """外資、投信連續買超：{日期: {'foreign': [(代號, 天數, 累計張數), ...], 'trust': [...]}}（只算 targets 那幾天）。
    某天不在三大法人名單上（沒有法人交易）或買賣超為 0、轉成賣超，連買就中斷"""
    if "institutional" not in have:
        return {}
    idx = {d: i for i, d in enumerate(days)}
    out = defaultdict(lambda: defaultdict(list))
    sql = ('SELECT "證券代號", _key, "外陸資買賣超股數(不含外資自營商)", "外資買賣超股數", "外資自營商買賣超股數", '
           '"投信買賣超股數" FROM institutional ORDER BY "證券代號", _key')
    state = {}
    for code, day, f1, f2, f3, trust in con.execute(sql):
        code = str(code).strip()
        if not is_stock(code) or day not in idx:
            continue
        nets = {"foreign": (num(f1) if num(f1) is not None else num(f2) or 0) + (num(f3) or 0), "trust": num(trust) or 0}
        for k, v in nets.items():
            last_i, n, total = state.get((code, k), (None, 0, 0))
            if v > 0:
                n, total = (n + 1, total + v) if last_i == idx[day] - 1 and n else (1, v)
            else:
                n, total = 0, 0
            state[(code, k)] = (idx[day], n, total)
            if day in targets and n:
                out[day][k].append((code, n, round(total / 1000)))
    return out


class Revenue:
    """月營收年增率排行與營收創新高（上市一般股票，當月營收至少 1 億元）；每個月份只算一次"""

    def __init__(self, con, have):
        self.rows = defaultdict(list)    # 年月 -> [(代號, 名稱, 營收, 年增, 月增)]
        if "monthly_revenue" in have:
            for code, name, ym, rev, yoy, mom in con.execute(
                    'SELECT "公司代號", "公司名稱", _key, "營業收入-當月營收", "營業收入-去年同月增減(%)", '
                    '"營業收入-上月比較增減(%)" FROM monthly_revenue'):
                if num(rev) is not None and is_stock(str(code).strip()):
                    self.rows[ym].append((str(code).strip(), name, num(rev), num(yoy), num(mom)))
        self.months = sorted(self.rows)
        self.cache = {}

    def month_before(self, day):
        """這天能看到的最新月份：交易日所在月份的前一個月以前（當月營收要到下個月 10 日前才公布完）"""
        return next((m for m in reversed(self.months) if m < day[:7]), None)

    def ranked(self, month):
        if month not in self.cache:
            best = {}
            for m in self.months:
                if m >= month:
                    break
                for code, _, rev, _, _ in self.rows[m]:
                    best[code] = (max(best.get(code, (0, 0))[0], rev), best.get(code, (0, 0))[1] + 1)
            items = [r for r in self.rows[month] if r[2] >= 1e5]   # 千元
            highs = sorted((r for r in items if best.get(r[0], (0, 0))[1] >= 36 and r[2] > best[r[0]][0]),
                           key=lambda r: -r[2])   # 至少有 3 年的紀錄才算創新高
            self.cache[month] = (sorted((r for r in items if r[3] is not None), key=lambda r: -r[3]), highs)
        return self.cache[month]

    def lists(self, day, stocks):
        month = self.month_before(day)
        if month is None:
            return None
        yoy, highs = self.ranked(month)
        item = lambda r: {"code": r[0], "name": (stocks.get(r[0]) or {}).get("name") or r[1],  # noqa: E731
                          "close": (stocks.get(r[0]) or {}).get("close"), "pct": (stocks.get(r[0]) or {}).get("pct"),
                          "revenue": round(r[2] / 1e5, 2), "yoy": r[3] if r[3] is None else round(r[3], 2),
                          "mom": r[4] if r[4] is None else round(r[4], 2)}
        return {"month": month, "yoy": [item(r) for r in yoy[:TOP]],
                "high": {"count": len(highs), "items": [item(r) for r in highs[:TOP]]}}


# ---- 單日報告 ----

def has_file(table, day):
    """這個資料集當天的檔案是否存在（注意股、處置股當天沒有也會有只有標題的檔案）"""
    return (build_db.DATA_DIR / table / day[:4] / f"{day}.csv").exists()


def official_institutional(con, have, day):
    """三大法人買賣金額統計表 -> {'foreign', 'trust', 'dealer', 'total'}（億）；沒有資料回傳 None"""
    if "institutional_amount" not in have:
        return None
    rows = query(con, "SELECT * FROM institutional_amount WHERE _key = ?", day)
    if not rows:
        return None
    net = {r["單位名稱"]: (num(r["買賣差額"]) or 0) / YI for r in rows}
    # 外資 = 外資及陸資（不含外資自營商）+ 外資自營商；自營商 = 自行買賣 + 避險（舊年份名稱略有不同）
    foreign = sum(v for k, v in net.items() if k.startswith("外資"))
    dealer = sum(v for k, v in net.items() if k.startswith("自營商"))
    trust = net.get("投信", 0)
    return {"foreign": round(foreign, 2), "trust": round(trust, 2), "dealer": round(dealer, 2),
            "total": round(net.get("合計", foreign + trust + dealer), 2)}


def sectors(con, have, day):
    """類股指數漲跌，依漲跌幅排序；沒有資料回傳 None"""
    if not has_file("sector_index", day) or "sector_index" not in have:
        return None
    out = []
    for r in query(con, "SELECT * FROM sector_index WHERE _key = ?", day):
        name = r["指數"]
        if name.endswith("類指數"):
            name = name.removesuffix("指數")
            out.append({"name": name, "close": num(r["收盤指數"]), "change": num(r["漲跌點數"]),
                        "pct": num(r["漲跌百分比"]), "group": name in SECTOR_GROUPS})
    return sorted(out, key=lambda x: -(x["pct"] or 0))


def flagged(con, have, day, stocks):
    """注意股、處置股（只列有成交行情的股票與 ETF，不含權證）；當天還沒查過的回傳 None"""
    out = {"notice": None, "disposition": None}
    if has_file("notice", day):
        rows = query(con, "SELECT * FROM notice WHERE _key = ?", day) if "notice" in have else []
        out["notice"] = [{"code": r["證券代號"], "name": r["證券名稱"], "count": r["累計次數"], "info": r["注意交易資訊"]}
                         for r in rows if r["證券代號"] in stocks]
    if has_file("disposition", day):
        rows = query(con, "SELECT * FROM disposition WHERE _key = ?", day) if "disposition" in have else []
        # 前後兩次處置期間重疊時會出現兩筆，只留期間較晚的那筆
        latest = {}
        for r in sorted(rows, key=lambda r: r["處置起迄時間"]):
            if r["證券代號"] in stocks:
                latest[r["證券代號"]] = {"code": r["證券代號"], "name": r["證券名稱"], "period": r["處置起迄時間"],
                                        "measure": r["處置措施"], "condition": r["處置條件"]}
        out["disposition"] = list(latest.values())
    return out


def stock_list(stocks, codes, limit):
    items = [stocks[c] for c in codes if c in stocks]
    items.sort(key=lambda s: -(s["value"] or 0))
    return [{k: s[k] for k in ("code", "name", "close", "pct", "value")} for s in items[:limit]]


def day_report(con, have, day, prev_close, signals, breadth, streaks, revenue):
    t = query(con, "SELECT * FROM taiex WHERE _key = ?", day)[0]
    close = num(t["ClosingIndex"])
    change = close - prev_close if prev_close else None
    market = {
        "open": num(t["OpeningIndex"]), "high": num(t["HighestIndex"]),
        "low": num(t["LowestIndex"]), "close": close,
        "change": round(change, 2) if change is not None else None,
        "pct": round(change / prev_close * 100, 2) if change is not None else None,
    }

    # 個股行情
    stocks = {}
    for r in query(con, 'SELECT Code, Name, ClosingPrice, Change, TradeValue, TradeVolume FROM stock_day_all WHERE _key = ?', day):
        c, chg = num(r["ClosingPrice"]), num(r["Change"])
        base = c - chg if c is not None and chg is not None else None
        stocks[r["Code"]] = {
            "code": r["Code"], "name": r["Name"], "close": c,
            "pct": round(chg / base * 100, 2) if base else None,
            "value": round((num(r["TradeValue"]) or 0) / YI, 4),   # 億；小型股要到百萬位才看得出差異
            "chg": chg, "volume": num(r["TradeVolume"]) or 0,
        }
    traded = [s for s in stocks.values() if s["close"] is not None and s["chg"] is not None]
    market["turnover"] = round(sum(s["value"] for s in stocks.values()), 1)
    market["up"] = sum(s["chg"] > 0 for s in traded)
    market["down"] = sum(s["chg"] < 0 for s in traded)
    market["flat"] = sum(s["chg"] == 0 for s in traded)
    movers = [s for s in traded if is_stock(s["code"]) and s["pct"] is not None]
    pick = lambda items: [{k: s[k] for k in ("code", "name", "close", "pct", "value")} for s in items[:TOP]]  # noqa: E731
    lists = {
        "gainers": pick(sorted(movers, key=lambda s: (-s["pct"], -s["value"]))),
        "losers": pick(sorted(movers, key=lambda s: (s["pct"], -s["value"]))),
        "turnover": pick(sorted(stocks.values(), key=lambda s: -s["value"])),
    }

    # 三大法人（金額 = 股數 × 收盤價，估算）
    inst = None
    rows = query(con, "SELECT * FROM institutional WHERE _key = ?", day) if "institutional" in have else []
    if rows:
        totals, per = defaultdict(float), defaultdict(list)
        for r in rows:
            code = str(r["證券代號"]).strip()
            price = (stocks.get(code) or {}).get("close")
            if not price:
                continue
            nets = {
                "foreign": (first(r, "外陸資買賣超股數(不含外資自營商)", "外資買賣超股數") or 0)
                + (first(r, "外資自營商買賣超股數") or 0),
                "trust": first(r, "投信買賣超股數") or 0,
                "dealer": first(r, "自營商買賣超股數") or 0,
            }
            for k, shares in nets.items():
                v = shares * price / YI
                totals[k] += v
                per[k].append({"code": code, "name": stocks[code]["name"], "close": price,
                               "pct": stocks[code]["pct"], "value": round(v, 2)})
        inst = {k: round(totals[k], 2) for k in ("foreign", "trust", "dealer")}
        inst["total"] = round(sum(inst.values()), 2)
        for k in ("foreign", "trust"):
            lists[f"{k}_buy"] = sorted(per[k], key=lambda s: -s["value"])[:TOP]
            lists[f"{k}_sell"] = sorted(per[k], key=lambda s: s["value"])[:TOP]
    # 大盤合計優先用實際金額；沒有才用上面的估算
    official = official_institutional(con, have, day)
    if official:
        inst = official
    elif inst:
        inst["estimated"] = True

    # 融資融券（單位：張）
    margin = None
    rows = query(con, "SELECT * FROM margin WHERE _key = ?", day) if "margin" in have else []
    if rows:
        items = []
        for r in rows:
            fin = (num(r["融資今日餘額"]) or 0) - (num(r["融資前日餘額"]) or 0)
            short = (num(r["融券今日餘額"]) or 0) - (num(r["融券前日餘額"]) or 0)
            items.append({"code": r["股票代號"], "name": r["股票名稱"], "fin": fin, "short": short,
                          "balance": num(r["融資今日餘額"]) or 0})
        margin = {"financing": round(sum(i["fin"] for i in items)), "short": round(sum(i["short"] for i in items))}
        stocks_only = [i for i in items if is_stock(i["code"])]
        lists["margin_up"] = [{"code": i["code"], "name": i["name"], "change": i["fin"], "balance": i["balance"]}
                              for i in sorted(stocks_only, key=lambda i: -i["fin"])[:TOP]]
        lists["margin_down"] = [{"code": i["code"], "name": i["name"], "change": i["fin"], "balance": i["balance"]}
                                for i in sorted(stocks_only, key=lambda i: i["fin"])[:TOP]]

    info = lambda code: {"code": code, "name": stocks[code]["name"], "close": stocks[code]["close"],  # noqa: E731
                         "pct": stocks[code]["pct"]}
    # 外資、投信連買天數（天數相同時累計買超張數多的在前）
    for k, rows in (streaks.get(day) or {}).items():
        rows = sorted((r for r in rows if r[0] in stocks), key=lambda r: (-r[1], -r[2]))[:TOP]
        lists[f"{k}_streak"] = [{**info(code), "days": n, "lots": total} for code, n, total in rows]

    # 借券賣出餘額增減（張；晚上才公布）
    rows = query(con, "SELECT * FROM sbl_short WHERE _key = ?", day) if "sbl_short" in have else []
    if rows:
        items = []
        for r in rows:
            code = str(r["代號"]).strip()
            if is_stock(code) and code in stocks:
                bal = num(r["借券賣出當日餘額"]) or 0
                items.append({**info(code), "change": round((bal - (num(r["借券賣出前日餘額"]) or 0)) / 1000),
                              "balance": round(bal / 1000)})
        lists["sbl_up"] = sorted(items, key=lambda i: -i["change"])[:TOP]
        lists["sbl_down"] = sorted(items, key=lambda i: i["change"])[:TOP]

    # 當沖比率：當沖成交股數 / 成交股數（成交金額至少 1 億，避免冷門股）
    rows = query(con, "SELECT * FROM day_trading WHERE _key = ?", day) if "day_trading" in have else []
    if rows:
        items = []
        for r in rows:
            code = str(r["證券代號"]).strip()
            s = stocks.get(code)
            if is_stock(code) and s and s["volume"] and s["value"] * YI >= SIGNAL_MIN_VALUE:
                items.append({**info(code), "ratio": round((num(r["當日沖銷交易成交股數"]) or 0) / s["volume"] * 100, 2),
                              "value": round(s["value"], 2)})
        lists["day_trading"] = sorted(items, key=lambda i: -i["ratio"])[:TOP]

    # 除權息
    ex = []
    if "ex_rights" in have:
        for r in query(con, "SELECT * FROM ex_rights WHERE _key = ?", day):
            ex.append({"code": r["股票代號"], "name": r["股票名稱"], "type": r["權/息"],
                       "amount": num(r["權值+息值"]), "before": num(r["除權息前收盤價"]),
                       "ref": num(r["除權息參考價"])})

    sig = dict(signals.get(day, {}))
    # 爆量依當天漲跌分開（跟個股頁一樣用證交所公布的漲跌）；volume 是兩者合計，保留給舊的讀取程式
    vol = sig.get("volume", [])
    sig["volume_up"] = [c for c in vol if (stocks.get(c) or {}).get("chg", 0) > 0]
    sig["volume_down"] = [c for c in vol if (stocks.get(c) or {}).get("chg", 0) < 0]
    return {
        "version": REPORT_VERSION,
        "date": day,
        "market": market,
        "breadth": breadth.get(day),
        "institutional": inst,
        "margin": margin,
        "sectors": sectors(con, have, day),
        **flagged(con, have, day, stocks),
        "lists": lists,
        "revenue": revenue.lists(day, stocks),
        "signals": {k: {"count": len(sig.get(k, [])), "items": stock_list(stocks, sig.get(k, []), SIGNAL_TOP)}
                    for k in SIGNAL_KEYS},
        "ex_rights": ex,
    }


def day_path(day):
    return DAYS_DIR / day[:4] / f"{day}.json"


def needs_update(day):
    """還沒有報告、報告格式是舊版，或當時還沒公布的資料（融資融券、三大法人等）現在有了"""
    path = day_path(day)
    if not path.exists():
        return True
    old = json.loads(path.read_text(encoding="utf-8"))
    if old.get("version") != REPORT_VERSION:
        return True
    later = (("margin", "margin"), ("institutional", "institutional"), ("institutional", "institutional_amount"),
             ("sectors", "sector_index"), ("notice", "notice"), ("disposition", "disposition"))
    if any(old.get(key) is None and has_file(table, day) for key, table in later):
        return True
    # 借券賣出、當沖晚上才公布，排行是之後才有的
    if any(key not in (old.get("lists") or {}) and has_file(table, day)
           for key, table in (("sbl_up", "sbl_short"), ("day_trading", "day_trading"))):
        return True
    return bool((old.get("institutional") or {}).get("estimated") and has_file("institutional_amount", day))


# 給排程任務判斷資料是否為當天、是否齊全的資料集
LATEST_DATASETS = ("stock_day_all", "bwibbu_all", "institutional", "institutional_amount", "margin",
                   "sector_index", "notice", "disposition")


def write_latest(con, have, latest):
    """site/data/latest.json：最新交易日、產生時間、各資料是否已公布、除權息預告"""
    forecast = []
    if "ex_rights_forecast" in have:
        for r in query(con, "SELECT * FROM ex_rights_forecast ORDER BY 除權除息日期, 股票代號"):
            if r["除權除息日期"] >= latest:
                forecast.append({"date": r["除權除息日期"], "code": r["股票代號"], "name": r["名稱"], "type": r["除權息"],
                                 **stock_data.dividend(r)})
    data = {
        "date": latest,
        "generated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds"),
        "available": {name: has_file(name, latest) for name in LATEST_DATASETS},
        "report": f"data/days/{latest[:4]}/{latest}.json",
        "summary": "data/summary/{code}.json",
        "ex_rights_forecast": forecast,
    }
    (SITE / "data" / "latest.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已更新 site/data/latest.json（{latest}）")


# ---- Discord ----

def fmt_signed(v, digits=0, unit=""):
    return "—" if v is None else f"{v:+,.{digits}f}{unit}"


def taiex_png(index, day):
    """最近 60 個交易日的加權指數 K 線 + 均線 + 成交金額，回傳 PNG bytes"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    plt.rcParams["font.sans-serif"] = ["Noto Sans CJK TC", "Microsoft JhengHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    end = next(i for i, d in enumerate(index) if d["d"] == day) + 1
    closes = [d["c"] for d in index[:end]]
    data = index[max(0, end - 60):end]
    x = range(len(data))
    up, down = "#e5484d", "#30a46c"
    fig, (ax, axv) = plt.subplots(2, 1, figsize=(10, 5.6), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1], "hspace": 0.05})
    for i, d in zip(x, data):
        color = up if d["c"] >= d["o"] else down
        ax.vlines(i, d["l"], d["h"], color=color, linewidth=1)
        ax.bar(i, max(abs(d["c"] - d["o"]), d["c"] * 0.0003), bottom=min(d["o"], d["c"]), color=color, width=0.6)
        axv.bar(i, d["v"], color=color, width=0.6, alpha=0.8)
    for n, color in ((5, "#f5a623"), (20, "#3b82f6"), (60, "#a855f7"), (100, "#14b8a6")):
        ma = [sum(closes[j - n + 1:j + 1]) / n if j >= n - 1 else None for j in range(end)][-len(data):]
        ax.plot(x, ma, color=color, linewidth=1.2, label=f"MA{n}")
    last = data[-1]
    chg = ""
    if end > 1:
        prev = index[end - 2]["c"]
        chg = f"{last['c'] - prev:+,.2f}（{(last['c'] - prev) / prev * 100:+.2f}%）"
    ax.set_title(f"加權指數 {day}　收 {last['c']:,.2f}　{chg}", loc="left", fontsize=13)
    ax.legend(loc="upper left", fontsize=9, frameon=False)
    for a in (ax, axv):
        a.grid(alpha=0.2)
        a.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    axv.set_ylabel("成交金額(億)", fontsize=9)
    ticks = list(range(0, len(data), 10))
    axv.set_xticks(ticks, [data[i]["d"][5:] for i in ticks])
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def discord_embed(report, page_url):
    m, inst, mg, day = report["market"], report["institutional"], report["margin"], report["date"]
    arrow = "▲" if (m["change"] or 0) >= 0 else "▼"
    lines = [
        f"**加權指數** {m['close']:,.2f}　{arrow} {abs(m['change'] or 0):,.2f}（{fmt_signed(m['pct'], 2, '%')}）",
        f"成交金額 {m['turnover']:,.0f} 億｜上漲 {m['up']}・下跌 {m['down']}・平盤 {m['flat']}",
    ]
    fields = []
    if inst:
        fields.append({"name": "三大法人（億，估算）" if inst.get("estimated") else "三大法人（億）", "inline": True, "value":
                       f"外資 {fmt_signed(inst['foreign'], 1)}\n投信 {fmt_signed(inst['trust'], 1)}\n"
                       f"自營商 {fmt_signed(inst['dealer'], 1)}"})
    fields.append({"name": "融資融券（張）", "inline": True, "value":
                   f"融資 {fmt_signed(mg['financing'])}\n融券 {fmt_signed(mg['short'])}" if mg else "尚未公布"})
    sig = report["signals"]
    fields.append({"name": "技術訊號（檔）", "inline": True, "value":
                   f"60 日新高 {sig['new_high']['count']}・新低 {sig['new_low']['count']}\n"
                   f"黃金交叉 {sig['golden']['count']}・死亡交叉 {sig['death']['count']}\n"
                   f"爆量上漲 {sig['volume_up']['count']}・下跌 {sig['volume_down']['count']}"})
    b = report.get("breadth")
    if b:
        fields.append({"name": "市場寬度", "inline": True, "value":
                       f"站上 MA20 {b['above_ma20']}%・MA60 {b['above_ma60']}%\n"
                       f"漲停 {b['limit_up']}・跌停 {b['limit_down']}"})
    secs = [x for x in report.get("sectors") or [] if not x["group"]]
    if secs:
        fmt = lambda x: f"{x['name']} {fmt_signed(x['pct'], 2, '%')}"  # noqa: E731
        fields.append({"name": "類股（最強／最弱）", "inline": True, "value":
                       "\n".join(map(fmt, secs[:3])) + "\n—\n" + "\n".join(map(fmt, secs[-3:][::-1]))})
    for key, title in (("gainers", "漲幅前 5"), ("turnover", "成交金額前 5")):
        items = report["lists"][key][:5]
        fields.append({"name": title, "inline": True, "value": "\n".join(
            f"{s['code']} {s['name']} {fmt_signed(s['pct'], 1, '%')}" if key == "gainers"
            else f"{s['code']} {s['name']} {s['value']:,.0f} 億" for s in items) or "—"})
    return {
        "title": f"台股收盤報告 {day}",
        "url": f"{page_url}#{day}",
        "color": 0xE5484D if (m["change"] or 0) >= 0 else 0x30A46C,
        "description": "\n".join(lines),
        "fields": fields,
        "image": {"url": "attachment://taiex.png"},
        "footer": {"text": "資料來源：臺灣證券交易所｜僅供參考，非投資建議"},
    }


def send_discord(report, index, page_url):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("未設定 DISCORD_WEBHOOK_URL，略過 Discord 通知")
        return
    payload = {"embeds": [discord_embed(report, page_url)]}
    png = taiex_png(index, report["date"])
    r = requests.post(url, data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                      files={"files[0]": ("taiex.png", png, "image/png")}, timeout=60)
    r.raise_for_status()
    print("已送出 Discord 通知")


def page_url():
    if os.environ.get("REPORT_URL"):
        return os.environ["REPORT_URL"].rstrip("/") + "/"
    owner, _, repo = os.environ.get("GITHUB_REPOSITORY", "owner/repo").partition("/")
    return f"https://{owner.lower()}.github.io/{repo}/"


def load_day(day):
    return json.loads(day_path(day).read_text(encoding="utf-8"))


def notify(day) -> int:
    index = json.loads((SITE / "data" / "index.json").read_text(encoding="utf-8"))
    send_discord(load_day(day), index, page_url())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="產生收盤後報告")
    ap.add_argument("--notify", metavar="日期", help="把該日報告送到 Discord，例如 2026-10-29")
    args = ap.parse_args()
    if args.notify:
        return notify(args.notify)

    build_db.main()
    con = sqlite3.connect(build_db.DB)
    have = tables(con)
    if "taiex" not in have or "stock_day_all" not in have:
        print("沒有加權指數或個股行情資料，無法產生報告")
        return 1
    taiex_rows = [(r[0], num(r[1])) for r in con.execute("SELECT _key, ClosingIndex FROM taiex ORDER BY _key")]
    prev_close = {d: taiex_rows[i - 1][1] if i else None for i, (d, _) in enumerate(taiex_rows)}
    available = {r[0] for r in con.execute("SELECT DISTINCT _key FROM stock_day_all")}
    days = [d for d, _ in taiex_rows if d in available]
    targets = {d for d in days if needs_update(d)} | set(days[-REFRESH_DAYS:])
    latest = days[-1]
    # 通知只在最新交易日的報告「第一次有融資融券」時送：下午先產生的報告不通知，之後重產也不重複通知
    had_margin = day_path(latest).exists() and load_day(latest).get("margin") is not None
    print(f"共 {len(days)} 個交易日，這次要產生 {len(targets)} 份報告")

    signals, breadth = scan_market(con, targets)
    streaks = scan_streaks(con, have, days, targets) if targets else {}
    revenue = Revenue(con, have)
    for day in sorted(targets):
        report = day_report(con, have, day, prev_close[day], signals, breadth, streaks, revenue)
        out = day_path(day)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    # 全市場融資餘額（張，含 ETF）
    financing = {}
    if "margin" in have:
        financing = {d: int(v) for d, v in con.execute(
            'SELECT _key, SUM(CAST("融資今日餘額" AS INTEGER)) FROM margin GROUP BY _key') if v is not None}
    print(f"已產生 {stock_data.build(con, have)} 檔個股資料（site/data/stocks/、site/data/summary/）")
    write_latest(con, have, latest)
    con.close()

    # 給網頁圖表用的每日摘要：大盤、法人，以及市場寬度（a20 / a60：站上 MA20 / MA60 的 %，nh / nl：60 日新高 / 新低家數，
    # ad：上漲減下跌家數，lu / ld：漲停 / 跌停家數；都只算上市一般股票）、融資餘額 mf（張）
    index = []
    for day in days:
        if not day_path(day).exists():
            continue
        r = load_day(day)
        m, inst, b = r["market"], r["institutional"] or {}, breadth.get(day) or {}
        index.append({"d": day, "o": m["open"], "h": m["high"], "l": m["low"], "c": m["close"],
                      "v": m["turnover"], "f": inst.get("foreign"), "t": inst.get("trust"),
                      "dl": inst.get("dealer"), "a20": b.get("above_ma20"), "a60": b.get("above_ma60"),
                      "nh": b.get("new_high"), "nl": b.get("new_low"),
                      "ad": None if not b else b["up"] - b["down"], "lu": b.get("limit_up"), "ld": b.get("limit_down"),
                      "mf": financing.get(day)})
    (SITE / "data" / "index.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")),
                                               encoding="utf-8")
    print(f"已更新 site/data/index.json（{len(index)} 天）")

    if load_day(latest).get("margin") is None:
        print(f"最新交易日 {latest} 的融資融券還沒公布，先不通知")
    elif had_margin:
        print(f"最新交易日 {latest} 的完整報告先前已產生，不重複通知")
    else:
        print(f"最新交易日 {latest} 的報告已完整，可以送出通知")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                f.write(f"new_day={latest}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
