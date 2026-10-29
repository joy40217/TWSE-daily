# twse-daily

每個交易日由 GitHub Actions 自動抓取臺灣證交所 OpenAPI，存成 CSV 累積歷史資料。

## 資料結構

    data/
      stock_day_all/2026/2026-10-29.csv     全市場日成交
      bwibbu_all/2026/2026-10-29.csv        本益比 / 殖利率 / 淨值比
      monthly_revenue/2026/2026-09.csv      上市公司月營收

檔名用的是資料本身的日期，假日或資料尚未更新時不會產生重複檔案。

## 設定步驟

1. 在 GitHub 建立新 repo，把這些檔案推上去
2. 到 Settings → Actions → General → Workflow permissions，選 Read and write permissions
3. 到 Actions 分頁，手動執行一次「每日抓取證交所資料」確認正常

## 新增資料集

在 `scripts/fetch.py` 的 `DATASETS` 加一行即可。

## 轉成 SQLite 分析

    python scripts/build_db.py

會在本機產生 `twse.db`（已被 .gitignore 排除，不會進 git）。
