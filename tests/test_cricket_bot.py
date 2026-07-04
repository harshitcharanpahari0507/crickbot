import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot import cricket_bot as cb


def make_match(id="m1", teams=("India", "Australia")):
    return {"id": id, "teams": list(teams), "name": f"{teams[0]} vs {teams[1]}"}


class FilterTests(unittest.TestCase):
    def test_india_international_is_relevant(self):
        self.assertTrue(cb.is_india_international(make_match(teams=("India", "Australia"))))

    def test_india_a_is_not_india_international(self):
        self.assertFalse(cb.is_india_international(make_match(teams=("India A", "Australia A"))))

    def test_india_women_is_not_india_international(self):
        self.assertFalse(cb.is_india_international(make_match(teams=("India Women", "England Women"))))

    def test_ipl_match_is_relevant(self):
        m = make_match(teams=("Mumbai Indians", "Chennai Super Kings"))
        self.assertTrue(cb.is_ipl_match(m))
        self.assertTrue(cb.is_relevant(m))

    def test_non_ipl_domestic_match_is_not_relevant(self):
        m = make_match(teams=("Mumbai", "Karnataka"))
        self.assertFalse(cb.is_relevant(m))

    def test_other_country_match_is_not_relevant(self):
        m = make_match(teams=("Australia", "England"))
        self.assertFalse(cb.is_relevant(m))


class ApiBudgetTests(unittest.TestCase):
    def test_blocks_calls_once_daily_cap_reached(self):
        state = cb.default_state()
        state["date"] = cb.today_str()
        state["calls_today"] = cb.MAX_CALLS_PER_DAY
        budget = cb.ApiBudget(state)
        self.assertFalse(budget.can_call())

    def test_blocks_calls_once_per_run_cap_reached(self):
        state = cb.default_state()
        budget = cb.ApiBudget(state)
        for _ in range(cb.MAX_CALLS_PER_RUN):
            self.assertTrue(budget.can_call())
            budget.record_call()
        self.assertFalse(budget.can_call())

    def test_resets_daily_counter_on_new_day(self):
        state = cb.default_state()
        state["date"] = "2000-01-01"
        state["calls_today"] = 999
        budget = cb.ApiBudget(state)
        self.assertEqual(state["calls_today"], 0)
        self.assertNotEqual(state["date"], "2000-01-01")

    def test_quota_exhausted_blocks_calls_same_day(self):
        state = cb.default_state()
        budget = cb.ApiBudget(state)
        budget.mark_quota_exhausted()
        self.assertTrue(budget.quota_exhausted_today)
        self.assertFalse(budget.can_call())

    def test_quota_exhausted_flag_clears_on_new_day(self):
        state = cb.default_state()
        state["date"] = "2000-01-01"
        state["quota_exhausted_date"] = "2000-01-01"
        budget = cb.ApiBudget(state)
        self.assertFalse(budget.quota_exhausted_today)


class SeriesDateParsingTests(unittest.TestCase):
    def test_parses_full_iso_date(self):
        self.assertEqual(cb.parse_series_date("2026-07-01"), cb.datetime(2026, 7, 1).date())

    def test_parses_month_day_using_reference_year(self):
        result = cb.parse_series_date("Jul 19", "2026-07-01")
        self.assertEqual(result, cb.datetime(2026, 7, 19).date())

    def test_rolls_forward_year_on_boundary_crossing(self):
        result = cb.parse_series_date("Jan 07", "2026-12-20")
        self.assertEqual(result, cb.datetime(2027, 1, 7).date())

    def test_returns_none_for_garbage(self):
        self.assertIsNone(cb.parse_series_date("not a date"))
        self.assertIsNone(cb.parse_series_date(""))
        self.assertIsNone(cb.parse_series_date(None))


