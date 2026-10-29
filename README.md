# twse-daily

每個交易日由 GitHub Actions 自動抓取臺灣證交所資料，存成 CSV 累積歷史資料。

## 資料結構

    data/
      stock_day_all/2026/2026-10-29.csv      個股日成交（K 線開高低收量）
      bwibbu_all/2026/2026-10-29.csv         本益比 / 殖利率 / 淨值比
      margin/2026/2026-10-29.csv             融資融券
      taiex/2026/2026-10-29.csv              加權指數日 K
      ex_rights/2026/2026-10-16.csv          除權除息計算結果（只在有除權息的日子產生）
      monthly_revenue/2026/2026-09.csv       上市公司月營收
      holiday_schedule/holiday_schedule.csv  休市日程（只保留最新一份）

檔名用的是資料本身的日期；一次回傳多天的資料（如加權指數、除權息）會自動拆成每天一個檔案，
已存在的檔案不會被覆寫，假日也不會產生重複檔案。

除權息資料來自[證交所網站](www.twse.com.tw/rwd)，其他都來自 OpenAPI。

## 設定步驟

1. 在 GitHub 建立新 repo，把這些檔案推上去
2. 到 Settings → Actions → General → Workflow permissions，選 Read and write permissions
3. 到 Actions 分頁，手動執行一次「每日抓取證交所資料」確認正常

## 檢查證交所網站端點（歷史回補前先做）

    python scripts/check_rwd.py

或在 Actions 分頁手動執行「檢查證交所網站端點」。兩邊都建議試，因為 GitHub 主機在國外。

## 回補歷史資料

    python scripts/backfill.py 2025-01-01                     # 從 2025-01-01 補到昨天
    python scripts/backfill.py 2025-01-01 2025-06-30          # 指定區間
    python scripts/backfill.py 2025-01-01 --only stock_day_all,taiex

或在 Actions 分頁手動執行「回補歷史資料」，填入開始 / 結束日期。

- 可回補 `stock_day_all`、`bwibbu_all`、`margin`、`taiex`、`ex_rights`；
  `monthly_revenue`、`holiday_schedule` 沒有歷史查詢，不在回補範圍
- 回補檔案的欄位與格式跟每日抓的完全相同，可以混在一起
- 每次查詢間隔 3 秒，一年約 40 分鐘、約 50 MB；建議一次補一到數年
- 已存在的檔案會略過，中斷或被暫時封鎖後重跑即可接著補

## 新增資料集

在 `scripts/fetch.py` 的 `DATASETS` 加一行即可。

## 轉成 SQLite 分析

    python scripts/build_db.py

會在本機產生 `twse.db`（不會進 git），其中 `stock_day_adj` 表是整理好的日 K，
價格已轉成數字並附上還原權息價格（adj_open / adj_high / adj_low / adj_close）。

    SELECT date, adj_open, adj_high, adj_low, adj_close, volume
    FROM stock_day_adj WHERE code = '2330' ORDER BY date;
