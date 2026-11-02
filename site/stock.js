// 個股頁：讀取 data/stocks/<代號>.json（欄位說明見 scripts/stock_data.py），網址為 stock.html#代號
// 共用的工具函式（格式、圖表設定、同步、搜尋）在 common.js

const SIGNAL_MIN_VALUE = 100;   // 成交金額不到 1 億（val 單位為百萬元）的訊號以灰字顯示；收盤報告則直接略過這些日子
const SIGNAL_ROWS = 30;         // 技術訊號表列最近幾筆
const EVENT_ROWS = 30;          // 事件紀錄列最近幾筆
const FORWARD = [5, 20];        // 訊號之後幾個交易日的報酬
const SIGNAL_ORDER = ["創 60 日新高", "黃金交叉", "死亡交叉", "爆量上漲", "爆量下跌", "爆量平盤"];

let S = null;          // 個股資料
let brief = null;      // 個股摘要 data/summary/<代號>.json（連買連賣天數、注意處置、除權息預告），載入失敗就不顯示
let factor = [];       // 每天的還原權息因子
let traded = [];       // 有成交的日子（索引），pos 是索引在 traded 裡的位置
let pos = new Map();
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

// ---- 技術指標：依目前顯示的價格（還原或原始）計算，沒有成交的日子跳過 ----

// 只取有成交的日子計算，再依日期展開回完整長度（沒有值的地方留白，圖表才能跟其他圖對齊）
function compact() {
  const idx = S.d.map((_, i) => i).filter((i) => S.c[i] != null);
  return { idx, o: idx.map((i) => price("o", i)), h: idx.map((i) => price("h", i)),
    l: idx.map((i) => price("l", i)), c: idx.map((i) => price("c", i)) };
}

function expand(idx, values, digits = 2) {
  const full = new Array(S.d.length).fill(null);
  idx.forEach((i, j) => { full[i] = values[j]; });
  return S.d.map((t, i) => (full[i] == null || !isFinite(full[i]) ? { time: t } : { time: t, value: +full[i].toFixed(digits) }));
}

function sma(a, n) {
  let sum = 0;
  return a.map((v, i) => {
    sum += v;
    if (i >= n) sum -= a[i - n];
    return i >= n - 1 ? sum / n : null;
  });
}

function ema(a, n) {
  const k = 2 / (n + 1);
  let e = null;
  return a.map((v) => (e = e == null ? v : e + k * (v - e)));
}

// 布林通道：20 日均線 ± 2 個標準差
function bollinger(c, n = 20, k = 2) {
  const mid = sma(c, n);
  const dev = c.map((_, i) => {
    if (mid[i] == null) return null;
    const w = c.slice(i - n + 1, i + 1);
    return Math.sqrt(w.reduce((sum, v) => sum + (v - mid[i]) ** 2, 0) / n);
  });
  return {
    mid,
    upper: mid.map((m, i) => (m == null ? null : m + k * dev[i])),
    lower: mid.map((m, i) => (m == null ? null : m - k * dev[i])),
  };
}

// MACD(12, 26, 9)：DIF = EMA12 - EMA26，MACD = DIF 的 9 日 EMA，柱狀 = DIF - MACD
function macd(c) {
  const e12 = ema(c, 12), e26 = ema(c, 26);
  const dif = c.map((_, i) => e12[i] - e26[i]);
  const dea = ema(dif, 9);
  return { dif, dea, osc: dif.map((v, i) => v - dea[i]) };
}

// KD(9, 3, 3)：台灣常用算法，RSV 取 9 日高低，K、D 以 1/3 權重平滑，起始值 50
function kd(h, l, c, n = 9) {
  let k = 50, d = 50;
  const K = [], D = [];
  c.forEach((v, i) => {
    if (i < n - 1) {
      K.push(null);
      D.push(null);
      return;
    }
    const hi = Math.max(...h.slice(i - n + 1, i + 1)), lo = Math.min(...l.slice(i - n + 1, i + 1));
    const rsv = hi === lo ? 50 : ((v - lo) / (hi - lo)) * 100;
    k = (2 / 3) * k + rsv / 3;
    d = (2 / 3) * d + k / 3;
    K.push(k);
    D.push(d);
  });
  return { K, D };
}

