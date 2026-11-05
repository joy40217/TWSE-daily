// 台股收盤報告：讀取 data/index.json（每日大盤摘要）與 data/days/<年>/<日期>.json（單日報告）
// 共用的工具函式（格式、圖表設定、同步、搜尋）在 common.js

let index = [];
let dates = [];
let latest = null;   // data/latest.json：最新交易日、各資料是否已公布、除權息預告
let current = null;
let range = 126;
const cache = {};
const charts = {};

async function loadDay(d) {
  cache[d] ??= getJSON(`data/days/${d.slice(0, 4)}/${d}.json`);
  return cache[d];
}

// ---- 圖表 ----

function movingAverage(n) {
  const out = [];
  let sum = 0;
  index.forEach((d, i) => {
    sum += d.c;
    if (i >= n) sum -= index[i - n].c;
    out.push(i >= n - 1 ? { time: d.d, value: +(sum / n).toFixed(2) } : { time: d.d });
  });
  return out;
}

function buildCharts() {
  const up = css("--up"), down = css("--down");
  const alpha = (c) => c + "88";

  // 加權指數 K 線 + 均線 + 成交金額
  const main = LightweightCharts.createChart($("chart-taiex"), chartOptions());
  const candles = main.addCandlestickSeries({
    upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down,
  });
  candles.priceScale().applyOptions({ scaleMargins: { top: 0.05, bottom: 0.25 } });
  candles.setData(index.map((d) => ({ time: d.d, open: d.o, high: d.h, low: d.l, close: d.c })));
  const mas = [5, 20, 60, 100].map((n) => {
    const s = main.addLineSeries({ ...LINE, color: css(`--ma${n}`) });
    s.setData(movingAverage(n));
    return s;
  });
  const volume = main.addHistogramSeries({ priceScaleId: "vol", priceLineVisible: false, lastValueVisible: false });
  main.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
  volume.setData(index.map((d) => ({ time: d.d, value: d.v, color: alpha(d.c >= d.o ? up : down) })));

  // 三大法人合計 + 外資、投信
  const inst = LightweightCharts.createChart($("chart-inst"), chartOptions());
  const total = inst.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
  total.setData(index.map((d) => {
    if (d.f == null) return { time: d.d };
    const v = +(d.f + d.t + d.dl).toFixed(2);
    return { time: d.d, value: v, color: alpha(v >= 0 ? up : down) };
  }));
  const instLine = (key, color) => {
    const s = inst.addLineSeries({ ...LINE, color });
    s.setData(index.map((d) => (d[key] == null ? { time: d.d } : { time: d.d, value: d[key] })));
    return s;
  };
  instLine("f", css("--foreign"));
  instLine("t", css("--trust"));

  const breadth = buildBreadthChart();

  // 三張圖同步捲動、縮放
  charts.sync = syncCharts([[main, $("chart-taiex")], [inst, $("chart-inst")], [breadth, $("chart-breadth")]]);

  // 點 K 棒切換日期
  for (const c of [main, inst, breadth]) c.subscribeClick((p) => p.time && go(timeKey(p.time)));

  // 滑過時顯示當天數值，離開時顯示選取日
  main.subscribeCrosshairMove((p) => showLegend(p.time ? timeKey(p.time) : current));
  inst.subscribeCrosshairMove((p) => showInstLegend(p.time ? timeKey(p.time) : current));
  breadth.subscribeCrosshairMove((p) => showBreadthLegend(p.time ? timeKey(p.time) : current));

  Object.assign(charts, { main, inst, breadth, candles, mas, volume });
}

// 市場寬度：上市一般股票（不含 ETF）每天的站上均線比例、新高減新低、漲跌家數累積線、漲跌停家數，以及全市場融資餘額
const BREADTH = [
  ["ma", "站上均線", "站上 <span class=\"ma20\">MA20</span>、<span class=\"ma60\">MA60</span> 的股票比例（%），用還原權息收盤價計算。"],
  ["hl", "新高減新低", "創 60 日新高家數減新低家數（不限成交金額，所以比技術訊號的家數多）。"],
  ["adl", "漲跌累積線", "每天上漲家數減下跌家數的累計（ADL），看的是走勢方向，數值本身沒有意義；指數上漲但這條線往下，表示上漲集中在少數股票。"],
  ["limit", "漲跌停", "收在最高價且漲幅達 9.5% 以上算漲停，收在最低價且跌幅達 9.5% 以上算跌停（2015 年 6 月以前漲跌幅限制是 7%，門檻用 6.5%）。"],
  ["margin", "融資餘額", "全市場融資餘額（萬張，含 ETF），由各股融資餘額加總。"],
];
let breadthTab = "ma";
let adlSum = [];   // 漲跌累積線：到每一天為止的累計

