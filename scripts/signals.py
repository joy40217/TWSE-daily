"""自選股訊號、⭐ 個股技術數字、持股停損停利與訊號後續驗證（給排程任務 clone repo 後直接跑）

只讀 data/ 的 CSV、不連網、只用標準函式庫；不在 GitHub Actions 裡跑。

需要的資料夾（上市＋上櫃 tpex_，日資料取今年與去年即可，缺的會在 ready 標明）：
    必要  stock_day_all、institutional、margin（年份資料夾），taiex、tpex_index
    建議  notice、disposition（今年），ex_rights（今年＋去年，算還原權息），ex_rights_forecast、holiday_schedule
    選用  day_trading、sbl_short、bwibbu_all（⭐ 個股的當沖比、借券、本益比，只讀當天與前一天）

用法：
    python scripts/signals.py --date auto --watch 2330,2317 --star 2330 --holdings holdings.json --backtest --out out/

    --date        auto（data/taiex/ 最新的交易日）或 YYYY-MM-DD
    --watch       自選股代號（逗號分隔）       --star  ⭐ 特別關注（--watch 的子集）
    --holdings    持股 JSON：[{"code": "2330", "cost": 1050.5}, ...]，代號可以不在 --watch 裡
    --backtest    另外輸出訊號驗證（backtest_*.json）；--backtest-days 回測幾個交易日（預設 120）
    --universe    watch（預設）或 all（全部上市櫃股票，只用於回測研究，比較慢）
    --out         輸出資料夾（預設 out/）

輸出 signals_<日期>.json、signals_latest.json（加 --backtest 時還有 backtest_<日期>.json、backtest_latest.json），
stdout 只印一行摘要。結束代碼：0 完成；2 完成但當天必要資料不齊（ready 會標明，排程任務等幾分鐘重新 clone 再跑）；
1 程式錯誤。

均線、60 日高低、RSI、N 日漲跌、回測報酬一律用還原權息收盤價（跟網站 summary 相同，算法共用 build_db.adjust_factors）；
成交量、法人、融資用原始數字；給人看的收盤價是原始收盤價。欄位定義見 README「自選股訊號腳本」與各函式說明。
"""
import argparse
import bisect
import csv
import datetime as dt
import json
import statistics
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_db  # noqa: E402
from stock_data import first, lots, roc_date, streak  # noqa: E402

num = build_db.num
WINDOW = 300            # 只載入 --date 往前 300 個交易日
TZ = dt.timezone(dt.timedelta(hours=8))
MA_DAYS = (5, 20, 60, 100)
MA_LABEL = {5: "5 日線", 20: "月線", 60: "季線", 100: "百日線"}
HORIZONS = (1, 5, 10)
TIE = 0.005            # 價格差不到 0.005 元視為相等（收盤平之前的高低點）

# 訊號矩陣的欄位（順序固定，跟文件的表格相同）：(key, 中文標題, 方向)
COLUMNS = (
    ("cross_up", "🟢站上均線", "bull"), ("high60", "🟢60日新高", "bull"), ("vol_up", "🟢爆量漲", "bull"),
    ("inst_buy", "🟢法人連買", "bull"), ("up_streak", "🟢連漲", "bull"), ("buy_price_down", "🟢法人買價跌", "bull"),
    ("cross_down", "🔴跌破均線", "bear"), ("low60", "🔴60日新低", "bear"), ("vol_down", "🔴爆量跌", "bear"),
    ("inst_sell", "🔴法人連賣", "bear"), ("down_streak", "🔴連跌", "bear"), ("sell_price_up", "🔴法人賣價漲", "bear"),
    ("margin_up", "🔴融資增", "bear"),
    ("notice", "⚠️注意處置", "event"), ("ex_rights", "⚠️除權息", "event"), ("earnings", "⚠️法說財報", "event"),
    ("vol_dry", "⚠️量縮", "event"),
)
DIRECTION = {k: d for k, _, d in COLUMNS}
TITLE = {k: t for k, t, _ in COLUMNS}

# 各資料集的代號欄位
CODE_COL = {"stock_day_all": "Code", "institutional": "證券代號", "margin": "股票代號", "notice": "證券代號",
            "disposition": "證券代號", "ex_rights": "股票代號", "day_trading": "證券代號", "sbl_short": "代號",
            "bwibbu_all": "Code"}
REQUIRED = {"price": "stock_day_all", "institutional": "institutional", "margin": "margin"}
INDEX_TABLE = {"上市": "taiex", "上櫃": "tpex_index"}


def both(ds):
    """上市資料集與對應的上櫃資料集"""
    return ((ds, "上市"), (f"tpex_{ds}", "上櫃"))


# ---------------------------------------------------------------- 讀資料

class Store:
    """讀 data/<資料集>/<年>/<日期>.csv。只要部分代號時，先在文字裡找代號所在的那一行再解析，比整份 CSV 解析快很多"""

    def __init__(self, root: Path):
        self.root = root

    def has(self, ds):
        return (self.root / ds).is_dir()

    def path(self, ds, day):
        return self.root / ds / day[:4] / f"{day}.csv"

    def text(self, ds, day):
        p = self.path(ds, day)
        return p.read_text(encoding="utf-8-sig") if p.exists() else None

    def rows(self, ds, day, codes=None):
        """{代號: [row, ...]}（同一個代號可能有好幾列，例如處置期間重疊）；檔案不存在回傳 None"""
        text = self.text(ds, day)
        return None if text is None else parse(text, CODE_COL[ds.removeprefix("tpex_")], codes)

    def has_rows(self, ds, day):
        text = self.text(ds, day)
        return bool(text) and any(line.strip() for line in text.splitlines()[1:])


def parse(text, code_col, codes=None):
    nl = text.find("\n")
    header = next(csv.reader([text[:nl if nl >= 0 else None].rstrip("\r")]))
    if nl < 0 or code_col not in header:
        return {}
    ki = header.index(code_col)
    out = {}

    def add(line):
        row = next(csv.reader([line.rstrip("\r")]), None)
        if row and len(row) > ki:
            out.setdefault(row[ki].strip(), []).append(dict(zip(header, row)))

    if codes is None:
        for line in text[nl + 1:].splitlines():
            if line.strip():
                add(line)
        return out
    for code in codes:
        needle = ("\n" if ki == 0 else ",") + code + ","
        i = text.find(needle, nl)
        while i >= 0:
            start = i + 1 if ki == 0 else text.rfind("\n", 0, i) + 1
            if ki == 0 or text.count(",", start, i + 1) == ki:
                end = text.find("\n", i + 1)
                add(text[start:end if end >= 0 else None])
            i = text.find(needle, i + 1)
    return out