// RSI：Wilder 平滑
function rsi(c, n) {
  let gain = 0, loss = 0;
  return c.map((v, i) => {
    if (i === 0) return null;
    const diff = v - c[i - 1];
    const g = Math.max(diff, 0), l = Math.max(-diff, 0);
    if (i <= n) {
      gain += g / n;
      loss += l / n;
      if (i < n) return null;
    } else {
      gain = (gain * (n - 1) + g) / n;
      loss = (loss * (n - 1) + l) / n;
    }
    return loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
  });
}

// i 之後第 n 個交易日的報酬 %（還原權息收盤價，除權息不算下跌）；還沒到那天回傳 null
function forward(i, n) {
  const j = traded[pos.get(i) + n];
  return j == null ? null : (adjClose(j) / adjClose(i) - 1) * 100;
}

// 技術訊號：條件跟 scripts/report.py 的 scan_signals 相同，以還原收盤價計算；
// 但不略過成交金額低的日子（標記 thin，表格以灰字顯示），爆量再依當天漲跌分成上漲、下跌
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
    const hits = [];
    if (prevHigh != null && close > prevHigh) hits.push(["創 60 日新高", "up"]);
    if (![pm5, pm20, ma5, ma20].includes(null)) {
      if (pm5 <= pm20 && ma5 > ma20) hits.push(["黃金交叉", "up"]);
      else if (pm5 >= pm20 && ma5 < ma20) hits.push(["死亡交叉", "down"]);
    }
    const volSum = vols.reduce((x, y) => x + y, 0);
    if (vols.length === 20 && volSum > 0 && vol >= (3 * volSum) / 20) {
      const { chg } = change(i);
      hits.push(chg > 0 ? ["爆量上漲", "up"] : chg < 0 ? ["爆量下跌", "down"] : ["爆量平盤", ""]);
    }
    if (hits.length) out.push({ i, hits, thin: (S.val[i] ?? 0) < SIGNAL_MIN_VALUE });
    vols.push(vol);
    if (vols.length > 20) vols.shift();
    pm5 = ma5;
    pm20 = ma20;
  });
  return out;
}

// 每種訊號的歷史表現：之後 n 個交易日上漲的比例、平均報酬；最後一列是所有交易日，當作比較基準
function signalStats(sig) {
  const groups = new Map(SIGNAL_ORDER.map((label) => [label, []]));
  for (const { i, hits } of sig) for (const [label] of hits) groups.get(label).push(i);
  groups.set("所有交易日", traded);
  return [...groups].filter(([, list]) => list.length).map(([label, list]) => ({
    label, count: list.length,
    after: FORWARD.map((n) => {
      const r = list.map((i) => forward(i, n)).filter((v) => v != null);
      return r.length ? { up: (r.filter((v) => v > 0).length / r.length) * 100, avg: r.reduce((a, b) => a + b, 0) / r.length } : null;
    }),
  }));
}

// 事件：注意股、處置期間、無成交（停止交易或沒有人成交）；依日期由新到舊
function events() {
  const out = S.nt.map(([d, info]) => ({ d, kind: "注意股", text: "", info }));
  for (const [start, end, measure, cond] of S.dp) out.push({ d: start, kind: "處置", text: `${start} ～ ${end} ${measure}`, info: cond });
  for (const i of S.d.keys()) if (S.c[i] == null) out.push({ d: S.d[i], kind: "無成交", text: "當天沒有成交（可能暫停交易）" });
  return out.sort((a, b) => (a.d < b.d ? 1 : a.d > b.d ? -1 : 0));
}

// ---- 圖表 ----

// 無成交日沒有 K 棒、放不了標記，改標在它前一個有成交的日子（連續多天無成交只標一次）
function noTradeMarkDays() {
  const out = new Set();
  let last = null;
  S.d.forEach((d, i) => {
    if (S.c[i] != null) last = d;
    else if (last) out.add(last);
  });
  return [...out];
}

const series = (key, f = (v) => v) => S.d.map((t, i) => (S[key][i] == null ? { time: t } : { time: t, value: f(S[key][i]) }));
const has = (key) => S[key] && S[key].some((v) => v != null);

function candleData() {
  return S.d.map((t, i) => (S.c[i] == null ? { time: t } :
    { time: t, open: price("o", i), high: price("h", i), low: price("l", i), close: price("c", i) }));
}

const MA_DAYS = [5, 20, 60, 100];
const overlays = { ma: true, bb: false, ev: true };