function buildBreadthChart() {
  const up = css("--up"), down = css("--down");
  const chart = LightweightCharts.createChart($("chart-breadth"), chartOptions());
  const line = (color) => chart.addLineSeries({ ...LINE, lineWidth: 2, color });
  const hist = () => chart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
  // 每天都要有資料點（沒有值就留白），才能跟其他圖對齊
  const points = (f, color) => index.map((d) => {
    const v = f(d);
    return v == null ? { time: d.d } : { time: d.d, value: v, ...(color ? { color: color(v) } : {}) };
  });
  let sum = 0;
  adlSum = index.map((d) => (sum += d.ad ?? 0));
  const adl = index.map((d, i) => (d.ad == null ? { time: d.d } : { time: d.d, value: adlSum[i] }));
  const sets = {
    ma: [[line(css("--ma20")), points((d) => d.a20)], [line(css("--ma60")), points((d) => d.a60)]],
    hl: [[hist(), points((d) => (d.nh == null ? null : d.nh - d.nl), (v) => (v >= 0 ? up : down) + "aa")]],
    adl: [[line(css("--accent")), adl]],
    limit: [[hist(), points((d) => d.lu, () => up + "aa")], [hist(), points((d) => (d.ld == null ? null : -d.ld), () => down + "aa")]],
    margin: [[line(css("--financing")), points((d) => (d.mf == null ? null : +(d.mf / 1e4).toFixed(2)))]],
  };
  for (const list of Object.values(sets)) for (const [s, data] of list) s.setData(data);
  const show = (key) => {
    breadthTab = key;
    for (const [k, list] of Object.entries(sets)) for (const [s] of list) s.applyOptions({ visible: k === key });
    $("breadth-tabs").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.breadth === key));
    $("breadth-note").innerHTML = BREADTH.find(([k]) => k === key)[2] + "只算上市股票。";
    chart.priceScale("right").applyOptions({ autoScale: true });
    if (current) showBreadthLegend(current);
  };
  $("breadth-tabs").innerHTML = BREADTH.map(([k, label]) => `<button type="button" data-breadth="${k}">${label}</button>`).join("");
  $("breadth-tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-breadth]"); if (b) show(b.dataset.breadth); });
  show(breadthTab);
  return chart;
}

function showBreadthLegend(d) {
  const i = dates.indexOf(d);
  const x = index[i];
  if (!x) return;
  if (x.a20 == null && x.mf == null) return void ($("legend-breadth").textContent = `${d}　無資料`);
  const text = {
    ma: `站上 <span class="ma20">MA20 ${nf(x.a20, 1)}%</span>　<span class="ma60">MA60 ${nf(x.a60, 1)}%</span>`,
    hl: `新高 <span class="up">${nf(x.nh)}</span>　新低 <span class="down">${nf(x.nl)}</span>　相減 <b class="${tone(x.nh - x.nl)}">${signed(x.nh - x.nl)}</b>`,
    adl: `當天 <span class="${tone(x.ad)}">${signed(x.ad)}</span>　累積 ${nf(adlSum[i])}`,
    limit: `漲停 <span class="up">${nf(x.lu)}</span>　跌停 <span class="down">${nf(x.ld)}</span>`,
    margin: x.mf == null ? "融資尚未公布" : `融資餘額 ${nf(x.mf / 1e4, 2)} 萬張` +
      (i > 0 && index[i - 1].mf != null ? `　<span class="${tone(x.mf - index[i - 1].mf)}">${signed(x.mf - index[i - 1].mf)} 張</span>` : ""),
  }[breadthTab];
  $("legend-breadth").innerHTML = `${d}　${text}`;
}