def index_close(store, day):
    """{'上市': 加權指數收盤, '上櫃': 櫃買指數收盤}"""
    out = {}
    for market, ds in INDEX_TABLE.items():
        text = store.text(ds, day)
        if text:
            rows = list(csv.DictReader(text.splitlines()))
            out[market] = num(rows[0].get("ClosingIndex")) if rows else None
    return out


def calendar(store):
    """交易日曆：data/taiex/ 的檔名"""
    return sorted(p.stem for p in (store.root / "taiex").glob("*/*.csv")) if store.has("taiex") else []


def holidays(store):
    """休市表 -> (休市日集合, 有資料的年份, 判斷不出來的列)。「開始交易日」「最後交易日」這類說明列不是休市日"""
    p = store.root / "holiday_schedule" / "holiday_schedule.csv"
    if not p.exists():
        return set(), set(), ["沒有 holiday_schedule"]
    days, years, unknown = set(), set(), []
    for r in csv.DictReader(p.read_text(encoding="utf-8-sig").splitlines()):
        raw = (r.get("Date") or "").strip()
        if len(raw) != 7 or not raw.isdigit():
            unknown.append(raw)
            continue
        day = f"{int(raw[:3]) + 1911}-{raw[3:5]}-{raw[5:]}"
        years.add(day[:4])
        name, desc = r.get("Name") or "", r.get("Description") or ""
        if "交易日" in name:
            continue
        if any(k in name + desc for k in ("放假", "無交易", "休市", "補假")):
            days.add(day)
        else:
            unknown.append(f"{day} {name}")
    return days, years, unknown


# ---------------------------------------------------------------- 個股時間序列

class Series:
    """一檔股票在載入範圍內的日 K、法人、融資（依有行情的日子對齊，第 i 個值對應 d[i]）"""

    def __init__(self, code):
        self.code = code
        self.d, self.market, self.name = [], [], []
        self.o, self.h, self.l, self.c, self.chg, self.v, self.shares = [], [], [], [], [], [], []
        self.fi, self.tr, self.dl, self.mf, self.ms = [], [], [], [], []

    def finish(self, events):
        self.adj = [None if c is None else round(c * f, 4)
                    for c, f in zip(self.c, build_db.adjust_factors(events, self.d))]
        self.at = {day: i for i, day in enumerate(self.d)}
        self.traded = [i for i, x in enumerate(self.adj) if x is not None]
        self.pos = {k: p for p, k in enumerate(self.traded)}
        self.ac = [self.adj[k] for k in self.traded]
        self.pct = [pct(c, chg) for c, chg in zip(self.c, self.chg)]
        self.rsi = wilder_rsi(self.ac)
        return self

    def prev_traded(self, k):
        p = self.pos.get(k)
        return self.traded[p - 1] if p else None


def pct(c, chg):
    """漲跌幅 %（同 stock_data.summary()：漲跌價差 ÷ 前一天的收盤或參考價）"""
    return round(chg / (c - chg) * 100, 2) if None not in (c, chg) and c != chg else None


def wilder_rsi(ac, n=14):
    """Wilder RSI：前 n 天漲跌的簡單平均作起點，之後 (前值 × (n−1) + 當天) ÷ n；out[p] 只用到 p（含）以前的資料"""
    out = [None] * len(ac)
    if len(ac) <= n:
        return out
    deltas = [b - a for a, b in zip(ac, ac[1:])]
    gain = sum(max(x, 0) for x in deltas[:n]) / n
    loss = sum(max(-x, 0) for x in deltas[:n]) / n
    for p in range(n, len(ac)):
        if p > n:
            x = deltas[p - 1]
            gain, loss = (gain * (n - 1) + max(x, 0)) / n, (loss * (n - 1) + max(-x, 0)) / n
        out[p] = 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def load_series(store, days, codes, until):
    """days：載入範圍的交易日；codes：要的代號（None＝全部）；until：除權息只算到這天（含）"""
    series = {}

    def get(code):
        if code not in series:
            series[code] = Series(code)
        return series[code]

    for day in days:
        for ds, market in both("stock_day_all"):
            for code, rows in (store.rows(ds, day, codes) or {}).items():
                r, s = rows[0], get(code)
                s.d.append(day)
                s.market.append(market)
                s.name.append((r.get("Name") or "").strip())
                for k, col in (("o", "OpeningPrice"), ("h", "HighestPrice"), ("l", "LowestPrice"),
                               ("c", "ClosingPrice"), ("chg", "Change")):
                    getattr(s, k).append(num(r.get(col)))
                shares = num(r.get("TradeVolume"))
                s.shares.append(shares)
                s.v.append(lots(shares))
        inst, margin = {}, {}
        for ds, _ in both("institutional"):
            inst.update({code: rows[0] for code, rows in (store.rows(ds, day, codes) or {}).items()})
        for ds, _ in both("margin"):
            margin.update({code: rows[0] for code, rows in (store.rows(ds, day, codes) or {}).items()})
        for code, s in series.items():
            if not s.d or s.d[-1] != day:
                continue
            r = inst.get(code)
            if r is None:
                s.fi.append(None), s.tr.append(None), s.dl.append(None)
            else:   # 外資＝外陸資＋外資自營商（同 stock_data.stock_json()）
                s.fi.append(lots((first(r, "外陸資買賣超股數(不含外資自營商)", "外資買賣超股數") or 0)
                                 + (first(r, "外資自營商買賣超股數") or 0)))
                s.tr.append(lots(first(r, "投信買賣超股數")))
                s.dl.append(lots(first(r, "自營商買賣超股數")))
            r = margin.get(code)
            for lst, col in ((s.mf, "融資今日餘額"), (s.ms, "融券今日餘額")):
                v = num(r.get(col)) if r else None
                lst.append(None if v is None else int(v))

    events = ex_events(store, days[0], until, None if codes is None else set(series))
    return {code: s.finish(events.get(code, [])) for code, s in series.items()}


def ex_events(store, start, until, codes):
    """還原權息用的除權息事件 {代號: [(日期, 因子), ...]}；只要 start～until 之間的（更早的不影響載入範圍的還原價）"""
    events = {}
    for ds, _ in both("ex_rights"):
        for p in sorted((store.root / ds).glob("*/*.csv")) if store.has(ds) else []:
            if not start <= p.stem <= until:
                continue
            text = p.read_text(encoding="utf-8-sig")
            header = next(csv.reader([text.split("\n", 1)[0].rstrip("\r")]))
            c_code, c_before, c_ref = build_db.ex_columns(header)
            if not all((c_code, c_before, c_ref)):
                continue
            for code, rows in parse(text, c_code, codes).items():
                for r in rows:
                    f = build_db.ex_factor(r.get(c_before), r.get(c_ref))
                    if f is not None:
                        events.setdefault(code, []).append((p.stem, f))
    for v in events.values():
        v.sort()
    return events