function buildPriceChart() {
  const up = css("--up"), down = css("--down");
  const chart = LightweightCharts.createChart($("chart-price"), chartOptions());
  const candles = chart.addCandlestickSeries({
    upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down,
  });
  const mas = MA_DAYS.map((n) => [n, chart.addLineSeries({ ...LINE, color: css(`--ma${n}`) })]);
  const bb = ["upper", "mid", "lower"].map((k) =>
    [k, chart.addLineSeries({ ...LINE, color: css("--bb"), lineStyle: k === "mid" ? 2 : 0 })]);
  const exDays = new Set(S.d);
  const exMarkers = S.ex.filter(([d]) => exDays.has(d))
    .map(([d, type]) => ({ time: d, position: "belowBar", shape: "circle", color: css("--accent"), text: `除${type}` }));
  // 事件標記（可關閉）：注意股小點、處置開始日、無成交日（標在前一個交易日，沒有 K 棒的日子放不了標記）
  const evMarkers = [
    ...S.nt.filter(([d]) => exDays.has(d)).map(([d]) => ({ time: d, position: "aboveBar", shape: "circle", size: 0.5, color: css("--trust") })),
    ...S.dp.map(([start]) => S.d.find((d) => d >= start)).filter(Boolean)
      .map((d) => ({ time: d, position: "aboveBar", shape: "arrowDown", color: css("--up"), text: "處置" })),
    ...noTradeMarkDays().map((d) => ({ time: d, position: "aboveBar", shape: "square", color: css("--muted"), text: "隔日起無成交" })),
  ];
  const draw = () => {
    const p = compact();
    candles.setData(candleData());
    for (const [n, s] of mas) {
      s.setData(expand(p.idx, sma(p.c, n)));
      s.applyOptions({ visible: overlays.ma });
    }
    const band = bollinger(p.c);
    for (const [k, s] of bb) {
      s.setData(expand(p.idx, band[k]));
      s.applyOptions({ visible: overlays.bb });
    }
    candles.setMarkers([...exMarkers, ...(overlays.ev ? evMarkers : [])].sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0)));
    charts.drawIndicators(p);
  };
  Object.assign(charts, { price: chart, draw });
  return chart;
}

// OBV（能量潮）：收盤上漲加上當天成交量、下跌減去，漲跌一律用還原權息價判斷（除權息日不算下跌）
function obv(idx) {
  let sum = 0;
  return idx.map((i, j) => {
    if (j > 0) {
      const diff = adjClose(i) - adjClose(idx[j - 1]);
      sum += diff > 0 ? S.v[i] ?? 0 : diff < 0 ? -(S.v[i] ?? 0) : 0;
    }
    return sum;
  });
}

// K 線下方的副圖：成交量、MACD、KD、RSI、OBV 擇一顯示
const INDICATORS = [["vol", "成交量"], ["macd", "MACD"], ["kd", "KD"], ["rsi", "RSI"], ["obv", "OBV"]];
let indicator = "vol";