class SeriesNameHeuristicTests(unittest.TestCase):
    def test_excludes_non_senior_series(self):
        self.assertFalse(cb.is_series_name_worth_expanding("India U19 tour of Sri Lanka 2026"))
        self.assertFalse(cb.is_series_name_worth_expanding("India A tour of Sri Lanka 2026"))
        self.assertFalse(cb.is_series_name_worth_expanding("England Women tour of India 2026"))

    def test_allows_senior_series(self):
        self.assertTrue(cb.is_series_name_worth_expanding("India tour of England, 2026"))
        self.assertTrue(cb.is_series_name_worth_expanding("Indian Premier League 2026"))


class RefreshWatchedSeriesTests(unittest.TestCase):
    def _state(self):
        return cb.default_state()

    @patch("bot.cricket_bot.fetch_series_info")
    @patch("bot.cricket_bot.fetch_series_search")
    def test_caches_series_covering_today_with_relevant_matches(self, mock_search, mock_info):
        mock_search.return_value = [
            {
                "id": "s1",
                "name": "India tour of England, 2026",
                "startDate": "2026-07-01",
                "endDate": "Jul 19",
            }
        ]
        mock_info.return_value = [
            make_match(id="m1", teams=("England", "India")),
            make_match(id="m2", teams=("Somewhere A", "Somewhere B")),
        ]
        state = self._state()
        budget = cb.ApiBudget(state)
        today = cb.datetime(2026, 7, 4).date()

        cached = cb.refresh_watched_series("india", "India", "key", state, budget, today)

        self.assertTrue(cached)
        self.assertIn("s1", state["watched_series"])
        matches = state["watched_series"]["s1"]["matches"]
        self.assertEqual([m["id"] for m in matches], ["m1"])
        self.assertEqual(state["watched_series"]["s1"]["end_date"], "2026-07-19")

    @patch("bot.cricket_bot.fetch_series_info")
    @patch("bot.cricket_bot.fetch_series_search")
    def test_skips_series_not_covering_today_or_soon(self, mock_search, mock_info):
        mock_search.return_value = [
            {"id": "s1", "name": "India tour of Zimbabwe 2026", "startDate": "2026-07-23", "endDate": "Jul 26"}
        ]
        state = self._state()
        budget = cb.ApiBudget(state)
        today = cb.datetime(2026, 7, 4).date()

        cb.refresh_watched_series("india", "India", "key", state, budget, today)

        mock_info.assert_not_called()
        self.assertEqual(state["watched_series"], {})

    @patch("bot.cricket_bot.fetch_series_info")
    @patch("bot.cricket_bot.fetch_series_search")
    def test_skips_non_senior_series_without_expanding(self, mock_search, mock_info):
        mock_search.return_value = [
            {"id": "s1", "name": "India U19 tour of Sri Lanka 2026", "startDate": "2026-07-01", "endDate": "Jul 23"}
        ]
        state = self._state()
        budget = cb.ApiBudget(state)
        today = cb.datetime(2026, 7, 4).date()

        cb.refresh_watched_series("india", "India", "key", state, budget, today)

        mock_info.assert_not_called()

    @patch("bot.cricket_bot.fetch_series_info")
    @patch("bot.cricket_bot.fetch_series_search")
    def test_caps_expansions_per_refresh(self, mock_search, mock_info):
        mock_search.return_value = [
            {"id": f"s{i}", "name": f"India tour of Place{i}", "startDate": "2026-07-01", "endDate": "Jul 19"}
            for i in range(10)
        ]
        mock_info.return_value = []
        state = self._state()
        budget = cb.ApiBudget(state)
        today = cb.datetime(2026, 7, 4).date()

        cb.refresh_watched_series("india", "India", "key", state, budget, today)

        self.assertEqual(mock_info.call_count, cb.MAX_NEW_SERIES_EXPANSIONS_PER_REFRESH)


