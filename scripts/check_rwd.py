"""檢查證交所網站 rwd 端點能不能用（歷史回補會用到）

在本機跑：python scripts/check_rwd.py
在 GitHub 跑：Actions 分頁 → 「檢查證交所網站端點」→ Run workflow
兩邊都建議試，因為 GitHub 的主機在國外，證交所可能對不同 IP 有不同限制。
"""
import datetime as dt
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
RWD = "https://www.twse.com.tw/rwd/zh"

# 找最近一個平日當測試日期
d = dt.date.today() - dt.timedelta(days=1)
while d.weekday() >= 5:
    d -= dt.timedelta(days=1)
D = d.strftime("%Y%m%d")

TESTS = {
    "個股月日K（2330）": f"{RWD}/afterTrading/STOCK_DAY?date={D}&stockNo=2330&response=json",
    "指定日期全市場行情": f"{RWD}/afterTrading/MI_INDEX?date={D}&type=ALLBUT0999&response=json",
    "三大法人買賣超": f"{RWD}/fund/T86?date={D}&selectType=ALL&response=json",
    "除權除息計算結果（近一年）": f"{RWD}/exRight/TWT49U?startDate={(d - dt.timedelta(days=365)):%Y%m%d}&endDate={D}&response=json",
    "舊年份資料（2015 年 2330）": f"{RWD}/afterTrading/STOCK_DAY?date=20150105&stockNo=2330&response=json",
}

print(f"測試日期：{D}（若是國定假日，部分端點會回報無資料，屬正常）\n")
for label, url in TESTS.items():
    t = time.time()
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30, verify=False)
        js = r.json()
        n = len(js.get("data") or []) or sum(len(t.get("data") or []) for t in js.get("tables", []))
        print(f"{'✅' if js.get('stat') == 'OK' else '⚠️ '} {label}：HTTP {r.status_code}，stat={js.get('stat')}，{n} 筆，{time.time() - t:.1f} 秒")
    except Exception as e:  # noqa: BLE001
        print(f"❌ {label}：{type(e).__name__}: {e}")
    time.sleep(3)