function buildIndicatorChart() {
  const up = css("--up"), down = css("--down");
  const chart = LightweightCharts.createChart($("chart-ind"), chartOptions());
  const hist = () => chart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
  const line = (color) => chart.addLineSeries({ ...LINE, color });
  const groups = {
    vol: { volume: hist(), vma5: line(css("--ma5")), vma20: line(css("--ma20")) },
    macd: { osc: hist(), dif: line(css("--ma5")), dea: line(css("--ma20")) },
    kd: { K: line(css("--ma5")), D: line(css("--ma20")) },
    rsi: { r6: line(css("--ma5")), r12: line(css("--ma20")) },
    obv: { obv: line(css("--accent")) },
  };
  // 超買、超賣參考線
  const guide = { color: css("--muted"), lineWidth: 1, lineStyle: 2, axisLabelVisible: false };
  for (const v of [20, 80]) groups.kd.K.createPriceLine({ ...guide, price: v });
  for (const v of [30, 70]) groups.rsi.r6.createPriceLine({ ...guide, price: v });

  const values = { idx: [] };
  charts.drawIndicators = (p) => {
    groups.vol.volume.setData(S.d.map((t, i) => {
      if (S.v[i] == null) return { time: t };
      const { chg } = change(i);
      return { time: t, value: S.v[i], color: (chg != null && chg < 0 ? down : up) + "88" };
    }));
    const m = macd(p.c), k = kd(p.h, p.l, p.c), vols = p.idx.map((i) => S.v[i] ?? 0);
    Object.assign(values, { idx: p.idx, dif: m.dif, dea: m.dea, osc: m.osc, K: k.K, D: k.D, r6: rsi(p.c, 6), r12: rsi(p.c, 12),
      vma5: sma(vols, 5), vma20: sma(vols, 20), obv: obv(p.idx) });
    for (const key of ["vma5", "vma20"]) groups.vol[key].setData(expand(p.idx, values[key], 0));
    groups.obv.obv.setData(expand(p.idx, values.obv, 0));
    groups.macd.osc.setData(expand(p.idx, m.osc).map((x) => ("value" in x ? { ...x, color: (x.value >= 0 ? up : down) + "88" } : x)));
    for (const key of ["dif", "dea"]) groups.macd[key].setData(expand(p.idx, values[key]));
    for (const key of ["K", "D"]) groups.kd[key].setData(expand(p.idx, values[key]));
    for (const key of ["r6", "r12"]) groups.rsi[key].setData(expand(p.idx, values[key]));
  };

  // 說明列：顯示游標所在日（沒有則最新一天）的指標數值
  let shown = S.d.length - 1;
  charts.showIndLegend = (i = shown) => {
    shown = i;
    const j = values.idx.indexOf(i);
    const v = (key) => (j < 0 ? null : values[key][j]);
    const d = S.d[i];
    $("legend-ind").innerHTML = {
      vol: `${d}　量 ${nf(S.v[i])} 張　均量 <span class="ma5">${nf(v("vma5"))}</span> / <span class="ma20">${nf(v("vma20"))}</span>`,
      macd: `${d}　DIF ${nf(v("dif"), 2)}　MACD ${nf(v("dea"), 2)}　柱 <span class="${tone(v("osc"))}">${signed(v("osc"), 2)}</span>`,
      kd: `${d}　K ${nf(v("K"), 1)}　D ${nf(v("D"), 1)}`,
      rsi: `${d}　RSI6 ${nf(v("r6"), 1)}　RSI12 ${nf(v("r12"), 1)}`,
      obv: `${d}　OBV ${nf(v("obv"))} 張`,
    }[indicator];
  };

  const tabs = $("ind-tabs");
  charts.showIndicator = (key) => {
    indicator = key;
    for (const [g, list] of Object.entries(groups)) for (const s of Object.values(list)) s.applyOptions({ visible: g === key });
    chart.applyOptions({ localization: { locale: "zh-TW", priceFormatter: (v) => nf(v, key === "vol" || key === "obv" ? 0 : 2) } });
    tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.ind === key));
    $("ind-note").innerHTML = {
      vol: `成交量，單位：張；線為 <span class="ma5">5 日</span>、<span class="ma20">20 日</span>均量。`,
      macd: `<span class="ma5">DIF</span>（EMA12 − EMA26）、<span class="ma20">MACD</span>（DIF 的 9 日 EMA），柱狀為 DIF − MACD。`,
      kd: `<span class="ma5">K</span>、<span class="ma20">D</span>（9 日），虛線為 20、80。`,
      rsi: `<span class="ma5">RSI 6</span>、<span class="ma20">RSI 12</span>，虛線為 30、70。`,
      obv: "OBV（能量潮）：收盤上漲加上當天成交量、下跌減去，看量能是否跟著價格走；漲跌用還原權息價判斷。單位：張。",
    }[key];
    charts.showIndLegend();
  };
  tabs.innerHTML = INDICATORS.map(([k, label]) => `<button type="button" data-ind="${k}">${label}</button>`).join("");
  tabs.addEventListener("click", (e) => {
    const b = e.target.closest("[data-ind]");
    if (b) charts.showIndicator(b.dataset.ind);
  });
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
  chart.applyOptions({ localization: { locale: "zh-TW", priceFormatter: (p) => nf(p) } });
  chart.addLineSeries({ ...LINE, lineWidth: 2, color: css("--financing") }).setData(series("mf"));
  // 融券數量通常小很多，用不顯示刻度的獨立座標畫出走勢（不放左側刻度，才能跟其他圖左右對齊），數值看說明列
  chart.addLineSeries({ ...LINE, lineWidth: 2, color: css("--short"), priceScaleId: "short" }).setData(series("ms"));
  chart.priceScale("short").applyOptions({ visible: false, scaleMargins: { top: 0.1, bottom: 0.1 } });
  return chart;
}