class MaybeRefreshSeriesTests(unittest.TestCase):
    @patch("bot.cricket_bot.refresh_watched_series", return_value=True)
    def test_skips_when_series_already_active(self, mock_refresh):
        state = cb.default_state()
        state["watched_series"]["s1"] = {"kind": "india", "end_date": "2026-07-19", "matches": []}
        budget = cb.ApiBudget(state)
        cb.maybe_refresh_series("india", "India", "key", state, budget, cb.datetime(2026, 7, 4).date())
        mock_refresh.assert_not_called()

    @patch("bot.cricket_bot.refresh_watched_series", return_value=False)
    def test_sets_throttle_when_nothing_found(self, mock_refresh):
        state = cb.default_state()
        budget = cb.ApiBudget(state)
        today = cb.datetime(2026, 7, 4).date()
        cb.maybe_refresh_series("ipl", "Indian Premier League", "key", state, budget, today)
        expected_retry = (today + cb.timedelta(days=cb.SEARCH_RETRY_DAYS["ipl"])).isoformat()
        self.assertEqual(state["series_search_next"]["ipl"], expected_retry)

    @patch("bot.cricket_bot.refresh_watched_series")
    def test_respects_throttle_and_does_not_research(self, mock_refresh):
        state = cb.default_state()
        state["series_search_next"]["ipl"] = "2026-07-10"
        budget = cb.ApiBudget(state)
        cb.maybe_refresh_series("ipl", "Indian Premier League", "key", state, budget, cb.datetime(2026, 7, 4).date())
        mock_refresh.assert_not_called()


class WatchedSeriesCollectionTests(unittest.TestCase):
    def test_prune_removes_expired_series(self):
        state = cb.default_state()
        state["watched_series"]["old"] = {"kind": "india", "end_date": "2026-01-01", "matches": []}
        state["watched_series"]["current"] = {"kind": "india", "end_date": "2026-12-01", "matches": []}
        cb.prune_watched_series(state, "2026-07-04")
        self.assertNotIn("old", state["watched_series"])
        self.assertIn("current", state["watched_series"])

    def test_collect_only_returns_matches_dated_today(self):
        state = cb.default_state()
        state["watched_series"]["s1"] = {
            "kind": "india",
            "end_date": "2026-07-19",
            "matches": [
                {"id": "m1", "date": "2026-07-04"},
                {"id": "m2", "date": "2026-07-07"},
            ],
        }
        result = cb.collect_watched_matches_for_today(state, "2026-07-04")
        self.assertEqual([m["id"] for m in result], ["m1"])


class CurrentMatchesPaginationTests(unittest.TestCase):
    @patch("bot.cricket_bot.requests.get")
    def test_stops_at_first_short_page(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "success", "data": [{"id": "m1"}], "info": {}},
        )
        budget = cb.ApiBudget(cb.default_state())
        result = cb.fetch_current_matches("key", budget)
        self.assertEqual(len(result), 1)
        self.assertEqual(mock_get.call_count, 1)

    @patch("bot.cricket_bot.requests.get")
    def test_pages_until_full_page_boundary(self, mock_get):
        full_page = [{"id": f"m{i}"} for i in range(cb.CURRENT_MATCHES_PAGE_SIZE)]
        short_page = [{"id": "last"}]
        responses = [full_page, short_page]

        def side_effect(*args, **kwargs):
            data = responses.pop(0)
            return MagicMock(status_code=200, json=lambda: {"status": "success", "data": data, "info": {}})

        mock_get.side_effect = side_effect
        budget = cb.ApiBudget(cb.default_state())
        result = cb.fetch_current_matches("key", budget)
        self.assertEqual(len(result), cb.CURRENT_MATCHES_PAGE_SIZE + 1)
        self.assertEqual(mock_get.call_count, 2)

    @patch("bot.cricket_bot.requests.get")
    def test_stops_at_max_pages_cap(self, mock_get):
        full_page = [{"id": f"m{i}"} for i in range(cb.CURRENT_MATCHES_PAGE_SIZE)]
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "success", "data": full_page, "info": {}},
        )
        budget = cb.ApiBudget(cb.default_state())
        cb.fetch_current_matches("key", budget)
        self.assertEqual(mock_get.call_count, cb.MAX_CURRENT_MATCHES_PAGES)


