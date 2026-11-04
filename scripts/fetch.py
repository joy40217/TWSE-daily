"""每天抓取證交所資料，存成 data/<資料集>/<年>/<日期>.csv

資料來源：
  證交所網站歷史查詢（rwd）：可指定日期，收盤後當天就查得到
    → 個股行情、本益比、融資融券、三大法人（個股與金額）、加權指數、類股指數、除權息、
      注意股、處置股、除權息預告、當日沖銷、借券賣出
  OpenAPI：只有最新一份，而且每日行情要到隔天才更新，只用在沒有日期問題的資料
    → 月營收、休市日程
  櫃買中心網站（www.tpex.org.tw/www/zh-tw）：上櫃的行情、本益比、融資融券、三大法人、注意股、處置股、
      當日沖銷、借券賣出、除權息、櫃買指數；OpenAPI：上櫃月營收、除權息預告
      → 資料集名稱加 tpex_ 前綴，欄位轉成對應的上市資料集格式
  公開資訊觀測站（mopsov.twse.com.tw）：上市、上櫃季報綜合損益表；歷史月營收（回補用）
  集保結算所開放資料：集保戶股權分散表（每週一份，只能取得最新一週，從開始抓的那週起累積）

網站資料一律轉成跟 OpenAPI 相同的欄位格式，新舊檔案可以混用。
每次執行會檢查最近 LOOKBACK 個交易日，缺的就補；
例如融資融券晚上才公布，傍晚執行時查不到，隔天執行會自動補上。
backfill.py 也是用這裡的函式回補歷史資料。
"""
import csv
import datetime as dt
import re
import sys
import time
from collections import defaultdict
from functools import partial
from pathlib import Path

import requests
import urllib3

OPENAPI = "https://openapi.twse.com.tw/v1"
RWD = "https://www.twse.com.tw/rwd/zh"
TPEX = "https://www.tpex.org.tw/www/zh-tw"
TPEX_OPENAPI = "https://www.tpex.org.tw/openapi/v1"
MOPS = "https://mopsov.twse.com.tw"
TDCC = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TW_TZ = dt.timezone(dt.timedelta(hours=8))
TODAY = dt.datetime.now(TW_TZ).date()
PAUSE = 3      # 每次查詢網站的間隔秒數，查詢太密集會被暫時封鎖
LOOKBACK = 5   # 每次檢查最近幾個交易日有沒有缺資料
REFRESH = 2    # 標記 refresh 的資料集，最近幾個交易日每次都重新查、有資料就覆寫

DAY_KEYS = ["Date", "日期", "資料日期", "出表日期"]
MONTH_KEYS = ["資料年月"]


def parse_date(s):
    """支援 1150731、115/07/31、115年07月31日、20260731、2026-07-31"""
    s = str(s).strip()
    m = re.match(r"^(\d{2,4})\D+(\d{1,2})\D+(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        digits = re.sub(r"\D", "", s)
        if len(digits) == 8:
            y, mo, d = int(digits[:4]), int(digits[4:6]), int(digits[6:])
        elif len(digits) in (6, 7):
            y, mo, d = int(digits[:-4]), int(digits[-4:-2]), int(digits[-2:])
        else:
            return None
    if y < 1911:
        y += 1911
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def parse_month(s):
    """支援 11507、115/07、115年7月、202607 -> '2026-07'"""
    s = str(s).strip()
    m = re.match(r"^(\d{2,4})\D+(\d{1,2})", s)
    if m:
        y, mo = map(int, m.groups())
    else:
        digits = re.sub(r"\D", "", s)
        if len(digits) == 6 and digits.startswith(("19", "20")):
            y, mo = int(digits[:4]), int(digits[4:])
        elif len(digits) in (4, 5):
            y, mo = int(digits[:-2]), int(digits[-2:])
        else:
            return None
    if y < 1911:
        y += 1911
    return f"{y}-{mo:02d}" if 1 <= mo <= 12 else None


RETRY_WAITS = (15, 45)   # 失敗後等幾秒再試；證交所偶爾會回空白或錯誤頁，通常稍等就恢復


def get_json(url: str, retries: int = len(RETRY_WAITS) + 1):
    headers = {"User-Agent": "Mozilla/5.0 (twse-daily-archiver)", "Accept": "application/json"}
    verify = True
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=60, verify=verify)
            r.raise_for_status()
            r.encoding = "utf-8"
            try:
                return r.json()
            except ValueError:
                # 不是 JSON：記下狀態碼與內容開頭，方便判斷是被封鎖、維護中還是空白回應
                title = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
                text = title.group(1) if title else re.sub(r"<script.*?</script>|<[^>]+>", " ", r.text, flags=re.S | re.I)
                body = " ".join(text.split())[:80] or "（空白）"
                raise ValueError(f"回應不是 JSON（HTTP {r.status_code}，{r.headers.get('Content-Type', '?')}）：{body}")
        except requests.exceptions.SSLError:
            print("  SSL 驗證失敗，改為略過憑證驗證重試")
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            verify = False
        except Exception as e:  # noqa: BLE001
            print(f"  第 {attempt} 次失敗：{e}")
            if attempt < retries:
                time.sleep(RETRY_WAITS[min(attempt, len(RETRY_WAITS)) - 1])
    raise RuntimeError(f"無法取得 {url}")


def get_text(url: str, encoding: str = "utf-8", data=None, retries: int = len(RETRY_WAITS) + 1):
    """取得網頁或 CSV 文字（data 有值時用 POST）；找不到（404）回傳 None"""
    headers = {"User-Agent": "Mozilla/5.0 (twse-daily-archiver)"}
    for attempt in range(1, retries + 1):
        try:
            r = (requests.post(url, data=data, headers=headers, timeout=60) if data
                 else requests.get(url, headers=headers, timeout=120))
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content.decode(encoding, errors="replace")
        except Exception as e:  # noqa: BLE001
            print(f"  第 {attempt} 次失敗：{e}")
            if attempt < retries:
                time.sleep(RETRY_WAITS[min(attempt, len(RETRY_WAITS)) - 1])
    raise RuntimeError(f"無法取得 {url}")


