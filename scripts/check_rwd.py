"""檢查證交所網站 rwd 端點、櫃買中心網站能不能用（每日抓取與歷史回補都會用到）

在本機跑：python scripts/check_rwd.py
在 GitHub 跑：Actions 分頁 → 「檢查證交所網站端點」→ Run workflow
兩邊都建議試，因為 GitHub 的主機在國外，證交所、櫃買中心可能對不同 IP 有不同限制。
"""
import datetime as dt
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
RWD = "https://www.twse.com.tw/rwd/zh"
TPEX = "https://www.tpex.org.tw/www/zh-tw"

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
    "上櫃行情（櫃買中心）": f"{TPEX}/afterTrading/dailyQuotes?date={d:%Y/%m/%d}&response=json",
    "上櫃三大法人（櫃買中心）": f"{TPEX}/insti/dailyTrade?type=Daily&sect=EW&date={d:%Y/%m/%d}&response=json",
    "上櫃舊年份行情（2016 年）": f"{TPEX}/afterTrading/dailyQuotes?date=2016/01/05&response=json",
}

print(f"測試日期：{D}（若是國定假日，部分端點會回報無資料，屬正常）\n")
for label, url in TESTS.items():
    t = time.time()
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30, verify=False)
        js = r.json()
        n = len(js.get("data") or []) or sum(len(t.get("data") or []) for t in js.get("tables", []))
        print(f"{'✅' if str(js.get('stat')).upper() == 'OK' else '⚠️ '} {label}：HTTP {r.status_code}，stat={js.get('stat')}，{n} 筆，{time.time() - t:.1f} 秒")
    except Exception as e:  # noqa: BLE001
        print(f"❌ {label}：{type(e).__name__}: {e}")
    time.sleep(3)