class ScorecardShapeTests(unittest.TestCase):
    @patch("bot.cricket_bot.requests.get")
    def test_scorecard_unwraps_nested_scorecard_key(self, mock_get):
        # Real API response nests innings under data["scorecard"], not data itself.
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "status": "success",
                "data": {"id": "m1", "scorecard": [{"inning": "X Inning 1", "batting": []}]},
                "info": {"hitsToday": 3, "hitsLimit": 100},
            },
        )
        budget = cb.ApiBudget(cb.default_state())
        result = cb.fetch_match_scorecard("key", "m1", budget)
        self.assertEqual(result, [{"inning": "X Inning 1", "batting": []}])


class BudgetSyncTests(unittest.TestCase):
    def test_sync_adopts_higher_api_reported_count(self):
        budget = cb.ApiBudget(cb.default_state())
        budget.sync_from_api_info({"hitsToday": 42, "hitsLimit": 100})
        self.assertEqual(budget.state["calls_today"], 42)
        self.assertFalse(budget.quota_exhausted_today)

    def test_sync_marks_exhausted_within_safety_buffer(self):
        budget = cb.ApiBudget(cb.default_state())
        budget.sync_from_api_info({"hitsToday": 96, "hitsLimit": 100})
        self.assertTrue(budget.quota_exhausted_today)

    def test_sync_ignores_malformed_info(self):
        budget = cb.ApiBudget(cb.default_state())
        budget.sync_from_api_info({"hitsToday": "oops"})
        self.assertFalse(budget.quota_exhausted_today)
        budget.sync_from_api_info(None)
        self.assertFalse(budget.quota_exhausted_today)


class CricapiGetTests(unittest.TestCase):
    def _budget(self):
        return cb.ApiBudget(cb.default_state())

    @patch("bot.cricket_bot.requests.get")
    def test_success_returns_data_and_records_one_call(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, json=lambda: {"status": "success", "data": {"foo": "bar"}}
        )
        budget = self._budget()
        result = cb.cricapi_get("match_info", {"id": "1"}, budget)
        self.assertEqual(result, {"foo": "bar"})
        self.assertEqual(budget.calls_this_run, 1)

    @patch("bot.cricket_bot.requests.get")
    def test_quota_message_sets_exhausted_flag_and_stops(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"status": "failure", "reason": "You have exceeded your daily hit limit"},
        )
        budget = self._budget()
        result = cb.cricapi_get("currentMatches", {}, budget)
        self.assertIsNone(result)
        self.assertTrue(budget.quota_exhausted_today)

    @patch("bot.cricket_bot.requests.get")
    def test_does_not_call_when_budget_exhausted(self, mock_get):
        budget = self._budget()
        budget.state["calls_today"] = cb.MAX_CALLS_PER_DAY
        result = cb.cricapi_get("match_info", {"id": "1"}, budget)
        self.assertIsNone(result)
        mock_get.assert_not_called()

    @patch("bot.cricket_bot.time.sleep", return_value=None)
    @patch("bot.cricket_bot.requests.get")
    def test_retries_at_most_max_attempts(self, mock_get, mock_sleep):
        mock_get.side_effect = cb.requests.RequestException("boom")
        budget = self._budget()
        result = cb.cricapi_get("match_info", {"id": "1"}, budget)
        self.assertIsNone(result)
        self.assertEqual(mock_get.call_count, cb.MAX_HTTP_ATTEMPTS)


