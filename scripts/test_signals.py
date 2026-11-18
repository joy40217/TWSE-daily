"""signals.py 的測試：python -m unittest scripts/test_signals.py（或 python scripts/test_signals.py）

- 用小型假資料測停牌、上市不滿 60 天、除權息、上櫃融資空白、注意／處置、休市推算、均線邊界、持股、回測
- 跟網站摘要對數字：site/data/summary/ 存在（本機跑過 report.py）才跑，全部個股逐檔比對
"""
import datetime as dt
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import signals  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def roc(day):
    y, m, d = day.split("-")
    return f"{int(y) - 1911}{m}{d}"


def bizdays(start, n, skip=()):
    out, day = [], dt.date.fromisoformat(start)
    while len(out) < n:
        if day.weekday() < 5 and day.isoformat() not in skip:
            out.append(day.isoformat())
        day += dt.timedelta(days=1)
    return out


def write(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(header)] + [",".join("" if v is None else str(v) for v in r) for r in rows]
    path.write_text("﻿" + "\n".join(lines) + "\n", encoding="utf-8")


def make_data(root, days, stocks, notice=None, disposition=None, forecast=(), holidays=()):
    """stocks: {代號: {"market": 上市/上櫃, "close": [每天收盤，None＝停牌沒有這一列], "vol": 張, "fi"/"tr"/"dl": [張],
    "mf": [融資餘額，None＝空白], "ex": {第幾天: (除權息前收盤, 參考價)}}}"""
    root = Path(root)
    stocks = {"9990": {"close": [50] * len(days)}, "9991": {"market": "上櫃", "close": [50] * len(days)}, **stocks}
    for i, day in enumerate(days):
        y = day[:4]
        for ds in ("taiex", "tpex_index"):
            write(root / ds / y / f"{day}.csv", ["Date", "OpeningIndex", "HighestIndex", "LowestIndex", "ClosingIndex"],
                  [[roc(day), 100, 100, 100, 100]])
        for prefix, market in (("", "上市"), ("tpex_", "上櫃")):
            price, inst, margin, ex = [], [], [], []
            for code, st in stocks.items():
                if st.get("market", "上市") != market or st["close"][i] is None:
                    continue
                c = st["close"][i]
                prev = next((x for x in reversed(st["close"][:i]) if x is not None), None)
                if i in st.get("ex", {}):
                    before, ref = st["ex"][i]
                    ex.append([day, code, code, before, ref, "息"])
                    prev = ref
                chg = round(c - prev, 2) if prev is not None else 0
                vol = st.get("vol", [1000] * len(days))[i]
                price.append([roc(day), code, f"N{code}", vol * 1000, 0, c, c, c, c, chg, 1])
                fi, tr, dl = (st.get(k, [0] * len(days))[i] for k in ("fi", "tr", "dl"))
                inst.append([code, f"N{code}", fi * 1000, 0, tr * 1000, dl * 1000])
                mf = st.get("mf", [1000] * len(days))[i]
                margin.append([code, f"N{code}", "", mf, 10])
            write(root / f"{prefix}stock_day_all" / y / f"{day}.csv",
                  ["Date", "Code", "Name", "TradeVolume", "TradeValue", "OpeningPrice", "HighestPrice", "LowestPrice",
                   "ClosingPrice", "Change", "Transaction"], price)
            write(root / f"{prefix}institutional" / y / f"{day}.csv",
                  ["證券代號", "證券名稱", "外陸資買賣超股數(不含外資自營商)", "外資自營商買賣超股數", "投信買賣超股數", "自營商買賣超股數"], inst)
            write(root / f"{prefix}margin" / y / f"{day}.csv", ["股票代號", "股票名稱", "融資前日餘額", "融資今日餘額", "融券今日餘額"], margin)
            write(root / f"{prefix}notice" / y / f"{day}.csv", ["證券代號", "證券名稱", "累計次數", "注意交易資訊"],
                  [[code, code, 1, info] for code, info in (notice or {}).get((day, market), [])])
            write(root / f"{prefix}disposition" / y / f"{day}.csv", ["證券代號", "證券名稱", "處置起迄時間", "處置措施"],
                  [[code, code, period, measure] for code, period, measure in (disposition or {}).get((day, market), [])])
            if ex:
                write(root / f"{prefix}ex_rights" / y / f"{day}.csv",
                      ["資料日期", "股票代號", "股票名稱", "除權息前收盤價", "除權息參考價", "權/息"], ex)
    for prefix in ("", "tpex_"):
        write(root / f"{prefix}ex_rights_forecast" / f"{prefix}ex_rights_forecast.csv",
              ["除權除息日期", "股票代號", "名稱", "除權息", "現金股利"],
              [list(r) for r in forecast if (r[1].startswith("6") == bool(prefix))])
    years = sorted({d[:4] for d in days} | {str(int(days[-1][:4]) + 1)})
    write(root / "holiday_schedule" / "holiday_schedule.csv", ["Name", "Date", "Weekday", "Description"],
          [["開國紀念日", roc(f"{y}-01-01"), "", "依規定放假1日。"] for y in years]
          + [["測試休市", roc(h), "", "依規定放假1日。"] for h in holidays])


