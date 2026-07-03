# crickbot

A Telegram bot that sends alerts for **Indian Cricket Team (international)**
and **IPL** matches: toss result, centuries, five-wicket hauls, and match
result. Built entirely on free-tier services, with no credit card required
anywhere in the stack.

## Why this exists

Score-update apps push far more noise than wanted. This project tracks only
a small set of major events per match (~4-5 messages) instead of ball-by-ball
commentary, and only for two scopes: the senior India national team, and IPL.

## Architecture

```
GitHub Actions (cron, every 30 min)
        |
        v
  bot/cricket_bot.py  (runs once, exits -- no internal loop)
        |
        |--> GET api.cricapi.com/v1/currentMatches   (1 call)
        |       filter: team == "India" OR both teams are IPL franchises
        |
        |--> for each relevant match:
        |       GET .../match_info          (toss, result, status)
        |       GET .../match_scorecard     (centuries, 5-wicket hauls)
        |       POST api.telegram.org/bot<token>/sendMessage  (new events only)
        |
        v
  bot/state.json  (dedup + daily call-budget state, committed back to repo)
```

Nothing here polls in a loop or runs continuously — a single script
invocation does one bounded pass and exits. All scheduling lives in
`.github/workflows/cricket-alerts.yml`.

## Free services used (no card required, confirmed)