// 本益比、殖利率、淨值比、本益比河流圖用分頁切換，一次只看一種
const VALUES = [["pe", "本益比"], ["dy", "殖利率 %"], ["pb", "股價淨值比"], ["river", "本益比河流"]];
const RIVER_PCT = [10, 25, 50, 75, 90];   // 河流圖的倍數：最近約 5 年（1250 個交易日）本益比的百分位數
const RIVER_DAYS = 1250;

// 近四季 EPS 由「收盤價 ÷ 本益比」反推（也就是證交所算本益比用的 EPS）；虧損時沒有本益比，也就算不出來
const eps = (i) => (S.c[i] != null && S.pe[i] > 0 ? S.c[i] / S.pe[i] : null);

function riverMultiples() {
  const pe = S.pe.filter((v) => v > 0).slice(-RIVER_DAYS).sort((a, b) => a - b);
  return pe.length < 60 ? [] : RIVER_PCT.map((q) => +pe[Math.floor((q / 100) * (pe.length - 1))].toFixed(1));
}

function buildValueChart() {
  const mult = has("pe") ? riverMultiples() : [];
  const keys = VALUES.filter(([k]) => (k === "river" ? mult.length : has(k)));
  if (!keys.length) return null;
  const chart = LightweightCharts.createChart($("chart-value"), chartOptions());
  // 每天都要有資料點（沒有值就留白），才能跟其他圖對齊
  const line = chart.addLineSeries({ ...LINE, lineWidth: 2 });
  // 河流圖：原始收盤價（本益比是用原始價算的）與各倍數的 EPS × 本益比
  const close = chart.addLineSeries({ ...LINE, lineWidth: 2, color: css("--text") });
  close.setData(series("c"));
  const bands = mult.map((m, j) => {
    // 刻度只依收盤價縮放：獲利起伏大的股票，高倍數的線會遠高於股價，超出範圍的部分就不畫
    const s = chart.addLineSeries({ ...LINE, color: css("--pe") + ["55", "88", "ff", "88", "55"][j], lineStyle: j === 2 ? 0 : 2,
      autoscaleInfoProvider: () => null });
    s.setData(S.d.map((t, i) => (eps(i) == null ? { time: t } : { time: t, value: +(eps(i) * m).toFixed(2) })));
    return s;
  });
  const tabs = $("value-tabs");
  const show = (key) => {
    const river = key === "river";
    line.applyOptions({ visible: !river });
    if (!river) {
      line.applyOptions({ color: css(`--${key}`) });
      line.setData(series(key));
    }
    for (const s of [close, ...bands]) s.applyOptions({ visible: river });
    $("value-note").innerHTML = river ?
      `河流圖：粗線為<b>原始</b>收盤價，細線為近四季 EPS × 本益比 ${mult.join("、")} 倍` +
      `（最近約 5 年本益比的第 ${RIVER_PCT.join("、")} 百分位，實線為中位數）。EPS 由收盤價 ÷ 本益比反推，虧損時線會中斷。` :
      "虧損時沒有本益比。";
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
    [buildIndicatorChart(), "chart-ind", null],
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
  charts.draw();
  charts.showIndicator(indicator);
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
  // 標出目前是哪一種價格：還原權息價跟看盤軟體的數字不同
  const basis = `<span class="tag">${adjust ? "還原權息" : "原始價格"}</span>`;
  $("legend-price").innerHTML = S.c[i] == null ? `${d}　無成交` :
    `${d} ${basis}　開 ${nf(price("o", i), 2)}　高 ${nf(price("h", i), 2)}　低 ${nf(price("l", i), 2)}` +
    `　收 <b class="${tone(chg)}">${nf(price("c", i), 2)}</b> <span class="${tone(chg)}">${signed(chg, 2)}（${signed(pct, 2, "%")}）</span>` +
    `　量 ${nf(S.v[i])} 張　金額 ${nf(S.val[i] / 100, 2)} 億`;
  charts.showIndLegend(i);
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
      (S.ms[i] && S.mf[i] ? `　券資比 ${nf((S.ms[i] / S.mf[i]) * 100, 1)}%` : "") +
      (S.ml?.[i] ? `　融資使用率 ${nf((S.mf[i] / S.ml[i]) * 100, 2)}%` : "");
  }
  if (!$("panel-value").hidden) {
    $("legend-value").innerHTML = `${d}　本益比 ${nf(S.pe[i], 2)}　殖利率 ${S.dy[i] == null ? "—" : nf(S.dy[i], 2) + "%"}　淨值比 ${nf(S.pb[i], 2)}` +
      (has("pe") ? `　近四季 EPS ${nf(eps(i), 2)}` : "");
  }
}