function showLegend(d) {
  const i = dates.indexOf(d);
  if (i < 0) return;
  const x = index[i], prev = index[i - 1];
  const chg = prev ? x.c - prev.c : null;
  $("legend").innerHTML =
    `${d}　開 ${nf(x.o, 2)}　高 ${nf(x.h, 2)}　低 ${nf(x.l, 2)}　收 <b class="${tone(chg)}">${nf(x.c, 2)}</b>` +
    ` <span class="${tone(chg)}">${signed(chg, 2)}</span>　成交 ${nf(x.v)} 億`;
}

function showInstLegend(d) {
  const x = index[dates.indexOf(d)];
  if (!x) return;
  $("legend-inst").innerHTML = x.f == null ? `${d}　無資料` :
    `${d}　合計 <b class="${tone(x.f + x.t + x.dl)}">${signed(x.f + x.t + x.dl, 1)}</b>` +
    `　外資 <span class="${tone(x.f)}">${signed(x.f, 1)}</span>　投信 <span class="${tone(x.t)}">${signed(x.t, 1)}</span>` +
    `　自營商 <span class="${tone(x.dl)}">${signed(x.dl, 1)}</span>`;
}

function focusChart() {
  const i = dates.indexOf(current);
  const from = range ? i - range + 1 : 0;
  charts.sync.driver = charts.main;
  charts.main.timeScale().setVisibleLogicalRange({ from, to: i + 3 });
  charts.candles.setMarkers([{ time: current, position: "aboveBar", shape: "arrowDown", color: css("--accent") }]);
  showLegend(current);
  showInstLegend(current);
  showBreadthLegend(current);
}

// ---- 單日內容 ----

function card(label, value, detail = "", cls = "") {
  return `<div class="card"><div class="label">${label}</div><div class="value ${cls}">${value}</div><div class="detail">${detail}</div></div>`;
}

function renderCards(r) {
  const m = r.market, inst = r.institutional, mg = r.margin;
  $("cards").innerHTML = [
    card("加權指數", nf(m.close, 2), `<span class="${tone(m.change)}">${signed(m.change, 2)}（${signed(m.pct, 2, "%")}）</span>`, tone(m.change)),
    card("成交金額", `${nf(m.turnover)} 億`, "上市個股與 ETF 合計"),
    card("漲跌家數", `<span class="up">${m.up}</span> / <span class="down">${m.down}</span>`, `平盤 ${m.flat} 家`),
    inst
      ? card(inst.estimated ? "三大法人（億，估算）" : "三大法人（億）", signed(inst.total, 1),
          [["外資", inst.foreign], ["投信", inst.trust], ["自營", inst.dealer]]
            .map(([k, v]) => `<span class="nowrap">${k} <span class="${tone(v)}">${signed(v, 1)}</span></span>`).join("　"),
          tone(inst.total))
      : card("三大法人", "尚未公布"),
    mg
      ? card("融資增減（張）", signed(mg.financing), `融券 ${signed(mg.short)} 張`, tone(mg.financing))
      : card("融資融券", "尚未公布", "約晚間公布"),
    r.breadth
      ? card("站上 MA20", `${nf(r.breadth.above_ma20, 1)}%`,
          `<span class="nowrap">MA60 ${nf(r.breadth.above_ma60, 1)}%</span>　` +
          `<span class="nowrap">新高 <span class="up">${r.breadth.new_high}</span>／新低 <span class="down">${r.breadth.new_low}</span></span>　` +
          `<span class="nowrap">漲停 <span class="up">${r.breadth.limit_up}</span>／跌停 <span class="down">${r.breadth.limit_down}</span></span>`)
      : "",
  ].join("");
}

const STOCK_COLS = [["收盤", (s) => nf(s.close, 2)], ["漲跌%", (s) => `<span class="${tone(s.pct)}">${signed(s.pct, 2)}</span>`]];