| Service | Purpose | Free limit |
|---|---|---|
| [CricketData.org](https://cricketdata.org/) (api.cricapi.com v1) | Match data | 100 hits/day, lifetime free |
| Telegram Bot API | Message delivery | No cost for personal use |
| GitHub Actions (public repo) | Scheduler/compute | Free for public repos |

## Repo layout

- `bot/cricket_bot.py` — all logic: fetching, filtering, event detection, dedup, budget enforcement, Telegram sending.
- `bot/state.json` — persisted dedup + daily API-call-count state. Committed back to the repo by the workflow after every run. Don't hand-edit while the workflow is active (risk of a race with the next scheduled run).
- `tests/test_cricket_bot.py` — 34 unit tests, all fully mocked (no real network or Telegram calls ever run in tests).
- `.github/workflows/cricket-alerts.yml` — cron schedule + manual dispatch trigger.
- `requirements.txt` / `requirements-dev.txt` — `requests`, and `pytest` for dev.

## Scope rules

- **India international**: a match counts only if a team name is exactly
  `"India"` (case-insensitive). This deliberately excludes `"India A"`,
  `"India Women"`, `"India Under-19"`, etc. — see `INDIA_TEAM_NAMES` in
  `bot/cricket_bot.py` if you want to widen this.
- **IPL**: a match counts only if *both* teams are in the hardcoded
  `IPL_TEAM_NAMES` set (includes current franchises plus recent historical
  renames like Delhi Daredevils -> Delhi Capitals). Update this set if a
  franchise is renamed again.

## Events tracked (per match)

| Event | Trigger | Dedup key |
|---|---|---|
| Toss | `match_info.tossWinner` + `tossChoice` present | once per match |
| Century | a batter's `r` (runs) in the scorecard reaches >= 100 | `player｜innings label` — fires once even if the player's score keeps climbing |
| Five-wicket haul | a bowler's `w` (wickets) reaches >= 5 | `player｜innings label` |
| Result | `match_info.matchEnded` is true | once per match, also marks the match fully "completed" (no further API calls for it, ever) |

## Anti-spam / anti-runaway-API design

This was a hard requirement, not an afterthought — the mechanisms, layered:

1. **No internal loop.** The script runs once and exits. It cannot itself
   spin in a retry storm; GitHub Actions' cron is the only re-trigger.
2. **Authoritative quota sync.** Every successful API response includes an
   `info: {hitsToday, hitsLimit}` block reflecting the account's *real*
   usage. The bot adopts this value into its own counter and halts all
   further calls for the day once within `QUOTA_SAFETY_BUFFER` (default 5)
   hits of the real limit — this is not a guess, it's read from the API
   itself on every call.
3. **Local daily cap as a backstop.** `MAX_CALLS_PER_DAY` (default 90) and
   `MAX_CALLS_PER_RUN` (default 8) apply even if the `info` block is ever
   missing or malformed.
4. **Bounded retries.** Each HTTP call retries at most `MAX_HTTP_ATTEMPTS`
   (2) times with a short fixed backoff — never an unbounded retry loop.
5. **Dedup before send, confirm before marking sent.** Every event has a
   stable key checked against `state.json` before a Telegram send is even
   attempted. State is only updated to "sent" *after* Telegram confirms
   delivery (`ok: true`), so a failed send is naturally retried on the next
   scheduled run instead of being silently dropped or duplicated.
6. **Completed matches are skipped entirely** — once a match's result has
   been sent, `process_match` returns immediately without any further API
   calls for that match ID, forever.

## Why every 30 minutes

A live Test match can have ~6 hours of play in a day; polling more
frequently risks the daily hit budget on a single long day of cricket. At a
30-minute cadence, worst-case usage (checking + fetching info/scorecard for
one match all day) stays comfortably under the 90-hit local cap, while still
surfacing events within half an hour of happening — acceptable for
milestone alerts, not intended for ball-by-ball commentary.

## Setup

### Required GitHub Secrets

Settings -> Secrets and variables -> Actions:

- `CRICKET_API_KEY` — from cricketdata.org (free signup, no card).
- `TELEGRAM_BOT_TOKEN` — from @BotFather.
- `TELEGRAM_CHAT_ID` — your personal Telegram chat ID (get it from
  @userinfobot).

### Local development

```
pip install -r requirements-dev.txt
pytest                                    # 34 tests, fully mocked, no network
```

Dry-run against the *real* API without sending real Telegram messages
(prints the message text instead):

```
CRICKET_API_KEY=your_key DRY_RUN=true python -m bot.cricket_bot
```

> Note: on machines behind a corporate SSL-inspecting proxy, Python's
> `requests` may fail with `CERTIFICATE_VERIFY_FAILED` even though `curl`
> succeeds (curl uses the OS cert store; `requests` uses its own bundled
> `certifi` list, which won't include a corporate MITM root CA). This is a
> local-machine-only issue — GitHub Actions runners are unaffected. Use
> `curl` for local ad-hoc API poking if you hit this.

### Verifying Telegram credentials without waiting for a live match

Trigger the workflow manually from the Actions tab with **Dry run**
unchecked and **Send test message** checked — this sends one fixed message
using the configured secrets and makes zero CricketData API calls (see
`run_telegram_self_test` in `bot/cricket_bot.py`).

### Manual dry-run against live data

Trigger the workflow manually with **Dry run** checked (the default) to
fetch real current matches and log what would have been sent, without
sending anything for real. Useful after changing filtering/parsing logic.

## Known real API response shapes (validated against live data)

Cricketdata.org's public docs were not fetchable when this was built,
so the shapes below were confirmed by hitting the live API directly:

- `currentMatches` -> `data` is a **list** of matches: `id`, `name`,
  `matchType`, `status`, `teams` (list of 2 strings), `matchStarted`,
  `matchEnded`, `series_id`, etc.
- `match_info` -> `data` is a **dict** with `tossWinner`, `tossChoice`,
  `matchWinner`, `status`, `matchStarted`, `matchEnded`.
- `match_scorecard` -> `data` is a **dict**, and the actual per-innings
  list is nested under `data["scorecard"]` (not `data` itself — this
  tripped up the first implementation). Each innings has `inning` (label
  string), `batting` (list with `batsman.name`, `r`, `b`, `4s`, `6s`), and
  `bowling` (list with `bowler.name`, `o`, `m`, `r`, `w`).

## Adjusting behavior

- Change polling cadence: edit the `cron:` line in
  `.github/workflows/cricket-alerts.yml`.
- Change event thresholds: `CENTURY_THRESHOLD_RUNS`,
  `FIVE_WICKET_HAUL_WICKETS` in `bot/cricket_bot.py`.
- Widen/narrow scope: `INDIA_TEAM_NAMES`, `IPL_TEAM_NAMES`.
- Change daily/per-run API budget: `MAX_CALLS_PER_DAY`, `MAX_CALLS_PER_RUN`,
  `QUOTA_SAFETY_BUFFER` (env vars, or edit the defaults).
