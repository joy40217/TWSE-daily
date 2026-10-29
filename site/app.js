// 台股收盤報告：讀取 data/index.json（每日大盤摘要）與 data/days/<年>/<日期>.json（單日報告）
//
// 圖表使用 Lightweight Charts（Apache License 2.0），依授權附上 NOTICE：
//   TradingView Lightweight Charts™
//   Copyright (с) 2023 TradingView, Inc. https://www.tradingview.com/
// 頁尾已有連到 tradingview.com 的標示，所以關掉圖表上的 attribution logo。
const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const nf = (v, d = 0) => (v == null ? "—" : v.toLocaleString("zh-TW", { minimumFractionDigits: d, maximumFractionDigits: d }));
const signed = (v, d = 0, unit = "") => (v == null ? "—" : (v > 0 ? "+" : "") + nf(v, d) + unit);
const tone = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const timeKey = (t) => (typeof t === "string" ? t : `${t.year}-${String(t.month).padStart(2, "0")}-${String(t.day).padStart(2, "0")}`);

let index = [];
let dates = [];
let current = null;
let range = 126;
const cache = {};
const charts = {};

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}：HTTP ${r.status}`);
  return r.json();
}

async function loadDay(d) {
  cache[d] ??= getJSON(`data/days/${d.slice(0, 4)}/${d}.json`);
  return cache[d];
}

// ---- 圖表 ----

function chartOptions() {
  return {
    autoSize: true,
    layout: {
      background: { color: "transparent" }, textColor: css("--muted"),
      fontFamily: getComputedStyle(document.body).fontFamily, attributionLogo: false,
    },
    grid: { vertLines: { color: css("--grid") }, horzLines: { color: css("--grid") } },
    rightPriceScale: { borderVisible: false },
    timeScale: { borderVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    localization: { locale: "zh-TW", priceFormatter: (p) => nf(p, 2) },
  };
}

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
  const line = { lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
  const mas = [5, 20, 60].map((n) => {
    const s = main.addLineSeries({ ...line, color: css(`--ma${n}`) });
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
    const s = inst.addLineSeries({ ...line, color });
    s.setData(index.map((d) => (d[key] == null ? { time: d.d } : { time: d.d, value: d[key] })));
    return s;
  };
  instLine("f", css("--foreign"));
  instLine("t", css("--trust"));

  // 兩張圖同步捲動、縮放（資料日期相同，可以直接對齊 logical range）。
  // 範圍變更是非同步套用的，雙向同步會互相蓋回舊範圍，所以只由正在操作的那張圖帶動另一張
  const pairs = [[main, inst, "chart-taiex"], [inst, main, "chart-inst"]];
  for (const [from, to, id] of pairs) {
    for (const ev of ["pointerdown", "pointerenter", "wheel", "touchstart"]) {
      $(id).addEventListener(ev, () => { charts.driver = from; }, { passive: true });
    }
    from.timeScale().subscribeVisibleLogicalRangeChange((r) => {
      if (r && charts.driver === from) to.timeScale().setVisibleLogicalRange(r);
    });
  }
  charts.driver = main;

  // 點 K 棒切換日期
  for (const c of [main, inst]) c.subscribeClick((p) => p.time && go(timeKey(p.time)));

  // 滑過時顯示當天數值，離開時顯示選取日
  main.subscribeCrosshairMove((p) => showLegend(p.time ? timeKey(p.time) : current));
  inst.subscribeCrosshairMove((p) => showInstLegend(p.time ? timeKey(p.time) : current));

  Object.assign(charts, { main, inst, candles, mas, volume });
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
  charts.driver = charts.main;
  charts.main.timeScale().setVisibleLogicalRange({ from, to: i + 3 });
  charts.candles.setMarkers([{ time: current, position: "aboveBar", shape: "arrowDown", color: css("--accent") }]);
  showLegend(current);
  showInstLegend(current);
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
      ? card("三大法人（億）", signed(inst.total, 1),
          [["外資", inst.foreign], ["投信", inst.trust], ["自營", inst.dealer]]
            .map(([k, v]) => `<span class="nowrap">${k} <span class="${tone(v)}">${signed(v, 1)}</span></span>`).join("　"),
          tone(inst.total))
      : card("三大法人", "尚未公布"),
    mg
      ? card("融資增減（張）", signed(mg.financing), `融券 ${signed(mg.short)} 張`, tone(mg.financing))
      : card("融資融券", "尚未公布", "約晚間公布"),
  ].join("");
}

const STOCK_COLS = [["收盤", (s) => nf(s.close, 2)], ["漲跌%", (s) => `<span class="${tone(s.pct)}">${signed(s.pct, 2)}</span>`]];

function table(items, cols, emptyText = "沒有資料") {
  if (!items || !items.length) return `<div class="empty">${emptyText}</div>`;
  const head = cols.map(([h]) => `<th>${h}</th>`).join("");
  const body = items.map((s) =>
    `<tr><td>${esc(s.code)}</td><td class="name" title="${esc(s.name)}">${esc(s.name)}</td>${cols.map(([, f]) => `<td>${f(s)}</td>`).join("")}</tr>`
  ).join("");
  return `<div class="table-scroll"><table><thead><tr><th>代號</th><th>名稱</th>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