def rwd_rows(js):
    """證交所網站 rwd 格式 {stat, fields, data} 轉成 list[dict]"""
    if js.get("stat") != "OK":
        print(f"  網站回應：{js.get('stat')}")
        return []
    fields, data = js.get("fields"), js.get("data")
    if fields is None and js.get("tables"):
        fields, data = js["tables"][0].get("fields"), js["tables"][0].get("data")
    names, seen = [], defaultdict(int)
    for f in fields or []:
        seen[f] += 1
        names.append(f if seen[f] == 1 else f"{f}_{seen[f]}")
    return [dict(zip(names, row)) for row in data or []]


def write_csv(out: Path, rows, overwrite=False, fields=None) -> bool:
    """fields 可指定欄位（沒有資料時也寫出標題列）"""
    if out.exists() and not overwrite:
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or list(dict.fromkeys(k for r in rows for k in r))
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return True


def group_rows(rows, keys, parser, fallback):
    """依資料內的日期欄位分組；沒有日期欄位就整批歸到 fallback"""
    key = next((k for k in keys if k in rows[0]), None)
    if key is None:
        if fallback is None:
            raise RuntimeError("資料沒有日期欄位，且無法判斷交易日")
        print(f"  無日期欄位，歸入交易日 {fallback}")
        return {fallback: rows}
    groups, bad = defaultdict(list), 0
    for r in rows:
        v = parser(r.get(key, ""))
        if v is None:
            bad += 1
            continue
        groups[v.isoformat() if isinstance(v, dt.date) else v].append(r)
    if bad:
        print(f"  {bad} 筆日期無法解析，已略過")
    return groups


def out_path(name: str, key: str) -> Path:
    return DATA_DIR / name / key[:4] / f"{key}.csv"


def save(name: str, key: str, rows, overwrite=False) -> None:
    out = out_path(name, key)
    if write_csv(out, rows, overwrite, DATASETS[name].get("fields")):
        print(f"  已寫入 {out.relative_to(DATA_DIR)}（{len(rows)} 筆）")
    else:
        print(f"  {out.relative_to(DATA_DIR)} 已存在，略過")


# ---- 證交所網站（rwd）依日期查詢，轉成 OpenAPI 格式 ----

def num(s) -> str:
    """'2,425.00' -> '2425.00'；'--'、'-' 等無資料 -> ''"""
    s = str(s).strip()
    return "" if re.fullmatch(r"-*", s) else s.replace(",", "")


def count(s) -> str:
    """數量為 0 時 OpenAPI 給空白，這裡比照辦理"""
    n = num(s)
    return "" if n == "0" else n


def ratio(s) -> str:
    """比率為 0（如 '0.00'）時 OpenAPI 給空白，這裡比照辦理；'N/A' 之類的文字也給空白"""
    n = num(s)
    try:
        return n if n and float(n) != 0 else ""
    except ValueError:
        return ""


def roc(day: dt.date) -> str:
    return f"{day.year - 1911}{day:%m%d}"


def query(path: str, **params):
    """查詢 rwd 端點；沒有資料（假日、尚未公布）時回傳 None"""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    js = get_json(f"{RWD}/{path}?{qs}&response=json")
    time.sleep(PAUSE)
    return js if js.get("stat") == "OK" else None


def table(js, first_field: str):
    """從回應中找出第一個欄位是 first_field 的表，回傳 (欄位, 資料)"""
    for t in [js] + list(js.get("tables") or []):
        if (t.get("fields") or [None])[0] == first_field:
            return t["fields"], t.get("data") or []
    raise RuntimeError(f"找不到第一欄為「{first_field}」的資料表，格式可能改了")


def stock_day_all(day: dt.date):
    """MI_INDEX 每日收盤行情 -> OpenAPI STOCK_DAY_ALL 格式"""
    js = query("afterTrading/MI_INDEX", date=f"{day:%Y%m%d}", type="ALLBUT0999")
    if js is None:
        return []
    fields, data = table(js, "證券代號")
    rows = []
    for r in data:
        f = dict(zip(fields, r))
        sign = "-" if "-" in f["漲跌(+/-)"] else ""
        rows.append({
            "Date": roc(day),
            "Code": f["證券代號"].strip(),
            "Name": f["證券名稱"].strip(),
            "TradeVolume": count(f["成交股數"]),
            "TradeValue": count(f["成交金額"]),
            "OpeningPrice": num(f["開盤價"]),
            "HighestPrice": num(f["最高價"]),
            "LowestPrice": num(f["最低價"]),
            "ClosingPrice": num(f["收盤價"]),
            "Change": f"{sign}{float(num(f['漲跌價差']) or 0):.4f}",
            "Transaction": count(f["成交筆數"]),
        })
    return rows


def bwibbu_all(day: dt.date):
    """BWIBBU_d 本益比、殖利率及股價淨值比 -> OpenAPI BWIBBU_ALL 格式（舊年份欄位較少，依名稱對應）"""
    js = query("afterTrading/BWIBBU_d", date=f"{day:%Y%m%d}", selectType="ALL")
    if js is None:
        return []
    fields, data = table(js, "證券代號")
    rows = []
    for r in data:
        f = dict(zip(fields, r))
        rows.append({
            "Date": roc(day),
            "Code": f["證券代號"].strip(),
            "Name": f["證券名稱"].strip(),
            "PEratio": ratio(f["本益比"]),
            "DividendYield": ratio(f["殖利率(%)"]),
            "PBratio": ratio(f["股價淨值比"]),
        })
    return rows


MARGIN_COLS = ["股票代號", "股票名稱",
               "融資買進", "融資賣出", "融資現金償還", "融資前日餘額", "融資今日餘額", "融資限額",
               "融券買進", "融券賣出", "融券現券償還", "融券前日餘額", "融券今日餘額", "融券限額",
               "資券互抵", "註記"]