def run(root, date, watch, star="", holdings=None, backtest=False, days=120):
    out = Path(root) / "out"
    argv = ["--date", date, "--watch", ",".join(watch), "--star", star, "--data", str(root), "--out", str(out)]
    if holdings is not None:
        p = Path(root) / "h.json"
        p.write_text(json.dumps(holdings), encoding="utf-8")
        argv += ["--holdings", str(p)]
    if backtest:
        argv += ["--backtest", "--backtest-days", str(days)]
    code = signals.main(argv)
    sig = json.loads((out / "signals_latest.json").read_text(encoding="utf-8"))
    bt = json.loads((out / "backtest_latest.json").read_text(encoding="utf-8")) if backtest else None
    return code, sig, bt


def cell(sig, code, key):
    row = next((r for r in sig["matrix"] if r["code"] == code), None)
    if row is None:
        return ""
    return row["cells"][[c["key"] for c in sig["columns"]].index(key)]


class Synthetic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.days = bizdays("2026-01-05", 150)   # 到 2026-07-31（週五）

    def tearDown(self):
        self.tmp.cleanup()

    def test_suspension_skips_days(self):
        n = len(self.days)
        close = [100 + i for i in range(n)]
        for i in (n - 4, n - 3):   # 最近停牌兩天
            close[i] = None
        fi = [10] * n
        make_data(self.root, self.days, {"1111": {"close": close, "fi": fi}})
        _, sig, _ = run(self.root, self.days[-1], ["1111"])
        s = sig["stocks"]["1111"]
        traded = [x for x in close if x is not None]
        self.assertEqual(s["ma5"], round(sum(traded[-5:]) / 5, 2))
        self.assertEqual(s["streak"]["foreign"], len(traded))      # 停牌日不算中斷
        self.assertEqual(s["ret_5d"], round((traded[-1] / traded[-6] - 1) * 100, 2))
        self.assertEqual(s["up_days"], len(traded) - 1)

    def test_new_listing_under_60_days(self):
        n = len(self.days)
        close = [None] * (n - 40) + [100 + i for i in range(40)]
        make_data(self.root, self.days, {"2222": {"close": close}})
        _, sig, _ = run(self.root, self.days[-1], ["2222"])
        s = sig["stocks"]["2222"]
        self.assertIsNone(s["ma60"])
        self.assertIsNone(s["ma100"])
        self.assertEqual(cell(sig, "2222", "high60"), "")
        self.assertNotIn("季線", cell(sig, "2222", "cross_up"))

    def test_ex_dividend_is_not_a_drop(self):
        n = len(self.days)
        close = [100.0] * (n - 1) + [95.0]
        make_data(self.root, self.days, {"3333": {"close": close, "ex": {n - 1: (100.0, 95.0)}}})
        _, sig, _ = run(self.root, self.days[-1], ["3333"])
        s = sig["stocks"]["3333"]
        self.assertEqual(s["ma20"], 95.0)          # 還原後之前也是 95
        self.assertEqual(s["pct"], 0.0)
        for key in ("cross_down", "low60", "down_streak"):
            self.assertFalse(cell(sig, "3333", key).startswith("●"), key)   # 一直持平：不算平新低，「○ 差0.0%」是對的

    def test_tpex_margin_blank(self):
        n = len(self.days)
        close = [100 + (i % 3) for i in range(n)]
        mf = [1000 + 50 * i for i in range(n - 1)] + [None]
        make_data(self.root, self.days, {"6666": {"market": "上櫃", "close": close, "mf": mf}})
        _, sig, _ = run(self.root, self.days[-1], ["6666"])
        s = sig["stocks"]["6666"]
        self.assertIsNone(s["financing"])
        self.assertIsNone(s["financing_change"])
        self.assertEqual(cell(sig, "6666", "margin_up"), "")

    def test_notice_and_overlapping_disposition(self):
        n = len(self.days)
        d = self.days[-1]
        make_data(self.root, self.days, {"4444": {"close": [100] * n}, "5555": {"close": [100] * n}},
                  notice={(d, "上市"): [("4444", "漲幅過大")]},
                  disposition={(d, "上市"): [("5555", "115/07/13～115/08/03", "第一次處置"),
                                            ("5555", "115/07/27～115/08/07", "第二次處置"),
                                            ("5555", "115/08/03～115/08/14", "第三次處置")]})   # 第三次還沒開始
        _, sig, _ = run(self.root, d, ["4444", "5555"])
        self.assertEqual(cell(sig, "4444", "notice"), "● 注意股")
        self.assertEqual(sig["stocks"]["4444"]["notice"], "漲幅過大")
        self.assertEqual(cell(sig, "5555", "notice"), "● 處置股")
        self.assertEqual(sig["stocks"]["5555"]["disposition"]["measure"], "第二次處置")

    def test_empty_notice_file(self):
        n = len(self.days)
        make_data(self.root, self.days, {"4444": {"close": [100] * n}})
        code, sig, _ = run(self.root, self.days[-1], ["4444"])
        self.assertEqual(code, 0)
        self.assertTrue(sig["ready"]["notice"])
        self.assertEqual(cell(sig, "4444", "notice"), "")

    def test_ex_rights_next_trading_day_skips_weekend_and_holiday(self):
        days = [d for d in self.days if d <= "2026-07-10"]   # 2026-07-10 是週五
        n = len(days)
        make_data(self.root, days, {"1234": {"close": [100] * n}, "1235": {"close": [100] * n}},
                  forecast=[("2026-07-14", "1234", "x", "息", "1.5"), ("2026-07-13", "1235", "x", "權", "0")],
                  holidays=["2026-07-13"])
        _, sig, _ = run(self.root, days[-1], ["1234", "1235"])
        self.assertEqual(cell(sig, "1234", "ex_rights"), "● 明天除息")   # 週末、7/13 休市，下一個交易日是 7/14
        self.assertEqual(cell(sig, "1235", "ex_rights"), "")             # 休市日不會除權息，也不在本週
        self.assertIsNone(sig["calendar_warning"])

    def test_ex_rights_later_this_week(self):
        days = [d for d in self.days if d <= "2026-07-07"]   # 週二
        n = len(days)
        make_data(self.root, days, {"1234": {"close": [100] * n}},
                  forecast=[("2026-07-09", "1234", "x", "權息", "1")])
        _, sig, _ = run(self.root, days[-1], ["1234"])
        self.assertEqual(cell(sig, "1234", "ex_rights"), "○ 7/9除權息")

    def test_cross_ma_boundary(self):
        n = len(self.days)
        flat = [100.0] * (n - 1)
        make_data(self.root, self.days, {"7001": {"close": flat + [101.0]}, "7002": {"close": flat + [100.0]},
                                         "7003": {"close": flat + [99.0]}})
        _, sig, _ = run(self.root, self.days[-1], ["7001", "7002", "7003"])
        self.assertEqual(cell(sig, "7001", "cross_up"), "● 月線 季線")   # 昨天剛好等於均線、今天站上
        self.assertEqual(cell(sig, "7002", "cross_up"), "")             # 今天剛好等於均線不算
        self.assertEqual(cell(sig, "7002", "cross_down"), "")
        self.assertEqual(cell(sig, "7003", "cross_down"), "● 月線 季線")

    def test_tie_with_previous_high_low(self):
        n = len(self.days)
        up = [100.0] * (n - 30) + [110.0] + [105.0] * 28 + [110.0]      # 今天收盤平 30 天前的最高價
        down = [100.0] * (n - 30) + [90.0] + [95.0] * 28 + [90.0]
        make_data(self.root, self.days, {"7101": {"close": up}, "7102": {"close": down},
                                         "7103": {"close": [100.0] * n}})
        _, sig, _ = run(self.root, self.days[-1], ["7101", "7102", "7103"], star="7101")
        d = self.days[n - 30]
        label = f"{int(d[5:7])}/{int(d[8:])}"
        self.assertEqual(cell(sig, "7101", "high60"), f"● 新高（{label}）")
        self.assertEqual(cell(sig, "7102", "low60"), f"● 新低（{label}）")
        self.assertFalse(cell(sig, "7103", "high60").startswith("●"))   # 完全沒動不算
        self.assertFalse(cell(sig, "7103", "low60").startswith("●"))
        self.assertEqual(sig["star"][0]["price_map"][0]["label"], "◀ 收盤（60 日新高）")

    def test_ready_missing_margin(self):
        n = len(self.days)
        make_data(self.root, self.days, {"1111": {"close": [100] * n}})
        (self.root / "tpex_margin" / self.days[-1][:4] / f"{self.days[-1]}.csv").unlink()
        code, sig, _ = run(self.root, self.days[-1], ["1111", "9999"])
        self.assertEqual(code, 2)
        self.assertFalse(sig["ready"]["ok"])
        self.assertEqual(sig["ready"]["missing"], ["tpex_margin"])
        self.assertEqual(sig["ready"]["watch_missing"], ["9999"])

    def test_backtest_first_dedupe_pending(self):
        n = len(self.days)
        close = [100.0] * 70 + [100.0 + i for i in range(1, n - 70 + 1)]   # 第 70 天起每天創新高
        fi = [0] * 70 + [100] * (n - 70)
        make_data(self.root, self.days, {"8888": {"close": close, "fi": fi}})
        _, _, bt = run(self.root, self.days[-1], ["8888"], backtest=True, days=60)
        highs = [e for e in bt["events"] if e["key"] == "high60" and e["level"] == 2]
        self.assertEqual(len(highs), 60)
        self.assertEqual([e["first"] for e in highs].count(True), 0)    # 回測開始前就已經連續創新高
        self.assertIsNone(highs[-1]["fwd"]["1d"])                       # 最後一天還沒有後續
        self.assertIsNone(highs[-10]["fwd"]["10d"])
        self.assertIsNotNone(highs[-11]["fwd"]["10d"])
        # 同一天有新高、法人連買、連漲等好幾個偏多訊號，依方向只算一次
        bull = bt["stats"]["all"]["by_direction"]["bull"]["1d"]["all"]
        self.assertEqual(bull["n"] + bull["pending"], 60)
        self.assertEqual(bull["hit"], bull["n"])                        # 大盤不動、每天上漲

    def test_weekly_counts_signals_due_this_week(self):
        n = len(self.days)   # 最後一天 2026-07-31 是週五
        close = [100.0] * 70 + [100.0 + i for i in range(1, n - 70 + 1)]
        make_data(self.root, self.days, {"8890": {"close": close}})
        _, _, bt = run(self.root, self.days[-1], ["8890"], backtest=True, days=30)
        w = bt["weekly"]
        self.assertEqual((w["week"], w["title"]), ("7/27–7/31", "上週訊號、5 日後驗證"))
        self.assertEqual((w["signal_from"], w["signal_to"]), ("2026-07-20", "2026-07-24"))   # 上週一到週五
        self.assertEqual((w["bull"]["n"], w["bull"]["hit"], w["bull"]["pending"]), (5, 5, 0))
        self.assertEqual(w["bull"]["text"], "偏多 5/5")
        # 週三跑：週一到週三到期的已經有結果，週四、週五到期的還在 pending
        _, _, bt = run(self.root, "2026-07-29", ["8890"], backtest=True, days=30)
        self.assertEqual((bt["weekly"]["bull"]["n"], bt["weekly"]["bull"]["pending"]), (3, 2))

    def test_backtest_first_signal(self):
        n = len(self.days)
        close = [100.0] * (n - 5) + [101.0, 102.0, 103.0, 104.0, 105.0]
        make_data(self.root, self.days, {"8889": {"close": close}})
        _, _, bt = run(self.root, self.days[-1], ["8889"], backtest=True, days=20)
        highs = [e for e in bt["events"] if e["key"] == "high60" and e["level"] == 2]
        self.assertEqual([e["first"] for e in highs], [True, False, False, False, False])
        near = [e for e in bt["events"] if e["key"] == "high60" and e["level"] == 1]   # 持平時每天「○ 差0.0%」
        self.assertTrue(near)
        self.assertFalse(any(e["first"] for e in near))   # 回測開始前就一直有，不算新出現