const panel = (title, items, cols, emptyText) => `<div class="panel"><h3>${title}</h3>${table(items, cols, emptyText)}</div>`;

function renderLists(r) {
  const L = r.lists;
  const value = ["成交（億）", (s) => nf(s.value, 1)];
  const net = ["買賣超（億）", (s) => `<span class="${tone(s.value)}">${signed(s.value, 2)}</span>`];
  const marginCols = [["融資增減", (s) => `<span class="${tone(s.change)}">${signed(s.change)}</span>`], ["融資餘額", (s) => nf(s.balance)]];
  const noInst = "三大法人資料尚未公布", noMargin = "融資融券尚未公布（約晚間公布）";
  $("lists").innerHTML = [
    panel("漲幅排行", L.gainers, [...STOCK_COLS, value]),
    panel("跌幅排行", L.losers, [...STOCK_COLS, value]),
    panel("成交金額排行", L.turnover, [...STOCK_COLS, value]),
    panel("外資買超", L.foreign_buy, [...STOCK_COLS, net], noInst),
    panel("外資賣超", L.foreign_sell, [...STOCK_COLS, net], noInst),
    panel("投信買超", L.trust_buy, [...STOCK_COLS, net], noInst),
    panel("投信賣超", L.trust_sell, [...STOCK_COLS, net], noInst),
    panel("融資增加（張）", L.margin_up, marginCols, noMargin),
    panel("融資減少（張）", L.margin_down, marginCols, noMargin),
  ].join("");
}

const SIGNALS = [
  ["new_high", "創 60 日新高"],
  ["golden", "MA5 黃金交叉 MA20"],
  ["death", "MA5 死亡交叉 MA20"],
  ["volume", "爆量（≥ 20 日均量 3 倍）"],
];
let signalTab = "new_high";

function renderSignals(r) {
  const s = r.signals;
  const tabs = SIGNALS.map(([k, label]) =>
    `<button type="button" data-signal="${k}" class="${k === signalTab ? "on" : ""}">${label}（${s[k].count}）</button>`).join("");
  const cur = s[signalTab];
  const more = cur.count > cur.items.length ? `<p class="note">共 ${cur.count} 檔，依成交金額列出前 ${cur.items.length} 檔。</p>` : "";
  const body = table(cur.items, [...STOCK_COLS, ["成交（億）", (x) => nf(x.value, 1)]], "今天沒有符合的股票");
  $("signals").innerHTML = `<div class="panel-head"><h2>技術訊號</h2><div class="tabs">${tabs}</div></div>${body}${more}`;
}

function renderExRights(r) {
  const cols = [
    ["權/息", (x) => esc(x.type)],
    ["權值+息值", (x) => nf(x.amount, 4)],
    ["除權息前收盤", (x) => nf(x.before, 2)],
    ["參考價", (x) => nf(x.ref, 2)],
  ];
  $("ex-rights").innerHTML = `<div class="panel-head"><h2>今日除權息</h2></div>${table(r.ex_rights, cols, "今天沒有除權息的股票")}`;
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
    renderSignals(r);
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
  $("date").innerHTML = [...dates].reverse().map((d) => `<option value="${d}">${d}</option>`).join("");
  buildCharts();

  $("date").addEventListener("change", (e) => go(e.target.value));
  $("prev").addEventListener("click", () => go(dates[dates.indexOf(current) - 1]));
  $("next").addEventListener("click", () => go(dates[dates.indexOf(current) + 1]));
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "SELECT") return;
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
  window.addEventListener("hashchange", () => go(location.hash.slice(1)));
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => location.reload());

  go(location.hash.slice(1));
}

init();
