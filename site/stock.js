// 個股頁：讀取 data/stocks/<代號>.json（欄位說明見 scripts/stock_data.py），網址為 stock.html#代號
// 共用的工具函式（格式、圖表設定、同步、搜尋）在 common.js

const SIGNAL_MIN_VALUE = 100;   // 技術訊號只看成交金額 ≥ 1 億的日子（val 單位為百萬元），跟收盤報告相同
const SIGNAL_ROWS = 30;         // 技術訊號表列最近幾筆

let S = null;          // 個股資料
let factor = [];       // 每天的還原權息因子
let adjust = true;
let range = 250;
const charts = {};

// 還原權息因子：除權息日「之前」的價格乘上之後各次（參考價 / 除權息前收盤價）的連乘積，跟 build_db.py 相同
function adjFactors() {
  const ev = S.ex.filter(([, , , before, ref]) => before > 0 && ref).map(([d, , , before, ref]) => [d, ref / before]);
  const out = new Array(S.d.length);
  let p = 1, j = ev.length - 1;
  for (let i = S.d.length - 1; i >= 0; i--) {
    while (j >= 0 && ev[j][0] > S.d[i]) p *= ev[j--][1];
    out[i] = p;
  }
  return out;
}

const price = (k, i) => (S[k][i] == null ? null : adjust ? +(S[k][i] * factor[i]).toFixed(4) : S[k][i]);
const adjClose = (i) => (S.c[i] == null ? null : S.c[i] * factor[i]);

// 漲跌用證交所公布的價差（除權息日是跟參考價比），漲跌幅 = 價差 / (收盤 - 價差)
function change(i) {
  const c = S.c[i], chg = S.chg[i];
  if (c == null || chg == null || c === chg) return { chg: null, pct: null };
  return { chg, pct: (chg / (c - chg)) * 100 };
}

function movingAverage(n) {
  const out = [];
  const win = [];
  let sum = 0;
  S.d.forEach((t, i) => {
    const c = price("c", i);
    if (c == null) return out.push({ time: t });
    win.push(c);
    sum += c;
    if (win.length > n) sum -= win.shift();
    out.push(win.length === n ? { time: t, value: +(sum / n).toFixed(2) } : { time: t });
  });
  return out;
}

// 技術訊號：條件跟 scripts/report.py 的 scan_signals 相同，以還原收盤價計算
function scanSignals() {
  const out = [];
  const closes = [], vols = [];
  let pm5 = null, pm20 = null;
  const avg = (a, n) => a.slice(-n).reduce((x, y) => x + y, 0) / n;
  S.d.forEach((day, i) => {
    const close = adjClose(i);
    if (close == null) return;
    const prevHigh = closes.length >= 59 ? Math.max(...closes.slice(-59)) : null;
    closes.push(close);
    if (closes.length > 60) closes.shift();
    const ma5 = closes.length >= 5 ? avg(closes, 5) : null;
    const ma20 = closes.length >= 20 ? avg(closes, 20) : null;
    const vol = S.v[i] ?? 0;
    if ((S.val[i] ?? 0) >= SIGNAL_MIN_VALUE) {
      const hits = [];
      if (prevHigh != null && close > prevHigh) hits.push(["創 60 日新高", "up"]);
      if (![pm5, pm20, ma5, ma20].includes(null)) {
        if (pm5 <= pm20 && ma5 > ma20) hits.push(["黃金交叉", "up"]);
        else if (pm5 >= pm20 && ma5 < ma20) hits.push(["死亡交叉", "down"]);
      }
      const volSum = vols.reduce((x, y) => x + y, 0);
      if (vols.length === 20 && volSum > 0 && vol >= (3 * volSum) / 20) hits.push(["爆量", ""]);
      if (hits.length) out.push({ i, hits });
    }
    vols.push(vol);
    if (vols.length > 20) vols.shift();
    pm5 = ma5;
    pm20 = ma20;
  });
  return out;
}

// ---- 圖表 ----

const series = (key, f = (v) => v) => S.d.map((t, i) => (S[key][i] == null ? { time: t } : { time: t, value: f(S[key][i]) }));
const has = (key) => S[key] && S[key].some((v) => v != null);

function candleData() {
  return S.d.map((t, i) => (S.c[i] == null ? { time: t } :
    { time: t, open: price("o", i), high: price("h", i), low: price("l", i), close: price("c", i) }));
}

