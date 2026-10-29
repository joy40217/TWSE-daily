# twse-daily

每個交易日由 GitHub Actions 自動抓取臺灣證交所資料，存成 CSV 累積歷史資料，
並可在本機匯入 SQLite、產生還原權息日 K 做技術分析。

## 資料集

| 資料夾 | 內容 | 來源 |
|---|---|---|
| `stock_day_all/` | 個股日成交（K 線開高低收量） | 網站 MI_INDEX |
| `bwibbu_all/` | 本益比 / 殖利率 / 淨值比 | 網站 BWIBBU_d |
| `margin/` | 融資融券 | 網站 MI_MARGN |
| `institutional/` | 三大法人買賣超（不含權證） | 網站 T86 |
| `taiex/` | 加權指數日 K | 網站 MI_5MINS_HIST |
| `ex_rights/` | 除權除息計算結果（只在有除權息的日子產生） | 網站 TWT49U |
| `monthly_revenue/` | 上市公司月營收（依資料年月存檔） | OpenAPI |
| `holiday_schedule/` | 休市日程（只保留最新一份） | OpenAPI |

檔案路徑為 `data/<資料夾>/<年>/<日期>.csv`，例如 `data/stock_day_all/2026/2026-10-29.csv`；
日期是資料本身的交易日，已存在的檔案不會被覆寫，假日也不會產生檔案。

「網站」指[證交所網站的歷史查詢](https://www.twse.com.tw/zh/trading/historical/mi-index.html)，
可以指定日期查詢，收盤後當天就有資料；欄位已轉成跟 [OpenAPI](https://openapi.twse.com.tw/) 相同的格式。
OpenAPI 的每日行情要到隔天才更新，所以只用在月營收、休市日程這類沒有日期問題的資料。

## 設定步驟

1. 在 GitHub 建立 repo，把這些檔案推上去
2. 到 Settings → Actions → General → Workflow permissions，選 **Read and write permissions**
3. 到 Actions 分頁手動執行一次「每日抓取證交所資料」，確認正常

## 使用方式

### 每日自動抓取

不需要手動操作。每個平日台灣時間 18:00，「每日抓取證交所資料」workflow 會執行
`scripts/fetch.py` 並 commit 新資料。

每次執行會檢查最近 5 個交易日，缺的就補。融資融券約晚上才公布，傍晚抓不到是正常的，
隔天執行時會自動補上；某天 workflow 沒跑成功也一樣。

在本機手動執行：

    pip install requests
    python scripts/fetch.py

### 回補歷史資料

在 Actions 分頁手動執行「回補歷史資料」，填入開始 / 結束日期（結束日期留空 = 昨天）。
也可以在本機執行：

    python scripts/backfill.py 2025-01-01                     # 從 2025-01-01 補到昨天
    python scripts/backfill.py 2025-01-01 2025-12-31          # 指定區間
    python scripts/backfill.py 2025-01-01 --only institutional,margin   # 只補部分資料集

- 可回補上表中所有「網站」來源的資料集，格式跟每日抓的完全相同
- 已存在的檔案會略過，中斷後重跑即可接著補
- 每次查詢間隔 3 秒，連續失敗 5 次（多半是被證交所暫時封鎖）會自動停止
- 一年約 1 小時、約 80 MB（git 壓縮後約一半以下）；GitHub Actions 單次最多 6 小時，建議一次補一到數年
- 2015 年 6 月起漲跌幅改為 10%，技術分析建議從這之後開始補

### 檢查證交所網站端點

    python scripts/check_rwd.py

或在 Actions 分頁手動執行「檢查證交所網站端點」。GitHub 主機在國外，兩邊結果可能不同。

### 轉成 SQLite 分析

    python scripts/build_db.py

會在本機產生 `twse.db`（不會進 git）。每個資料夾一張表，欄位都存成文字，另有 `_key` 欄是日期；
`stock_day_adj` 是整理好的日 K，價格已轉成數字並附上還原權息價格（adj_open / adj_high / adj_low / adj_close）。

    SELECT date, adj_open, adj_high, adj_low, adj_close, volume
    FROM stock_day_adj WHERE code = '2330' ORDER BY date;

還原權息只處理除權息；減資、面額變更不在 `ex_rights` 裡，這些股票的還原價在事件當天會有跳空。

## 新增資料集

在 `scripts/fetch.py` 的 `DATASETS` 加一行：

- 網站來源、每天一份：寫一個 `函式(日期) -> list[dict]`，登記為 `{"by_day": 函式}`，回補也會自動支援
- OpenAPI 來源：登記為 `{"url": ..., "mode": "monthly" 或 "snapshot"}`
