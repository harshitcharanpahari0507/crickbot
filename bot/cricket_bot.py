"""
Polls CricketData.org (api.cricapi.com v1) for India international and IPL
matches, and sends Telegram alerts for a small set of major events: toss
result, centuries, five-wicket hauls, and match result.

Designed to run once per invocation (no internal loop) so scheduling is
entirely owned by the caller (GitHub Actions cron). Safety is layered:

  1. Structural: no while-loop polling here, so a single run can only ever
     make a bounded number of HTTP calls (see MAX_CALLS_PER_RUN).
  2. Daily budget: a persisted counter in state.json caps total API calls
     per calendar day well under the free-tier limit (see MAX_CALLS_PER_DAY).
  3. Provider signal: if the API itself reports the daily quota exhausted,
     we stop calling it for the rest of the day instead of retrying.
  4. Dedup: every Telegram message has a stable key checked against state
     before sending, and state is only updated to "sent" after Telegram
     confirms delivery -- so the same event is never sent twice, and a
     failed send is retried on the next scheduled run rather than silently
     dropped or duplicated.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

CRICAPI_BASE = "https://api.cricapi.com/v1"
TELEGRAM_BASE = "https://api.telegram.org"

HTTP_TIMEOUT_SECONDS = 15
MAX_HTTP_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 3

# Conservative caps. The free plan is 100 hits/day; MAX_CALLS_PER_DAY leaves
# a buffer in case any single endpoint costs more than one hit.
MAX_CALLS_PER_DAY = int(os.environ.get("MAX_CALLS_PER_DAY", "90"))
MAX_CALLS_PER_RUN = int(os.environ.get("MAX_CALLS_PER_RUN", "8"))
MAX_MATCHES_PER_RUN = int(os.environ.get("MAX_MATCHES_PER_RUN", "5"))

# The API reports live usage via an "info" block on every response
# (hitsToday/hitsLimit). We treat that as authoritative over our own
# counter and stop early once within this many hits of the real limit.
QUOTA_SAFETY_BUFFER = int(os.environ.get("QUOTA_SAFETY_BUFFER", "5"))

# Team names that count as "the Indian Cricket Team" (senior international
# side only -- deliberately excludes "India A", "India Women", "India U19").
INDIA_TEAM_NAMES = {"india"}

# IPL franchise names, including recent historical renames, matched
# case-insensitively. Both teams in a match must be in this set.
IPL_TEAM_NAMES = {
    "mumbai indians",
    "chennai super kings",
    "royal challengers bengaluru",
    "royal challengers bangalore",
    "kolkata knight riders",
    "delhi capitals",
    "delhi daredevils",
    "punjab kings",
    "kings xi punjab",
    "rajasthan royals",
    "sunrisers hyderabad",
    "gujarat titans",
    "lucknow super giants",
}

CENTURY_THRESHOLD_RUNS = 100
FIVE_WICKET_HAUL_WICKETS = 5


def log(message):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{timestamp}] {message}", flush=True)


class ConfigError(Exception):
    pass


def load_config():
    api_key = os.environ.get("CRICKET_API_KEY")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    dry_run = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

    if not api_key:
        raise ConfigError("CRICKET_API_KEY is not set")
    if not dry_run and not bot_token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set (required outside dry-run)")
    if not dry_run and not chat_id:
        raise ConfigError("TELEGRAM_CHAT_ID is not set (required outside dry-run)")

    return {
        "api_key": api_key,
        "bot_token": bot_token,
        "chat_id": chat_id,
        "dry_run": dry_run,
    }


def default_state():
    return {
        "date": "",
        "calls_today": 0,
        "quota_exhausted_date": None,
        "matches": {},
    }


def load_state(path):
    if not os.path.exists(path):
        return default_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log(f"WARNING: failed to read state file ({e}); starting from a fresh state")
        return default_state()

    state.setdefault("date", "")
    state.setdefault("calls_today", 0)
    state.setdefault("quota_exhausted_date", None)
    state.setdefault("matches", {})
    return state


def save_state(path, state):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class ApiBudget:
    """Tracks and enforces the daily + per-run API call budget."""

    def __init__(self, state):
        self.state = state
        today = today_str()
        if state["date"] != today:
            state["date"] = today
            state["calls_today"] = 0
            if state.get("quota_exhausted_date") != today:
                state["quota_exhausted_date"] = None
        self.calls_this_run = 0

    @property
    def quota_exhausted_today(self):
        return self.state.get("quota_exhausted_date") == today_str()

    def mark_quota_exhausted(self):
        self.state["quota_exhausted_date"] = today_str()

    def can_call(self):
        if self.quota_exhausted_today:
            return False
        if self.state["calls_today"] >= MAX_CALLS_PER_DAY:
            return False
        if self.calls_this_run >= MAX_CALLS_PER_RUN:
            return False
        return True

    def record_call(self):
        self.state["calls_today"] += 1
        self.calls_this_run += 1

    def sync_from_api_info(self, info):
        """Reconcile our local counter with the API's own usage report and
        proactively stop for the day if we're within the safety buffer of
        the real limit -- this is authoritative over our own guess at how
        many hits each endpoint costs."""
        if not isinstance(info, dict):
            return
        hits_today = info.get("hitsToday")
        hits_limit = info.get("hitsLimit")
        if not isinstance(hits_today, int) or not isinstance(hits_limit, int):
            return
        self.state["calls_today"] = max(self.state["calls_today"], hits_today)
        if hits_today >= hits_limit - QUOTA_SAFETY_BUFFER:
            log(
                f"API-reported usage {hits_today}/{hits_limit} is within the "
                f"safety buffer; pausing further calls for today."
            )
            self.mark_quota_exhausted()


def cricapi_get(endpoint, params, budget):
    """GET a CricketData.org v1 endpoint, honoring the call budget.

    Returns the parsed `data` payload on success, or None if the call was
    skipped (budget exhausted) or failed. Never raises for ordinary
    HTTP/API failures -- callers treat None as "try again next run".
    """
    if not budget.can_call():
        log(f"SKIP {endpoint}: call budget exhausted for this run/day")
        return None

    url = f"{CRICAPI_BASE}/{endpoint}"
    last_error = None
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        budget.record_call()
        try:
            resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as e:
            last_error = str(e)
            log(f"WARNING: {endpoint} request failed (attempt {attempt}): {e}")
            if attempt < MAX_HTTP_ATTEMPTS and budget.can_call():
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            return None

        if resp.status_code != 200:
            last_error = f"HTTP {resp.status_code}"
            log(f"WARNING: {endpoint} returned {last_error} (attempt {attempt})")
            if attempt < MAX_HTTP_ATTEMPTS and budget.can_call():
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            return None

        try:
            body = resp.json()
        except ValueError:
            log(f"WARNING: {endpoint} returned non-JSON body")
            return None

        if body.get("status") != "success":
            reason = str(body.get("reason", "")).lower()
            if "limit" in reason or "exceed" in reason or "quota" in reason:
                log(f"API quota appears exhausted for today: {body.get('reason')}")
                budget.mark_quota_exhausted()
            else:
                log(f"WARNING: {endpoint} returned failure: {body.get('reason')}")
            return None

        budget.sync_from_api_info(body.get("info"))
        return body.get("data")

    log(f"WARNING: {endpoint} failed after {MAX_HTTP_ATTEMPTS} attempts: {last_error}")
    return None


def fetch_current_matches(api_key, budget):
    data = cricapi_get("currentMatches", {"apikey": api_key, "offset": 0}, budget)
    return data if isinstance(data, list) else []


def fetch_match_info(api_key, match_id, budget):
    return cricapi_get("match_info", {"apikey": api_key, "id": match_id}, budget)


def fetch_match_scorecard(api_key, match_id, budget):
    data = cricapi_get("match_scorecard", {"apikey": api_key, "id": match_id}, budget)
    if isinstance(data, dict):
        innings = data.get("scorecard")
        return innings if isinstance(innings, list) else []
    return data if isinstance(data, list) else []


def is_india_international(match):
    teams = [str(t).strip().lower() for t in match.get("teams", [])]
    return any(t in INDIA_TEAM_NAMES for t in teams)


def is_ipl_match(match):
    teams = [str(t).strip().lower() for t in match.get("teams", [])]
    if len(teams) != 2:
        return False
    return all(t in IPL_TEAM_NAMES for t in teams)


def is_relevant(match):
    return is_india_international(match) or is_ipl_match(match)


def match_label(match):
    teams = match.get("teams") or []
    if len(teams) == 2:
        return f"{teams[0]} vs {teams[1]}"
    return match.get("name", match.get("id", "unknown match"))


def get_match_state(state, match_id):
    return state["matches"].setdefault(
        match_id,
        {
            "toss_sent": False,
            "result_sent": False,
            "completed": False,
            "centuries_sent": [],
            "fivefers_sent": [],
        },
    )


def send_telegram(config, text):
    if config["dry_run"]:
        log(f"DRY-RUN Telegram message (not sent):\n{text}")
        return True

    url = f"{TELEGRAM_BASE}/bot{config['bot_token']}/sendMessage"
    payload = {
        "chat_id": config["chat_id"],
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        try:
            resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as e:
            log(f"WARNING: Telegram send failed (attempt {attempt}): {e}")
            if attempt < MAX_HTTP_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            return False

        if resp.status_code == 200 and resp.json().get("ok"):
            return True

        log(f"WARNING: Telegram send returned HTTP {resp.status_code}: {resp.text[:200]}")
        if attempt < MAX_HTTP_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_SECONDS)

    return False


def format_toss_message(match, info):
    winner = info.get("tossWinner")
    choice = info.get("tossChoice")
    if not winner or not choice:
        return None
    return f"*Toss*: {match_label(match)}\n{winner} won the toss and chose to {choice}."


def format_result_message(match, info):
    status = info.get("status")
    if not status:
        return None
    return f"*Result*: {match_label(match)}\n{status}"


def format_century_message(match, inning_label, player, runs, balls):
    ball_text = f" off {balls} balls" if balls is not None else ""
    return (
        f"*Century*: {match_label(match)}\n"
        f"{player} reached {runs}{ball_text} in {inning_label}."
    )


def format_fivefer_message(match, inning_label, bowler, wickets, runs_conceded):
    return (
        f"*Five-wicket haul*: {match_label(match)}\n"
        f"{bowler} took {wickets}/{runs_conceded} in {inning_label}."
    )


def extract_batting_entries(inning):
    for key in ("batting", "batsmen", "batTeamDetails"):
        entries = inning.get(key)
        if isinstance(entries, list):
            return entries
    return []


def extract_bowling_entries(inning):
    for key in ("bowling", "bowlers", "bowlTeamDetails"):
        entries = inning.get(key)
        if isinstance(entries, list):
            return entries
    return []


def player_name(entry, key_candidates):
    for key in key_candidates:
        value = entry.get(key)
        if isinstance(value, dict):
            name = value.get("name")
            if name:
                return name
        elif isinstance(value, str) and value:
            return value
    return None


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def process_scorecard_events(match, scorecard, match_state, config):
    for idx, inning in enumerate(scorecard):
        inning_label = inning.get("inning") or f"innings {idx + 1}"

        for batter in extract_batting_entries(inning):
            name = player_name(batter, ("batsman", "batName", "name"))
            runs = to_int(batter.get("r", batter.get("runs")))
            balls = to_int(batter.get("b", batter.get("balls")))
            if not name or runs is None or runs < CENTURY_THRESHOLD_RUNS:
                continue
            dedup_key = f"{name}|{inning_label}"
            if dedup_key in match_state["centuries_sent"]:
                continue
            text = format_century_message(match, inning_label, name, runs, balls)
            if text and send_telegram(config, text):
                match_state["centuries_sent"].append(dedup_key)

        for bowler in extract_bowling_entries(inning):
            name = player_name(bowler, ("bowler", "bowlName", "name"))
            wickets = to_int(bowler.get("w", bowler.get("wickets")))
            runs_conceded = to_int(bowler.get("r", bowler.get("runs")))
            if not name or wickets is None or wickets < FIVE_WICKET_HAUL_WICKETS:
                continue
            dedup_key = f"{name}|{inning_label}"
            if dedup_key in match_state["fivefers_sent"]:
                continue
            text = format_fivefer_message(match, inning_label, name, wickets, runs_conceded)
            if text and send_telegram(config, text):
                match_state["fivefers_sent"].append(dedup_key)


def process_match(match, state, config, budget):
    match_id = match.get("id")
    if not match_id:
        return

    match_state = get_match_state(state, match_id)
    if match_state["completed"] and match_state["result_sent"]:
        return

    info = fetch_match_info(config["api_key"], match_id, budget)
    if info is None:
        return

    if not match_state["toss_sent"]:
        text = format_toss_message(match, info)
        if text and send_telegram(config, text):
            match_state["toss_sent"] = True

    match_started = bool(info.get("matchStarted"))
    match_ended = bool(info.get("matchEnded"))

    if match_started and budget.can_call():
        scorecard = fetch_match_scorecard(config["api_key"], match_id, budget)
        if scorecard:
            process_scorecard_events(match, scorecard, match_state, config)

    if match_ended and not match_state["result_sent"]:
        text = format_result_message(match, info)
        if text and send_telegram(config, text):
            match_state["result_sent"] = True
            match_state["completed"] = True


def prune_old_matches(state, keep_last=50):
    matches = state["matches"]
    completed_ids = [mid for mid, m in matches.items() if m.get("completed")]
    if len(completed_ids) <= keep_last:
        return
    for mid in completed_ids[: len(completed_ids) - keep_last]:
        del matches[mid]


TEST_MESSAGE = (
    "crickbot setup check: this is a test message. If you received this, "
    "your TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID secrets are configured "
    "correctly. This message is not tied to any real match."
)


def run_telegram_self_test(config):
    """Send one fixed message to prove the Telegram credentials work, with
    zero CricketData API calls and no interaction with match state."""
    ok = send_telegram(config, TEST_MESSAGE)
    if ok:
        log("Telegram self-test message sent successfully.")
    else:
        log("Telegram self-test FAILED to send -- check the bot token and chat ID.")
        sys.exit(1)


def run(state_path):
    config = load_config()

    if os.environ.get("SEND_TEST_MESSAGE", "false").strip().lower() == "true":
        run_telegram_self_test(config)
        return

    state = load_state(state_path)
    budget = ApiBudget(state)

    if budget.quota_exhausted_today:
        log("Daily API quota already marked exhausted; skipping this run.")
        save_state(state_path, state)
        return

    matches = fetch_current_matches(config["api_key"], budget)
    log(f"Fetched {len(matches)} current match(es) from the API")

    relevant = [m for m in matches if is_relevant(m)]
    log(f"{len(relevant)} match(es) are India-international or IPL")

    for match in relevant[:MAX_MATCHES_PER_RUN]:
        if not budget.can_call():
            log("Call budget reached for this run; remaining matches will be checked next run")
            break
        try:
            process_match(match, state, config, budget)
        except Exception as e:
            log(f"ERROR processing match {match.get('id')}: {e}")

    prune_old_matches(state)
    save_state(state_path, state)
    log(f"Done. API calls this run: {budget.calls_this_run}, calls today: {state['calls_today']}")


def main():
    state_path = os.environ.get(
        "STATE_PATH", os.path.join(os.path.dirname(__file__), "state.json")
    )
    try:
        run(state_path)
    except ConfigError as e:
        log(f"CONFIG ERROR: {e}")
        sys.exit(1)
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