def margin(day: dt.date):
    """MI_MARGN 融資融券彙總 -> OpenAPI MI_MARGN 格式（融資、融券欄位同名，只能依位置對應）"""
    js = query("marginTrading/MI_MARGN", date=f"{day:%Y%m%d}", selectType="ALL")
    if js is None:
        return []
    fields, data = table(js, "代號")
    if len(fields) != len(MARGIN_COLS):
        raise RuntimeError(f"融資融券欄位數變了（{len(fields)} 欄）：{fields}")
    rows = []
    for r in data:
        row = dict(zip(MARGIN_COLS, r))
        for k in MARGIN_COLS[2:-1]:
            row[k] = count(row[k])
        row["股票代號"], row["股票名稱"] = row["股票代號"].strip(), row["股票名稱"].strip()
        rows.append(row)
    return rows


def institutional(day: dt.date):
    """T86 三大法人買賣超（不含權證）；OpenAPI 沒有對應資料，保留網站欄位名稱、數字去掉千分位"""
    js = query("fund/T86", date=f"{day:%Y%m%d}", selectType="ALLBUT0999")
    if js is None:
        return []
    fields, data = table(js, "證券代號")
    return [{f: (v.strip() if i < 2 else num(v)) for i, (f, v) in enumerate(zip(fields, r))}
            for r in data]


def taiex(day: dt.date):
    """MI_5MINS_HIST 加權指數（day 所在月份）-> {'2026-10-01': [row], ...}，OpenAPI 格式"""
    js = query("TAIEX/MI_5MINS_HIST", date=f"{day:%Y%m%d}")
    if js is None:
        return {}
    fields, data = table(js, "日期")
    out = {}
    for r in data:
        f = dict(zip(fields, r))
        d = parse_date(f["日期"])
        out[d.isoformat()] = [{
            "Date": roc(d),
            "OpeningIndex": num(f["開盤指數"]),
            "HighestIndex": num(f["最高指數"]),
            "LowestIndex": num(f["最低指數"]),
            "ClosingIndex": num(f["收盤指數"]),
        }]
    return out


def institutional_amount(day: dt.date):
    """BFI82U 三大法人買賣金額統計表（實際金額，單位：元）；舊年份外資沒有分外資自營商"""
    js = query("fund/BFI82U", type="day", dayDate=f"{day:%Y%m%d}")
    if js is None:
        return []
    fields, data = table(js, "單位名稱")
    return [{f: (v.strip() if i == 0 else num(v)) for i, (f, v) in enumerate(zip(fields, r))} for r in data]


def sector_index(day: dt.date):
    """MI_INDEX type=IND 的「價格指數(臺灣證券交易所)」：加權指數、各類股指數的收盤與漲跌"""
    js = query("afterTrading/MI_INDEX", date=f"{day:%Y%m%d}", type="IND")
    if js is None:
        return []
    fields, data = table(js, "指數")
    rows = []
    for r in data:
        f = dict(zip(fields, r))
        sign = "-" if "-" in f["漲跌(+/-)"] else ""
        change = num(f["漲跌點數"])
        rows.append({
            "Date": roc(day),
            "指數": f["指數"].strip(),
            "收盤指數": num(f["收盤指數"]),
            "漲跌點數": f"{sign}{change}" if change else "",
            "漲跌百分比": num(f["漲跌百分比(%)"]),
        })
    return rows


# 注意股、處置股：沒有資料的日子是正常的（照樣寫出只有標題的檔案），所以用固定欄位
NOTICE_COLS = ["證券代號", "證券名稱", "累計次數", "注意交易資訊", "日期", "收盤價", "本益比"]
DISPOSITION_COLS = ["證券代號", "證券名稱", "公布日期", "累計", "處置條件", "處置起迄時間", "處置措施"]


def announcement(path, cols, **params):
    js = query(path, **params)
    if js is None:
        return None
    # 去掉 HTML 標記（例如原因裡標紅字的 <font>）與多餘空白
    return [{c: " ".join(re.sub(r"<[^>]+>", "", str(r.get(c, ""))).split()) for c in cols} for r in rwd_rows(js)]


def notice(day: dt.date):
    """公布注意有價證券資訊：當天被列為注意股的（含原因）"""
    return announcement("announcement/notice", NOTICE_COLS, querytype=1,
                        startDate=f"{day:%Y%m%d}", endDate=f"{day:%Y%m%d}")


def disposition(day: dt.date):
    """公布處置有價證券資訊：查某一天會列出「當天在處置期間內」的股票"""
    return announcement("announcement/punish", DISPOSITION_COLS,
                        startDate=f"{day:%Y%m%d}", endDate=f"{day:%Y%m%d}")


def day_trading(day: dt.date):
    """TWTB4U 當日沖銷交易標的：個股當沖成交股數與買進、賣出金額（2017 年以前可以當沖的股票較少）"""
    js = query("dayTrading/TWTB4U", date=f"{day:%Y%m%d}", selectType="All")
    if js is None:
        return []
    fields, data = table(js, "證券代號")
    return [{f: (v.strip() if i < 3 else num(v)) for i, (f, v) in enumerate(zip(fields, r))} for r in data]


# 信用額度總量管制餘額表：前半是融券（跟 MI_MARGN 相同）、後半是借券賣出，欄位同名，只能依位置對應；單位：股
SBL_COLS = ["代號", "名稱",
            "融券前日餘額", "融券賣出", "融券買進", "融券現券", "融券今日餘額", "融券次一營業日限額",
            "借券賣出前日餘額", "借券賣出當日賣出", "借券賣出當日還券", "借券賣出當日調整", "借券賣出當日餘額",
            "借券賣出次一營業日可限額", "備註"]


def sbl_short(day: dt.date):
    """TWT93U 信用額度總量管制餘額表：個股借券賣出餘額（晚上才公布）"""
    js = query("marginTrading/TWT93U", date=f"{day:%Y%m%d}")
    if js is None:
        return []
    fields, data = table(js, "代號")
    if len(fields) != len(SBL_COLS):
        raise RuntimeError(f"借券賣出欄位數變了（{len(fields)} 欄）：{fields}")
    return [{c: (v.strip() if c in ("代號", "名稱", "備註") else num(v)) for c, v in zip(SBL_COLS, r)} for r in data]


