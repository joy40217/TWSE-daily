// 收盤報告與個股頁共用的工具：數字格式、圖表設定、圖表同步、個股搜尋
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
const stockLink = (code, text = code) => `<a class="stock" href="stock.html#${encodeURIComponent(code)}">${esc(text)}</a>`;

// 自選股：只存在這個瀏覽器（localStorage），不會上傳，也不在 repo 或網站上。無痕視窗、封鎖網站資料時存不了，當成空的
const WATCH_KEY = "twse-daily-watchlist";
function loadWatchlist() {
  try {
    const list = JSON.parse(localStorage.getItem(WATCH_KEY));
    return Array.isArray(list) ? list.filter((c) => typeof c === "string") : [];
  } catch {
    return [];
  }
}
function saveWatchlist(list) {
  try {
    localStorage.setItem(WATCH_KEY, JSON.stringify(list));
    return true;
  } catch {
    return false;
  }
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}：HTTP ${r.status}`);
  return r.json();
}

function chartOptions() {
  return {
    autoSize: true,
    layout: {
      background: { color: "transparent" }, textColor: css("--muted"),
      fontFamily: getComputedStyle(document.body).fontFamily, attributionLogo: false,
    },
    grid: { vertLines: { color: css("--grid") }, horzLines: { color: css("--grid") } },
    leftPriceScale: { borderVisible: false },
    // 同步捲動的圖要左右對齊，右側刻度固定寬度（刻度寬度不同時，同一個日期會落在不同的水平位置）
    rightPriceScale: { borderVisible: false, minimumWidth: 80 },
    // 拖曳時最左、最右停在第一筆和最新一筆資料，不會拉出一片空白
    timeScale: { borderVisible: false, fixLeftEdge: true, fixRightEdge: true },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    localization: { locale: "zh-TW", priceFormatter: (p) => nf(p, 2) },
  };
}

// 細線、不顯示最後值，用在均線與各種走勢線
const LINE = { lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };

// 多張日期相同的圖同步捲動、縮放（直接對齊 logical range）。
// 範圍變更是非同步套用的，雙向同步會互相蓋回舊範圍，所以只由正在操作的那張圖帶動其他圖。
// 只認真正的操作（按下、滾輪、觸控），滑鼠只是經過不算，否則游標停在別張圖上時，那張圖的預設範圍會蓋掉其他圖。
// 回傳的物件可以設定 driver，例如程式要調整範圍時先指定由哪張圖帶動
function syncCharts(list) {
  const state = { driver: list[0][0] };
  for (const [chart, el] of list) {
    for (const ev of ["pointerdown", "wheel", "touchstart"]) {
      el.addEventListener(ev, () => { state.driver = chart; }, { passive: true });
    }
    chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
      if (!r || state.driver !== chart) return;
      for (const [other] of list) if (other !== chart) other.timeScale().setVisibleLogicalRange(r);
    });
  }
  return state;
}

// 個股搜尋框：輸入代號或名稱，從清單選取或按 Enter 就開啟個股頁
function setupStockSearch(input) {
  let loading = null;
  const list = document.createElement("datalist");
  list.id = "stock-list";
  document.body.append(list);
  input.setAttribute("list", list.id);
  // 第一次用到才下載個股清單；同時多次呼叫共用同一個下載
  const load = () => (loading ??= getJSON("data/stocks.json").then(
    (stocks) => {
      list.innerHTML = stocks.filter((s) => s[3]).map(([c, n]) => `<option value="${esc(c)} ${esc(n)}">`).join("");
      return stocks;
    },
    () => {
      input.placeholder = "個股清單載入失敗";
      return [];
    },
  ));
  const open = async () => {
    const q = input.value.trim();
    if (!q) return;
    const all = await load();
    const key = q.split(/\s+/)[0].toUpperCase();
    const hit = all.find((s) => s[0] === key) || all.find((s) => s[1] === q) || all.find((s) => s[3] && s[1].includes(q));
    if (hit) {
      input.value = "";
      location.href = `stock.html#${encodeURIComponent(hit[0])}`;
    } else {
      input.setCustomValidity("找不到這檔股票");
      input.reportValidity();
    }
  };
  input.addEventListener("focus", load, { once: true });
  input.addEventListener("input", () => {
    input.setCustomValidity("");
    // 從清單點選時，值會是「代號 名稱」且跟某個選項完全相同
    if ([...list.options].some((o) => o.value === input.value)) open();
  });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
}

matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => location.reload());
