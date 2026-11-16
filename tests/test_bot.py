"""오프라인 테스트. DART 응답은 가짜 픽스처, Supabase는 FakeSB로 대체"""
import json
import os
import unittest
import urllib.parse
from datetime import date
from pathlib import Path
from unittest import mock

import bot

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


if __name__ == "__main__":
    unittest.main()