SHAREHOLDING_COLS = ["證券代號", "持股分級", "人數", "股數", "占集保庫存數比例%"]


def shareholding():
    """集保戶股權分散表（最新一週）-> (資料日期, rows)。
    原始檔含興櫃與權證約 4,000 檔，只留最新行情裡有的上市、上櫃股票與 ETF（約 2,200 檔）"""
    rows = list(csv.DictReader(get_text(TDCC, "utf-8-sig").splitlines()))
    if not rows:
        raise RuntimeError("集保戶股權分散表是空的")
    day = parse_date(rows[0]["資料日期"])
    listed = set()
    for name in ("stock_day_all", "tpex_stock_day_all"):
        files = list((DATA_DIR / name).glob("*/*.csv"))
        if files:
            with max(files).open(encoding="utf-8-sig", newline="") as f:
                listed |= {r["Code"] for r in csv.DictReader(f)}
    keep = []
    for r in rows:
        r["證券代號"] = r["證券代號"].strip()
        if r["證券代號"] in listed:
            keep.append({c: r[c] for c in SHAREHOLDING_COLS})
    return day.isoformat(), keep


def html_cells(row: str):
    """一列 HTML 表格 -> 各格文字"""
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
    return [" ".join(re.sub(r"<[^>]+>|&nbsp;", " ", c).split()) for c in cells]


REVENUE_COLS = ["出表日期", "資料年月", "公司代號", "公司名稱", "產業別",
                "營業收入-當月營收", "營業收入-上月營收", "營業收入-去年當月營收",
                "營業收入-上月比較增減(%)", "營業收入-去年同月增減(%)",
                "累計營業收入-當月累計營收", "累計營業收入-去年累計營收", "累計營業收入-前期比較增減(%)", "備註"]


def monthly_revenue_history(month: str, market: str = "sii"):
    """公開資訊觀測站的月營收彙總表（回補用）-> OpenAPI t187ap05_L 格式；還沒公布回傳 []。
    month 如 '2016-01'；market 是 sii（上市）或 otc（上櫃）；
    國內公司（_0）、國外公司（_1，KY 股）分兩頁，每頁依產業分成好幾張表"""
    y, m = map(int, month.split("-"))
    rows = []
    for kind in (0, 1):
        html = get_text(f"{MOPS}/nas/t21/{market}/t21sc03_{y - 1911}_{m}_{kind}.html", "big5")
        time.sleep(PAUSE)
        for block in re.split(r"產業別：", html or "")[1:]:
            industry = block.split("<", 1)[0].strip()
            for tr in re.findall(r"<tr align=right>(.*?)</tr>", block, re.S | re.I):
                cells = html_cells(tr)
                if len(cells) != 11 or not cells[0].isalnum():
                    continue   # 合計列
                rows.append(dict(zip(REVENUE_COLS, ["", f"{y - 1911}{m:02d}", cells[0], cells[1], industry,
                                                    *map(num, cells[2:10]), cells[10]])))
    return rows


def income_statement(year: int, season: int, market: str = "sii"):
    """公開資訊觀測站綜合損益表彙總。year 為西元年；market 是 sii（上市）或 otc（上櫃）。
    金額單位千元，都是當年累計（第 2 季是上半年合計）；各業別欄位不同，合併成同一張表。還沒有資料回傳 []"""
    html = get_text(f"{MOPS}/mops/web/ajax_t163sb04", data={
        "encodeURIComponent": 1, "step": 1, "firstin": 1, "off": 1, "isQuery": "Y",
        "TYPEK": market, "year": year - 1911, "season": f"{season:02d}"})
    time.sleep(PAUSE)
    rows = []
    for tb in re.findall(r"<table class='hasBorder'.*?</table>", html or "", re.S):
        head = re.search(r"<tr class='tblHead'>(.*?)</tr>", tb, re.S)
        heads = html_cells(head.group(1)) if head else []
        for tr in re.findall(r"<tr class='(?:even|odd)'>(.*?)</tr>", tb, re.S):
            cells = html_cells(tr)
            if len(cells) == len(heads) > 2:
                rows.append({"公司代號": cells[0], "公司名稱": cells[1],
                             **{h: num(v) for h, v in zip(heads[2:], cells[2:])}})
    return rows


def recent_quarters(n=2):
    """最近 n 個已結束的季度 [(年, 季), ...]：季報在季度結束後一個半月內陸續公布，每次都重查"""
    y, q = TODAY.year, (TODAY.month - 1) // 3   # 本季的前一季
    out = []
    for _ in range(n):
        if q == 0:
            y, q = y - 1, 4
        out.append((y, q))
        q -= 1
    return out


def ex_rights_forecast():
    """TWT48U 除權除息預告表（目前公告、尚未除權息的）"""
    js = query("exRight/TWT48U")
    if js is None:
        return []
    rows = []
    for r in rwd_rows(js):
        d = parse_date(r["除權除息日期"])
        rows.append({
            "除權除息日期": d.isoformat() if d else r["除權除息日期"],
            "股票代號": r["股票代號"].strip(),
            "名稱": r["名稱"].strip(),
            "除權息": r["除權息"],
            "無償配股率": num(r["無償配股率"]),
            "現金增資配股率": num(r["現金增資配股率"]),
            "現金增資認購價": num(r["現金增資認購價"]),
            "現金股利": re.sub(r"<[^>]+>", "", num(r["現金股利"])).strip(),
        })
    return rows


def ex_rights(start: dt.date, end: dt.date):
    """TWT49U 除權除息計算結果 -> {'2026-10-16': [...], ...}"""
    js = query("exRight/TWT49U", startDate=f"{start:%Y%m%d}", endDate=f"{end:%Y%m%d}")
    rows = rwd_rows(js) if js else []
    return group_rows(rows, DAY_KEYS, parse_date, None) if rows else {}