def ex_schedule(store, start, until, codes):
    """已知的除權息日 {代號: [(日期, 權/息/權息), ...]}：除權息預告，加上載入範圍內實際除權息的紀錄（回測用）"""
    out = {}
    for ds, _ in both("ex_rights_forecast"):
        p = store.root / ds / f"{ds}.csv"
        if p.exists():
            for r in csv.DictReader(p.read_text(encoding="utf-8-sig").splitlines()):
                code = (r.get("股票代號") or "").strip()
                if codes is None or code in codes:
                    out.setdefault(code, set()).add(((r.get("除權除息日期") or "").strip(), (r.get("除權息") or "").strip()))
    for ds, _ in both("ex_rights"):
        for p in sorted((store.root / ds).glob("*/*.csv")) if store.has(ds) else []:
            if start <= p.stem <= until:
                for code, rows in parse(p.read_text(encoding="utf-8-sig"), "股票代號", codes).items():
                    out.setdefault(code, set()).add((p.stem, (rows[0].get("權/息") or "").strip()))
    return {code: sorted(v) for code, v in out.items()}


def forecast(store, codes, after):
    """除權息預告：{代號: [{'date', 'type', 'cash_dividend'}, ...]}，只取 after 之後的"""
    out = {}
    for ds, _ in both("ex_rights_forecast"):
        p = store.root / ds / f"{ds}.csv"
        if not p.exists():
            continue
        for r in csv.DictReader(p.read_text(encoding="utf-8-sig").splitlines()):
            code, day = (r.get("股票代號") or "").strip(), (r.get("除權除息日期") or "").strip()
            if code in codes and day > after:
                cash = num(r.get("現金股利"))
                out.setdefault(code, []).append({"date": day, "type": (r.get("除權息") or "").strip(),
                                                 "cash_dividend": r.get("現金股利") if cash is None else cash})
    return {code: sorted(v, key=lambda x: x["date"]) for code, v in out.items()}


def load_events_by_day(store, days, codes):
    """注意股 {日期: {代號: 注意交易資訊}}、處置股 {日期: {代號: [(起, 迄, 處置措施), ...]}}"""
    notice, disp = {}, {}
    for day in days:
        for ds, _ in both("notice"):
            for code, rows in (store.rows(ds, day, codes) or {}).items():
                notice.setdefault(day, {})[code] = (rows[0].get("注意交易資訊") or "").strip()
        for ds, _ in both("disposition"):
            for code, rows in (store.rows(ds, day, codes) or {}).items():
                for r in rows:
                    try:
                        start, end = (roc_date(x) for x in r["處置起迄時間"].split("～"))
                    except (KeyError, ValueError, AttributeError):
                        continue
                    disp.setdefault(day, {}).setdefault(code, []).append((start, end, (r.get("處置措施") or "").strip()))
    return notice, disp


# ---------------------------------------------------------------- 指標

def avg(values):
    return sum(values) / len(values)


def snapshot(s, k, ctx):
    """股票 s 在第 k 天（s.d[k]，必須有收盤價）的指標，只用到 k（含）以前的資料。
    價格類換算成「站在第 k 天看」的還原價（第 k 天的還原價＝實際價格），數字不四捨五入"""
    p = s.pos[k]
    ac = s.ac
    scale = s.c[k] / s.adj[k]

    def ma(n, q=p):
        return sum(ac[q - n + 1:q + 1]) / n * scale if q + 1 >= n else None

    m = {"date": s.d[k], "close": s.c[k], "pct": s.pct[k], "volume": s.v[k], "n": p + 1}
    for n in MA_DAYS:
        m[f"ma{n}"] = ma(n)
    m["prev_close"] = ac[p - 1] * scale if p >= 1 else None
    m["prev_ma20"], m["prev_ma60"] = (ma(20, p - 1), ma(60, p - 1)) if p >= 1 else (None, None)
    w = ac[max(0, p - 59):p + 1]
    m["high_60"], m["low_60"] = max(w) * scale, min(w) * scale
    # 前 59 天的最高、最低收盤與最近一次出現的日子（有 60 天才判斷新高新低）
    m["prev59_high"] = m["prev59_low"] = m["prev59_high_date"] = m["prev59_low_date"] = None
    if p >= 59:
        prev = list(range(p - 59, p))
        hi = max(reversed(prev), key=lambda q: ac[q])   # 同樣的價格出現好幾次時取最近的
        lo = min(reversed(prev), key=lambda q: ac[q])
        m["prev59_high"], m["prev59_high_date"] = ac[hi] * scale, s.d[s.traded[hi]]
        m["prev59_low"], m["prev59_low_date"] = ac[lo] * scale, s.d[s.traded[lo]]
    vols = [s.v[j] for j in s.traded[max(0, p - 20):p] if s.v[j] is not None]
    m["vol_avg20"] = avg(vols) if p >= 20 and len(vols) == 20 else None
    m["vol_ratio"] = round(s.v[k] / m["vol_avg20"], 2) if m["vol_avg20"] and s.v[k] is not None else None
    m["rsi14"] = s.rsi[p]
    for n in (20, 60):
        m[f"dev_ma{n}"] = (s.c[k] / m[f"ma{n}"] - 1) * 100 if m[f"ma{n}"] else None
    m["up_days"], m["down_days"] = run(s, p, lambda x: x > 0), run(s, p, lambda x: x < 0)
    m["ret_5d"] = (ac[p] / ac[p - 5] - 1) * 100 if p >= 5 else None

    fi, tr, dl = s.fi[:k + 1], s.tr[:k + 1], s.dl[:k + 1]
    m["streak"] = {"foreign": streak(fi), "trust": streak(tr), "dealer": streak(dl)}
    j = next((i for i in range(k, -1, -1) if fi[i] is not None), -1)
    for n in (5, 20):   # 算法同 summary：最後一個有法人資料的日子往前 n 筆
        sums = {key: sum(v for v in src[max(0, j - n + 1):j + 1] if v is not None) if j >= 0 else None
                for key, src in (("foreign", fi), ("trust", tr), ("dealer", dl))}
        sums["total"] = None if j < 0 else sum(sums.values())
        m[f"sum_{n}d"] = sums
    m["inst_today"] = None if s.fi[k] is None else {
        "foreign": s.fi[k], "trust": s.tr[k], "dealer": s.dl[k], "total": s.fi[k] + (s.tr[k] or 0) + (s.dl[k] or 0)}

    change = lambda i, lst=s.mf: lst[i] - lst[i - 1] if i >= 1 and None not in (lst[i], lst[i - 1]) else None  # noqa: E731
    m["financing"], m["financing_change"] = s.mf[k], change(k)
    m["short"], m["short_change"] = s.ms[k], change(k, s.ms)
    n_up, i = 0, k
    while (x := change(i)) is not None and x > 0:
        n_up, i = n_up + 1, i - 1
    m["financing_up_days"] = n_up
    prev = [abs(x) for i in range(max(0, k - 20), k) if (x := change(i)) is not None]
    m["financing_abs_avg20"] = avg(prev) if prev else None

    day = s.d[k]
    m["notice"] = ctx["notice"].get(day, {}).get(s.code)
    disp = sorted(x for x in ctx["disposition"].get(day, {}).get(s.code, []) if x[0] <= day <= x[1])
    m["disposition"] = {"period": f"{disp[-1][0]}～{disp[-1][1]}", "measure": disp[-1][2]} if disp else None
    m["ex_upcoming"] = [x for x in ctx["ex_dates"].get(s.code, []) if x[0] > day]
    return m