function buildPriceChart() {
  const up = css("--up"), down = css("--down");
  const chart = LightweightCharts.createChart($("chart-price"), chartOptions());
  const candles = chart.addCandlestickSeries({
    upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down,
  });
  candles.priceScale().applyOptions({ scaleMargins: { top: 0.05, bottom: 0.25 } });
  const mas = [5, 20, 60].map((n) => [n, chart.addLineSeries({ ...LINE, color: css(`--ma${n}`) })]);
  const volume = chart.addHistogramSeries({ priceScaleId: "vol", priceLineVisible: false, lastValueVisible: false });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
  volume.setData(S.d.map((t, i) => {
    if (S.v[i] == null) return { time: t };
    const { chg } = change(i);
    return { time: t, value: S.v[i], color: (chg != null && chg < 0 ? down : up) + "88" };
  }));
  const exDays = new Set(S.d);
  const markers = S.ex.filter(([d]) => exDays.has(d))
    .map(([d, type]) => ({ time: d, position: "belowBar", shape: "circle", color: css("--accent"), text: `除${type}` }));
  const draw = () => {
    candles.setData(candleData());
    for (const [n, s] of mas) s.setData(movingAverage(n));
    candles.setMarkers(markers);
  };
  draw();
  Object.assign(charts, { price: chart, draw });
  return chart;
}

function buildInstChart() {
  if (!has("fi")) return null;
  const up = css("--up"), down = css("--down");
  const chart = LightweightCharts.createChart($("chart-inst"), chartOptions());
  chart.applyOptions({ localization: { locale: "zh-TW", priceFormatter: (p) => nf(p) } });
  const total = chart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
  total.setData(S.d.map((t, i) => {
    if (S.fi[i] == null) return { time: t };
    const v = S.fi[i] + (S.tr[i] ?? 0) + (S.dl[i] ?? 0);
    return { time: t, value: v, color: (v >= 0 ? up : down) + "88" };
  }));
  chart.addLineSeries({ ...LINE, color: css("--foreign") }).setData(series("fi"));
  chart.addLineSeries({ ...LINE, color: css("--trust") }).setData(series("tr"));
  return chart;
}

function buildMarginChart() {
  if (!has("mf")) return null;
  const chart = LightweightCharts.createChart($("chart-margin"), chartOptions());
  chart.applyOptions({ leftPriceScale: { visible: true }, localization: { locale: "zh-TW", priceFormatter: (p) => nf(p) } });
  chart.addLineSeries({ ...LINE, lineWidth: 2, color: css("--financing") }).setData(series("mf"));
  chart.addLineSeries({ ...LINE, lineWidth: 2, color: css("--short"), priceScaleId: "left" }).setData(series("ms"));
  return chart;
}

// 本益比、殖利率、淨值比用分頁切換，一次只畫一條線
const VALUES = [["pe", "本益比"], ["dy", "殖利率 %"], ["pb", "股價淨值比"]];

function buildValueChart() {
  const keys = VALUES.filter(([k]) => has(k));
  if (!keys.length) return null;
  const chart = LightweightCharts.createChart($("chart-value"), chartOptions());
  // 每天都要有資料點（沒有值就留白），才能跟其他圖對齊
  const line = chart.addLineSeries({ ...LINE, lineWidth: 2 });
  const tabs = $("value-tabs");
  const show = (key) => {
    line.applyOptions({ color: css(`--${key}`) });
    line.setData(series(key));
    tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.value === key));
  };
  tabs.innerHTML = keys.map(([k, label]) => `<button type="button" data-value="${k}">${label}</button>`).join("");
  tabs.addEventListener("click", (e) => { const b = e.target.closest("[data-value]"); if (b) show(b.dataset.value); });
  show(keys[0][0]);
  return chart;
}

// 月營收要累積 3 個月以上才畫圖，之前只列表格
function buildRevenueChart() {
  if (S.rev.length < 3) return null;
  const chart = LightweightCharts.createChart($("chart-rev"), chartOptions());
  chart.applyOptions({ leftPriceScale: { visible: true } });
  const time = (ym) => `${ym}-01`;
  chart.addHistogramSeries({ color: css("--accent") + "aa", priceLineVisible: false, lastValueVisible: false })
    .setData(S.rev.map(([ym, v]) => (v == null ? { time: time(ym) } : { time: time(ym), value: +(v / 1e5).toFixed(2) })));
  chart.addLineSeries({ ...LINE, lineWidth: 2, color: css("--yoy"), priceScaleId: "left", pointMarkersVisible: true })
    .setData(S.rev.map(([ym, , yoy]) => (yoy == null ? { time: time(ym) } : { time: time(ym), value: +yoy.toFixed(2) })));
  chart.timeScale().fitContent();
  chart.subscribeCrosshairMove((p) => showRevLegend(p.time ? timeKey(p.time).slice(0, 7) : null));
  return chart;
}