EX_DETAIL_COLS = ["股票代號", "股票名稱", "每股現金股利", "每千股無償配股", "每千股認購", "每股認購金額"]


def ex_rights_detail(start: dt.date, end: dt.date):
    """除權（配股）的明細（TWT49UDetail）：每股現金股利、每千股配股、每千股現金增資認購 -> {'2026-09-02': [...], ...}。
    計算結果表只有參考價，算不出配股後股數變成幾倍（本益比、殖利率要用）；只查已存的 ex_rights 裡
    權/息有「權」的股票，已經查過的日子略過"""
    out = {}
    for f in sorted((DATA_DIR / "ex_rights").glob("*/*.csv")):
        if not start.isoformat() <= f.stem <= end.isoformat() or out_path("ex_rights_detail", f.stem).exists():
            continue
        with f.open(encoding="utf-8-sig", newline="") as fh:
            codes = [r["股票代號"].strip() for r in csv.DictReader(fh) if "權" in r["權/息"]]
        rows = []
        for code in codes:
            js = get_json(f"{RWD}/exRight/TWT49UDetail?STK_NO={code}&T1={f.stem.replace('-', '')}&response=json")
            time.sleep(PAUSE)
            if str(js.get("stat")).lower() != "ok":   # 例如「系統忙碌中」，下次再查
                raise RuntimeError(f"{code} {f.stem} 除權明細：{js.get('stat')}")
            if not js.get("data"):   # 查得到但沒有明細：留空白，不要每次重查
                rows.append({c: (code if c == "股票代號" else "") for c in EX_DETAIL_COLS})
                continue
            r = dict(zip(js["fields"], js["data"][0]))

            def amount(*keys):
                """欄位名稱包含 keys 其中之一的第一欄（普通股是「A. 按普通股股東持股比例每千股無償配股」，
                特別股是「F. 按特別股股東…」），'1,982.8 股' -> '1982.8'"""
                k = next((f for f in r if any(x in f for x in keys)), None)
                return num(re.sub(r"[^\d.,-]", "", r[k])) if k else ""

            rows.append({"股票代號": code, "股票名稱": r["股票名稱"].strip(),
                         "每股現金股利": amount("每股配發現金股利"), "每千股無償配股": amount("每千股無償配股"),
                         "每千股認購": amount("每千股認購", "每千股有償認股"), "每股認購金額": amount("每股認購金額")})
        if rows:
            out[f.stem] = rows
    return out


# ---- 櫃買中心網站依日期查詢（上櫃），欄位一律轉成上面對應的上市資料集格式，個股頁可以用同一套程式 ----

TPEX_CODE = re.compile(r"\d{4}[A-Z]?|00\d{3,4}[A-Z]?")   # 上櫃股票、特別股與 ETF；行情表裡其他的是權證（約 1 萬檔）


def tpex_query(path: str, **params):
    """查詢櫃買中心網站（www.tpex.org.tw/www/zh-tw/...）；假日、尚未公布時表格是空的，由各函式判斷。
    從 GitHub 連過去常被中途斷線（回應不完整、連線被重設），尤其約 2 MB 的行情表，所以多試幾次"""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    js = get_json(f"{TPEX}/{path}?{qs}&response=json", retries=5)
    time.sleep(PAUSE)
    return js if str(js.get("stat", "")).lower() == "ok" else None


def tpex_rows(js, first_field: str):
    """所有第一欄是 first_field 的表（行情表另有一張「管理股票」）合併成 list[dict]，欄位同名的依序加 _2、_3"""
    rows = []
    for t in (js or {}).get("tables") or []:
        fields = t.get("fields") or []
        if fields[:1] != [first_field]:
            continue
        names, seen = [], defaultdict(int)
        for f in fields:
            seen[f] += 1
            names.append(f if seen[f] == 1 else f"{f}_{seen[f]}")
        rows += [dict(zip(names, r)) for r in t.get("data") or []]
    return rows


def slash(day: dt.date) -> str:
    return f"{day:%Y/%m/%d}"


def change(s) -> str:
    """'+0.14'、'-3.10 ' -> 跟 stock_day_all 一樣的 '0.1400'、'-3.1000'；除權息、無比價等文字給空白"""
    m = re.match(r"^([+-]?)(\d+(?:\.\d+)?)$", str(s).strip().replace(",", ""))
    return f"{'-' if m.group(1) == '-' else ''}{float(m.group(2)):.4f}" if m else ""


def tpex_stock_day_all(day: dt.date):
    """上櫃股票行情（dailyQuotes）-> stock_day_all 格式，去掉權證"""
    rows = []
    for f in tpex_rows(tpex_query("afterTrading/dailyQuotes", date=slash(day)), "代號"):
        code = f["代號"].strip()
        if not TPEX_CODE.fullmatch(code):
            continue
        rows.append({
            "Date": roc(day), "Code": code, "Name": f["名稱"].strip(),
            "TradeVolume": count(f["成交股數"]), "TradeValue": count(f["成交金額(元)"]),
            "OpeningPrice": num(f["開盤"]), "HighestPrice": num(f["最高"]), "LowestPrice": num(f["最低"]),
            "ClosingPrice": num(f["收盤"]), "Change": change(f["漲跌"]), "Transaction": count(f["成交筆數"]),
        })
    return rows


def tpex_bwibbu_all(day: dt.date):
    """上櫃個股本益比、殖利率、股價淨值比（peQryDate）-> bwibbu_all 格式"""
    return [{"Date": roc(day), "Code": f["股票代號"].strip(), "Name": f["公司名稱"].strip(),
             "PEratio": ratio(f["本益比"]), "DividendYield": ratio(f["殖利率(%)"]), "PBratio": ratio(f["股價淨值比"])}
            for f in tpex_rows(tpex_query("afterTrading/peQryDate", date=slash(day)), "股票代號")]