def run(s, p, cond):
    """截至第 p 個交易日，連續符合 cond 的天數（用漲跌幅判斷，0 或沒資料就中斷）"""
    n = 0
    while p >= 0:
        x = s.pct[s.traded[p]]
        if x is None or not cond(x):
            break
        n, p = n + 1, p - 1
    return n


# ---------------------------------------------------------------- 訊號

def fmt_lots(v):
    return f"{v:+,}張".replace("-", "−")


def fmt_pct(v):
    return f"{v:+.1f}%".replace("-", "−")


def ex_word(kind):
    return "除" + (kind or "權息")


def md(day):
    return f"{int(day[5:7])}/{int(day[8:])}"


def signals(m, ctx):
    """第 6 節的訊號：{key: (level, 格子文字)}，沒觸發的不放。level 2＝●、1＝○"""
    out = {}
    close, n = m["close"], m["n"]

    for key, cmp in (("cross_up", lambda prev, pm, c, cm: prev <= pm and c > cm),
                     ("cross_down", lambda prev, pm, c, cm: prev >= pm and c < cm)):
        lines = [MA_LABEL[x] for x in (20, 60)
                 if None not in (m["prev_close"], m[f"prev_ma{x}"], m[f"ma{x}"])
                 and cmp(m["prev_close"], m[f"prev_ma{x}"], close, m[f"ma{x}"])]
        if lines:
            out[key] = (2, "● " + " ".join(lines))

    # 收盤剛好等於之前的最高（最低）收盤也算新高（新低），括號寫上次出現的日子；
    # 還原價換算有浮點誤差，差不到 0.005 元視為相等。60 天完全沒動（最高＝最低）時不算平新高、新低
    flat = m["prev59_high"] is not None and m["prev59_high"] - m["prev59_low"] < TIE
    if m["prev59_high"] is not None:
        if abs(close - m["prev59_high"]) < TIE and not flat:
            out["high60"] = (2, f"● 新高（{md(m['prev59_high_date'])}）")
        elif close > m["prev59_high"]:
            out["high60"] = (2, "● 新高")
        elif m["high_60"] / close - 1 <= 0.03:
            out["high60"] = (1, f"○ 差{(m['high_60'] / close - 1) * 100:.1f}%")
    if m["prev59_low"] is not None:
        if abs(close - m["prev59_low"]) < TIE and not flat:
            out["low60"] = (2, f"● 新低（{md(m['prev59_low_date'])}）")
        elif close < m["prev59_low"]:
            out["low60"] = (2, "● 新低")
        elif close / m["low_60"] - 1 <= 0.03:
            out["low60"] = (1, f"○ 差{(close / m['low_60'] - 1) * 100:.1f}%")

    vr, change = m["vol_ratio"], m["pct"]
    if vr is not None and change is not None and vr >= 2:
        if change > 0:
            out["vol_up"] = (2, f"● {vr:.1f}倍")
        elif change < 0:
            out["vol_down"] = (2, f"● {vr:.1f}倍")
    if vr is not None and vr <= 0.5:
        out["vol_dry"] = (1, f"○ {vr:.1f}倍")

    st = m["streak"]
    for key, sign in (("inst_buy", 1), ("inst_sell", -1)):
        parts = [(label, st[who] * sign) for label, who in (("外", "foreign"), ("投", "trust")) if st[who] * sign >= 3]
        if parts:
            level = 2 if any(x >= 5 for _, x in parts) else 1
            out[key] = (level, ("● " if level == 2 else "○ ") + " ".join(f"{label}{x}" for label, x in parts))

    if m["up_days"] >= 3:
        out["up_streak"] = (1, f"○ {m['up_days']}天")
    if m["down_days"] >= 3:
        out["down_streak"] = (1, f"○ {m['down_days']}天")

    total, ret = m["sum_5d"]["total"], m["ret_5d"]
    if total is not None and ret is not None:
        if total > 0 and ret < 0:
            out["buy_price_down"] = (2, f"● {fmt_lots(total)} {fmt_pct(ret)}")
        elif total < 0 and ret > 0:
            out["sell_price_up"] = (2, f"● {fmt_lots(total)} {fmt_pct(ret)}")

    fc, fa = m["financing_change"], m["financing_abs_avg20"]
    if fc is not None and fa is not None and change is not None and fc > 0 and fc >= 2 * fa and fc >= 200 and change < 0:
        out["margin_up"] = (2, f"● {fmt_lots(fc)}")
    elif m["financing_up_days"] >= 3:
        out["margin_up"] = (1, f"○ 連增{m['financing_up_days']}天")

    tags = [x for x, on in (("注意股", m["notice"] is not None), ("處置股", m["disposition"] is not None)) if on]
    if tags:
        out["notice"] = (2, "● " + " ".join(tags))

    nxt, friday = ctx["next_day"](m["date"]), ctx["friday"](m["date"])
    for day, kind in m["ex_upcoming"]:
        if day == nxt:
            out["ex_rights"] = (2, f"● 明天{ex_word(kind)}")
            break
        if day <= friday and "ex_rights" not in out:
            out["ex_rights"] = (1, f"○ {md(day)}{ex_word(kind)}")
    return out


def cells(sig):
    return [sig[k][1] if k in sig else "" for k, _, _ in COLUMNS]


# ---------------------------------------------------------------- 輸出：個股、⭐、持股

def r2(v):
    return None if v is None else round(v, 2)


def r1(v):
    return None if v is None else round(v, 1)


def price(v):
    """價格兩位小數，去掉多餘的 0（整數輸出成整數）"""
    if v is None:
        return None
    v = round(v, 2)
    return int(v) if v == int(v) else v


