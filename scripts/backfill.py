"""用證交所、櫃買中心網站的歷史查詢回補過去的資料

用法：
  python scripts/backfill.py 2026-01-01                             # 從 2026-01-01 補到昨天
  python scripts/backfill.py 2026-01-01 2026-06-30                  # 指定區間
  python scripts/backfill.py 2026-01-01 --only stock_day_all,taiex  # 只補部分資料集
  python scripts/backfill.py 2026-01-01 --only tpex                 # 只補上櫃（全部 tpex_ 資料集）

可回補 fetch.py 裡所有網站來源的資料集（上市與 tpex_ 開頭的上櫃），以及從公開資訊觀測站回補月營收
（monthly_revenue、tpex_monthly_revenue）、季報綜合損益表（income_statement、tpex_income_statement）；
holiday_schedule、ex_rights_forecast、tpex_ex_rights_forecast、shareholding 沒有歷史查詢。
抓取與轉換都用 fetch.py 的函式，回補的檔案跟每日抓的格式完全相同。
已存在的檔案會略過，中斷後重跑會從缺的地方接著補。
"""
import argparse
import datetime as dt
import sys

from fetch import DATASETS, TODAY, out_path, save

MAX_FAILS = 5   # 連續失敗這麼多次就停下來（多半是被封鎖了），稍後重跑即可
BY_DAY = {name: cfg["by_day"] for name, cfg in DATASETS.items() if "by_day" in cfg}
BY_MONTH = [name for name, cfg in DATASETS.items() if "by_month" in cfg]   # 加權指數、櫃買指數：一次查一個月
BY_RANGE = [name for name, cfg in DATASETS.items() if "by_range" in cfg]   # 除權息：一次查一段期間
EMPTY_OK = {name for name, cfg in DATASETS.items() if cfg.get("fields")}   # 注意股、處置股：當天沒有也是正常的
MONTHLY = [name for name, cfg in DATASETS.items() if "history" in cfg]      # 月營收：每月一份
QUARTERLY = [name for name, cfg in DATASETS.items() if "by_quarter" in cfg]  # 季報：每季一份
PERIODIC = MONTHLY + QUARTERLY   # 不需要交易日清單
SUPPORTED = BY_MONTH + BY_RANGE + list(BY_DAY) + PERIODIC


def months(start: dt.date, end: dt.date):
    d = start.replace(day=1)
    while d <= end:
        nxt = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        yield max(d, start), min(nxt - dt.timedelta(days=1), end)
        d = nxt


def main() -> int:
    ap = argparse.ArgumentParser(description="用證交所、櫃買中心網站歷史查詢回補資料")
    ap.add_argument("start", type=dt.date.fromisoformat, help="開始日期，例如 2026-01-01")
    ap.add_argument("end", type=dt.date.fromisoformat, nargs="?", help="結束日期（預設昨天）")
    ap.add_argument("--only", help="只補這些資料集，逗號分隔（tpex 代表全部上櫃資料集）：" + ",".join(SUPPORTED))
    args = ap.parse_args()

    end = min(args.end or TODAY - dt.timedelta(days=1), TODAY)
    selected = args.only.split(",") if args.only else SUPPORTED
    if "tpex" in selected:
        selected = [n for n in selected if n != "tpex"] + [n for n in SUPPORTED if n.startswith("tpex_")]
    unknown = set(selected) - set(SUPPORTED)
    if unknown:
        ap.error(f"不認識的資料集：{', '.join(sorted(unknown))}")

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
                print(f"連續失敗 {MAX_FAILS} 次，可能被暫時封鎖，先停止。稍後重跑即可接著補。")
                raise SystemExit(1)
            return None

    for m_start, m_end in months(args.start, end):
        print(f"== {m_start:%Y-%m}")
        # 月營收、季報：每月或每季一份，從公開資訊觀測站查（還沒公布的月份、季度查不到，略過）
        periodic = [(name, f"{m_start:%Y-%m}", DATASETS[name]["history"], (f"{m_start:%Y-%m}",)) for name in MONTHLY]
        if m_start.month % 3 == 0:
            q = m_start.month // 3
            periodic += [(name, f"{m_start.year}-Q{q}", DATASETS[name]["by_quarter"], (m_start.year, q)) for name in QUARTERLY]
        for name, key, fn, fn_args in periodic:
            if name in selected and not out_path(name, key).exists():
                rows = attempt(f"{name} {key}", fn, *fn_args)
                if rows:
                    save(name, key, rows)
                elif rows is not None:
                    print(f"  {name} {key} 沒有資料")
        if not set(selected) - set(PERIODIC):
            continue

        # 加權指數一定要查：用它的日期當作當月的交易日清單（上市、上櫃的交易日相同）
        days = attempt(f"taiex {m_start:%Y-%m}", DATASETS["taiex"]["by_month"], m_end)
        if days is None:
            continue
        in_month = lambda groups: {k: v for k, v in groups.items() if m_start.isoformat() <= k <= m_end.isoformat()}  # noqa: E731
        days = in_month(days)
        for name in (n for n in BY_MONTH if n in selected):
            groups = days if name == "taiex" else in_month(attempt(f"{name} {m_start:%Y-%m}", DATASETS[name]["by_month"], m_end) or {})
            for key, rows in sorted(groups.items()):
                if not out_path(name, key).exists():
                    save(name, key, rows)

        for name in (n for n in BY_RANGE if n in selected):
            groups = attempt(f"{name} {m_start:%Y-%m}", DATASETS[name]["by_range"], m_start, m_end) or {}
            for key, rows in sorted(groups.items()):
                if not out_path(name, key).exists():
                    save(name, key, rows)

        for key in sorted(days):
            for name in (n for n in BY_DAY if n in selected):
                if out_path(name, key).exists():
                    continue
                rows = attempt(f"{name} {key}", BY_DAY[name], dt.date.fromisoformat(key))
                if rows or (rows is not None and name in EMPTY_OK):
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