# 上櫃融資融券餘額表的欄位 -> margin 的欄位（單位都是張）
TPEX_MARGIN = {"代號": "股票代號", "名稱": "股票名稱", "資買": "融資買進", "資賣": "融資賣出", "現償": "融資現金償還",
               "前資餘額(張)": "融資前日餘額", "資餘額": "融資今日餘額", "資限額": "融資限額",
               "券買": "融券買進", "券賣": "融券賣出", "券償": "融券現券償還",
               "前券餘額(張)": "融券前日餘額", "券餘額": "融券今日餘額", "券限額": "融券限額",
               "資券相抵(張)": "資券互抵", "備註": "註記"}


def tpex_margin(day: dt.date):
    """上櫃融資融券餘額（margin/balance）-> margin 格式"""
    rows = []
    for f in tpex_rows(tpex_query("margin/balance", date=slash(day)), "代號"):
        row = {c: f.get(k, "") for k, c in TPEX_MARGIN.items()}
        for c in MARGIN_COLS[2:-1]:
            row[c] = count(row[c])
        row["股票代號"], row["股票名稱"] = row["股票代號"].strip(), row["股票名稱"].strip()
        rows.append(row)
    return rows


# 上櫃三大法人買賣明細依欄位數對應到 institutional 的欄位：外資自營商分開列以前 16 欄（跟上市舊格式相同、順序也相同），
# 之後 24 欄（多了外資自營商、外資合計、自營商合計）；None 是外資合計等上市沒有的欄位，不存
TPEX_INST = {
    16: ["證券代號", "證券名稱", "外資買進股數", "外資賣出股數", "外資買賣超股數",
         "投信買進股數", "投信賣出股數", "投信買賣超股數", "自營商買賣超股數",
         "自營商買進股數(自行買賣)", "自營商賣出股數(自行買賣)", "自營商買賣超股數(自行買賣)",
         "自營商買進股數(避險)", "自營商賣出股數(避險)", "自營商買賣超股數(避險)", "三大法人買賣超股數"],
    24: ["證券代號", "證券名稱",
         "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)", "外陸資買賣超股數(不含外資自營商)",
         "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數", None, None, None,
         "投信買進股數", "投信賣出股數", "投信買賣超股數",
         "自營商買進股數(自行買賣)", "自營商賣出股數(自行買賣)", "自營商買賣超股數(自行買賣)",
         "自營商買進股數(避險)", "自營商賣出股數(避險)", "自營商買賣超股數(避險)",
         None, None, "自營商買賣超股數", "三大法人買賣超股數"],
}
# 存檔的欄位順序跟上市 T86 相同（自營商合計在自行買賣前面）
INST_ORDER = TPEX_INST[24][:8] + TPEX_INST[24][11:14] + ["自營商買賣超股數"] + TPEX_INST[24][14:20] + ["三大法人買賣超股數"]


def tpex_institutional(day: dt.date):
    """上櫃三大法人買賣明細（insti/dailyTrade，不含權證）-> institutional 格式（同名欄位只能依位置對應）"""
    js = tpex_query("insti/dailyTrade", type="Daily", sect="EW", date=slash(day))
    for t in (js or {}).get("tables") or []:
        fields = t.get("fields") or []
        if fields[:1] != ["代號"]:
            continue
        cols = TPEX_INST.get(len(fields))
        if cols is None:
            raise RuntimeError(f"上櫃三大法人欄位數變了（{len(fields)} 欄）：{fields}")
        order = INST_ORDER if len(fields) == 24 else cols
        rows = []
        for r in t.get("data") or []:
            row = {c: (v.strip() if i < 2 else num(v)) for i, (c, v) in enumerate(zip(cols, r)) if c}
            rows.append({c: row[c] for c in order})
        return rows
    return []


def tpex_notice(day: dt.date):
    """上櫃公布注意有價證券（bulletin/attention）-> notice 格式；沒有資料回傳 []，查詢失敗回傳 None。
    網站的「累計」是當天的總筆數（每一列都一樣），不是個股被列入的次數，所以累計次數留空"""
    js = tpex_query("bulletin/attention", startDate=slash(day), endDate=slash(day), type="all", order="date")
    if js is None:
        return None
    clean = lambda s: " ".join(re.sub(r"<[^>]+>", " ", str(s)).split())  # noqa: E731
    return [{"證券代號": f["證券代號"].strip(), "證券名稱": f["證券名稱"].strip(), "累計次數": "",
             "注意交易資訊": clean(f["注意交易資訊"]), "日期": f["公告日期"], "收盤價": num(f["收盤價"]),
             "本益比": num(f["本益比"])}
            for f in tpex_rows(js, "編號") if str(f.get("證券代號", "")).strip()]


def tpex_disposition(day: dt.date):
    """上櫃處置有價證券（bulletin/disposal）：查某一天會列出當天在處置期間內的股票 -> disposition 格式。
    期間的「~」改成跟上市相同的「～」；名稱後面附的連結去掉"""
    js = tpex_query("bulletin/disposal", startDate=slash(day), endDate=slash(day),
                    type="all", reason=-1, measure=-1, order="date")
    if js is None:
        return None
    return [{"證券代號": f["證券代號"].strip(), "證券名稱": re.sub(r"\(.*\)$", "", f["證券名稱"]).strip(),
             "公布日期": f["公布日期"], "累計": str(f["累計"]), "處置條件": f["處置原因"],
             "處置起迄時間": f["處置起訖時間"].replace("~", "～"), "處置措施": f["處置措施"]}
            for f in tpex_rows(js, "編號") if str(f.get("證券代號", "")).strip()]


def tpex_day_trading(day: dt.date):
    """上櫃現股當沖交易統計（intraday/stat）-> day_trading 格式（欄位名稱相同）"""
    return [{k: (v.strip() if i < 3 else num(v)) for i, (k, v) in enumerate(f.items())}
            for f in tpex_rows(tpex_query("intraday/stat", type="Daily", date=slash(day)), "證券代號")]