class DedupTests(unittest.TestCase):
    def setUp(self):
        self.match = make_match()
        self.state = cb.default_state()
        self.match_state = cb.get_match_state(self.state, "m1")
        self.config = {"dry_run": True, "bot_token": None, "chat_id": None}

    def test_century_sent_once_even_if_score_increases(self):
        scorecard = [
            {
                "inning": "India Inning 1",
                "batting": [{"batsman": {"name": "V Kohli"}, "r": 102, "b": 90}],
                "bowling": [],
            }
        ]
        with patch("bot.cricket_bot.send_telegram", return_value=True) as mock_send:
            cb.process_scorecard_events(self.match, scorecard, self.match_state, self.config)
            self.assertEqual(mock_send.call_count, 1)

        scorecard[0]["batting"][0]["r"] = 145
        with patch("bot.cricket_bot.send_telegram", return_value=True) as mock_send:
            cb.process_scorecard_events(self.match, scorecard, self.match_state, self.config)
            mock_send.assert_not_called()

    def test_fivefer_sent_once(self):
        scorecard = [
            {
                "inning": "Australia Inning 1",
                "batting": [],
                "bowling": [{"bowler": {"name": "J Bumrah"}, "w": 5, "r": 40}],
            }
        ]
        with patch("bot.cricket_bot.send_telegram", return_value=True) as mock_send:
            cb.process_scorecard_events(self.match, scorecard, self.match_state, self.config)
            self.assertEqual(mock_send.call_count, 1)
        with patch("bot.cricket_bot.send_telegram", return_value=True) as mock_send:
            cb.process_scorecard_events(self.match, scorecard, self.match_state, self.config)
            mock_send.assert_not_called()

    def test_below_threshold_not_sent(self):
        scorecard = [
            {
                "inning": "India Inning 1",
                "batting": [{"batsman": {"name": "R Sharma"}, "r": 45, "b": 30}],
                "bowling": [{"bowler": {"name": "R Jadeja"}, "w": 2, "r": 20}],
            }
        ]
        with patch("bot.cricket_bot.send_telegram", return_value=True) as mock_send:
            cb.process_scorecard_events(self.match, scorecard, self.match_state, self.config)
            mock_send.assert_not_called()

    def test_state_not_updated_when_send_fails(self):
        scorecard = [
            {
                "inning": "India Inning 1",
                "batting": [{"batsman": {"name": "V Kohli"}, "r": 102, "b": 90}],
                "bowling": [],
            }
        ]
        with patch("bot.cricket_bot.send_telegram", return_value=False):
            cb.process_scorecard_events(self.match, scorecard, self.match_state, self.config)
        self.assertEqual(self.match_state["centuries_sent"], [])


