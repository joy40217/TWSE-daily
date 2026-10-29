"""每天抓取證交所 OpenAPI 資料，存成 data/<資料集>/<年>/<日期>.csv"""
import csv
import datetime as dt
import re
import sys
import time
from pathlib import Path

import requests
import urllib3

BASE = "https://openapi.twse.com.tw/v1"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TW_TZ = dt.timezone(dt.timedelta(hours=8))

# 想多抓什麼資料，就在這裡加一行
# period: "day" 以交易日存檔，"month" 以資料年月存檔
DATASETS = {
    "stock_day_all":   {"path": "/exchangeReport/STOCK_DAY_ALL", "period": "day"},    # 全市場日成交
    "bwibbu_all":      {"path": "/exchangeReport/BWIBBU_ALL",    "period": "day"},    # 本益比/殖利率/淨值比
    "monthly_revenue": {"path": "/opendata/t187ap05_L",          "period": "month"},  # 上市公司月營收
}

DAY_KEYS = ["Date", "日期", "出表日期"]
MONTH_KEYS = ["資料年月"]


def roc_to_date(s: str):
    """民國年日期 '1150731' 或 '115/07/31' -> date"""
    d = re.sub(r"\D", "", str(s))
    if len(d) < 6:
        return None
    try:
        return dt.date(int(d[:-4]) + 1911, int(d[-4:-2]), int(d[-2:]))
    except ValueError:
        return None


def roc_to_month(s: str):
    """民國年月 '11507' 或 '115/07' -> '2026-07'"""
    d = re.sub(r"\D", "", str(s))
    if len(d) < 4:
        return None
    y, m = int(d[:-2]) + 1911, int(d[-2:])
    return f"{y}-{m:02d}" if 1 <= m <= 12 else None


def get_json(url: str, retries: int = 3):
    headers = {"User-Agent": "twse-daily-archiver", "Accept": "application/json"}
    verify = True
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=60, verify=verify)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.json()
        except requests.exceptions.SSLError:
            # 證交所憑證在部分 Python 版本會驗證失敗，退而略過驗證
            print("  SSL 驗證失敗，改為略過憑證驗證重試")
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            verify = False
        except Exception as e:  # noqa: BLE001
            print(f"  第 {attempt} 次失敗：{e}")
            time.sleep(5 * attempt)
    raise RuntimeError(f"無法取得 {url}")


def file_key(rows, period: str) -> str:
    """從資料本身找出日期，避免假日把舊資料存成新檔"""
    keys, parser = (DAY_KEYS, roc_to_date) if period == "day" else (MONTH_KEYS, roc_to_month)
    for k in keys:
        if k in rows[0]:
            vals = [v for v in (parser(r.get(k, "")) for r in rows) if v]
            if vals:
                v = max(vals)
                return v.isoformat() if period == "day" else v
    today = dt.datetime.now(TW_TZ).date()
    print("  找不到日期欄位，改用今天日期")
    return today.isoformat() if period == "day" else today.strftime("%Y-%m")


def save(name: str, rows, key: str) -> bool:
    out = DATA_DIR / name / key[:4] / f"{key}.csv"
    if out.exists():
        print(f"  {out.relative_to(DATA_DIR)} 已存在，略過")
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"  已寫入 {out.relative_to(DATA_DIR)}（{len(rows)} 筆）")
    return True


def main() -> int:
    failed = []
    for name, cfg in DATASETS.items():
        print(f"[{name}]")
        try:
            rows = get_json(BASE + cfg["path"])
            if not isinstance(rows, list) or not rows:
                raise RuntimeError("回傳資料為空")
            save(name, rows, file_key(rows, cfg["period"]))
        except Exception as e:  # noqa: BLE001
            print(f"  失敗：{e}")
            failed.append(name)
        time.sleep(1)  # 對伺服器客氣一點
    if failed:
        print("以下資料集失敗：", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