def m(close, ma20=None, ma60=None, high=None, low=None, date=""):
    return {"close": close, "ma20": ma20, "ma60": ma60, "high_60": high if high is not None else close,
            "low_60": low if low is not None else close, "date": date}


class Holdings(unittest.TestCase):
    def out(self, cost, *days):
        """days：由舊到新的每日指標，最後一個是今天"""
        return signals.track(cost, [(d, 1.0) for d in days])

    def test_profit(self):
        h = self.out(100, m(130, high=140, low=110))
        self.assertEqual((h["status_key"], h["exit_line"], h["exit_basis"]), ("profit", 119, "60 日高 −15%"))
        self.assertEqual(h["tp_line"], "已達 +20%")

    def test_small_gain_and_loss(self):
        h = self.out(100, m(105, high=110, low=92))
        self.assertEqual((h["status_key"], h["exit_line"], h["exit_basis"], h["tp_line"]), ("small_gain", 92, "60 日低", 120))
        h = self.out(100, m(95, high=110, low=85))
        self.assertEqual((h["status_key"], h["exit_line"], h["exit_basis"]), ("small_loss", 90, "成本 −10%"))
        self.assertEqual(h["dist_exit_pct"], 5.6)
        self.assertFalse(h["warn"])

    def test_deep_loss(self):
        h = self.out(100, m(80, ma20=78, ma60=85, high=95, low=70))
        self.assertEqual((h["status_key"], h["exit_line"], h["exit_basis"]), ("deep_loss", 78, "月線"))
        self.assertEqual((h["tp_line"], h["tp_basis"]), (85, "季線"))
        self.assertTrue(h["warn"])

    def test_deep_loss_lines_all_above(self):
        h = self.out(100, m(70, ma20=78, ma60=85, high=95, low=70))   # 收盤就是 60 日低，均線都在上面
        self.assertEqual((h["exit_line"], h["exit_basis"]), (70, "60 日低"))
        self.assertEqual(h["tp_basis"], "月線")

    def test_states(self):
        line90 = m(95, high=110, low=85, date="d1")          # 小虧：出場線 max(成本 −10%, 60 日低) = 90
        broke = m(89, high=110, low=85, date="d2")           # 跌破 90；重算的線會降到 85
        h = self.out(100, line90, broke)
        self.assertEqual((h["state"], h["exit_line"], h["recalc_exit_line"], h["broken_since"]), ("pending", 90, 85, "d2"))
        self.assertTrue(h["broke_today"])
        self.assertEqual(self.out(100, line90, line90)["state"], "ok")
        # 隔天收盤還在 90 以下（但在重算的 85 之上）→ 已觸發，出場線仍顯示被跌破的 90
        h = self.out(100, line90, broke, m(88, high=110, low=80))
        self.assertEqual((h["state"], h["exit_line"], h["compared_line"]), ("triggered", 90, 90))
        self.assertTrue(h["broke_yesterday"])
        self.assertEqual(self.out(100, line90, broke, m(88, low=80), m(87, low=80))["state"], "triggered")
        # 站上 90 才算站回，隔天起改用重算的線
        h = self.out(100, line90, broke, m(91, high=110, low=80))
        self.assertEqual((h["state"], h["exit_line"]), ("recovered", 90))
        self.assertEqual(self.out(100, line90, broke, m(91, high=110, low=80), m(91, high=110, low=80))["state"], "ok")

    def test_profit_line_kept_after_status_drops(self):
        # 像台積電：🟢 時的出場線 2,197.25（60 日高 2,585 × 0.85），跌破後掉回 🔵，隔天 2,175 仍在線下
        day1 = m(2300, high=2585, low=2135)
        day2 = m(2190, high=2585, low=2135)
        day3 = m(2175, high=2585, low=2135)
        self.assertEqual(self.out(1815, day1, day2)["state"], "pending")
        h = self.out(1815, day1, day2, day3)
        self.assertEqual((h["state"], h["exit_line"], h["exit_basis"]), ("triggered", 2197.25, "60 日高 −15%"))

    def test_turns_green(self):
        self.assertTrue(self.out(100, m(119, low=100), m(121, low=100))["status_changed_to_green"])
        self.assertFalse(self.out(100, m(121, low=100), m(122, low=100))["status_changed_to_green"])