def tpex_sbl_short(day: dt.date):
    """上櫃信用額度總量管制餘額表（margin/sbl）-> sbl_short 格式（欄位順序跟上市相同，單位：股）"""
    js = tpex_query("margin/sbl", date=slash(day))
    for t in (js or {}).get("tables") or []:
        fields = t.get("fields") or []
        if fields[:1] != ["股票代號"]:
            continue
        if len(fields) != len(SBL_COLS):
            raise RuntimeError(f"上櫃借券賣出欄位數變了（{len(fields)} 欄）：{fields}")
        return [{c: (v.strip() if c in ("代號", "名稱", "備註") else num(v)) for c, v in zip(SBL_COLS, r)}
                for r in t.get("data") or []]
    return []


def tpex_index(day: dt.date):
    """櫃買指數（day 所在月份）-> {'2026-10-01': [row], ...}，跟 taiex 相同格式"""
    out = {}
    for f in tpex_rows(tpex_query("indexInfo/inx", date=f"{day:%Y/%m}/01"), "日期"):
        d = parse_date(f["日期"])
        out[d.isoformat()] = [{"Date": roc(d), "OpeningIndex": num(f["開市"]), "HighestIndex": num(f["最高"]),
                               "LowestIndex": num(f["最低"]), "ClosingIndex": num(f["收市"])}]
    return out


def tpex_ex_rights(start: dt.date, end: dt.date):
    """上櫃除權除息計算結果（bulletin/exDailyQ）-> {'2026-10-16': [...], ...}；
    代號、名稱改成跟上市相同的欄位名稱，權/息的「除息」改成「息」"""
    groups = defaultdict(list)
    for f in tpex_rows(tpex_query("bulletin/exDailyQ", startDate=slash(start), endDate=slash(end)), "除權息日期"):
        d = parse_date(f.pop("除權息日期"))
        row = {"資料日期": roc(d), "股票代號": f.pop("代號").strip(), "股票名稱": f.pop("名稱").strip()}
        row.update({k: (v.removeprefix("除") if k == "權/息" else num(v)) for k, v in f.items()})
        groups[d.isoformat()].append(row)
    return groups


def tpex_ex_rights_forecast():
    """上櫃除權除息預告（OpenAPI tpex_exright_prepost）-> ex_rights_forecast 格式"""
    rows = get_json(TPEX_OPENAPI + "/tpex_exright_prepost")
    out = []
    for r in rows:
        d = parse_date(r["ExRrightsExDividendDate"])
        out.append({
            "除權除息日期": d.isoformat() if d else r["ExRrightsExDividendDate"],
            "股票代號": r["SecuritiesCompanyCode"].strip(),
            "名稱": r["CompanyName"].strip(),
            "除權息": r["ExRrightsExDividend"].removeprefix("除"),
            "無償配股率": num(r["StockDividendRatio"]),
            "現金增資配股率": num(r["SubscriptionRatioToNewSharesIssued"]),
            "現金增資認購價": num(r["SubscriptionPricePerShare"]),
            "現金股利": num(r["CashDividend"]),
        })
    return out


# 順序有意義：taiex 放第一個，它的日期就是交易日清單，其他逐日資料依此判斷要查哪幾天
DATASETS = {
    "taiex":            {"by_month": taiex},          # 加權指數日 K
    # 這四個公布後內容可能還會變（10/30 觀察到行情 13:56 公布、14:53 又變了一次），下午排程抓到的不一定是定案，
    # 所以也每次重查最近 REFRESH 個交易日；內容沒變（只差排列順序）或還沒公布（查到空的）時不會覆寫
    "stock_day_all":    {"by_day": stock_day_all, "refresh": True},    # 個股日成交（K 線本體）
    "bwibbu_all":       {"by_day": bwibbu_all, "refresh": True},       # 本益比/殖利率/淨值比
    "margin":           {"by_day": margin, "refresh": True},           # 融資融券（晚上才公布，多半隔天補上）
    "institutional":    {"by_day": institutional, "refresh": True},    # 三大法人買賣超（個股）
    "institutional_amount": {"by_day": institutional_amount},  # 三大法人買賣金額（大盤實際金額）
    "sector_index":     {"by_day": sector_index},     # 類股指數
    # 注意股、處置股：當天沒有也是正常的，所以最近 REFRESH 個交易日每次都重新查（傍晚查到空的不代表晚點沒有）
    "notice":           {"by_day": notice, "fields": NOTICE_COLS, "refresh": True},
    "disposition":      {"by_day": disposition, "fields": DISPOSITION_COLS, "refresh": True},
    "ex_rights":        {"by_range": ex_rights},      # 除權除息計算結果（查最近 14 天，晚公布的也抓得到）
    "ex_rights_detail": {"by_range": ex_rights_detail},   # 配股明細（每千股配股數；要放在 ex_rights 之後）
    "ex_rights_forecast": {"snapshot": ex_rights_forecast},  # 除權除息預告（只保留最新一份）
    "day_trading":      {"by_day": day_trading},      # 個股當日沖銷成交量值
    "sbl_short":        {"by_day": sbl_short},        # 借券賣出餘額（晚上才公布，多半隔天補上）
    # 月營收：OpenAPI 只有最新一個月，月初公司陸續申報，內容有變就覆寫；歷史月份由 backfill.py 從公開資訊觀測站補
    "monthly_revenue":  {"url": OPENAPI + "/opendata/t187ap05_L", "mode": "monthly", "history": monthly_revenue_history},
    "holiday_schedule": {"url": OPENAPI + "/holidaySchedule/holidaySchedule", "mode": "snapshot"},  # 休市日程
    # 季報、集保不是證交所的網站，連不上時只顯示警告、不算失敗，下次執行再試
    "income_statement": {"by_quarter": income_statement, "optional": True},   # 季報綜合損益表（當年累計）
    # 上櫃（櫃買中心），跟上面對應的上市資料集相同格式與更新規則；交易日跟上市相同，所以也用加權指數的日期
    "tpex_index":         {"by_month": tpex_index},                              # 櫃買指數日 K
    "tpex_stock_day_all": {"by_day": tpex_stock_day_all, "refresh": True},
    "tpex_bwibbu_all":    {"by_day": tpex_bwibbu_all, "refresh": True},
    "tpex_margin":        {"by_day": tpex_margin, "refresh": True},
    "tpex_institutional": {"by_day": tpex_institutional, "refresh": True},
    "tpex_notice":        {"by_day": tpex_notice, "fields": NOTICE_COLS, "refresh": True},
    "tpex_disposition":   {"by_day": tpex_disposition, "fields": DISPOSITION_COLS, "refresh": True},
    "tpex_ex_rights":     {"by_range": tpex_ex_rights},
    "tpex_ex_rights_forecast": {"snapshot": tpex_ex_rights_forecast},
    "tpex_day_trading":   {"by_day": tpex_day_trading},
    "tpex_sbl_short":     {"by_day": tpex_sbl_short},
    "tpex_monthly_revenue": {"url": TPEX_OPENAPI + "/mopsfin_t187ap05_O", "mode": "monthly",
                             "history": partial(monthly_revenue_history, market="otc")},
    "tpex_income_statement": {"by_quarter": partial(income_statement, market="otc"), "optional": True},
    # 放在上市、上櫃行情之後：只留最新行情裡有的股票
    "shareholding":     {"weekly": shareholding, "optional": True},           # 集保戶股權分散表（每週）
}


