"""用證交所網站歷史查詢（rwd）回補過去的資料

用法：
  python scripts/backfill.py 2026-01-01                          # 從 2026-01-01 補到昨天
  python scripts/backfill.py 2026-01-01 2026-06-30               # 指定區間
  python scripts/backfill.py 2026-01-01 --only stock_day_all,taiex  # 只補部分資料集

可回補：stock_day_all、bwibbu_all、margin、taiex、ex_rights
（monthly_revenue、holiday_schedule 沒有歷史查詢，不在回補範圍）

回補的檔案會轉成跟每日 OpenAPI 抓的完全相同的欄位與格式，兩種來源可以混在一起。
已存在的檔案會略過，中斷後重跑會從缺的地方接著補。
每次查詢間隔 3 秒；個股相關資料每個交易日各查一次，一年約需 40 分鐘。
"""
import argparse
import datetime as dt
import sys
import time

from fetch import DATA_DIR, DAY_KEYS, RWD, TODAY, get_json, group_rows, parse_date, rwd_rows, write_csv

PAUSE = 3          # 每次查詢間隔秒數，證交所網站查詢太密集會被暫時封鎖
MAX_FAILS = 5      # 連續失敗這麼多次就停下來（多半是被封鎖了），稍後重跑即可
DAILY = ["stock_day_all", "bwibbu_all", "margin"]   # 每個交易日查一次
MONTHLY = ["taiex", "ex_rights"]                    # 每個月查一次
MARGIN_COLS = ["股票代號", "股票名稱",
               "融資買進", "融資賣出", "融資現金償還", "融資前日餘額", "融資今日餘額", "融資限額",
               "融券買進", "融券賣出", "融券現券償還", "融券前日餘額", "融券今日餘額", "融券限額",
               "資券互抵", "註記"]


def num(s) -> str:
    """'2,425.00' -> '2425.00'；'--'、'-' 等無資料 -> ''"""
    s = str(s).strip()
    return "" if s in ("", "-", "--", "---") else s.replace(",", "")


def count(s) -> str:
    """數量為 0 時 OpenAPI 給空白，這裡比照辦理"""
    n = num(s)
    return "" if n == "0" else n


def ratio(s) -> str:
    """比率為 0（如 '0.00'）時 OpenAPI 給空白，這裡比照辦理"""
    n = num(s)
    return n if n and float(n) != 0 else ""


def roc(day: dt.date) -> str:
    return f"{day.year - 1911}{day:%m%d}"


def query(path: str, **params):
    """查詢 rwd 端點；沒有資料時回傳 None"""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    js = get_json(f"{RWD}/{path}?{qs}&response=json")
    time.sleep(PAUSE)
    if js.get("stat") != "OK":
        print(f"  {path}：{js.get('stat')}")
        return None
    return js


def table(js, first_field: str):
    """從回應中找出第一個欄位是 first_field 的表，回傳 (欄位, 資料)"""
    candidates = [js] + list(js.get("tables") or [])
    for t in candidates:
        if (t.get("fields") or [None])[0] == first_field:
            return t["fields"], t.get("data") or []
    raise RuntimeError(f"找不到第一欄為「{first_field}」的資料表，格式可能改了")


# ---- 每個交易日的資料：轉成 OpenAPI 格式 ----

def stock_day_all(day: dt.date):
    """MI_INDEX 每日收盤行情 -> STOCK_DAY_ALL"""
    js = query("afterTrading/MI_INDEX", date=f"{day:%Y%m%d}", type="ALLBUT0999")
    if js is None:
        return []
    fields, data = table(js, "證券代號")
    rows = []
    for r in data:
        f = dict(zip(fields, r))
        sign = "-" if "-" in f["漲跌(+/-)"] else ""
        rows.append({
            "Date": roc(day),
            "Code": f["證券代號"].strip(),
            "Name": f["證券名稱"].strip(),
            "TradeVolume": count(f["成交股數"]),
            "TradeValue": count(f["成交金額"]),
            "OpeningPrice": num(f["開盤價"]),
            "HighestPrice": num(f["最高價"]),
            "LowestPrice": num(f["最低價"]),
            "ClosingPrice": num(f["收盤價"]),
            "Change": f"{sign}{float(num(f['漲跌價差']) or 0):.4f}",
            "Transaction": count(f["成交筆數"]),
        })
    return rows


def bwibbu_all(day: dt.date):
    """BWIBBU_d 個股本益比、殖利率及股價淨值比 -> BWIBBU_ALL（舊年份欄位較少，依名稱對應）"""
    js = query("afterTrading/BWIBBU_d", date=f"{day:%Y%m%d}", selectType="ALL")
    if js is None:
        return []
    fields, data = table(js, "證券代號")
    rows = []
    for r in data:
        f = dict(zip(fields, r))
        rows.append({
            "Date": roc(day),
            "Code": f["證券代號"].strip(),
            "Name": f["證券名稱"].strip(),
            "PEratio": ratio(f["本益比"]),
            "DividendYield": ratio(f["殖利率(%)"]),
            "PBratio": ratio(f["股價淨值比"]),
        })
    return rows


