"""檢查證交所網站 rwd 端點、櫃買中心網站能不能用（每日抓取與歷史回補都會用到）

在本機跑：python scripts/check_rwd.py               # 測試日期 = 最近一個平日
          python scripts/check_rwd.py 2018-10-02    # 指定日期（例如某一天回補一直失敗）
在 GitHub 跑：Actions 分頁 → 「檢查證交所網站端點」→ Run workflow（可填日期）
兩邊都建議試，因為 GitHub 的主機在國外，證交所、櫃買中心可能對不同 IP 有不同限制
（例如 2018-10-02 的上櫃行情，GitHub 上一直回 403，本機正常）。
"""
import datetime as dt
import sys
import time

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
RWD = "https://www.twse.com.tw/rwd/zh"
TPEX = "https://www.tpex.org.tw/www/zh-tw"
TPEX_OPENAPI = "https://www.tpex.org.tw/openapi/v1"

# 測試日期：有指定就用指定的，否則找最近一個平日
if len(sys.argv) > 1 and sys.argv[1]:
    d = dt.date.fromisoformat(sys.argv[1])
else:
    d = dt.date.today() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
D, S = d.strftime("%Y%m%d"), d.strftime("%Y/%m/%d")   # 證交所、櫃買中心的日期格式

TESTS = {
    "個股月日K（2330）": f"{RWD}/afterTrading/STOCK_DAY?date={D}&stockNo=2330&response=json",
    "指定日期全市場行情": f"{RWD}/afterTrading/MI_INDEX?date={D}&type=ALLBUT0999&response=json",
    "三大法人買賣超": f"{RWD}/fund/T86?date={D}&selectType=ALL&response=json",
    "除權除息計算結果（近一年）": f"{RWD}/exRight/TWT49U?startDate={(d - dt.timedelta(days=365)):%Y%m%d}&endDate={D}&response=json",
    "舊年份資料（2015 年 2330）": f"{RWD}/afterTrading/STOCK_DAY?date=20150105&stockNo=2330&response=json",
    # 櫃買中心：每個 tpex_ 資料集用到的端點都測（行情表約 2 MB，最容易被擋或中途斷線）
    "上櫃行情": f"{TPEX}/afterTrading/dailyQuotes?date={S}&response=json",
    "上櫃本益比": f"{TPEX}/afterTrading/peQryDate?date={S}&response=json",
    "上櫃融資融券": f"{TPEX}/margin/balance?date={S}&response=json",
    "上櫃三大法人": f"{TPEX}/insti/dailyTrade?type=Daily&sect=EW&date={S}&response=json",
    "上櫃注意股": f"{TPEX}/bulletin/attention?startDate={S}&endDate={S}&type=all&order=date&response=json",
    "上櫃處置股": f"{TPEX}/bulletin/disposal?startDate={S}&endDate={S}&type=all&reason=-1&measure=-1&order=date&response=json",
    "上櫃當沖": f"{TPEX}/intraday/stat?type=Daily&date={S}&response=json",
    "上櫃借券賣出": f"{TPEX}/margin/sbl?date={S}&response=json",
    "櫃買指數（當月）": f"{TPEX}/indexInfo/inx?date={d:%Y/%m}/01&response=json",
    "上櫃除權息（近一個月）": f"{TPEX}/bulletin/exDailyQ?startDate={(d - dt.timedelta(days=30)):%Y/%m/%d}&endDate={S}&response=json",
    "上櫃除權息預告（OpenAPI）": f"{TPEX_OPENAPI}/tpex_exright_prepost",
    "上櫃月營收（OpenAPI）": f"{TPEX_OPENAPI}/mopsfin_t187ap05_O",
    "上櫃舊年份行情（2016 年）": f"{TPEX}/afterTrading/dailyQuotes?date=2016/01/05&response=json",
}

print(f"測試日期：{D}（若是假日，部分端點會回報無資料，屬正常）\n")
for label, url in TESTS.items():
    t = time.time()
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60, verify=False)
        size = f"{len(r.content) / 1024:,.0f} KB"
        if r.status_code != 200:   # 403 多半是被擋（防火牆、CDN），重試也沒用
            print(f"❌ {label}：HTTP {r.status_code}，{size}，{time.time() - t:.1f} 秒")
        else:
            js = r.json()
            if isinstance(js, list):   # OpenAPI 直接回傳清單
                ok, stat, n = bool(js), "-", len(js)
            else:
                ok, stat = str(js.get("stat")).upper() == "OK", js.get("stat")
                n = len(js.get("data") or []) or sum(len(x.get("data") or []) for x in js.get("tables") or [])
            print(f"{'✅' if ok else '⚠️ '} {label}：HTTP 200，stat={stat}，{n} 筆，{size}，{time.time() - t:.1f} 秒")
    except Exception as e:  # noqa: BLE001
        print(f"❌ {label}：{type(e).__name__}: {e}")
    time.sleep(3)
