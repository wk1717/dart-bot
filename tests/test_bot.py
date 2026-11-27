"""오프라인 테스트. DART 응답은 가짜 픽스처, Supabase는 FakeSB로 대체"""
import io
import json
import os
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import analyze
import backfill
import backtest
import bot
import prices
import trader

TODAY = date.today().strftime("%Y%m%d")
_raw = json.loads((Path(__file__).parent / "fixtures" / "list_page1.json").read_text())
# 요약이 오늘 날짜 기준이라 접수일을 오늘로 바꿔둠
FIXTURE = {**_raw, "list": [{**i, "rcept_dt": TODAY} for i in _raw["list"]]}
ENV = {k: "x" for k in bot.ENV_KEYS}


class FakeSB:
    """bot.sb에서 쓰는 요청만 흉내"""

    def __init__(self):
        self.disclosures, self.runs, self.alerts = {}, [], {}

    def __call__(self, method, path, body=None, prefer=None, count=False):
        table, _, q = path.partition("?")
        p = dict(urllib.parse.parse_qsl(q))
        if method == "POST" and table == "disclosures":
            assert "ignore-duplicates" in prefer
            new = [r for r in body if r["rcept_no"] not in self.disclosures]
            self.disclosures.update((r["rcept_no"], r) for r in new)
            return [{"rcept_no": r["rcept_no"]} for r in new]
        if method == "POST" and table == "runs":
            self.runs.append(body)
            return None
        if method == "POST" and table == "alerts":
            self.alerts[body["key"]] = body
            return None
        if table == "disclosures":
            rows = [r for r in self.disclosures.values() if r["rcept_dt"] == p["rcept_dt"][3:]]
            if "category" in p:
                rows = [r for r in rows if r["category"]]
            return len(rows) if count else rows
        if table == "runs":
            prefix = p["started_at"].removeprefix("like.").removesuffix("*")
            return [r for r in reversed(self.runs) if r["started_at"].startswith(prefix)]
        if table == "alerts":
            r = self.alerts.get(p["key"][3:])
            return [r] if r else []
        raise AssertionError(f"예상 못 한 요청 {method} {path}")