def recent_trading_days():
    """最近 LOOKBACK 個交易日（以已存的加權指數檔案為準）"""
    days = sorted(p.stem for p in (DATA_DIR / "taiex").glob("*/*.csv"))
    return days[-LOOKBACK:]


def same_rows(out: Path, rows, ignore=None) -> bool:
    """已存的檔案內容是否跟 rows 相同（不管順序：三大法人買賣超相同的股票，每次查詢的排列順序可能不同）；
    ignore 可指定不比較的欄位"""
    with out.open(encoding="utf-8-sig", newline="") as f:
        old = list(csv.reader(f))
    fields = list(dict.fromkeys(k for r in rows for k in r))
    if old[:1] != [fields] and rows:
        return False
    keep = [i for i, k in enumerate(fields) if k != ignore]
    new = [[str(r.get(fields[i], "")) for i in keep] for r in rows]
    return sorted([row[i] for i in keep] for row in old[1:]) == sorted(new)


def fetch_by_day(name, cfg):
    days = recent_trading_days()
    for key in days:
        out = out_path(name, key)
        refresh = cfg.get("refresh") and key in days[-REFRESH:]
        if out.exists() and not refresh:
            continue
        try:
            rows = cfg["by_day"](dt.date.fromisoformat(key))
        except Exception as e:  # noqa: BLE001
            if not out.exists():
                raise
            # 已經有檔案、只是重查有沒有更正：查不到就算了，不算失敗
            print(f"  {key} 重查失敗，保留已存的檔案：{e}")
            continue
        if not rows and not (rows is not None and cfg.get("fields")):
            print(f"  {key} 尚未公布，下次執行再補")
        elif out.exists() and same_rows(out, rows):
            print(f"  {key} 內容沒有變")
        else:
            save(name, key, rows, overwrite=refresh)


def fetch_openapi(name, cfg):
    rows = get_json(cfg["url"])
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("回傳資料為空或格式不是清單")
    if cfg["mode"] == "snapshot":
        out = DATA_DIR / name / f"{name}.csv"
        write_csv(out, rows, overwrite=True)
        print(f"  已更新 {out.relative_to(DATA_DIR)}（{len(rows)} 筆）")
        return
    if cfg["mode"] == "daily":
        groups = group_rows(rows, DAY_KEYS, parse_date, None)
    else:
        groups = group_rows(rows, MONTH_KEYS, parse_month, None)
    for key in sorted(groups):
        out = out_path(name, key)
        if cfg["mode"] == "monthly" and out.exists():
            # 月營收月初陸續申報：已存的月份多了公司或數字更正就覆寫（出表日期每天不同，不算）
            if same_rows(out, groups[key], ignore="出表日期"):
                print(f"  {key} 內容沒有變")
            else:
                save(name, key, groups[key], overwrite=True)
        else:
            save(name, key, groups[key])


def fetch_by_quarter(name, cfg):
    for year, season in recent_quarters():
        key = f"{year}-Q{season}"
        out = out_path(name, key)
        rows = cfg["by_quarter"](year, season)
        if not rows:
            print(f"  {key} 還沒有資料")
        elif out.exists() and same_rows(out, rows):
            print(f"  {key} 內容沒有變")
        else:
            save(name, key, rows, overwrite=True)


def main() -> int:
    failed = []
    for name, cfg in DATASETS.items():
        print(f"[{name}]")
        try:
            if "by_day" in cfg:
                fetch_by_day(name, cfg)
            elif "snapshot" in cfg:
                rows = cfg["snapshot"]()
                out = DATA_DIR / name / f"{name}.csv"
                write_csv(out, rows, overwrite=True)
                print(f"  已更新 {out.relative_to(DATA_DIR)}（{len(rows)} 筆）")
            elif "by_month" in cfg:
                # 月初時連上個月一起查，避免月底的交易日漏掉
                months = {f"{d:%Y-%m}": d for d in (TODAY - dt.timedelta(days=10), TODAY)}
                for day in sorted(months.values()):
                    for key, rows in sorted(cfg["by_month"](day).items()):
                        if not out_path(name, key).exists():
                            save(name, key, rows)
            elif "by_quarter" in cfg:
                fetch_by_quarter(name, cfg)
            elif "weekly" in cfg:
                key, rows = cfg["weekly"]()
                save(name, key, rows)
            elif "by_range" in cfg:
                groups = cfg["by_range"](TODAY - dt.timedelta(days=14), TODAY)
                if not groups:
                    print("  這段期間沒有資料")
                for key in sorted(groups):
                    save(name, key, groups[key])
            else:
                fetch_openapi(name, cfg)
                time.sleep(1)
        except Exception as e:  # noqa: BLE001
            if cfg.get("optional"):
                print(f"  警告：{e}（下次執行再試）")
                continue
            print(f"  失敗：{e}")
            failed.append(name)
    if failed:
        print("以下資料集失敗：", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