function buildCharts() {
  const daily = [
    [buildPriceChart(), "chart-price", null],
    [buildInstChart(), "chart-inst", "panel-inst"],
    [buildMarginChart(), "chart-margin", "panel-margin"],
    [buildValueChart(), "chart-value", "panel-value"],
  ];
  for (const [chart, , panelId] of daily) if (!chart && panelId) $(panelId).hidden = true;
  const live = daily.filter(([chart]) => chart);
  charts.sync = syncCharts(live.map(([chart, id]) => [chart, $(id)]));
  // 滑過任一張圖時，所有圖的說明列都顯示同一天
  for (const [chart] of live) {
    chart.subscribeCrosshairMove((p) => showLegends(p.time ? S.d.indexOf(timeKey(p.time)) : S.d.length - 1));
  }
  if (!buildRevenueChart()) {
    $("chart-rev").hidden = true;
    if (!S.rev.length) $("panel-rev").hidden = true;
  }
}

function focusRange() {
  const n = S.d.length;
  charts.sync.driver = charts.price;
  charts.price.timeScale().setVisibleLogicalRange({ from: range ? n - range : 0, to: n + 2 });
}

// ---- 說明列與摘要 ----

function showLegends(i) {
  if (i < 0) return;
  const d = S.d[i];
  const { chg, pct } = change(i);
  $("legend-price").innerHTML = S.c[i] == null ? `${d}　無成交` :
    `${d}　開 ${nf(price("o", i), 2)}　高 ${nf(price("h", i), 2)}　低 ${nf(price("l", i), 2)}` +
    `　收 <b class="${tone(chg)}">${nf(price("c", i), 2)}</b> <span class="${tone(chg)}">${signed(chg, 2)}（${signed(pct, 2, "%")}）</span>` +
    `　量 ${nf(S.v[i])} 張　金額 ${nf(S.val[i] / 100, 2)} 億`;
  if (has("fi")) {
    const total = S.fi[i] == null ? null : S.fi[i] + (S.tr[i] ?? 0) + (S.dl[i] ?? 0);
    $("legend-inst").innerHTML = total == null ? `${d}　無資料` :
      `${d}　合計 <b class="${tone(total)}">${signed(total)}</b>` +
      [["外資", S.fi[i]], ["投信", S.tr[i]], ["自營商", S.dl[i]]]
        .map(([k, v]) => `　${k} <span class="${tone(v)}">${signed(v)}</span>`).join("");
  }
  if (has("mf")) {
    $("legend-margin").innerHTML = S.mf[i] == null ? `${d}　無資料` :
      `${d}　融資 ${nf(S.mf[i])}（${signed(S.mf[i] - (S.mf[i - 1] ?? S.mf[i]))}）` +
      `　融券 ${nf(S.ms[i])}（${signed(S.ms[i] - (S.ms[i - 1] ?? S.ms[i]))}）` +
      (S.ms[i] && S.mf[i] ? `　券資比 ${nf((S.ms[i] / S.mf[i]) * 100, 1)}%` : "");
  }
  if (!$("panel-value").hidden) {
    $("legend-value").innerHTML = `${d}　本益比 ${nf(S.pe[i], 2)}　殖利率 ${nf(S.dy[i], 2)}%　淨值比 ${nf(S.pb[i], 2)}`;
  }
}

function showRevLegend(ym) {
  const r = S.rev.find((x) => x[0] === ym) || S.rev[S.rev.length - 1];
  if (r) $("legend-rev").innerHTML = `${r[0]}　營收 ${nf(r[1] / 1e5, 2)} 億　年增 <span class="${tone(r[2])}">${signed(r[2], 2, "%")}</span>`;
}

function card(label, value, detail = "", cls = "") {
  return `<div class="card"><div class="label">${label}</div><div class="value ${cls}">${value}</div><div class="detail">${detail}</div></div>`;
}

function renderCards() {
  const i = S.d.length - 1;
  const { chg, pct } = change(i);
  const out = [
    card("收盤價", nf(S.c[i], 2), `<span class="${tone(chg)}">${signed(chg, 2)}（${signed(pct, 2, "%")}）</span>`, tone(chg)),
    card("成交量", `${nf(S.v[i])} 張`, `金額 ${nf(S.val[i] / 100, 2)} 億`),
  ];
  if (has("pe") || has("dy") || has("pb")) {
    out.push(card("本益比", nf(S.pe[i], 2), `殖利率 ${nf(S.dy[i], 2)}%　淨值比 ${nf(S.pb[i], 2)}`));
  }
  if (has("fi")) {
    const total = S.fi[i] == null ? null : S.fi[i] + (S.tr[i] ?? 0) + (S.dl[i] ?? 0);
    out.push(total == null ? card("三大法人", "尚未公布") : card("三大法人（張）", signed(total),
      [["外資", S.fi[i]], ["投信", S.tr[i]], ["自營", S.dl[i]]]
        .map(([k, v]) => `<span class="nowrap">${k} <span class="${tone(v)}">${signed(v)}</span></span>`).join("　"),
      tone(total)));
  }
  if (has("mf")) {
    out.push(S.mf[i] == null ? card("融資融券", "尚未公布", "約晚間公布") :
      card("融資餘額（張）", nf(S.mf[i]),
        `增減 <span class="${tone(S.mf[i] - (S.mf[i - 1] ?? S.mf[i]))}">${signed(S.mf[i] - (S.mf[i - 1] ?? S.mf[i]))}</span>　融券 ${nf(S.ms[i])}`));
  }
  if (S.rev.length) {
    const [ym, v, yoy] = S.rev[S.rev.length - 1];
    out.push(card(`月營收（${ym}）`, `${nf(v / 1e5, 2)} 億`, `年增 <span class="${tone(yoy)}">${signed(yoy, 2, "%")}</span>`));
  }
  $("cards").innerHTML = out.join("");
}