SUMMARY = ROOT / "site" / "data" / "summary"


@unittest.skipUnless(SUMMARY.is_dir() and any(SUMMARY.glob("*.json")), "沒有 site/data/summary（本機先跑 report.py）")
class MatchesWebsite(unittest.TestCase):
    """跟網站摘要逐檔對數字（全部個股，不挑代號）"""

    def test_all_stocks(self):
        files = sorted(SUMMARY.glob("*.json"))
        latest = json.loads(files[0].read_text(encoding="utf-8"))["latest_market_date"]
        store = signals.Store(ROOT / "data")
        cal = [d for d in signals.calendar(store) if d <= latest]
        days = cal[-signals.WINDOW:]
        series = signals.load_series(store, days, None, latest)
        notice, disp = signals.load_events_by_day(store, days[-1:], None)
        ctx = {"notice": notice, "disposition": disp, "ex_dates": {}}
        checked, problems = 0, []
        for f in files:
            want = json.loads(f.read_text(encoding="utf-8"))
            s = series.get(want["code"])
            if want["date"] != latest or s is None or s.at.get(latest) not in s.pos:
                continue
            k = s.at[latest]
            got = signals.stock_out(s, k, signals.snapshot(s, k, ctx))
            checked += 1
            pairs = [("close", got["close"], want["price"]["close"]), ("pct", got["pct"], want["price"]["pct"]),
                     ("volume", got["volume"], want["price"]["volume"]),
                     ("high_60", got["high_60"], want["high_60"]), ("low_60", got["low_60"], want["low_60"]),
                     ("ma_raw", got["ma_raw"], want["ma_raw"])]
            pairs += [(f"ma{n}", got[f"ma{n}"], want["ma"].get(f"ma{n}")) for n in signals.MA_DAYS]
            # 最後一筆法人資料早於載入範圍（例如很久沒有法人進出的上櫃股），摘要還留著舊數字，腳本不載入那麼久以前的資料
            if want.get("institutional") and want["institutional"]["date"] >= days[0]:
                pairs.append(("streak", got["streak"], want["streak"]))
                for n in (5, 20):
                    pairs.append((f"sum_{n}d", {k2: v for k2, v in got[f"sum_{n}d"].items() if k2 != "total"},
                                  want[f"sum_{n}d"]))
            mg = want.get("margin")
            if mg and mg["date"] == latest and got["financing"] is not None:   # CSV 空白時摘要寫 0、這裡是 null
                pairs.append(("financing", got["financing"], mg["financing"]))
                if got["financing_change"] is not None:
                    pairs.append(("financing_change", got["financing_change"], mg["financing_change"]))
            problems += [f"{want['code']} {name}: {a} != {b}" for name, a, b in pairs if a != b]
        self.assertGreater(checked, 1000)
        self.assertEqual(problems, [], f"{len(problems)} 個不一致（共比對 {checked} 檔）：\n" + "\n".join(problems[:40]))


if __name__ == "__main__":
    unittest.main()