def stock_out(s, k, m):
    """第 5 節：每檔股票在 D 的指標"""
    traded = s.traded[:s.pos[k] + 1]
    raw = [s.c[j] for j in traded]
    w = traded[-60:]
    hi = max(w, key=lambda j: s.h[j] or 0)
    lo = min(w, key=lambda j: s.l[j] if s.l[j] is not None else float("inf"))
    return {
        "code": s.code, "name": s.name[k], "market": s.market[k], "date": s.d[k],
        "close": s.c[k], "pct": m["pct"], "volume": m["volume"],
        **{f"ma{n}": r2(m[f"ma{n}"]) for n in MA_DAYS},
        "ma_raw": {f"ma{n}": round(sum(raw[-n:]) / n, 2) for n in MA_DAYS if len(raw) >= n},
        "high_60": r2(m["high_60"]), "low_60": r2(m["low_60"]),
        "range_60": {"high": s.h[hi], "high_date": s.d[hi], "low": s.l[lo], "low_date": s.d[lo]},
        "vol_avg20": None if m["vol_avg20"] is None else round(m["vol_avg20"]),
        "vol_ratio": m["vol_ratio"], "rsi14": r1(m["rsi14"]),
        "dev_ma20": r1(m["dev_ma20"]), "dev_ma60": r1(m["dev_ma60"]),
        "up_days": m["up_days"], "down_days": m["down_days"], "ret_5d": r2(m["ret_5d"]),
        "streak": m["streak"], "sum_5d": m["sum_5d"], "sum_20d": m["sum_20d"], "institutional": m["inst_today"],
        "financing": m["financing"], "financing_change": m["financing_change"],
        "financing_up_days": m["financing_up_days"], "financing_abs_avg20": r1(m["financing_abs_avg20"]),
        "short": m["short"], "short_change": m["short_change"],
        "notice": m["notice"], "disposition": m["disposition"],
    }


def star_out(s, k, m, extra):
    """第 7.1 節：⭐ 個股的價位地圖、技術面、籌碼面"""
    close = s.c[k]
    is_high = m["high_60"] is not None and close >= m["high_60"] - TIE
    is_low = m["low_60"] is not None and close <= m["low_60"] + TIE
    items = [(m["high_60"], "60 日高")] if not is_high else []
    items += [(m[f"ma{n}"], MA_LABEL[n]) for n in (100, 60, 20, 5)]
    items += [(m["low_60"], "60 日低")] if not is_low else []
    rows = [{"price": price(v), "label": label, "dev_pct": r1((close / v - 1) * 100)} for v, label in items if v]
    label = "◀ 收盤" + ("（60 日新高）" if is_high else "（60 日新低）" if is_low else "")
    rows.append({"price": price(close), "label": label, "dev_pct": None})
    rows.sort(key=lambda x: (-x["price"], x["dev_pct"] is None))

    mas = [m[f"ma{n}"] for n in MA_DAYS]
    order = ("mixed" if None in mas else "bull" if mas[0] > mas[1] > mas[2] > mas[3]
             else "bear" if mas[0] < mas[1] < mas[2] < mas[3] else "mixed")
    span = None if None in (m["high_60"], m["low_60"]) else m["high_60"] - m["low_60"]
    recent = s.traded[max(0, s.pos[k] - 4):s.pos[k] + 1]
    tech = {
        "ma_order": order,
        "above": [MA_LABEL[n] for n in MA_DAYS if m[f"ma{n}"] is not None and close > m[f"ma{n}"]],
        "below": [MA_LABEL[n] for n in MA_DAYS if m[f"ma{n}"] is not None and close < m[f"ma{n}"]],
        "range_pos": r1((close - m["low_60"]) / span * 100) if span else None,
        "rsi14": r1(m["rsi14"]), "vol_ratio": m["vol_ratio"],
        "recent": [{"date": s.d[j], "pct": s.pct[j], "volume": s.v[j]} for j in recent],
    }
    fin, short = m["financing"], m["short"]
    chips = {"streak": m["streak"], "sum_5d": m["sum_5d"], "sum_20d": m["sum_20d"], "institutional": m["inst_today"],
             "margin": {"financing": fin, "financing_change": m["financing_change"], "short": short,
                        "short_change": m["short_change"],
                        "short_ratio": round(short / fin * 100, 2) if fin and short is not None else None}}
    chips.update(extra)
    tags = [t for t, on in (("overheat", (m["rsi14"] or 0) >= 70 or (m["dev_ma20"] or 0) >= 15),
                            ("oversold", m["rsi14"] is not None and m["rsi14"] <= 30)) if on]
    tech_hint = ("bull" if order == "bull" and m["ma20"] and close > m["ma20"]
                 else "bear" if order == "bear" and m["ma20"] and close < m["ma20"] else "neutral")
    t5, t20 = m["sum_5d"]["total"], m["sum_20d"]["total"]
    chips_hint = ("bull" if t5 is not None and t5 > 0 and t20 > 0
                  else "bear" if t5 is not None and t5 < 0 and t20 < 0 else "neutral")
    return {"code": s.code, "name": s.name[k], "close": close, "pct": m["pct"], "price_map": rows, "tech": tech,
            "chips": chips, "tags": tags, "tech_score_hint": tech_hint, "chips_score_hint": chips_hint}


def star_extra(store, code, day, prev_day, shares):
    """選用資料集（有才放）：當沖比、借券賣出餘額、本益比"""
    out = {}
    for ds, _ in both("day_trading"):
        rows = store.rows(ds, day, [code])
        if rows is not None and code in rows:
            n = num(rows[code][0].get("當日沖銷交易成交股數")) or 0
            out["day_trading_ratio"] = round(n / shares * 100, 2) if shares else None
        elif rows is not None and "day_trading_ratio" not in out:
            out["day_trading_ratio"] = 0
    bal = {}
    for when in (day, prev_day):
        for ds, _ in both("sbl_short"):
            rows = store.rows(ds, when, [code]) if when else None
            if rows and code in rows:
                bal[when] = lots(num(rows[code][0].get("借券賣出當日餘額")) or 0)
    if day in bal:
        out["sbl_short"] = {"balance": bal[day], "change": bal[day] - bal[prev_day] if prev_day in bal else None}
    for ds, _ in both("bwibbu_all"):
        rows = store.rows(ds, day, [code])
        if rows and code in rows:
            r = rows[code][0]
            out["valuation"] = {"pe": num(r.get("PEratio")), "dy": num(r.get("DividendYield")), "pb": num(r.get("PBratio"))}
    return out


STATUS = {"profit": "🟢 獲利", "small_gain": "🔵 小賺", "small_loss": "🟡 小虧", "deep_loss": "🔴 深虧"}