// limit：先只顯示前幾列，其餘藏起來，下方加「顯示全部」按鈕（點了展開整個 panel）
function table(items, cols, emptyText = "沒有資料", limit = 0) {
  if (!items || !items.length) return `<div class="empty">${emptyText}</div>`;
  const head = cols.map(([h]) => `<th>${h}</th>`).join("");
  const body = items.map((s, i) =>
    `<tr${limit && i >= limit ? ` class="extra"` : ""}><td>${stockLink(s.code)}</td><td class="name" title="${esc(s.name)}">${stockLink(s.code, s.name)}</td>` +
    `${cols.map(([, f]) => `<td>${f(s)}</td>`).join("")}</tr>`
  ).join("");
  const more = limit && items.length > limit ? `<button type="button" class="more">顯示全部 ${items.length} 檔</button>` : "";
  return `<div class="table-scroll"><table><thead><tr><th>代號</th><th>名稱</th>${head}</tr></thead><tbody>${body}</tbody></table></div>${more}`;
}

const SHOW = 10;   // 排行先顯示幾檔（報告裡存 20 檔）
const panel = (title, items, cols, emptyText) => `<div class="panel"><h3>${title}</h3>${table(items, cols, emptyText, SHOW)}</div>`;

// 「顯示全部」：展開或收起所在的 panel
document.addEventListener("click", (e) => {
  const b = e.target.closest("button.more");
  if (!b) return;
  const p = b.closest(".panel");
  const open = p.classList.toggle("expanded");
  b.textContent = open ? `只顯示前 ${SHOW} 檔` : `顯示全部 ${p.querySelectorAll("tbody tr").length} 檔`;
});

function renderLists(r) {
  const L = r.lists;
  const value = ["成交（億）", (s) => nf(s.value, 1)];
  const valueM = ["成交（百萬）", (s) => nf(s.value * 100, 1)];   // 漲跌幅排行常有小型股，用百萬才看得出差異
  const net = ["買賣超（億）", (s) => `<span class="${tone(s.value)}">${signed(s.value, 2)}</span>`];
  const marginCols = [["融資增減", (s) => `<span class="${tone(s.change)}">${signed(s.change)}</span>`], ["融資餘額", (s) => nf(s.balance)]];
  const streak = [["連買", (s) => `${s.days} 天`], ["累計（張）", (s) => nf(s.lots)]];
  const sblCols = [["增減（張）", (s) => `<span class="${tone(s.change)}">${signed(s.change)}</span>`], ["餘額（張）", (s) => nf(s.balance)]];
  const noInst = "三大法人資料尚未公布", noMargin = "融資融券尚未公布（約晚間公布）", noLate = "尚未公布（約晚間公布）";
  $("lists").innerHTML = [
    panel("漲幅排行", L.gainers, [...STOCK_COLS, valueM]),
    panel("跌幅排行", L.losers, [...STOCK_COLS, valueM]),
    panel("成交金額排行", L.turnover, [...STOCK_COLS, value]),
    panel("外資買超", L.foreign_buy, [...STOCK_COLS, net], noInst),
    panel("外資賣超", L.foreign_sell, [...STOCK_COLS, net], noInst),
    panel("投信買超", L.trust_buy, [...STOCK_COLS, net], noInst),
    panel("投信賣超", L.trust_sell, [...STOCK_COLS, net], noInst),
    panel("融資增加（張）", L.margin_up, marginCols, noMargin),
    panel("融資減少（張）", L.margin_down, marginCols, noMargin),
    panel("外資連買天數", L.foreign_streak, [...STOCK_COLS, ...streak], noInst),
    panel("投信連買天數", L.trust_streak, [...STOCK_COLS, ...streak], noInst),
    panel("借券賣出增加", L.sbl_up, sblCols, noLate),
    panel("借券賣出減少", L.sbl_down, sblCols, noLate),
    panel("當沖比率", L.day_trading, [...STOCK_COLS, ["當沖%", (s) => nf(s.ratio, 1)], ["成交（億）", (s) => nf(s.value, 1)]],
      "尚未公布"),
  ].join("");
}

// 月營收：交易日前一個月以前最新的月份（每月 10 日前陸續公布，月初的名單還不完整）
let revenueTab = "yoy";