class ProcessMatchTests(unittest.TestCase):
    def setUp(self):
        self.match = make_match()
        self.state = cb.default_state()
        self.config = {"dry_run": True, "api_key": "k", "bot_token": None, "chat_id": None}
        self.budget = cb.ApiBudget(self.state)

    @patch("bot.cricket_bot.fetch_match_scorecard")
    @patch("bot.cricket_bot.fetch_match_info")
    @patch("bot.cricket_bot.send_telegram", return_value=True)
    def test_completed_match_is_skipped_entirely(self, mock_send, mock_info, mock_scorecard):
        match_state = cb.get_match_state(self.state, "m1")
        match_state["completed"] = True
        match_state["result_sent"] = True
        cb.process_match(self.match, self.state, self.config, self.budget)
        mock_info.assert_not_called()
        mock_scorecard.assert_not_called()
        mock_send.assert_not_called()

    @patch("bot.cricket_bot.fetch_match_scorecard", return_value=[])
    @patch("bot.cricket_bot.fetch_match_info")
    @patch("bot.cricket_bot.send_telegram", return_value=True)
    def test_toss_sent_once(self, mock_send, mock_info, mock_scorecard):
        mock_info.return_value = {
            "tossWinner": "India",
            "tossChoice": "bat",
            "matchStarted": True,
            "matchEnded": False,
        }
        cb.process_match(self.match, self.state, self.config, self.budget)
        match_state = self.state["matches"]["m1"]
        self.assertTrue(match_state["toss_sent"])
        self.assertEqual(mock_send.call_count, 1)

        cb.process_match(self.match, self.state, self.config, self.budget)
        self.assertEqual(mock_send.call_count, 1)

    @patch("bot.cricket_bot.fetch_match_scorecard", return_value=[])
    @patch("bot.cricket_bot.fetch_match_info")
    @patch("bot.cricket_bot.send_telegram", return_value=True)
    def test_result_marks_match_completed(self, mock_send, mock_info, mock_scorecard):
        mock_info.return_value = {
            "tossWinner": "India",
            "tossChoice": "bat",
            "matchStarted": True,
            "matchEnded": True,
            "status": "India won by 6 wickets",
        }
        cb.process_match(self.match, self.state, self.config, self.budget)
        match_state = self.state["matches"]["m1"]
        self.assertTrue(match_state["result_sent"])
        self.assertTrue(match_state["completed"])

    @patch("bot.cricket_bot.fetch_match_scorecard")
    @patch("bot.cricket_bot.fetch_match_info")
    def test_scorecard_not_fetched_before_match_starts(self, mock_info, mock_scorecard):
        mock_info.return_value = {
            "tossWinner": None,
            "tossChoice": None,
            "matchStarted": False,
            "matchEnded": False,
        }
        cb.process_match(self.match, self.state, self.config, self.budget)
        mock_scorecard.assert_not_called()


class DryRunTests(unittest.TestCase):
    @patch("bot.cricket_bot.requests.post")
    def test_dry_run_never_calls_telegram_http(self, mock_post):
        config = {"dry_run": True, "bot_token": "x", "chat_id": "y"}
        result = cb.send_telegram(config, "hello")
        self.assertTrue(result)
        mock_post.assert_not_called()

    @patch("bot.cricket_bot.requests.post")
    def test_live_mode_calls_telegram_http(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})
        config = {"dry_run": False, "bot_token": "x", "chat_id": "y"}
        result = cb.send_telegram(config, "hello")
        self.assertTrue(result)
        mock_post.assert_called_once()


class TelegramSelfTestTests(unittest.TestCase):
    @patch("bot.cricket_bot.fetch_current_matches")
    @patch("bot.cricket_bot.send_telegram", return_value=True)
    def test_self_test_sends_message_and_skips_polling(self, mock_send, mock_fetch):
        env = {"CRICKET_API_KEY": "k", "DRY_RUN": "false", "SEND_TEST_MESSAGE": "true",
               "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
        with patch.dict(os.environ, env, clear=True):
            cb.run("unused_state_path.json")
        mock_send.assert_called_once_with(
            {"api_key": "k", "bot_token": "t", "chat_id": "c", "dry_run": False},
            cb.TEST_MESSAGE,
        )
        mock_fetch.assert_not_called()

    @patch("bot.cricket_bot.send_telegram", return_value=False)
    def test_self_test_exits_nonzero_on_failure(self, mock_send):
        config = {"api_key": "k", "bot_token": "t", "chat_id": "c", "dry_run": False}
        with self.assertRaises(SystemExit):
            cb.run_telegram_self_test(config)


class ConfigTests(unittest.TestCase):
    def test_missing_api_key_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(cb.ConfigError):
                cb.load_config()

    def test_missing_telegram_creds_raises_outside_dry_run(self):
        env = {"CRICKET_API_KEY": "k", "DRY_RUN": "false"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(cb.ConfigError):
                cb.load_config()

    def test_dry_run_does_not_require_telegram_creds(self):
        env = {"CRICKET_API_KEY": "k", "DRY_RUN": "true"}
        with patch.dict(os.environ, env, clear=True):
            config = cb.load_config()
            self.assertTrue(config["dry_run"])


if __name__ == "__main__":
    unittest.main()