def lines(cost, m):
    """持股的狀態、出場線、停利／減碼線（7.2 節的規則）-> (狀態, 出場線, 依據, 停利線, 依據)"""
    close = m["close"]
    pnl = close / cost - 1

    def below(cands):   # 低於收盤的最高者；都在收盤以上就取最低的
        cands = [(v, b) for v, b in cands if v is not None]
        under = [x for x in cands if x[0] < close]
        return max(under) if under else min(cands) if cands else (None, None)

    if pnl >= 0.2:
        exit_line = below([(m["high_60"] * 0.85, "60 日高 −15%"), (cost * 1.1, "成本 +10%"), (m["low_60"], "60 日低")])
        return "profit", *exit_line, "已達 +20%", None
    if pnl > -0.1:
        cands = [(cost * 0.9, "成本 −10%"), (m["low_60"], "60 日低")]
        exit_line = max(x for x in cands if x[0] is not None)
        return ("small_gain" if pnl >= 0 else "small_loss"), *exit_line, cost * 1.2, "成本 +20%"
    exit_line = below([(m["ma20"], "月線"), (m["ma60"], "季線"), (m["low_60"], "60 日低")])
    above = [x for x in ((m["ma20"], "月線"), (m["ma60"], "季線"), (m["high_60"], "60 日高")) if x[0] is not None and x[0] > close]
    tp = min(above) if above else (None, None)
    return "deep_loss", *exit_line, *tp


def track(cost, snaps):
    """持股的出場線追蹤。snaps：依日期排序的 [(指標, scale)]，最後一個是 D；scale＝原始收盤 ÷ 還原收盤，
    用來把某一天算出的價位換算到另一天（中間除權息時價位跟著調整）。

    出場線取「低於收盤」的線，所以每天拿「前一天的出場線」比：今天收盤跌破 → ⏳ 待確認（pending）。
    跌破之後就記住那條線，之後每天都跟它比（不換成重算後比較低的線）：收盤還在線下 → ❌ 已觸發（triggered），
    站上去 → 站回（recovered），隔天起恢復用每天重算的出場線"""
    m = snaps[-1][0]
    status, exit_line, exit_basis, tp, tp_basis = lines(cost, m)
    states, broken, prev = [], None, None   # broken：(還原價空間的線, 依據, 跌破日)
    for day, scale in snaps:
        close, state, against = day["close"], "ok", None
        if broken:
            against = (broken[0] * scale, broken[1])
            if close < against[0]:
                state = "triggered"
            else:
                state, broken = "recovered", None
        elif prev and prev[0][1] is not None:
            against = (prev[0][1] / prev[1] * scale, prev[0][2])
            if close < against[0]:
                state, broken = "pending", (prev[0][1] / prev[1], prev[0][2], day["date"])
        states.append((state, against))
        prev = (lines(cost, day), scale)
    state, against = states[-1]
    recalc = exit_line
    if broken:   # 待確認、已觸發期間顯示被跌破的那條線
        exit_line, exit_basis = broken[0] * snaps[-1][1], broken[1]
    dist = None if not exit_line else (m["close"] / exit_line - 1) * 100
    prev_status = lines(cost, snaps[-2][0])[0] if len(snaps) > 1 else None
    return {"cost": cost, "close": m["close"], "pnl_pct": r2((m["close"] / cost - 1) * 100),
            "status": STATUS[status], "status_key": status,
            "exit_line": price(exit_line), "exit_basis": exit_basis,
            "tp_line": tp if isinstance(tp, str) else price(tp), "tp_basis": tp_basis,
            "dist_exit_pct": r1(dist), "warn": dist is not None and 0 <= round(dist, 1) <= 3,
            "state": state, "broken_since": broken[2] if broken else None,
            "compared_line": None if against is None else price(against[0]),   # 今天收盤拿來比的線
            "recalc_exit_line": price(recalc),   # 依今天收盤重算的出場線（站回後隔天起用這條）
            "broke_today": state == "pending",
            "broke_yesterday": len(states) > 1 and states[-2][0] in ("pending", "triggered"),
            "status_changed_to_green": status == "profit" and prev_status is not None and prev_status != "profit"}


def holding_out(s, k, ctx, cost, lookback=60):
    """從 D 往前 lookback 個交易日開始追蹤出場線（跌破後一直沒站回的線不會因為重算而消失）"""
    p = s.pos[k]
    snaps = [(snapshot(s, j, ctx), s.c[j] / s.adj[j]) for j in s.traded[max(0, p - lookback):p + 1]]
    return {"code": s.code, "name": s.name[k], **track(cost, snaps)}


# ---------------------------------------------------------------- 回測

def backtest(series, codes, cal, di, n_days, ctx, index):
    """8 節：回測範圍每一天、每一檔用當天以前的資料跑同一套訊號，記錄 1／5／10 個交易日後相對大盤的報酬"""
    start = max(0, di - n_days + 1)
    events = []
    for code in codes:
        s = series.get(code)
        if s is None:
            continue
        cache = {}

        def sig_at(k):
            if k not in cache:
                cache[k] = signals(snapshot(s, k, ctx), ctx) if k is not None and k in s.pos else {}
            return cache[k]

        for ti in range(start, di + 1):
            t = cal[ti]
            k = s.at.get(t)
            if k is None or k not in s.pos:
                continue
            now, before = sig_at(k), sig_at(s.prev_traded(k))
            due = {f"{h}d": due_date(ti, h, cal, di, ctx) for h in HORIZONS}
            for key, (level, text) in now.items():
                if key == "earnings":
                    continue
                fwd = {}
                for h in HORIZONS:
                    fwd[f"{h}d"] = forward(s, k, ti, h, cal, di, index)
                events.append({"date": t, "code": code, "key": key, "level": level, "direction": DIRECTION[key],
                               "text": text, "first": key not in before or before[key][0] < level,
                               "due": due, "fwd": fwd})
    return events, cal[start]


def due_date(ti, h, cal, di, ctx):
    """訊號日之後第 h 個交易日（結果出爐的日子）；超過資料範圍的用休市表往後推算"""
    if ti + h <= di:
        return cal[ti + h]
    day = cal[di]
    for _ in range(ti + h - di):
        day = ctx["next_day"](day)
    return day


def forward(s, k, ti, h, cal, di, index):
    """t+h 個交易日後的報酬（停牌日往後找下一個有交易的日子）；超出資料範圍為 None（pending）"""
    if ti + h > di:
        return None
    target = cal[ti + h]
    p = bisect.bisect_left(s.d, target)
    while p < len(s.d) and s.adj[p] is None:
        p += 1
    if p >= len(s.d):
        return None
    market = s.market[k]
    m0, m1 = index.get(s.d[k], {}).get(market), index.get(s.d[p], {}).get(market)
    if not m0 or not m1:
        return None
    r = (s.adj[p] / s.adj[k] - 1) * 100
    m = (m1 / m0 - 1) * 100
    return {"r": round(r, 4), "m": round(m, 4), "x": round(r - m, 4), "exit_date": s.d[p]}