function renderRevenue(r) {
  const rv = r.revenue;
  if (!rv) return void ($("revenue").innerHTML = `<h2>月營收</h2><div class="empty">沒有月營收資料</div>`);
  const cols = [["營收（億）", (s) => nf(s.revenue, 2)], ["年增%", (s) => `<span class="${tone(s.yoy)}">${signed(s.yoy, 1)}</span>`],
    ["月增%", (s) => `<span class="${tone(s.mom)}">${signed(s.mom, 1)}</span>`], ...STOCK_COLS];
  const tabs = [["yoy", "年增率排行"], ["high", `營收創新高（${rv.high.count}）`]].map(([k, label]) =>
    `<button type="button" data-revenue="${k}" class="${k === revenueTab ? "on" : ""}">${label}</button>`).join("");
  const items = revenueTab === "yoy" ? rv.yoy : rv.high.items;
  $("revenue").innerHTML = `<div class="panel-head"><h2>月營收（${rv.month}）</h2><div class="tabs">${tabs}</div></div>` +
    table(items, cols, "沒有符合的公司", SHOW) +
    `<p class="note">上市公司當月營收 1 億元以上。創新高是比過去所有月份都高（至少要有 3 年紀錄），依營收排序` +
    (rv.high.count > rv.high.items.length ? `，共 ${rv.high.count} 家，列出前 ${rv.high.items.length} 家` : "") +
    `。收盤、漲跌是這個交易日的。</p>`;
}

const SIGNALS = [
  ["new_high", "創 60 日新高"],
  ["new_low", "創 60 日新低"],
  ["golden", "MA5 黃金交叉 MA20"],
  ["death", "MA5 死亡交叉 MA20"],
  ["volume_up", "爆量上漲"],
  ["volume_down", "爆量下跌"],
];
let signalTab = "new_high";

function renderSignals(r) {
  const s = r.signals;
  const keys = SIGNALS.filter(([k]) => s[k]);   // 舊格式的報告沒有新低、爆量上漲 / 下跌
  if (!s.volume_up && s.volume) keys.push(["volume", "爆量"]);
  if (!s[signalTab]) signalTab = keys[0][0];
  const tabs = keys.map(([k, label]) =>
    `<button type="button" data-signal="${k}" class="${k === signalTab ? "on" : ""}">${label}（${s[k].count}）</button>`).join("");
  const cur = s[signalTab];
  const more = cur.count > cur.items.length ? `共 ${cur.count} 檔，依成交金額列出前 ${cur.items.length} 檔。` : "";
  const body = table(cur.items, [...STOCK_COLS, ["成交（億）", (x) => nf(x.value, 1)]], "今天沒有符合的股票", SHOW);
  $("signals").innerHTML = `<div class="panel-head"><h2>技術訊號</h2><div class="tabs">${tabs}</div></div>${body}` +
    `<p class="note">${more}新高、新低是收盤比前 59 個交易日都高（低）；爆量是成交量達前 20 日均量 3 倍，依當天漲跌分開。</p>`;
}

function renderExRights(r) {
  const cols = [
    ["權/息", (x) => esc(x.type)],
    ["權值+息值", (x) => nf(x.amount, 4)],
    ["除權息前收盤", (x) => nf(x.before, 2)],
    ["參考價", (x) => nf(x.ref, 2)],
  ];
  $("ex-rights").innerHTML = `<h3>當日除權息</h3>${table(r.ex_rights, cols, "這天沒有除權息的股票")}`;
}

// 除權息預告只有最新一份（不分日期），所以不管看哪一天都顯示目前的預告
function renderExForecast() {
  const items = (latest?.ex_rights_forecast || []).filter((x) => x.date > latest.date);
  const cols = [
    ["日期", (x) => x.date.slice(5)],
    ["權/息", (x) => esc(x.type)],
    ["現金股利", (x) => (typeof x.cash_dividend === "number" ? nf(x.cash_dividend, 4) : `<span class="muted">${esc(x.cash_dividend) || "—"}</span>`)],
  ];
  $("ex-forecast").innerHTML = `<h3>除權息預告</h3>${table(items, cols, latest ? "目前沒有預告" : "預告資料載入失敗")}`;
}

