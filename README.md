# twse-daily

每個交易日由 GitHub Actions 自動抓取臺灣證交所資料，存成 CSV 累積歷史資料，
並可在本機匯入 SQLite、產生還原權息日 K 做技術分析。

## 資料集

| 資料夾 | 內容 | 證交所報表 |
|---|---|---|
| `stock_day_all/` | 個股日成交（K 線開高低收量） | [每日收盤行情](https://www.twse.com.tw/zh/trading/historical/mi-index.html) |
| `bwibbu_all/` | 本益比 / 殖利率 / 淨值比 | [個股日本益比、殖利率及股價淨值比](https://www.twse.com.tw/zh/trading/historical/bwibbu-day.html) |
| `margin/` | 融資融券 | [融資融券餘額](https://www.twse.com.tw/zh/trading/margin/mi-margn.html) |
| `institutional/` | 三大法人買賣超（不含權證） | [三大法人買賣超日報](https://www.twse.com.tw/zh/trading/foreign/t86.html) |
| `taiex/` | 加權指數日 K | [發行量加權股價指數歷史資料](https://www.twse.com.tw/zh/indices/taiex/mi-5min-hist.html) |
| `ex_rights/` | 除權除息計算結果（只在有除權息的日子產生） | [除權除息計算結果表](https://www.twse.com.tw/zh/announcement/ex-right/twt49u.html) |
| `monthly_revenue/` | 上市公司月營收（依資料年月存檔） | [OpenAPI](https://openapi.twse.com.tw/) 上市公司每月營業收入彙總表 |
| `holiday_schedule/` | 休市日程（只保留最新一份） | [OpenAPI](https://openapi.twse.com.tw/) 有價證券集中交易市場開（休）市日期 |

檔案路徑為 `data/<資料夾>/<年>/<日期>.csv`，例如 `data/stock_day_all/2026/2026-10-29.csv`；
日期是資料本身的交易日，已存在的檔案不會被覆寫，假日也不會產生檔案。

除了最後兩項，資料都是用證交所網站的查詢功能依日期抓取，收盤後當天就有資料，
欄位也轉成跟 OpenAPI 相同的格式。OpenAPI 的每日行情要到隔天才更新，
所以只用在月營收、休市日程這類沒有日期問題的資料。

## 設定步驟

1. 在 GitHub 建立 repo，把這些檔案推上去
2. 到 Settings → Actions → General → Workflow permissions，選 **Read and write permissions**
3. 到 Settings → Pages → Build and deployment → Source，選 **GitHub Actions**（收盤後報告網頁用）
4. （選用）到 Settings → Secrets and variables → Actions 新增 `DISCORD_WEBHOOK_URL`，
   值為 Discord 頻道的 Webhook 網址（頻道設定 → 整合 → Webhook → 新 Webhook → 複製網址）
5. 到 Actions 分頁手動執行一次「每日抓取證交所資料」，確認正常

## 使用方式

### 每日自動抓取

不需要手動操作。「每日抓取證交所資料」workflow 每個平日執行三次：

| 台灣時間 | 做什麼 |
|---|---|
| 18:23 | 抓行情、本益比、三大法人等收盤資料 |
| 22:23 | 再抓一次（補上晚間公布的融資融券，通常約 21:00 前公布），產生收盤後報告、發布網頁、送 Discord 通知 |
| 23:47 | 備援：跟 22:23 做一樣的事，上一次沒跑到時補上；已經通知過的日子不會重複通知 |

排程刻意避開整點：整點是 GitHub 排程最壅塞的時段，常會延遲甚至被丟棄。
就算避開整點，排程偶爾還是會延遲或沒跑，所以才加一次備援。
每次執行會檢查最近 5 個交易日，缺的就補；某天沒抓到的資料（例如融資融券較晚公布），
下次執行時會自動補上。

在本機手動執行：

    pip install requests
    python scripts/fetch.py

### 收盤後報告

網址：`https://<GitHub 帳號>.github.io/<repo 名稱>/`，網址後面加 `#2026-10-29` 可直接開某一天。

- 加權指數 K 線（含 MA5 / MA20 / MA60 / MA100 與成交金額）、三大法人買賣超走勢，可縮放、拖曳，點 K 棒切換日期
- 當日摘要：漲跌家數、成交金額、三大法人、融資融券增減
- 排行：漲跌幅、成交金額、外資 / 投信買賣超、融資增減
- 技術訊號：創 60 日新高、MA5 與 MA20 黃金 / 死亡交叉、爆量（以還原權息價格計算）
- 當日除權息股票
- 表格裡的股票代號、名稱可以點，右上角也能輸入代號或名稱查個股

個股頁（`stock.html#2330`）包含：

- 日 K 線（可切換還原權息 / 原始價格，含 MA5 / MA20 / MA60 / MA100、布林通道、除權息標記）
- K 線下方副圖可切換成交量（張）、MACD(12, 26, 9)、KD(9)、RSI(6, 12)，指標依目前顯示的價格計算
- 三大法人買賣超、融資融券餘額、本益比 / 殖利率 / 淨值比走勢，跟 K 線同步縮放
- 月營收與年增率、歷史技術訊號、除權息紀錄，以及 Yahoo 股市的外部連結

有設定 `DISCORD_WEBHOOK_URL` 的話，每天產生新報告時會送一則摘要和大盤 K 線圖到 Discord，附上網頁連結。

> **注意**：GitHub Pages 網站是公開的，就算 repo 設為私有也一樣，任何知道網址的人都看得到。
> 報告內容只有證交所的公開資料，但不要把個人持股之類的資訊放進報告。

每天的報告存在 `site/data/days/<年>/<日期>.json`；回補歷史資料後，下次產生報告時會自動補上那些日子的報告。
個股頁的資料（`site/data/stocks/<代號>.json`，每檔一個）每次發布時從 CSV 重新產生，不進 git；
網頁點到哪一檔才下載哪一檔，一檔一年約 20 KB（壓縮後更小）。
在本機產生與預覽：

    pip install requests matplotlib
    python scripts/report.py                          # 產生報告資料
    python -m http.server --directory site            # 開 http://localhost:8000 預覽
    python scripts/report.py --notify 2026-10-29      # 送 Discord（需設定環境變數 DISCORD_WEBHOOK_URL）

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