function showRevLegend(ym) {
  const r = S.rev.find((x) => x[0] === ym) || S.rev[S.rev.length - 1];
  if (r) {
    $("legend-rev").innerHTML = `${r[0]}　營收 ${nf(r[1] / 1e5, 2)} 億　年增 <span class="${tone(r[2])}">${signed(r[2], 2, "%")}</span>` +
      `　月增 <span class="${tone(r[3])}">${signed(r[3], 2, "%")}</span>` +
      `　累計 ${nf(r[4] / 1e5, 2)} 億（年增 <span class="${tone(r[5])}">${signed(r[5], 2, "%")}</span>）`;
  }
}

function card(label, value, detail = "", cls = "") {
  return `<div class="card"><div class="label">${label}</div><div class="value ${cls}">${value}</div><div class="detail">${detail}</div></div>`;
}

// 外資、投信連續買超或賣超天數（2 天以上才顯示）
function streakText() {
  const st = brief?.streak;
  if (!st) return "";
  const parts = [["外資", st.foreign], ["投信", st.trust]].filter(([, n]) => Math.abs(n) >= 2)
    .map(([k, n]) => `<span class="nowrap ${tone(n)}">${k}連${n > 0 ? "買" : "賣"} ${Math.abs(n)} 天</span>`);
  return parts.length ? `<br>${parts.join("　")}` : "";
}

// 標題下方的提醒：處置中、近 5 日被列注意股、即將除權息
function renderBadges() {
  if (!brief) return;
  const out = [];
  if (brief.disposition) out.push(`<span class="badge alert" title="${esc(brief.disposition.condition)}">處置中 ${esc(brief.disposition.period)}</span>`);
  if (brief.notice?.length) {
    const last = brief.notice[brief.notice.length - 1];
    out.push(`<span class="badge warn" title="${esc(last.info)}">注意股 ${last.date.slice(5)}（近 5 日 ${brief.notice.length} 次）</span>`);
  }
  for (const x of brief.ex_rights_upcoming || []) {
    const cash = typeof x.cash_dividend === "number" ? ` 現金 ${nf(x.cash_dividend, 2)}` : "";
    out.push(`<span class="badge info">除${esc(x.type)} ${x.date.slice(5)}${cash}</span>`);
  }
  $("badges").innerHTML = out.join("");
  $("badges").hidden = !out.length;
}