def cell(items, direction, h):
    """一組事件在天期 h 的統計"""
    done = [e for e in items if e["fwd"][f"{h}d"] is not None]
    out = {"n": len(done), "pending": len(items) - len(done)}
    xs = [e["fwd"][f"{h}d"]["x"] for e in done]
    rs = [e["fwd"][f"{h}d"]["r"] for e in done]
    if direction in ("bull", "bear"):
        sign = 1 if direction == "bull" else -1
        out["hit"] = sum(1 for x in xs if x * sign > 0)
        out["hit_rate"] = round(out["hit"] / len(xs) * 100, 1) if xs else None
        out["hit_rate_abs"] = round(sum(1 for r in rs if r * sign > 0) / len(rs) * 100, 1) if rs else None
    out["mean_x"] = round(statistics.fmean(xs), 2) if xs else None
    out["median_x"] = round(statistics.median(xs), 2) if xs else None
    return out


def dedupe(events):
    """同一檔、同一天、同方向的多個訊號算一次；有任一個是新出現的就算新出現。fwd 只跟日期、代號有關，取第一個"""
    groups = {}
    for e in events:
        if e["direction"] not in ("bull", "bear"):
            continue
        g = groups.setdefault((e["date"], e["code"], e["direction"]), {**e, "first": False})
        g["first"] = g["first"] or e["first"]
    return list(groups.values())


def stats(events, periods):
    """periods：{名稱: (說明, 篩選事件的函式)}"""
    out = {}
    for name, (start, keep) in periods.items():
        evs = [e for e in events if keep(e)]
        both_ = lambda items, direction: {f"{h}d": {"all": cell(items, direction, h),  # noqa: E731
                                                    "first": cell([e for e in items if e["first"]], direction, h)}
                                          for h in HORIZONS}
        grouped = dedupe(evs)
        by_signal = {}
        for key, title, direction in COLUMNS:
            for level in (2, 1):
                items = [e for e in evs if e["key"] == key and e["level"] == level]
                if items:
                    by_signal[f"{key}:{level}"] = {"key": key, "title": title, "level": "●" if level == 2 else "○",
                                                   "direction": direction, **both_(items, direction)}
        out[name] = {"start": start, "keep": keep,
                     "by_direction": {d: both_([e for e in grouped if e["direction"] == d], d) for d in ("bull", "bear")},
                     "by_signal": by_signal}
    return out


def weekly(st, events, week_label, h=5):
    """週報用：本週到期的訊號（第 h 個交易日落在本週一到週五，大多是上週出現的訊號）"""
    w = st["due_week"]
    dates = sorted(e["date"] for e in events if w["keep"](e))
    out = {"title": f"上週訊號、{h} 日後驗證", "week": week_label, "h": h,
           "signal_from": dates[0] if dates else None, "signal_to": dates[-1] if dates else None}
    for d, word in (("bull", "偏多"), ("bear", "偏空")):
        c = w["by_direction"][d][f"{h}d"]["all"]
        out[d] = {"n": c["n"], "hit": c["hit"], "pending": c["pending"], "text": f"{word} {c['hit']}/{c['n']}"}
    ranked = [{"key": v["key"], "title": v["title"], "level": v["level"], **v[f"{h}d"]["all"]}
              for v in w["by_signal"].values() if v["direction"] in ("bull", "bear") and v[f"{h}d"]["all"]["n"]]
    ranked.sort(key=lambda x: (-x["hit_rate"], -x["n"], x["key"]))
    out["by_signal"] = ranked
    out["note"] = (f"統計第 {h} 個交易日落在本週（{week_label}）的訊號，以相對大盤的超額報酬判斷；"
                   "週間執行時，到期日在今天之後的還在 pending")
    return out


# ---------------------------------------------------------------- 主程式

class Parser(argparse.ArgumentParser):
    def error(self, message):   # 參數錯誤用結束代碼 1（2 保留給「資料不齊」）
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(1)


def codes_arg(v):
    return [c.strip() for c in (v or "").split(",") if c.strip()]


