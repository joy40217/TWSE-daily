# TWSE-daily

每個交易日由 GitHub Actions 自動抓取臺灣證交所資料，存成 CSV 累積歷史資料，
並可在本機匯入 SQLite、產生還原權息日 K 做技術分析。

## 資料集

| 資料夾 | 內容 | 證交所報表 |
|---|---|---|
| `stock_day_all/` | 個股日成交（K 線開高低收量） | [每日收盤行情](https://www.twse.com.tw/zh/trading/historical/mi-index.html) |
| `bwibbu_all/` | 本益比 / 殖利率 / 淨值比 | [個股日本益比、殖利率及股價淨值比](https://www.twse.com.tw/zh/trading/historical/bwibbu-day.html) |
| `margin/` | 融資融券 | [融資融券餘額](https://www.twse.com.tw/zh/trading/margin/mi-margn.html) |
| `institutional/` | 三大法人買賣超（不含權證） | [三大法人買賣超日報](https://www.twse.com.tw/zh/trading/foreign/t86.html) |
| `institutional_amount/` | 三大法人買賣金額（大盤實際金額） | [三大法人買賣金額統計表](https://www.twse.com.tw/zh/trading/foreign/bfi82u.html) |
| `sector_index/` | 加權指數與各類股指數收盤、漲跌 | [每日收盤行情](https://www.twse.com.tw/zh/trading/historical/mi-index.html)（類股指數） |
| `notice/` | 注意股（當天被列入的股票與原因；沒有也會產生只有標題的檔案） | [公布注意有價證券資訊](https://www.twse.com.tw/zh/announcement/notice.html) |
| `disposition/` | 處置股（當天在處置期間內的股票；同上） | [公布處置有價證券資訊](https://www.twse.com.tw/zh/announcement/punish.html) |
| `taiex/` | 加權指數日 K | [發行量加權股價指數歷史資料](https://www.twse.com.tw/zh/indices/taiex/mi-5min-hist.html) |
| `ex_rights/` | 除權除息計算結果（只在有除權息的日子產生） | [除權除息計算結果表](https://www.twse.com.tw/zh/announcement/ex-right/twt49u.html) |
| `ex_rights_forecast/` | 除權除息預告（只保留最新一份） | [除權除息預告表](https://www.twse.com.tw/zh/announcement/ex-right/twt48u.html) |
| `day_trading/` | 個股當日沖銷成交股數與買賣金額（2017 年以前可當沖的股票較少） | [當日沖銷交易標的及成交量值](https://www.twse.com.tw/zh/trading/day-trading/twtb4u.html) |
| `sbl_short/` | 借券賣出餘額（含融券，單位：股） | [信用額度總量管制餘額表](https://www.twse.com.tw/zh/trading/margin/twt93u.html) |
| `monthly_revenue/` | 上市公司月營收（依資料年月存檔） | [OpenAPI](https://openapi.twse.com.tw/) 上市公司每月營業收入彙總表；歷史月份從[公開資訊觀測站](https://mopsov.twse.com.tw/nas/t21/sii/t21sc03_115_9_0.html)回補 |
| `income_statement/` | 季報綜合損益表（`<年>-Q<季>.csv`，金額千元，當年累計） | [公開資訊觀測站](https://mopsov.twse.com.tw/mops/web/t163sb04) 綜合損益表彙總 |
| `shareholding/` | 集保戶股權分散表（每週一份，只留上市股票與 ETF） | [集保結算所開放資料](https://opendata.tdcc.com.tw/) 集保戶股權分散表 |
| `holiday_schedule/` | 休市日程（只保留最新一份） | [OpenAPI](https://openapi.twse.com.tw/) 有價證券集中交易市場開（休）市日期 |

檔案路徑為 `data/<資料夾>/<年>/<日期>.csv`，例如 `data/stock_day_all/2026/2026-10-29.csv`；
日期是資料本身的交易日，假日不會產生檔案。已存在的檔案通常不會被覆寫，但以下資料集最近 2 個交易日每次都重新查、有資料就覆寫：

- 行情、本益比、融資融券、三大法人：公布後內容可能還會更正（例如 2026-10-30 行情 13:56 公布、14:53 又變了一次），
  重查後晚上的版本會蓋掉下午的版本；內容沒變（三大法人買賣超相同的股票，每次查詢的排列順序可能不同）
  或還沒公布時查到空的，都不會覆寫，所以 git 紀錄裡看得到的變動就是真的更正
- 注意股、處置股：當天沒有也是正常的，傍晚查到空的不代表晚點沒有

另外幾個不是每天一份的資料集：

- 月營收：OpenAPI 只有最新一個月，月初公司陸續申報，內容有變（多了公司或更正）就覆寫
- 季報：每次重查最近兩季（季報在季度結束後一個半月內陸續公布），內容有變就覆寫
- 集保股權分散：每週一份，開放資料只提供最新一週，**沒有辦法回補**，從開始抓的那週起累積

證交所的資料都是用網站的查詢功能依日期抓取，收盤後當天就有資料，
欄位也轉成跟 OpenAPI 相同的格式。OpenAPI 的每日行情要到隔天才更新，
所以只用在月營收、休市日程這類沒有日期問題的資料。
季報和集保不是證交所的網站，連不上時只顯示警告、不算抓取失敗，下次執行再試。

## 設定步驟

1. 在 GitHub 建立 repo，把這些檔案推上去
2. 到 Settings → Actions → General → Workflow permissions，選 **Read and write permissions**
3. 到 Settings → Pages → Build and deployment → Source，選 **GitHub Actions**（收盤後報告網頁用）
4. （選用）到 Settings → Secrets and variables → Actions 新增 `DISCORD_WEBHOOK_URL`，
   值為 Discord 頻道的 Webhook 網址（頻道設定 → 整合 → Webhook → 新 Webhook → 複製網址）
5. 到 Actions 分頁手動執行一次「每日抓取證交所資料」，確認正常
6. （建議）設定 Cloudflare Worker 準時觸發，見下方「準時觸發（Cloudflare Workers）」；沒設定的話只靠 GitHub 備援排程，資料會晚 4～5 小時

## 使用方式

### 每日自動抓取

不需要手動操作。「每日抓取證交所資料」workflow 每個平日執行四次：

| 台灣時間 | 觸發方式 | 做什麼 |
|---|---|---|
| 15:07 | Cloudflare Worker | 抓加權指數、個股收盤行情（約 14:00 公布），先更新網頁（不送通知）；三大法人、本益比等這時還沒公布 |
| 18:23 | Cloudflare Worker | 抓本益比（約 17:10）、三大法人（約 16:20）等，更新網頁（不送通知） |
| 22:23 | Cloudflare Worker | 再抓一次（補上晚間公布的融資融券，通常約 21:00 前公布），更新網頁、送 Discord 通知 |
| 23:47 | GitHub 排程（備援） | 跟 22:23 做一樣的事；實際多半凌晨 4～5 點才跑，Worker 沒觸發成功時最晚隔天清晨補上 |

每次執行都會更新網頁；Discord 通知只在最新交易日的報告「第一次有融資融券」時送一次，
所以傍晚先更新的網頁不會觸發通知，晚上補齊後才通知，備援再跑一次也不會重複通知。

為什麼不用 GitHub 自己的排程：GitHub 排程在負載高時會延遲，
2026 年 6～10 月實測每天 UTC 00:00 的排程平均晚 4 小時才跑（10 月約 4.8 小時，最久 10.7 小時），
避開整點也一樣。workflow_dispatch（手動或用 API 觸發）則幾秒內就開始，所以改由 Worker 準時觸發。

每次執行會檢查最近 5 個交易日，缺的就補；某天沒抓到的資料（例如融資融券較晚公布），
下次執行時會自動補上。

在本機手動執行：

    pip install requests
    python scripts/fetch.py

### 準時觸發（Cloudflare Workers）

用 Cloudflare Workers 免費方案的 Cron Trigger，準時呼叫 GitHub API 觸發 workflow。程式在 `cloudflare/dispatch.js`。

**1. 建立 GitHub token**

1. GitHub 右上角頭像 → Settings → 左側最下面 Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token
2. Token name 填 `twse-daily-dispatch`；Expiration 選最長的期限（到期前 GitHub 會寄信提醒，到期後依同樣步驟換新 token）
3. Repository access 選 **Only select repositories**，只勾這個 repo
4. Permissions → Repository permissions → **Actions** 選 **Read and write**（Metadata 會自動變成 Read-only，其他都不用開）
5. 產生後複製 token（只會顯示這一次）

這個 token 只能觸發、取消這個 repo 的 workflow，改不了程式碼，也碰不到其他 repo。

**2. 建立 Worker**

1. 註冊並登入 [Cloudflare](https://dash.cloudflare.com/)，左側選 **Workers & Pages** → Create → Create Worker（從 Hello World 開始）
2. 名稱填 `twse-daily-dispatch` → Deploy
3. 按 **Edit code**，把編輯器裡的程式全部換成 `cloudflare/dispatch.js` 的內容 → Deploy
4. 回到 Worker 頁面 → Settings → **Variables and Secrets** → Add：Type 選 **Secret**，名稱 `GITHUB_TOKEN`，值貼上第 1 步的 token → Deploy
5. Settings → **Trigger Events** → Add → **Cron Triggers**，加入三個（Cloudflare 也用 UTC）。
   星期要寫 `MON-FRI`：Cloudflare 的星期從 1 = 週日算起，寫 `1-5` 會變成週日到週四，跟 GitHub 不同。
   加好後看旁邊的「Runs」，應該顯示 Monday through Friday：

   | Cron | 台灣時間 |
   |---|---|
   | `7 7 * * MON-FRI` | 15:07 |
   | `23 10 * * MON-FRI` | 18:23 |
   | `23 14 * * MON-FRI` | 22:23 |

**3. 確認有沒有成功**

- 到 Worker 的 Logs（或 Observability）看執行紀錄，成功會印出「已觸發 daily.yml」
- 到 GitHub Actions 分頁看，Worker 觸發的執行會顯示為 `workflow_dispatch`
- 失敗時 Logs 會顯示 GitHub 的回應：401 是 token 貼錯或過期，403、404 多半是 token 沒開 Actions 權限或沒選到這個 repo

想調整觸發時間，改 Cron Triggers 即可，不用動 repo；記得同步更新 `cloudflare/dispatch.js` 開頭的註解和上面的排程表。

### 收盤後報告

網址：`https://<GitHub 帳號>.github.io/<repo 名稱>/`，網址後面加 `#2026-10-29` 可直接開某一天。

- 加權指數 K 線（含 MA5 / MA20 / MA60 / MA100 與成交金額）、三大法人買賣超走勢，可縮放、拖曳，點 K 棒切換日期
- 當日摘要：漲跌家數、成交金額、三大法人（證交所實際金額）、融資融券增減
- 類股漲跌（依漲跌幅排序）、注意股與處置股、除權息預告
- 排行：漲跌幅、成交金額、外資 / 投信買賣超、融資增減
- 技術訊號：創 60 日新高、MA5 與 MA20 黃金 / 死亡交叉、爆量（以還原權息價格計算）
- 當日除權息股票
- 表格裡的股票代號、名稱可以點，右上角也能輸入代號或名稱查個股

個股頁（`stock.html#2330`）包含：

- 日 K 線（可切換還原權息 / 原始價格，說明列會標出目前是哪一種；含 MA5 / MA20 / MA60 / MA100、布林通道、除權息標記，
  以及可關閉的事件標記：注意股、處置開始日、無成交）
- K 線下方副圖可切換成交量（含 5 / 20 日均量）、MACD(12, 26, 9)、KD(9)、RSI(6, 12)、OBV，指標依目前顯示的價格計算
- 三大法人買賣超、融資融券餘額（含融資使用率）、當沖比率 / 借券賣出餘額 / 千張與 400 張大戶持股、
  本益比 / 殖利率 / 淨值比走勢與本益比河流圖，跟 K 線同步縮放
- 月營收（年增、月增、累計年增）、季報（單季 EPS、毛利率、營益率、淨利率）、除權息紀錄，以及 Yahoo 股市的外部連結
- 技術訊號：條件跟收盤報告相同，但成交金額不到 1 億的日子也列出（灰字）；爆量分成上漲、下跌，並列出之後 5 / 20 日的報酬
- 訊號歷史表現：每種訊號之後 5 / 20 日上漲的比例與平均報酬，跟「所有交易日」比較
- 事件紀錄：注意股（含原因）、處置期間、無成交的日子
- 各張日線圖連動：游標所在那一天在每張圖都畫直線，主要的線的端點（K 棒為最高、最低，直方圖為柱子頂端）拉橫線到刻度，
  用各線的顏色標出數值（橫線固定在數值上，不跟著滑鼠上下移動）；K 線的最高、最低、均線、布林通道，以及河流圖的收盤價和各倍數，
  數值寫在圖表頂端、刻度不另外標，
  均量、MACD 柱、外資投信等看上方說明列，刻度才不會擠滿標籤。月營收、季報圖也一樣（各自連動）
- 融資融券圖的融券刻度在左側；為了讓各張日線圖的日期上下對齊，每張日線圖都留同樣寬的左側刻度
- 點一下圖表會用藍色直線、藍色虛線和深色標籤標記那一天（游標移開後說明列回到那一天，再點一次取消）；
  技術訊號、事件、除權息表格的日期點一下會跳到 K 線並標記那一天，旁邊的 ↗ 開當天的收盤報告
- 圖表拖曳到最舊、最新一筆資料就停住，不會拉出空白
- 摘要卡片：ATR(14)、20 日年化波動率、距一年最高收盤的回檔、近 20 日相對大盤強弱
- 標題下方提醒處置中、近 5 日被列注意股、即將除權息；三大法人卡片顯示外資、投信連買 / 連賣天數

還原權息價是把除權息日以前的價格依參考價往下調，讓配息配股不會在 K 線、均線、報酬率上變成假的下跌；
最新一天等於實際價格，但以前的價格會跟看盤軟體（原始價格）不同。技術訊號、風險指標一律用還原權息價計算。

### 給排程任務讀的資料

開盤前、收盤後的報告任務可以直接讀網站上的 JSON，不必自己整理：

| 網址（接在網站網址後面） | 內容 |
|---|---|
| `data/latest.json` | 最新交易日 `date`、產生時間 `generated_at`、各資料是否已公布 `available`（例如 `margin: false` 表示融資融券還沒出來）、除權息預告 |
| `data/days/<年>/<日期>.json` | 單日報告：大盤、三大法人、融資融券、排行、類股、注意 / 處置股、技術訊號 |
| `data/summary/<代號>.json` | 個股摘要（約 3 KB）：最新行情、均線（還原 `ma` 與原始 `ma_raw`）、60 日高低（還原收盤 `high_60` / `low_60` 與原始盤中 `range_60`）、風險指標 `risk`（ATR、波動率、回檔、相對大盤）、三大法人與連買連賣天數、5 / 20 日法人合計、融資融券與融資使用率、當沖比率 `day_trading`、借券賣出 `sbl_short`、千張大戶 `shareholding`、最新一季財報 `financials`、本益比、月營收（年增、月增、累計）、注意 / 處置、除權息預告、最近 20 天走勢；`basis` 欄位說明哪些數字用還原權息價 |

建議任務先讀 `latest.json`，確認 `date` 是當天、需要的資料 `available` 為 true，再讀其他檔案，
避免把前一天的數字當成今天的。自選股清單放在任務那一端就好：每一檔股票都有摘要，
所以網站上看不出你追蹤哪些股票。

有設定 `DISCORD_WEBHOOK_URL` 的話，每天產生新報告時會送一則摘要和大盤 K 線圖到 Discord，附上網頁連結。

> **注意**：GitHub Pages 網站是公開的，就算 repo 設為私有也一樣，任何知道網址的人都看得到。
> 報告內容只有證交所的公開資料，但不要把個人持股之類的資訊放進報告。

每天的報告存在 `site/data/days/<年>/<日期>.json`；回補歷史資料後，下次產生報告時會自動補上那些日子的報告。
個股頁的資料（`site/data/stocks/<代號>.json`，每檔一個）、個股摘要與 `latest.json` 每次發布時從 CSV 重新產生，不進 git；
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

- 可回補上表中所有「網站」來源的資料集，以及從公開資訊觀測站回補月營收、季報（`--only monthly_revenue,income_statement`，
  2016 年起約 20 分鐘）；集保股權分散沒有歷史資料可補。格式跟每日抓的完全相同
- 已存在的檔案會略過，中斷後重跑即可接著補
- 每次查詢間隔 3 秒，連續失敗 5 次（多半是被證交所暫時封鎖）會自動停止
- 每次查詢連同等待約 5～6 秒；每個交易日要查 10 個資料集，一年約 3 小時 25 分～4 小時、約 100 MB（git 壓縮後約一半以下）
- workflow 單次上限 350 分鐘，**一次補一年**；逾時被中止時不保證會提交，快到上限還沒跑完就手動取消（取消會先提交已補的部分），再用同樣設定重跑接著補
- 已經補過的年份只要補新增的資料集：`--only institutional_amount,sector_index,notice,disposition`，一年約 1 小時 20 分～1 小時 40 分，一次最多補三年
- 當沖、借券賣出（`--only day_trading,sbl_short`）：一年約 40～50 分鐘，一次可以補五、六年
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
- 每季一份：寫一個 `函式(年, 季) -> list[dict]`，登記為 `{"by_quarter": 函式}`；每週只有最新一份：`{"weekly": 函式}`，函式回傳 `(日期, rows)`
- 不是證交所的來源可加上 `"optional": True`，連不上時只顯示警告