// 類股指數依漲跌幅排序；大分類（底下還有細分類股）加上「大分類」標籤
function renderSectors(r) {
  const head = `<div class="panel-head"><h3>類股漲跌</h3></div>`;
  if (!r.sectors) return void ($("sectors").innerHTML = `${head}<div class="empty">類股指數尚未公布</div>`);
  const rows = r.sectors.map((x) =>
    `<tr><td>${esc(x.name)}${x.group ? ` <span class="tag">大分類</span>` : ""}</td><td>${nf(x.close, 2)}</td>` +
    `<td><span class="${tone(x.pct)}">${signed(x.pct, 2)}</span></td></tr>`).join("");
  $("sectors").innerHTML = `${head}<div class="table-scroll table-tall"><table><thead><tr><th>類股</th><th>收盤指數</th><th>漲跌%</th></tr></thead>` +
    `<tbody>${rows}</tbody></table></div><p class="note">「大分類」涵蓋多個類股，例如電子工業類包含半導體、光電等。</p>`;
}

function renderFlagged(r) {
  // 原因通常很長，先只顯示開頭，點一下在下方整列展開全文（手機沒有滑鼠，不能靠 title 提示；表格會橫向捲動，也不能原地展開）
  const notice = table(r.notice, [["累計", (x) => esc(x.count)],
    ["原因", (x) => `<button type="button" class="reason" aria-expanded="false">${esc(x.info)}</button>`]],
    r.notice ? "這天沒有注意股" : "尚未公布");
  const disposition = table(r.disposition, [["處置期間", (x) => esc(x.period)], ["措施", (x) => esc(x.measure)]],
    r.disposition ? "這天沒有處置中的股票" : "尚未公布");
  $("flagged").innerHTML = `<h3>處置股（處置期間內）</h3>${disposition}<h3 class="gap">注意股</h3>${notice}` +
    `<p class="note">不含權證。原因太長只顯示開頭，點一下可展開全文。</p>`;
}

$("flagged").addEventListener("click", (e) => {
  const b = e.target.closest(".reason");
  if (!b) return;
  const tr = b.closest("tr");
  const open = tr.nextElementSibling?.classList.contains("detail");
  if (open) tr.nextElementSibling.remove();
  else tr.insertAdjacentHTML("afterend", `<tr class="detail"><td colspan="${tr.cells.length}"><div>${esc(b.textContent)}</div></td></tr>`);
  b.setAttribute("aria-expanded", String(!open));
});

// 自選股：清單只存在這個瀏覽器；固定顯示最新交易日（個股摘要），不跟著上方選的日期變
async function renderWatchlist() {
  const codes = loadWatchlist();
  const el = $("watchlist");
  if (!codes.length) {
    el.innerHTML = `<h2>自選股</h2><p class="note">還沒有自選股：在個股頁按「☆ 加入自選」。清單只存在這個瀏覽器，不會上傳，換瀏覽器或清除網站資料就會不見。</p>`;
    return;
  }
  const [briefs, report] = await Promise.all([
    Promise.all(codes.map((c) => getJSON(`data/summary/${encodeURIComponent(c)}.json`).catch(() => null))),
    loadDay(dates[dates.length - 1]).catch(() => null),
  ]);
  // 最新報告的技術訊號（只有上市股票，而且每種只列成交金額前 20 檔）
  const hits = {};
  for (const [k, label] of SIGNALS) {
    for (const x of report?.signals?.[k]?.items || []) (hits[x.code] ??= []).push([label, k.includes("low") || k.includes("down") || k === "death" ? "down" : "up"]);
  }
  const rows = codes.map((c, i) => {
    const b = briefs[i];
    const remove = `<button type="button" class="unwatch" data-unwatch="${esc(c)}" aria-label="從自選移除 ${esc(c)}">✕</button>`;
    if (!b) return `<tr><td>${stockLink(c)}</td><td colspan="6" class="muted">找不到資料</td><td>${remove}</td></tr>`;
    const p = b.price || {}, inst = b.institutional;
    const flags = [...(hits[c] || []).map(([t, cls]) => `<span class="badge ${cls === "up" ? "alert" : "info"}">${t}</span>`),
      ...(b.disposition ? [`<span class="badge alert">處置中</span>`] : []),
      ...(b.notice?.length ? [`<span class="badge warn">注意股</span>`] : [])].join(" ");
    return `<tr><td>${stockLink(c)}</td><td class="name" title="${esc(b.name)}">${stockLink(c, b.name)}</td>` +
      `<td>${nf(p.close, 2)}</td><td><span class="${tone(p.pct)}">${signed(p.pct, 2)}</span></td>` +
      `<td><span class="${tone(inst?.foreign)}">${signed(inst?.foreign)}</span></td><td><span class="${tone(inst?.trust)}">${signed(inst?.trust)}</span></td>` +
      `<td class="flags">${flags || `<span class="muted">—</span>`}</td><td>${remove}</td></tr>`;
  }).join("");
  const day = briefs.find(Boolean)?.date;
  el.innerHTML = `<div class="panel-head"><h2>自選股</h2><div class="legend">${day ? `${day} 收盤` : ""}</div></div>` +
    `<div class="table-scroll"><table><thead><tr><th>代號</th><th>名稱</th><th>收盤</th><th>漲跌%</th><th>外資（張）</th><th>投信（張）</th><th>訊號、提醒</th><th></th></tr></thead>` +
    `<tbody>${rows}</tbody></table></div>` +
    `<p class="note">清單只存在這個瀏覽器，不會上傳。顯示最新交易日的資料；技術訊號只有上市股票，而且只比對報告列出的前 20 檔。在個股頁按「★ 已加入自選」可以移除。</p>`;
}