def main(argv=None):
    t0 = time.perf_counter()
    ap = Parser(description=__doc__.split("\n")[0])
    ap.add_argument("--date", default="auto")
    ap.add_argument("--watch", default="")
    ap.add_argument("--star", default="")
    ap.add_argument("--holdings")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--backtest-days", type=int, default=120)
    ap.add_argument("--universe", choices=("watch", "all"), default="watch")
    ap.add_argument("--out", default="out")
    ap.add_argument("--data", default=str(build_db.DATA_DIR), help=argparse.SUPPRESS)   # 測試用
    args = ap.parse_args(argv)

    store = Store(Path(args.data))
    cal = calendar(store)
    if not cal:
        raise SystemExit("data/taiex/ 沒有資料，無法決定交易日")
    D = cal[-1] if args.date == "auto" else args.date
    if D not in cal:
        cal_before = [x for x in cal if x < D]
        if not cal_before:
            raise SystemExit(f"{D} 之前沒有交易日資料")
        cal = cal_before + [D]   # D 還沒有大盤資料：照樣跑，ready 會標明
    else:
        cal = cal[:cal.index(D) + 1]
    di = len(cal) - 1
    days = cal[max(0, di - WINDOW + 1):]

    watch = codes_arg(args.watch)
    star = [c for c in codes_arg(args.star)]
    holdings = []
    if args.holdings:
        holdings = [{"code": str(h["code"]).strip(), "cost": float(h["cost"])}
                    for h in json.loads(Path(args.holdings).read_text(encoding="utf-8-sig"))]
    wanted = list(dict.fromkeys(watch + star + [h["code"] for h in holdings]))
    load_codes = None if args.universe == "all" and args.backtest else wanted

    # 資料就緒
    ready = {"date": D}
    missing = []
    for key, ds in REQUIRED.items():
        ok = all(store.has_rows(t, D) for t, _ in both(ds))
        ready[key] = ok
        missing += [t for t, _ in both(ds) if not store.has_rows(t, D)]
    ready["index"] = all(store.path(t, D).exists() for t in INDEX_TABLE.values())
    missing += [t for t in INDEX_TABLE.values() if not store.path(t, D).exists()]
    for key in ("notice", "disposition"):
        ready[key] = all(store.path(t, D).exists() for t, _ in both(key))
    ready["optional_missing"] = sorted(t for ds in ("notice", "disposition", "ex_rights", "ex_rights_forecast",
                                                    "holiday_schedule", "day_trading", "sbl_short", "bwibbu_all")
                                       for t in ([ds] if ds == "holiday_schedule" else [x for x, _ in both(ds)])
                                       if not store.has(t))
    ready["missing"] = missing
    ready["ok"] = not missing
    stale = False
    if args.date == "auto":
        now = dt.datetime.now(TZ)
        today = now.date().isoformat()
        off, _, _ = holidays(store)
        stale = now.weekday() < 5 and today not in off and now.hour >= 18 and D != today
    ready["stale"] = stale

    series = load_series(store, days, load_codes, D)
    ev_codes = None if load_codes is None else set(load_codes)
    notice, disp = load_events_by_day(store, days, load_codes)
    off, years, unknown = holidays(store)
    warnings = []

    def next_day(t):
        i = bisect.bisect_right(cal, t)
        if i < len(cal) and cal[i] <= D and i <= di:
            return cal[i]
        day = dt.date.fromisoformat(t)
        while True:
            day += dt.timedelta(days=1)
            iso = day.isoformat()
            if iso[:4] not in years:
                msg = f"休市表沒有 {iso[:4]} 年的資料，推算下一個交易日只跳過週末"
                if msg not in warnings:
                    warnings.append(msg)
            if day.weekday() < 5 and iso not in off:
                return iso

    def friday(t):
        day = dt.date.fromisoformat(t)
        return (day + dt.timedelta(days=4 - day.weekday())).isoformat()

    ctx = {"notice": notice, "disposition": disp, "ex_dates": ex_schedule(store, days[0], D, ev_codes),
           "next_day": next_day, "friday": friday}
    if unknown:
        warnings.append("休市表有判斷不出是否休市的列：" + "、".join(unknown))

    # 個股、矩陣、⭐、持股
    snaps, stocks, watch_missing = {}, {}, []
    for code in wanted:
        s = series.get(code)
        k = s.at.get(D) if s else None
        if k is None or k not in s.pos:
            watch_missing.append(code)
            continue
        snaps[code] = (s, k, snapshot(s, k, ctx))
    ready["watch_missing"] = watch_missing
    fc = forecast(store, set(wanted), D)
    for code, (s, k, m) in snaps.items():
        stocks[code] = {**stock_out(s, k, m), "ex_rights_upcoming": fc.get(code, [])}

    matrix = []
    for code in sorted(c for c in watch if c in snaps):
        s, k, m = snaps[code]
        sig = signals(m, ctx)
        if not sig:
            continue
        row = {"code": code, "name": s.name[k], "market": s.market[k], "cells": cells(sig)}
        for d in ("bull", "bear", "event"):
            row[d] = sum(1 for key in sig if DIRECTION[key] == d)
        matrix.append(row)
    head = "| 代號 名稱 | " + " | ".join(t for _, t, _ in COLUMNS) + " |"
    matrix_md = "\n".join([head, "|" + "---|" * (len(COLUMNS) + 1)]
                          + [f"| {r['code']} {r['name']} | " + " | ".join(r["cells"]) + " |" for r in matrix])

    prev_day = cal[di - 1] if di >= 1 else None
    star_rows = []
    for code in star:
        if code in snaps:
            s, k, m = snaps[code]
            star_rows.append(star_out(s, k, m, star_extra(store, code, D, prev_day, s.shares[k])))
    hold_rows = []
    for h in holdings:
        if h["code"] not in snaps:
            hold_rows.append({"code": h["code"], "cost": h["cost"], "close": None, "no_data": True})
            continue
        s, k, _ = snaps[h["code"]]
        hold_rows.append(holding_out(s, k, ctx, h["cost"]))

    idx_now, idx_prev = index_close(store, D), index_close(store, prev_day) if prev_day else {}
    market = {name: {"close": idx_now.get(mk),
                     "pct": r2((idx_now[mk] / idx_prev[mk] - 1) * 100) if idx_now.get(mk) and idx_prev.get(mk) else None}
              for name, mk in (("taiex", "上市"), ("tpex", "上櫃"))}

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_at = dt.datetime.now(TZ).isoformat(timespec="seconds")
    result = {"date": D, "generated_at": generated_at, "ready": ready, "calendar_warning": warnings or None,
              "market": market, "columns": [{"key": k, "title": t, "direction": d} for k, t, d in COLUMNS],
              "matrix": matrix, "matrix_md": matrix_md, "stocks": stocks, "star": star_rows, "holdings": hold_rows}
    write(out_dir, "signals", D, result)

    bt_text = ""
    if args.backtest:
        index = {day: index_close(store, day) for day in days}
        universe = sorted(series) if args.universe == "all" else watch
        events, start = backtest(series, universe, cal, di, args.backtest_days, ctx, index)
        monday = (dt.date.fromisoformat(D) - dt.timedelta(days=dt.date.fromisoformat(D).weekday())).isoformat()
        fri = friday(D)
        periods = {"week": (monday, lambda e: e["date"] >= monday),
                   "due_week": (f"第 5 天在 {monday}～{fri}", lambda e: monday <= e["due"]["5d"] <= fri),
                   "4w": (cal[max(0, di - 19)], lambda e: e["date"] >= cal[max(0, di - 19)]),
                   "all": (start, lambda e: True)}
        st = stats(events, periods)
        week_label = f"{md(monday)}–{md(fri)}"
        wk = weekly(st, events, week_label)
        for v in st.values():
            del v["keep"]
        bt = {"date": D, "generated_at": generated_at, "range": {"start": start, "end": D, "days": args.backtest_days},
              "universe": args.universe, "horizons": list(HORIZONS),
              "hit_rule": "偏多：相對大盤超額報酬 > 0 算命中；偏空：< 0 算命中；hit_rate_abs 用絕對報酬",
              "stats": st, "weekly": wk}
        if args.universe != "all":
            bt["events"] = events
        write(out_dir, "backtest", D, bt)
        bt_text = f" backtest={args.backtest_days}d events={len(events)}"

    warn = sum(1 for h in hold_rows if h.get("warn"))
    pend = sum(1 for h in hold_rows if h.get("state") == "pending")
    trig = sum(1 for h in hold_rows if h.get("state") == "triggered")
    status = "ok" if ready["ok"] else "missing:" + ",".join(missing)
    if stale:
        status += ",stale"
    print(f"{D} ready={status} matrix={len(matrix)} rows star={len(star_rows)} "
          f"holdings={len(hold_rows)} ({warn} warn, {pend} pending, {trig} triggered){bt_text} {time.perf_counter() - t0:.1f}s")
    return 0 if ready["ok"] and not stale else 2


def write(out_dir, prefix, day, data):
    text = json.dumps(data, ensure_ascii=False, indent=1)
    for name in (f"{prefix}_{day}.json", f"{prefix}_latest.json"):
        (out_dir / name).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:   # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