function renderCards() {
  const i = S.d.length - 1;
  const { chg, pct } = change(i);
  const out = [
    card("收盤價", nf(S.c[i], 2), `<span class="${tone(chg)}">${signed(chg, 2)}（${signed(pct, 2, "%")}）</span>`, tone(chg)),
    card("成交量", `${nf(S.v[i])} 張`, `金額 ${nf(S.val[i] / 100, 2)} 億`),
  ];
  if (has("pe") || has("dy") || has("pb")) {
    out.push(card("本益比", nf(S.pe[i], 2), `殖利率 ${S.dy[i] == null ? "—" : nf(S.dy[i], 2) + "%"}　淨值比 ${nf(S.pb[i], 2)}`));
  }
  if (has("fi")) {
    const total = S.fi[i] == null ? null : S.fi[i] + (S.tr[i] ?? 0) + (S.dl[i] ?? 0);
    out.push(total == null ? card("三大法人", "尚未公布") : card("三大法人（張）", signed(total),
      [["外資", S.fi[i]], ["投信", S.tr[i]], ["自營", S.dl[i]]]
        .map(([k, v]) => `<span class="nowrap">${k} <span class="${tone(v)}">${signed(v)}</span></span>`).join("　") +
        streakText(),
      tone(total)));
  }
  if (has("mf")) {
    out.push(S.mf[i] == null ? card("融資融券", "尚未公布", "約晚間公布") :
      card("融資餘額（張）", nf(S.mf[i]),
        `增減 <span class="${tone(S.mf[i] - (S.mf[i - 1] ?? S.mf[i]))}">${signed(S.mf[i] - (S.mf[i - 1] ?? S.mf[i]))}</span>　融券 ${nf(S.ms[i])}` +
        (S.ml?.[i] ? `<br><span class="nowrap">融資使用率 ${nf((S.mf[i] / S.ml[i]) * 100, 2)}%</span>` : "")));
  }
  if (S.rev.length) {
    const [ym, v, yoy, mom, cum, cumYoy] = S.rev[S.rev.length - 1];
    out.push(card(`月營收（${ym}）`, `${nf(v / 1e5, 2)} 億`,
      `<span class="nowrap">年增 <span class="${tone(yoy)}">${signed(yoy, 2, "%")}</span></span>　` +
      `<span class="nowrap">月增 <span class="${tone(mom)}">${signed(mom, 2, "%")}</span></span>` +
      `<br><span class="nowrap">累計 ${nf(cum / 1e5, 2)} 億</span> <span class="nowrap">（年增 <span class="${tone(cumYoy)}">${signed(cumYoy, 2, "%")}</span>）</span>`));
  }
  // 波動與回檔：摘要裡用還原權息價算好的 ATR、波動率、回檔、相對大盤
  const rk = brief?.risk;
  if (rk) {
    out.push(card("ATR（14 日）", `${nf(rk.atr14, 2)}`,
      `<span class="nowrap">約股價 ${nf(rk.atr_pct, 2)}%</span>　<span class="nowrap">20 日年化波動 ${nf(rk.hv20, 1)}%</span>` +
      `<br><span class="nowrap">距一年最高收盤 <span class="${tone(rk.drawdown_250)}">${signed(rk.drawdown_250, 2, "%")}</span></span>` +
      (rk.rs_20d == null ? "" : `<br><span class="nowrap">近 20 日比大盤 <span class="${tone(rk.rs_20d)}">${signed(rk.rs_20d, 2)}</span> 個百分點</span>`)));
  }
  $("cards").innerHTML = out.join("");
}

const pctCell = (v) => `<td><span class="${tone(v)}">${signed(v, 2)}</span></td>`;
const reportLink = (d) => `<a href="index.html#${d}">${d}</a>`;