class ClassifyTest(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(bot.classify("주요사항보고서(유상증자결정)"), "유상증자")
        self.assertEqual(bot.classify("[기재정정]최대주주변경"), "최대주주변경")
        self.assertEqual(bot.classify("단일판매ㆍ공급계약체결"), "공급계약")
        self.assertIsNone(bot.classify("임원ㆍ주요주주특정증권등소유상황보고서"))


class SupabaseUrlTest(unittest.TestCase):
    def test_accepts_url_with_or_without_rest_v1(self):
        for base in ["https://a.supabase.co", "https://a.supabase.co/", "https://a.supabase.co/rest/v1/"]:
            with mock.patch.dict(os.environ, {**ENV, "SUPABASE_URL": base}), \
                 mock.patch("bot.urllib.request.urlopen") as op:
                op.return_value.__enter__.return_value.read.return_value = b"[]"
                bot.sb("GET", "runs?select=ok")
            self.assertEqual(op.call_args.args[0].full_url, "https://a.supabase.co/rest/v1/runs?select=ok")

    def test_encodes_korean_query(self):
        with mock.patch.dict(os.environ, {**ENV, "SUPABASE_URL": "https://a.supabase.co"}), \
             mock.patch("bot.urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = b"[]"
            bot.sb("GET", "trades?category=eq.자사주취득&name=ilike.%2A삼성%2A&code=in.(Y,K)")
        self.assertEqual(op.call_args.args[0].full_url, "https://a.supabase.co/rest/v1/trades?category=eq.%EC%9E%90%EC%82%AC%EC%A3%BC%EC%B7%A8%EB%93%9D"
                         "&name=ilike.%2A%EC%82%BC%EC%84%B1%2A&code=in.(Y,K)")


@mock.patch.dict(os.environ, ENV)
class CollectTest(unittest.TestCase):
    def setUp(self):
        self.sb = FakeSB()
        patcher = mock.patch("bot.sb", self.sb)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_saves_all_listed_once_without_alerts(self):
        with mock.patch("bot.http_json", return_value=FIXTURE), \
             mock.patch("bot.send_telegram") as send:
            self.assertEqual(bot.collect(TODAY), 4)  # 비상장 1건 빠짐
            self.assertEqual(bot.collect(TODAY), 0)  # 두 번째는 다 중복
        send.assert_not_called()  # 공시별 알림 없음
        self.assertEqual(sum(r["category"] is None for r in self.sb.disclosures.values()), 1)
        self.assertEqual([(r["ok"], r["saved"]) for r in self.sb.runs], [(1, 4), (1, 0)])

    def test_default_range_is_yesterday_to_today(self):
        with mock.patch("bot.http_json", return_value=FIXTURE) as h:
            bot.collect(None)
        url = h.call_args.args[0]
        self.assertIn(f"end_de={TODAY}", url)
        self.assertNotIn(f"bgn_de={TODAY}", url)

    def test_failure_records_run_and_alerts_once(self):
        with mock.patch("bot.http_json", side_effect=OSError("timeout")), \
             mock.patch("bot.send_telegram") as send:
            for _ in range(2):
                with self.assertRaises(OSError):
                    bot.collect(TODAY)
        self.assertEqual(send.call_count, 1)  # 1시간 안엔 한 번만
        self.assertIn("OSError: timeout", send.call_args.args[0])
        self.assertEqual([(r["ok"], r["error"]) for r in self.sb.runs], [(0, "OSError: timeout")] * 2)

    def test_same_error_alerts_again_after_interval(self):
        with mock.patch("bot.send_telegram") as send:
            self.assertTrue(bot.alert_error("E", now=1000))
            self.assertFalse(bot.alert_error("E", now=1000 + 3599))
            self.assertTrue(bot.alert_error("E", now=1000 + 3600))
            self.assertTrue(bot.alert_error("다른 에러", now=1000 + 3601))
        self.assertEqual(send.call_count, 3)

    def test_alert_still_sent_when_supabase_down(self):
        with mock.patch("bot.sb", side_effect=RuntimeError("Supabase 503")), \
             mock.patch("bot.send_telegram") as send:
            self.assertTrue(bot.alert_error("E", now=1000))
        send.assert_called_once()

    def test_pagination(self):
        p1 = {**FIXTURE, "total_page": 2}
        p2 = {**FIXTURE, "page_no": 2, "list": []}
        with mock.patch("bot.http_json", side_effect=[p1, p2]) as h:
            items = bot.fetch_disclosures("k", TODAY, TODAY)
        self.assertEqual(h.call_count, 2)
        self.assertEqual(len(items), 5)

    def test_no_data_day(self):
        with mock.patch("bot.http_json", return_value={"status": "013", "message": "조회된 데이타가 없습니다."}):
            self.assertEqual(bot.collect(TODAY), 0)

    def test_summary_day_survives_cron_delay(self):
        self.assertEqual(bot.summary_day(datetime(2026, 11, 18, 19, 5)), "20261118")
        self.assertEqual(bot.summary_day(datetime(2026, 11, 19, 3, 47)), "20261118")  # 밀린 실행

    def test_summary_ok(self):
        with mock.patch("bot.http_json", return_value=FIXTURE):
            bot.collect(TODAY)
        with mock.patch("bot.send_telegram") as send:
            text = bot.summary(TODAY)
        send.assert_called_once()
        self.assertTrue(text.startswith("✅"))
        self.assertIn("공시 4건 저장", text)
        self.assertIn("유상증자 1", text)
        self.assertIn("실행 1회 중 성공 1회", text)

    def test_summary_warns_on_failure_or_no_runs(self):
        with mock.patch("bot.send_telegram"):
            self.assertIn("⚠️", bot.summary(TODAY))  # 실행 기록 없음
            with mock.patch("bot.http_json", side_effect=OSError("x")), self.assertRaises(OSError):
                bot.collect(TODAY)
            text = bot.summary(TODAY)
        self.assertIn("실행 1회 중 성공 0회", text)
        self.assertIn("마지막 에러: OSError: x", text)


@mock.patch.dict(os.environ, ENV)
class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.sb = FakeSB()
        patcher = mock.patch("bot.sb", self.sb)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.done = Path(tempfile.mkdtemp()) / "done.txt"

    @staticmethod
    def by_cls(url):
        cls = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["corp_cls"][0]
        return {**FIXTURE, "list": [i for i in FIXTURE["list"] if i["corp_cls"] == cls]}

    def test_saves_only_classified_and_resumes(self):
        with mock.patch("bot.http_json", side_effect=self.by_cls) as h:
            self.assertEqual(backfill.backfill("20261112", "20261113", self.done), 3)
            self.assertEqual(h.call_count, 4)  # 2일 x 유가/코스닥
            backfill.backfill("20261112", "20261113", self.done)
            self.assertEqual(h.call_count, 4)  # 끝난 날은 다시 안 부름
        self.assertTrue(all(r["category"] for r in self.sb.disclosures.values()))
        self.assertEqual(self.done.read_text().split(), ["20261112", "20261113"])

    def test_stops_on_daily_limit_without_marking_day(self):
        limit = {"status": "020", "message": "요청 제한을 초과하였습니다."}
        with mock.patch("bot.http_json", side_effect=[self.by_cls("?corp_cls=Y"), self.by_cls("?corp_cls=K"), limit]):
            backfill.backfill("20261112", "20261113", self.done)
        self.assertEqual(self.done.read_text().split(), ["20261112"])


class PriceTest(unittest.TestCase):
    def test_parse_chart(self):
        xml = """<chartdata symbol="005930" name="삼성전자">
	<item data="20261112|208500|214000|206000|206500|19721557" />
	<item data="20261113|214000|216000|205000|209000|12829515" />
</chartdata>"""
        self.assertEqual(prices.parse_chart(xml), [
            ["20261112", "208500", "214000", "206000", "206500", "19721557"],
            ["20261113", "214000", "216000", "205000", "209000", "12829515"],
        ])
        self.assertEqual(prices.parse_chart("<chartdata></chartdata>"), [])

    def test_empty_chart_keeps_old_file(self):
        d = Path(tempfile.mkdtemp())
        (d / "X.csv").write_text("old")
        with mock.patch("prices.PRICE_DIR", d), mock.patch("prices.INDEXES", ["X"]), \
             mock.patch("prices.stock_codes", return_value=[]), mock.patch("prices.fresh", return_value=False), \
             mock.patch("prices.fetch_chart", return_value=[]), mock.patch("bot.check_env"):
            prices.main()
        self.assertEqual((d / "X.csv").read_text(), "old")


class AnalyzeTest(unittest.TestCase):
    STOCK = [("20261102", 100, 100), ("20261103", 100, 110), ("20261104", 110, 99),
             ("20261105", 99, 120), ("20261106", 120, 121), ("20261109", 121, 130)]
    INDEX = {d: (1000, 1010) for d, _, _ in STOCK}

    def test_abnormal_return_from_next_day_open(self):
        e = {"rcept_dt": "20261102", "category": "유상증자"}
        ret, reason = analyze.event_returns(e, self.STOCK, self.INDEX)
        self.assertIsNone(reason)
        # 기준가 = 11/03 시가 100, 지수는 1000 → 그날 종가 1010
        self.assertAlmostEqual(ret[1], 0.10 - 0.01)
        self.assertAlmostEqual(ret[3], 0.20 - 0.01)
        self.assertAlmostEqual(ret[5], 0.30 - 0.01)

    def test_long_gap_is_trading_halt(self):
        stock = [("20261102", 100, 100)] + [(f"202611{d}", 100, 100) for d in range(20, 26)]
        ret, reason = analyze.event_returns({"rcept_dt": "20261102", "category": "감자"}, stock,
                                            {d: (1, 1) for d, _, _ in stock})
        self.assertEqual(reason, "거래정지")

    def test_zero_volume_days_are_dropped(self):
        csv_text = "date,open,high,low,close,volume\n20261102,100,1,1,100,5\n20261103,100,1,1,100,0\n"
        d = Path(tempfile.mkdtemp())
        (d / "X.csv").write_text(csv_text)
        with mock.patch("prices.PRICE_DIR", d):
            self.assertEqual(analyze.load_prices("X"), [("20261102", 100.0, 100.0)])


class TraderTest(unittest.TestCase):
    DAYS = ["20261113", "20261116", "20261117"]  # 금, 월, 화 → 오늘은 수 20261118

    def row(self, no, dt, code, nm="주요사항보고서(자기주식취득결정)"):
        return {"rcept_no": no, "rcept_dt": dt, "stock_code": code, "report_nm": nm}

    def test_pick_only_previous_trading_day(self):
        rows = [self.row("1", "20261116", "A"),          # 월요일 공시 → 화요일에 샀어야 함
                self.row("2", "20261117", "B"),          # 화요일 공시 → 오늘 매수
                self.row("3", "20261117", "C", "[기재정정]주요사항보고서(자기주식취득결정)"),
                self.row("4", "20261117", "D"),          # 이미 보유
                self.row("5", "20261117", "E"),          # 이미 처리
                self.row("6", "20261117", "B")]          # 같은 종목 두 번
        got = trader.pick_signals(rows, self.DAYS, done={"5"}, held={"D"})
        self.assertEqual([r["rcept_no"] for r in got], ["2"])

    def test_sell_on_third_trading_day(self):
        self.assertFalse(trader.due_to_sell("20261117", self.DAYS))  # 화 매수, 오늘 수 = 2일째
        self.assertTrue(trader.due_to_sell("20261116", self.DAYS))   # 월 매수, 오늘 수 = 3일째
        self.assertTrue(trader.due_to_sell("20261113", self.DAYS))   # 놓쳤으면 바로 팜

    def test_buyback_qty_matches_same_day(self):
        detail = {"20231117000309": "654,665", "20260226004558": "-"}
        self.assertEqual(trader.buyback_qty(detail, "20231117000309"), 654665)
        self.assertEqual(trader.buyback_qty(detail, "20231117000243"), 654665)  # 같은 날 다시 낸 공시
        self.assertIsNone(trader.buyback_qty(detail, "20260226004558"))        # 수량 "-"
        self.assertIsNone(trader.buyback_qty(detail, "20250101000001"))

    def test_buy_skips_small_and_buys_bigger_first(self):
        rows = [{"rcept_no": n, "rcept_dt": "20261117", "corp_code": n, "stock_code": n, "corp_name": n,
                 "report_nm": "주요사항보고서(자기주식취득결정)"} for n in ["small", "mid", "big"]]
        size = {"small": "500", "mid": "2000", "big": "5000"}  # 상장주식 10만주 → 0.5%, 2%, 5%
        sb_calls = []

        def sb(method, path, body=None, *a, **k):
            sb_calls.append((method, path, body))
            return rows if path.startswith("disclosures") else []

        with mock.patch.object(trader, "MAX_HOLD", 1), mock.patch("trader.check_env"), \
             mock.patch("trader.trading_days", return_value=self.DAYS), mock.patch("bot.sb", sb), \
             mock.patch("trader.dart_buyback", side_effect=lambda c, b, e: {c: size[c]}), \
             mock.patch("trader.quote", return_value=(10000, 100000)), mock.patch("trader.holdings", return_value={}), \
             mock.patch("trader.order", return_value="1") as order, mock.patch("bot.send_telegram") as send, \
             mock.patch("trader.save_cash"):
            trader.buy("20261118")
        order.assert_called_once_with("buy", "big", 100)  # 자리 1개 → 제일 큰 것
        text = send.call_args.args[0]
        self.assertIn("건너뜀 small: 매입 비율 0.50%", text)
        self.assertIn("매수 big(big) 100주, 매입 비율 5.00%", text)

    def test_settle_returns(self):
        trades = [{"rcept_no": "1", "corp_name": "가"}, {"rcept_no": "2", "corp_name": "나"}]
        filled = {"1": ((10, 10000.0), (10, 10500.0)), "2": ((10, 10000.0), None)}  # 나는 매도 아직 체결 안 됨
        closed = [{"buy_price": 100.0, "sell_price": 90.0, "sell_qty": 10}]  # 예전 거래 -10%, -100원
        updates, lines = trader.settle_lines(trades, filled, closed)
        self.assertEqual(updates, [("1", {"buy_price": 10000.0, "sell_price": 10500.0, "sell_qty": 10})])
        self.assertEqual(lines[0], "가 10주 +5.00% (+5,000원)")
        self.assertIn("확인 안 됨 나", lines[1])
        self.assertEqual(lines[2], "누적 2종목 평균 -2.50%, +4,900원 (수수료·세금 전)")

    def test_fill_buy_prices(self):
        trades = [{"rcept_no": "0", "stock_code": "X", "corp_name": "엑스", "buy_date": "20261119"},  # 조회 실패
                  {"rcept_no": "1", "stock_code": "A", "corp_name": "에이", "buy_date": "20261120"},
                  {"rcept_no": "2", "stock_code": "B", "corp_name": "비", "buy_date": "20261123"}]  # 아직 체결 안 됨

        def fill(c, s, d):
            if c == "X":
                raise RuntimeError("KIS 500")
            return (10, 9870.0) if c == "A" else None

        with mock.patch("bot.sb", return_value=trades) as sb, mock.patch("trader.fill", side_effect=fill), \
             mock.patch("bot.alert_error") as alert:
            trader.fill_buy_prices()
        self.assertIn("sell_date=is.null", sb.call_args_list[0].args[1])  # buy_price 있어도 보유 중이면 다시 채움
        sb.assert_called_with("PATCH", "trades?rcept_no=eq.1", {"qty": 10, "buy_price": 9870.0})
        self.assertEqual(sb.call_count, 2)  # GET 1번 + PATCH 1번
        self.assertIn("엑스", alert.call_args.args[0])

    def test_settle_skips_failed_lookup(self):
        trades = [{"rcept_no": "1", "corp_name": "가", "stock_code": "A", "buy_date": "20261118", "sell_date": "20261120",
                   "buy_odno": "11", "sell_odno": "21"},
                  {"rcept_no": "2", "corp_name": "나", "stock_code": "B", "buy_date": "20261118", "sell_date": "20261120",
                   "buy_odno": "12", "sell_odno": "22"}]

        def sb(method, path, body=None, *a, **k):
            return trades if "sell_odno=not.is.null" in path else []

        def fill(c, side, d):
            if c == "B":
                raise RuntimeError("KIS 500")
            return (10, 10000.0) if side == "buy" else (10, 10500.0)

        with mock.patch("trader.check_env"), mock.patch("bot.sb", side_effect=sb) as sbm, \
             mock.patch("trader.fill", side_effect=fill), mock.patch("trader.fill_buy_prices"), \
             mock.patch("trader.save_cash") as save, mock.patch("trader.reconcile"), mock.patch("bot.send_telegram") as send:
            trader.settle("20261120")
        sbm.assert_any_call("PATCH", "trades?rcept_no=eq.1", {"buy_price": 10000.0, "sell_price": 10500.0, "sell_qty": 10})
        text = send.call_args.args[0]
        self.assertIn("실패 나: KIS 500", text)
        self.assertIn("가 10주 +5.00%", text)
        save.assert_called_once()

    def test_fill_uses_summary(self):
        res = {"output1": [], "output2": {"tot_ccld_qty": "312", "tot_ccld_amt": "1001520"}}
        with mock.patch("trader.kis", return_value=res) as kis, mock.patch("trader.account", return_value={}):
            self.assertEqual(trader.fill("223250", "buy", "20261120"), (312, 3210.0))
        self.assertEqual(kis.call_args.args[3]["SLL_BUY_DVSN_CD"], "02")
        with mock.patch("trader.kis", return_value={"output2": {"tot_ccld_qty": "0", "tot_ccld_amt": "0"}}), \
             mock.patch("trader.account", return_value={}):
            self.assertIsNone(trader.fill("223250", "sell", "20261120"))

    def test_save_cash(self):
        res = {"output1": [], "output2": [{"prvs_rcdl_excc_amt": "8007200", "tot_evlu_amt": "9992960"}]}
        with mock.patch("trader.balance", return_value=res), mock.patch("bot.sb") as sb:
            trader.save_cash()
        body = sb.call_args.args[2]
        self.assertEqual((body["id"], body["cash"], body["total"]), (1, 8007200, 9992960))


class FetchNowTest(unittest.TestCase):
    def test_parses_comma_price(self):
        body = io.BytesIO(json.dumps({"datas": [{"itemCode": "005930", "closePrice": "175,700"},
                                                {"itemCode": "002450", "closePrice": "991"}]}).encode())
        with mock.patch("urllib.request.urlopen", return_value=body) as urlopen:
            self.assertEqual(prices.fetch_now(("005930", "002450")), {"005930": 175700, "002450": 991})
        self.assertTrue(urlopen.call_args.args[0].full_url.endswith("/005930,002450"))


if __name__ == "__main__":
    unittest.main()


class BacktestTest(unittest.TestCase):
    def test_simulate_follows_bot_rules(self):
        days = ["d1", "d2", "d3", "d4"]
        prices = {"A": {d: (1000, 1100, 1000) for d in days},
                  "B": {"d1": (500, 500, 500), "d2": (500, 500, 500), "d4": (500, 600, 500)}}  # B는 d3 거래정지
        signals = {"d1": [(0.02, "A", "a"), (0.005, "C", "c"), (0.03, "B", "b")]}  # C는 1% 미만
        equity, trades = backtest.simulate(signals, days, prices, fee=0, tax=0)
        self.assertEqual([(t["code"], t["sell_date"]) for t in trades], [("A", "d3"), ("B", "d4")])
        self.assertEqual(trades[0]["qty"], 1000)  # 100만원 / 전날 종가 1000
        self.assertAlmostEqual(trades[0]["ret"], 0.1)
        self.assertEqual(equity[-1][1], backtest.START_CASH + 1000 * 100 + 2000 * 100)


class TraderLedgerTest(unittest.TestCase):
    """주문 전에 장부부터 쓰고, 끝나면 장부와 계좌를 맞춰봄"""
    ROW = {"rcept_no": "r1", "rcept_dt": "20261117", "corp_code": "c", "stock_code": "A", "corp_name": "에이",
           "report_nm": "주요사항보고서(자기주식취득결정)"}

    def run_buy(self, order, in_account=()):
        calls = []

        def sb(method, path, body=None, *a, **k):
            calls.append((method, path.split("?")[0]))
            return [self.ROW] if path.startswith("disclosures") else []

        with mock.patch("trader.check_env"), mock.patch("trader.trading_days", return_value=TraderTest.DAYS), \
             mock.patch("bot.sb", sb), mock.patch("trader.dart_buyback", return_value={"r1": "5000"}), \
             mock.patch("trader.quote", return_value=(10000, 100000)), \
             mock.patch("trader.holdings", return_value={c: 1 for c in in_account}), \
             mock.patch("trader.order", side_effect=order) as o, mock.patch("bot.send_telegram") as send, \
             mock.patch("trader.fill_buy_prices"), mock.patch("trader.save_cash"):
            trader.buy("20261118")
        return calls, o, send.call_args.args[0]

    def test_ledger_written_before_order(self):
        calls, _, _ = self.run_buy(lambda *a: "77")
        self.assertEqual([c for c in calls if c[1] == "trades"][1:], [("POST", "trades"), ("PATCH", "trades")])

    def test_rejected_order_removed_from_ledger(self):
        def order(*a):
            raise trader.Rejected("KIS VTTC0012U 40580000: 장운영일자가 아닙니다")
        calls, _, text = self.run_buy(order)
        self.assertIn(("DELETE", "trades"), calls)  # 지워야 다음 거래일에 다시 삼
        self.assertIn("실패 에이", text)

    def test_unknown_order_stays_in_ledger(self):
        def order(*a):
            raise TimeoutError("timed out")
        calls, _, text = self.run_buy(order)
        self.assertNotIn(("DELETE", "trades"), calls)  # 남겨야 09:05에 또 안 삼
        self.assertIn("확인 필요 에이: 주문 결과 모름", text)

    def test_rate_limit_retries_get_only(self):
        busy = RuntimeError('KIS 500 /x: {"rt_cd":"1","msg_cd":"EGW00215"}')
        env = {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s"}
        with mock.patch.dict(os.environ, env), mock.patch("trader.token", return_value="t"), \
             mock.patch("time.sleep"), mock.patch("trader._http", side_effect=[busy, {"rt_cd": "0"}]) as h:
            trader.kis("GET", "/x", "T")
        self.assertEqual(h.call_count, 2)
        with mock.patch.dict(os.environ, env), mock.patch("trader.token", return_value="t"), \
             mock.patch("time.sleep"), mock.patch("trader._http", side_effect=[busy]) as h:
            self.assertRaises(RuntimeError, trader.kis, "POST", "/x", "T")  # 주문은 다시 안 냄
        self.assertEqual(h.call_count, 1)

    def test_skip_when_already_in_account(self):
        calls, order, text = self.run_buy(lambda *a: "77", in_account=["A"])
        order.assert_not_called()
        self.assertIn("건너뜀 에이: 장부에 없는데 계좌에 있음", text)

    def test_reconcile(self):
        book = [{"stock_code": "A", "corp_name": "에이", "qty": 10}, {"stock_code": "B", "corp_name": "비", "qty": 5}]
        with mock.patch("trader.check_env"), mock.patch("bot.sb", return_value=book), mock.patch("bot.send_telegram") as send, \
             mock.patch("trader.holdings", return_value={"A": 10, "B": 3, "C": 7}):
            trader.reconcile()
        text = send.call_args.args[0]
        self.assertNotIn("에이", text)
        self.assertIn("비(B) 장부 5주 · 계좌 3주", text)
        self.assertIn("C(C) 장부 0주 · 계좌 7주", text)
        with mock.patch("trader.check_env"), mock.patch("bot.sb", return_value=book[:1]), mock.patch("bot.send_telegram") as send, \
             mock.patch("trader.holdings", return_value={"A": 10}):
            trader.reconcile()
        send.assert_not_called()  # 맞으면 조용히

    def test_no_order_when_ledger_write_fails(self):
        def sb(method, path, body=None, *a, **k):
            if method == "POST":
                raise RuntimeError("Supabase 503 trades")
            return [self.ROW] if path.startswith("disclosures") else []

        with mock.patch("trader.check_env"), mock.patch("trader.trading_days", return_value=TraderTest.DAYS), \
             mock.patch("bot.sb", sb), mock.patch("trader.dart_buyback", return_value={"r1": "5000"}), \
             mock.patch("trader.quote", return_value=(10000, 100000)), mock.patch("trader.holdings", return_value={}), \
             mock.patch("trader.order") as order, mock.patch("bot.send_telegram") as send, \
             mock.patch("trader.fill_buy_prices"), mock.patch("trader.save_cash"):
            trader.buy("20261118")
        order.assert_not_called()
        self.assertIn("실패 에이: 장부 쓰기", send.call_args.args[0])
