"""產生收盤後報告：GitHub Pages 互動網頁的資料，以及 Discord 摘要

用法：
  python scripts/report.py                    產生報告資料
  python scripts/report.py --notify 日期      把該日報告送到 Discord（需要環境變數 DISCORD_WEBHOOK_URL）

產生報告資料時：
  1. 執行 build_db.py 建立 twse.db（含還原權息日 K）
  2. 為還沒有報告、或當時資料不齊（融資融券、三大法人晚公布）的交易日
     產生 site/data/days/<年>/<日期>.json（會 commit 進 repo）
  3. 重新產生 site/data/index.json（每個交易日的大盤摘要，給網頁圖表用；每次重建，不進 git）
  4. 重新產生個股頁面資料 site/data/stocks/（見 stock_data.py；每次重建，不進 git）
  5. 最新交易日的報告若是這次第一次產生的（補資料重新產生的不算），在 GitHub Actions 輸出 new_day=<日期>，
     workflow 會在網頁發布後再送 Discord，避免通知先到、網頁還沒更新

法人買賣超金額 = 買賣超股數 × 當日收盤價，是估算值。
"""
import argparse
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
TOP = 10                 # 排行榜列幾檔
SIGNAL_TOP = 20          # 每種技術訊號最多列幾檔
SIGNAL_MIN_VALUE = 1e8   # 當日成交金額低於 1 億的股票不列入技術訊號，避免冷門股雜訊
YI = 1e8                 # 億
REPORT_VERSION = 2       # 報告格式改變時加 1，舊的報告會在下次執行時全部重新產生
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


# ---- 技術訊號：依股票、日期依序掃過一次還原日 K ----

def scan_signals(con, targets):
    """回傳 {日期: {'new_high': [代號...], 'golden': [...], 'death': [...], 'volume': [...]}}"""
    out = defaultdict(lambda: defaultdict(list))
    sql = "SELECT code, date, adj_close, volume, value FROM stock_day_adj ORDER BY code, date"
    code_now, closes, vols, prev_ma5, prev_ma20 = None, None, None, None, None
    for code, day, close, volume, value in con.execute(sql):
        if not is_stock(code) or close is None:
            continue
        if code != code_now:
            code_now, closes, vols, prev_ma5, prev_ma20 = code, deque(maxlen=60), deque(maxlen=20), None, None
        prev_high = max(list(closes)[-59:]) if len(closes) >= 59 else None  # 前 59 天最高收盤
        closes.append(close)
        window = list(closes)
        ma5 = sum(window[-5:]) / 5 if len(window) >= 5 else None
        ma20 = sum(window[-20:]) / 20 if len(window) >= 20 else None
        if day in targets and (value or 0) >= SIGNAL_MIN_VALUE:
            s = out[day]
            if prev_high is not None and close > prev_high:
                s["new_high"].append(code)
            if None not in (prev_ma5, prev_ma20, ma5, ma20):
                if prev_ma5 <= prev_ma20 and ma5 > ma20:
                    s["golden"].append(code)
                elif prev_ma5 >= prev_ma20 and ma5 < ma20:
                    s["death"].append(code)
            if len(vols) == 20 and sum(vols) > 0 and (volume or 0) >= 3 * sum(vols) / 20:
                s["volume"].append(code)
        vols.append(volume or 0)
        prev_ma5, prev_ma20 = ma5, ma20
    return out


# ---- 單日報告 ----

def stock_list(stocks, codes, limit):
    items = [stocks[c] for c in codes if c in stocks]
    items.sort(key=lambda s: -(s["value"] or 0))
    return [{k: s[k] for k in ("code", "name", "close", "pct", "value")} for s in items[:limit]]