// ---- 日期切換 ----

async function go(d) {
  if (!dates.includes(d)) d = dates[dates.length - 1];
  current = d;
  if (location.hash !== `#${d}`) history.replaceState(null, "", `#${d}`);
  const i = dates.indexOf(d);
  $("date").value = d;
  $("prev").disabled = i === 0;
  $("next").disabled = i === dates.length - 1;
  focusChart();
  try {
    const r = await loadDay(d);
    if (current !== d) return;
    const w = ["日", "一", "二", "三", "四", "五", "六"][new Date(d + "T00:00:00").getDay()];
    $("subtitle").textContent = `${d}（${w}）${i === dates.length - 1 ? "・最新交易日" : ""}`;
    renderCards(r);
    renderLists(r);
    renderRevenue(r);
    renderSignals(r);
    renderSectors(r);
    renderFlagged(r);
    renderExRights(r);
  } catch (e) {
    $("subtitle").textContent = `${d} 的報告載入失敗：${e.message}`;
  }
}

async function init() {
  try {
    index = await getJSON("data/index.json");
  } catch (e) {
    $("subtitle").textContent = `報告資料載入失敗：${e.message}`;
    return;
  }
  dates = index.map((d) => d.d);
  latest = await getJSON("data/latest.json").catch(() => null);
  renderExForecast();
  $("date").innerHTML = [...dates].reverse().map((d) => `<option value="${d}">${d}</option>`).join("");
  buildCharts();

  $("date").addEventListener("change", (e) => go(e.target.value));
  $("prev").addEventListener("click", () => go(dates[dates.indexOf(current) - 1]));
  $("next").addEventListener("click", () => go(dates[dates.indexOf(current) + 1]));
  document.addEventListener("keydown", (e) => {
    if (["SELECT", "INPUT"].includes(e.target.tagName)) return;
    if (e.key === "ArrowLeft" && !$("prev").disabled) $("prev").click();
    if (e.key === "ArrowRight" && !$("next").disabled) $("next").click();
  });
  document.querySelector(".ranges").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    range = +b.dataset.range;
    document.querySelectorAll(".ranges button").forEach((x) => x.classList.toggle("on", x === b));
    focusChart();
  });
  $("signals").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-signal]");
    if (!b) return;
    signalTab = b.dataset.signal;
    renderSignals(await loadDay(current));
  });
  $("revenue").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-revenue]");
    if (!b) return;
    revenueTab = b.dataset.revenue;
    renderRevenue(await loadDay(current));
  });
  $("watchlist").addEventListener("click", (e) => {
    const b = e.target.closest("[data-unwatch]");
    if (!b) return;
    saveWatchlist(loadWatchlist().filter((c) => c !== b.dataset.unwatch));
    renderWatchlist();
  });
  renderWatchlist();
  window.addEventListener("hashchange", () => go(location.hash.slice(1)));
  setupStockSearch($("search"));

  go(location.hash.slice(1));
}

init();
