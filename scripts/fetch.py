"""每天抓取證交所資料，存成 data/<資料集>/<年>/<日期>.csv

資料來源：
  證交所網站歷史查詢（rwd）：可指定日期，收盤後當天就查得到
    → 個股行情、本益比、融資融券、三大法人（個股與金額）、加權指數、類股指數、除權息、
      注意股、處置股、除權息預告
  OpenAPI：只有最新一份，而且每日行情要到隔天才更新，只用在沒有日期問題的資料
    → 月營收、休市日程

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
from pathlib import Path

import requests
import urllib3

OPENAPI = "https://openapi.twse.com.tw/v1"
RWD = "https://www.twse.com.tw/rwd/zh"
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
    return "" if s in ("", "-", "--", "---") else s.replace(",", "")


def count(s) -> str:
    """數量為 0 時 OpenAPI 給空白，這裡比照辦理"""
    n = num(s)
    return "" if n == "0" else n


def ratio(s) -> str:
    """比率為 0（如 '0.00'）時 OpenAPI 給空白，這裡比照辦理"""
    n = num(s)
    return n if n and float(n) != 0 else ""


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
    "ex_rights_forecast": {"snapshot": ex_rights_forecast},  # 除權除息預告（只保留最新一份）
    "monthly_revenue":  {"url": OPENAPI + "/opendata/t187ap05_L", "mode": "monthly"},               # 月營收
    "holiday_schedule": {"url": OPENAPI + "/holidaySchedule/holidaySchedule", "mode": "snapshot"},  # 休市日程
}


def recent_trading_days():
    """最近 LOOKBACK 個交易日（以已存的加權指數檔案為準）"""
    days = sorted(p.stem for p in (DATA_DIR / "taiex").glob("*/*.csv"))
    return days[-LOOKBACK:]


def same_rows(out: Path, rows) -> bool:
    """已存的檔案內容是否跟 rows 相同（不管順序：三大法人買賣超相同的股票，每次查詢的排列順序可能不同）"""
    with out.open(encoding="utf-8-sig", newline="") as f:
        old = list(csv.reader(f))
    fields = list(dict.fromkeys(k for r in rows for k in r))
    if old[:1] != [fields] and rows:
        return False
    new = [[str(r.get(k, "")) for k in fields] for r in rows]
    return sorted(old[1:]) == sorted(new)


def fetch_by_day(name, cfg):
    days = recent_trading_days()
    for key in days:
        out = out_path(name, key)
        refresh = cfg.get("refresh") and key in days[-REFRESH:]
        if out.exists() and not refresh:
            continue
        rows = cfg["by_day"](dt.date.fromisoformat(key))
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
        save(name, key, groups[key])


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
            print(f"  失敗：{e}")
            failed.append(name)
    if failed:
        print("以下資料集失敗：", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