function renderTables() {
  const sig = scanSignals().slice(-SIGNAL_ROWS).reverse();
  const reportLink = (d) => `<a href="index.html#${d}">${d}</a>`;
  $("signals").innerHTML = `<h3>技術訊號（最近 ${SIGNAL_ROWS} 筆）</h3>` + (sig.length ?
    `<div class="table-scroll"><table><thead><tr><th>日期</th><th>訊號</th><th>收盤</th><th>漲跌%</th></tr></thead><tbody>` +
    sig.map(({ i, hits }) => `<tr><td>${reportLink(S.d[i])}</td><td>${hits.map(([h, c]) => `<span class="${c}">${h}</span>`).join("、")}</td>` +
      `<td>${nf(S.c[i], 2)}</td><td><span class="${tone(change(i).pct)}">${signed(change(i).pct, 2)}</span></td></tr>`).join("") +
    `</tbody></table></div>` : `<div class="empty">沒有符合的訊號（只看成交金額 1 億以上的日子）</div>`);
  const ex = [...S.ex].reverse();
  $("ex-rights").innerHTML = `<h3>除權息紀錄</h3>` + (ex.length ?
    `<div class="table-scroll"><table><thead><tr><th>日期</th><th>權/息</th><th>權值+息值</th><th>除權息前收盤</th><th>參考價</th></tr></thead><tbody>` +
    ex.map(([d, type, amount, before, ref]) => `<tr><td>${reportLink(d)}</td><td>${esc(type)}</td><td>${nf(amount, 4)}</td>` +
      `<td>${nf(before, 2)}</td><td>${nf(ref, 2)}</td></tr>`).join("") +
    `</tbody></table></div>` : `<div class="empty">資料期間內沒有除權息</div>`);
}

// ---- 載入 ----

async function init() {
  setupStockSearch($("search"));
  // 換股票時整頁重新載入，圖表與事件都重新建立
  window.addEventListener("hashchange", () => location.reload());
  const code = decodeURIComponent(location.hash.slice(1)).trim().toUpperCase();
  if (!code) {
    $("subtitle").textContent = "在上方輸入股票代號或名稱";
    $("main").hidden = true;
    $("yahoo").hidden = true;
    return;
  }
  try {
    S = await getJSON(`data/stocks/${encodeURIComponent(code)}.json`);
  } catch (e) {
    $("title").textContent = code;
    $("subtitle").textContent = `找不到 ${code} 的資料（只有上市股票與 ETF）`;
    $("main").hidden = true;
    $("yahoo").hidden = true;
    return;
  }
  factor = adjFactors();
  document.title = `${S.code} ${S.name}｜個股資訊`;
  $("title").textContent = `${S.code} ${S.name}`;
  $("subtitle").textContent = [S.industry, `資料 ${S.d[0]} ～ ${S.d[S.d.length - 1]}`].filter(Boolean).join("・");
  $("back").href = `index.html#${S.d[S.d.length - 1]}`;
  $("yahoo").href = `https://tw.stock.yahoo.com/quote/${encodeURIComponent(S.code)}.TW`;

  renderCards();
  buildCharts();
  requestAnimationFrame(focusRange);   // 等圖表取得實際寬度後再設定範圍
  showLegends(S.d.length - 1);
  showRevLegend(null);
  renderTables();

  document.querySelector(".ranges").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    range = +b.dataset.range;
    document.querySelectorAll(".ranges button").forEach((x) => x.classList.toggle("on", x === b));
    focusRange();
  });
  document.querySelector("[data-adjust]").parentElement.addEventListener("click", (e) => {
    const b = e.target.closest("[data-adjust]");
    if (!b) return;
    adjust = b.dataset.adjust === "1";
    document.querySelectorAll("[data-adjust]").forEach((x) => x.classList.toggle("on", x === b));
    charts.draw();
    showLegends(S.d.length - 1);
  });
}

init();