def day_report(con, have, day, prev_close, signals):
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
    for r in query(con, 'SELECT Code, Name, ClosingPrice, Change, TradeValue FROM stock_day_all WHERE _key = ?', day):
        c, chg = num(r["ClosingPrice"]), num(r["Change"])
        base = c - chg if c is not None and chg is not None else None
        stocks[r["Code"]] = {
            "code": r["Code"], "name": r["Name"], "close": c,
            "pct": round(chg / base * 100, 2) if base else None,
            "value": round((num(r["TradeValue"]) or 0) / YI, 4),   # 億；小型股要到百萬位才看得出差異
            "chg": chg,
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

    # 除權息
    ex = []
    if "ex_rights" in have:
        for r in query(con, "SELECT * FROM ex_rights WHERE _key = ?", day):
            ex.append({"code": r["股票代號"], "name": r["股票名稱"], "type": r["權/息"],
                       "amount": num(r["權值+息值"]), "before": num(r["除權息前收盤價"]),
                       "ref": num(r["除權息參考價"])})

    sig = signals.get(day, {})
    return {
        "version": REPORT_VERSION,
        "date": day,
        "market": market,
        "institutional": inst,
        "margin": margin,
        "lists": lists,
        "signals": {k: {"count": len(sig.get(k, [])), "items": stock_list(stocks, sig.get(k, []), SIGNAL_TOP)}
                    for k in ("new_high", "golden", "death", "volume")},
        "ex_rights": ex,
    }


def day_path(day):
    return DAYS_DIR / day[:4] / f"{day}.json"


def needs_update(con, have, day):
    """還沒有報告、報告格式是舊版，或當時融資融券、三大法人還沒公布但現在有了"""
    path = day_path(day)
    if not path.exists():
        return True
    old = json.loads(path.read_text(encoding="utf-8"))
    if old.get("version") != REPORT_VERSION:
        return True
    for key, table in (("margin", "margin"), ("institutional", "institutional")):
        if old.get(key) is None and table in have and \
                con.execute(f"SELECT 1 FROM {table} WHERE _key = ? LIMIT 1", (day,)).fetchone():
            return True
    return False


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
        fields.append({"name": "三大法人（億，估算）", "inline": True, "value":
                       f"外資 {fmt_signed(inst['foreign'], 1)}\n投信 {fmt_signed(inst['trust'], 1)}\n"
                       f"自營商 {fmt_signed(inst['dealer'], 1)}"})
    fields.append({"name": "融資融券（張）", "inline": True, "value":
                   f"融資 {fmt_signed(mg['financing'])}\n融券 {fmt_signed(mg['short'])}" if mg else "尚未公布"})
    sig = report["signals"]
    fields.append({"name": "技術訊號（檔）", "inline": True, "value":
                   f"60 日新高 {sig['new_high']['count']}\n黃金交叉 {sig['golden']['count']}\n"
                   f"死亡交叉 {sig['death']['count']}\n爆量 {sig['volume']['count']}"})
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
    targets = {d for d in days if needs_update(con, have, d)}
    brand_new = {d for d in targets if not day_path(d).exists()}   # 只有第一次產生才通知，補資料重產不重複通知
    print(f"共 {len(days)} 個交易日，這次要產生 {len(targets)} 份報告")

    signals = scan_signals(con, targets) if targets else {}
    for day in sorted(targets):
        report = day_report(con, have, day, prev_close[day], signals)
        out = day_path(day)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"已產生 {stock_data.build(con, have)} 檔個股資料（site/data/stocks/）")
    con.close()

    # 給網頁圖表用的每日摘要
    index = []
    for day in days:
        if not day_path(day).exists():
            continue
        r = load_day(day)
        m, inst = r["market"], r["institutional"] or {}
        index.append({"d": day, "o": m["open"], "h": m["high"], "l": m["low"], "c": m["close"],
                      "v": m["turnover"], "f": inst.get("foreign"), "t": inst.get("trust"),
                      "dl": inst.get("dealer")})
    (SITE / "data" / "index.json").write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")),
                                               encoding="utf-8")
    print(f"已更新 site/data/index.json（{len(index)} 天）")

    latest = days[-1]
    if latest in brand_new:
        print(f"最新交易日 {latest} 的報告是新產生的，可以送出通知")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                f.write(f"new_day={latest}\n")
    else:
        print(f"最新交易日 {latest} 的報告先前已產生，不重複通知")
    return 0


if __name__ == "__main__":
    sys.exit(main())