def margin(day: dt.date):
    """MI_MARGN 融資融券彙總 -> OpenAPI MI_MARGN（融資、融券欄位同名，只能依位置對應）"""
    js = query("marginTrading/MI_MARGN", date=f"{day:%Y%m%d}", selectType="ALL")
    if js is None:
        return []
    fields, data = table(js, "代號")
    if len(fields) != len(MARGIN_COLS):
        raise RuntimeError(f"融資融券欄位數變了（{len(fields)} 欄）：{fields}")
    rows = []
    for r in data:
        row = dict(zip(MARGIN_COLS, r))
        for k in MARGIN_COLS[2:-1]:
            row[k] = count(row[k])
        row["股票代號"], row["股票名稱"] = row["股票代號"].strip(), row["股票名稱"].strip()
        rows.append(row)
    return rows


# ---- 每個月查一次的資料 ----

def taiex(month_end: dt.date):
    """MI_5MINS_HIST 加權指數當月日 K -> {'2026-10-01': [row], ...}"""
    js = query("TAIEX/MI_5MINS_HIST", date=f"{month_end:%Y%m%d}")
    if js is None:
        return {}
    fields, data = table(js, "日期")
    out = {}
    for r in data:
        f = dict(zip(fields, r))
        d = parse_date(f["日期"])
        out[d.isoformat()] = [{
            "Date": roc(d),
            "OpeningIndex": num(f["開盤指數"]),
            "HighestIndex": num(f["最高指數"]),
            "LowestIndex": num(f["最低指數"]),
            "ClosingIndex": num(f["收盤指數"]),
        }]
    return out


def ex_rights(month_start: dt.date, month_end: dt.date):
    """TWT49U 除權除息計算結果，格式與 fetch.py 相同 -> {'2026-10-16': [...], ...}"""
    js = query("exRight/TWT49U", startDate=f"{month_start:%Y%m%d}", endDate=f"{month_end:%Y%m%d}")
    rows = rwd_rows(js) if js else []
    return group_rows(rows, DAY_KEYS, parse_date, None) if rows else {}


# ---- 主流程 ----

def months(start: dt.date, end: dt.date):
    d = start.replace(day=1)
    while d <= end:
        nxt = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        yield max(d, start), min(nxt - dt.timedelta(days=1), end)
        d = nxt


def out_path(name: str, key: str):
    return DATA_DIR / name / key[:4] / f"{key}.csv"


def save(name: str, key: str, rows) -> None:
    out = out_path(name, key)
    if write_csv(out, rows):
        print(f"  已寫入 {out.relative_to(DATA_DIR)}（{len(rows)} 筆）")


def main() -> int:
    ap = argparse.ArgumentParser(description="用證交所網站歷史查詢回補資料")
    ap.add_argument("start", type=dt.date.fromisoformat, help="開始日期，例如 2026-01-01")
    ap.add_argument("end", type=dt.date.fromisoformat, nargs="?", help="結束日期（預設昨天）")
    ap.add_argument("--only", help="只補這些資料集，逗號分隔：" + ",".join(DAILY + MONTHLY))
    args = ap.parse_args()

    end = min(args.end or TODAY - dt.timedelta(days=1), TODAY)
    selected = args.only.split(",") if args.only else DAILY + MONTHLY
    unknown = set(selected) - set(DAILY + MONTHLY)
    if unknown:
        ap.error(f"不認識的資料集：{', '.join(sorted(unknown))}")
    daily_funcs = {"stock_day_all": stock_day_all, "bwibbu_all": bwibbu_all, "margin": margin}

    failed, streak = [], 0

    def attempt(label, fn, *a):
        """執行一次查詢；記錄失敗，連續失敗太多次就中止"""
        nonlocal streak
        try:
            result = fn(*a)
            streak = 0
            return result
        except Exception as e:  # noqa: BLE001
            print(f"  {label} 失敗：{e}")
            failed.append(label)
            streak += 1
            if streak >= MAX_FAILS:
                print(f"連續失敗 {MAX_FAILS} 次，可能被證交所暫時封鎖，先停止。稍後重跑即可接著補。")
                raise SystemExit(1)
            return None

    for m_start, m_end in months(args.start, end):
        print(f"== {m_start:%Y-%m}")
        # 加權指數一定要查：用它的日期當作當月的交易日清單
        days = attempt(f"taiex {m_start:%Y-%m}", taiex, m_end)
        if days is None:
            continue
        days = {k: v for k, v in days.items() if m_start.isoformat() <= k <= m_end.isoformat()}
        if "taiex" in selected:
            for key, rows in sorted(days.items()):
                save("taiex", key, rows)

        if "ex_rights" in selected:
            groups = attempt(f"ex_rights {m_start:%Y-%m}", ex_rights, m_start, m_end) or {}
            for key, rows in sorted(groups.items()):
                save("ex_rights", key, rows)

        for key in sorted(days):
            for name in (n for n in DAILY if n in selected):
                if out_path(name, key).exists():
                    continue
                rows = attempt(f"{name} {key}", daily_funcs[name], dt.date.fromisoformat(key))
                if rows:
                    save(name, key, rows)
                elif rows is not None:
                    print(f"  {name} {key} 沒有資料")

    if failed:
        print("以下項目失敗，重跑一次即可補上：", ", ".join(failed))
        return 1
    print("回補完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