function renderTables() {
  const all = scanSignals();
  const sig = all.slice(-SIGNAL_ROWS).reverse();
  $("signals").innerHTML = `<h3>技術訊號（最近 ${SIGNAL_ROWS} 筆）</h3>` + (sig.length ?
    `<div class="table-scroll"><table><thead><tr><th>日期</th><th>訊號</th><th>收盤</th><th>漲跌%</th>` +
    FORWARD.map((n) => `<th>${n} 日後%</th>`).join("") + `</tr></thead><tbody>` +
    sig.map(({ i, hits, thin }) => `<tr${thin ? ' class="thin"' : ""}><td>${reportLink(S.d[i])}</td>` +
      `<td>${hits.map(([h, c]) => `<span class="${thin ? "" : c}">${h}</span>`).join("、")}</td>` +
      `<td>${nf(S.c[i], 2)}</td>${pctCell(change(i).pct)}${FORWARD.map((n) => pctCell(forward(i, n))).join("")}</tr>`).join("") +
    `</tbody></table></div><p class="note">灰字為成交金額不到 1 億的日子（收盤報告不列入）。之後報酬以還原權息收盤價計算，還沒到的日子顯示「—」。</p>` :
    `<div class="empty">沒有符合的訊號</div>`);

  const stats = signalStats(all);
  $("signal-stats").innerHTML = `<h3>訊號歷史表現（${S.d[0].slice(0, 4)} 年起）</h3>` +
    `<div class="table-scroll"><table><thead><tr><th>訊號</th><th>次數</th>` +
    FORWARD.map((n) => `<th>${n} 日後上漲</th><th>${n} 日後平均</th>`).join("") + `</tr></thead><tbody>` +
    stats.map(({ label, count, after }) => `<tr${label === "所有交易日" ? ' class="base"' : ""}><td>${label}</td><td>${nf(count)}</td>` +
      after.map((x) => (x ? `<td>${nf(x.up, 0)}%</td>${pctCell(x.avg)}` : "<td>—</td><td>—</td>")).join("") + `</tr>`).join("") +
    `</tbody></table></div><p class="note">「上漲」是之後報酬大於 0 的比例，「平均」是平均報酬 %；最後一列是所有交易日，用來比較訊號有沒有比隨便一天好。` +
    `只是這檔股票過去的統計，次數少時參考價值低，也沒有扣除交易成本，不代表未來表現。</p>`;

  const ev = events();
  $("events").innerHTML = `<h3>事件紀錄（最近 ${EVENT_ROWS} 筆）</h3>` + (ev.length ?
    `<div class="table-scroll"><table><thead><tr><th>日期</th><th>事件</th><th>內容</th></tr></thead><tbody>` +
    ev.slice(0, EVENT_ROWS).map((x) => `<tr><td>${reportLink(x.d)}</td><td>${x.kind}</td><td>${esc(x.text)}` +
      (x.info ? ` <button type="button" class="reason" aria-expanded="false">${esc(x.info)}</button>` : "") + `</td></tr>`).join("") +
    `</tbody></table></div><p class="note">共 ${ev.length} 筆（${S.d[0].slice(0, 4)} 年起）。原因只顯示開頭，點一下可展開全文。` +
    `無成交可能是暫停交易（例如公司有重大訊息待公布）或當天沒有人成交。</p>` :
    `<div class="empty">資料期間內沒有注意、處置或無成交紀錄</div>`);

  const ex = [...S.ex].reverse();
  $("ex-rights").innerHTML = `<h3>除權息紀錄</h3>` + (ex.length ?
    `<div class="table-scroll"><table><thead><tr><th>日期</th><th>權/息</th><th>權值+息值</th><th>除權息前收盤</th><th>參考價</th></tr></thead><tbody>` +
    ex.map(([d, type, amount, before, ref]) => `<tr><td>${reportLink(d)}</td><td>${esc(type)}</td><td>${nf(amount, 4)}</td>` +
      `<td>${nf(before, 2)}</td><td>${nf(ref, 2)}</td></tr>`).join("") +
    `</tbody></table></div>` : `<div class="empty">資料期間內沒有除權息</div>`);
}

// 事件原因點一下在下方整列展開全文（跟收盤報告的注意股相同）
$("events").addEventListener("click", (e) => {
  const b = e.target.closest(".reason");
  if (!b) return;
  const tr = b.closest("tr");
  const open = tr.nextElementSibling?.classList.contains("detail");
  if (open) tr.nextElementSibling.remove();
  else tr.insertAdjacentHTML("afterend", `<tr class="detail"><td colspan="${tr.cells.length}"><div>${esc(b.textContent)}</div></td></tr>`);
  b.setAttribute("aria-expanded", String(!open));
});

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
    [S, brief] = await Promise.all([
      getJSON(`data/stocks/${encodeURIComponent(code)}.json`),
      getJSON(`data/summary/${encodeURIComponent(code)}.json`).catch(() => null),
    ]);
  } catch (e) {
    $("title").textContent = code;
    $("subtitle").textContent = `找不到 ${code} 的資料（只有上市股票與 ETF）`;
    $("main").hidden = true;
    $("yahoo").hidden = true;
    return;
  }
  factor = adjFactors();
  traded = S.d.map((_, i) => i).filter((i) => S.c[i] != null);
  pos = new Map(traded.map((i, k) => [i, k]));
  document.title = `${S.code} ${S.name}｜個股資訊`;
  $("title").textContent = `${S.code} ${S.name}`;
  $("subtitle").textContent = [S.industry, `資料 ${S.d[0]} ～ ${S.d[S.d.length - 1]}`].filter(Boolean).join("・");
  $("back").href = `index.html#${S.d[S.d.length - 1]}`;
  $("yahoo").href = `https://tw.stock.yahoo.com/quote/${encodeURIComponent(S.code)}.TW`;

  renderBadges();
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
  document.querySelector("[data-overlay]").parentElement.addEventListener("click", (e) => {
    const b = e.target.closest("[data-overlay]");
    if (!b) return;
    overlays[b.dataset.overlay] = !overlays[b.dataset.overlay];
    b.classList.toggle("on", overlays[b.dataset.overlay]);
    b.setAttribute("aria-pressed", overlays[b.dataset.overlay]);
    charts.draw();
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
