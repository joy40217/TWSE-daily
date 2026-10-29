"""每天抓取證交所資料，存成 data/<資料集>/<年>/<日期>.csv

三種存檔模式：
  daily    依資料內的日期逐日拆檔（一次回傳多天的也會自動拆開）
  monthly  依「資料年月」存檔
  snapshot 只保留最新一份（git 只在內容有變時才會 commit）
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


def twt49u_url():
    """除權除息計算結果：查最近 14 天，晚公布或補登的資料也抓得到"""
    start = (TODAY - dt.timedelta(days=14)).strftime("%Y%m%d")
    return f"{RWD}/exRight/TWT49U?startDate={start}&endDate={TODAY:%Y%m%d}&response=json"


# 順序有意義：stock_day_all 放第一個，它的日期會當作其他無日期欄位資料的「交易日」
DATASETS = {
    "stock_day_all":    {"url": OPENAPI + "/exchangeReport/STOCK_DAY_ALL", "mode": "daily"},     # 個股日成交（K 線本體）
    "bwibbu_all":       {"url": OPENAPI + "/exchangeReport/BWIBBU_ALL",    "mode": "daily"},     # 本益比/殖利率/淨值比
    "margin":           {"url": OPENAPI + "/exchangeReport/MI_MARGN",      "mode": "daily"},     # 融資融券
    "taiex":            {"url": OPENAPI + "/indicesReport/MI_5MINS_HIST",  "mode": "daily"},     # 加權指數日 K（回傳當月全部）
    "ex_rights":        {"url": twt49u_url, "mode": "daily", "rwd": True, "allow_empty": True},  # 除權除息計算結果
    "monthly_revenue":  {"url": OPENAPI + "/opendata/t187ap05_L",          "mode": "monthly"},   # 月營收
    "holiday_schedule": {"url": OPENAPI + "/holidaySchedule/holidaySchedule", "mode": "snapshot"},  # 休市日程
}

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


def get_json(url: str, retries: int = 3):
    headers = {"User-Agent": "Mozilla/5.0 (twse-daily-archiver)", "Accept": "application/json"}
    verify = True
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=60, verify=verify)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.json()
        except requests.exceptions.SSLError:
            print("  SSL 驗證失敗，改為略過憑證驗證重試")
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            verify = False
        except Exception as e:  # noqa: BLE001
            print(f"  第 {attempt} 次失敗：{e}")
            time.sleep(5 * attempt)
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


def write_csv(out: Path, rows, overwrite=False) -> bool:
    if out.exists() and not overwrite:
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(k for r in rows for k in r))
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


def process(name, cfg, ref_date):
    url = cfg["url"]() if callable(cfg["url"]) else cfg["url"]
    js = get_json(url)
    rows = rwd_rows(js) if cfg.get("rwd") else js
    if not isinstance(rows, list):
        raise RuntimeError("回傳格式不是清單")
    if not rows:
        if cfg.get("allow_empty"):
            print("  這段期間沒有資料")
            return None
        raise RuntimeError("回傳資料為空")

    mode = cfg["mode"]
    if mode == "snapshot":
        out = DATA_DIR / name / f"{name}.csv"
        write_csv(out, rows, overwrite=True)
        print(f"  已更新 {out.relative_to(DATA_DIR)}（{len(rows)} 筆）")
        return None

    if mode == "daily":
        groups = group_rows(rows, DAY_KEYS, parse_date, ref_date)
    else:
        groups = group_rows(rows, MONTH_KEYS, parse_month, None)

    for key in sorted(groups):
        out = DATA_DIR / name / key[:4] / f"{key}.csv"
        if write_csv(out, groups[key]):
            print(f"  已寫入 {out.relative_to(DATA_DIR)}（{len(groups[key])} 筆）")
        else:
            print(f"  {out.relative_to(DATA_DIR)} 已存在，略過")
    return max(groups) if groups else None


def main() -> int:
    failed, ref_date = [], None
    for name, cfg in DATASETS.items():
        print(f"[{name}]")
        try:
            latest = process(name, cfg, ref_date)
            if name == "stock_day_all":
                ref_date = latest
        except Exception as e:  # noqa: BLE001
            print(f"  失敗：{e}")
            failed.append(name)
        time.sleep(3 if cfg.get("rwd") else 1)  # 證交所網站對頻繁請求較敏感
    if failed:
        print("以下資料集失敗：", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
