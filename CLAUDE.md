# CLAUDE.md

Context for AI agents working in this repo. See `README.md` for full
user-facing documentation — this file is the fast-orientation summary plus
things that aren't obvious from reading the code.

## What this is

A Telegram bot that alerts on major events (toss, centuries, five-wicket
hauls, match result — ~4-5 messages/match) for **Indian Cricket Team
international matches and IPL only**. Runs on GitHub Actions cron every 30
minutes. Built to be entirely free with zero credit-card exposure anywhere
in the stack — that was an explicit, hard user requirement, not a
nice-to-have. Preserve it when making changes: don't introduce a dependency
that requires billing info, even on a "free tier."

## Repo / remote setup — read before running any git command

This repo is pushed to the owner's **personal** GitHub
(`harshitcharanpahari0507/crickbot`), but the local machine's default git
credentials (Windows Credential Manager / Git Credential Manager) are tied
to a **work** GitHub account. To keep the two separated:

- The `origin` remote uses an SSH host alias, not plain `github.com`:
  `git@github.com-personal:harshitcharanpahari0507/crickbot.git`
- That alias is defined in `~/.ssh/config` (`Host github.com-personal`),
  pointing at a dedicated key `~/.ssh/id_ed25519_personal` used only for
  this account.
- `user.name`/`user.email` are set **locally** in this repo's `.git/config`
  (not globally) to the personal account's noreply address, so commits
  don't leak the work identity or vice versa.

If you ever add a remote, clone this repo elsewhere, or set up CI that
needs to push, use the `github.com-personal` alias / the repo's own
`GITHUB_TOKEN` (already wired up in the workflow) — never assume the
ambient/global git credentials are correct for this project.

## Architecture (one paragraph)

`bot/cricket_bot.py` runs once per invocation (no internal loop — all
scheduling is GitHub Actions cron). It fetches `currentMatches` from
CricketData.org, filters to India-international-only or IPL-only matches,
fetches `match_info`/`match_scorecard` per relevant match, diffs against
`bot/state.json` for already-notified events, sends new ones via Telegram,
and commits the updated state file back to the repo. Full design rationale
and the anti-spam/anti-runaway-API layering is documented in the module
docstring at the top of `bot/cricket_bot.py` and in `README.md`.

## Load-bearing constraints — don't casually change these

- **Don't remove or weaken the dedup-before-send / mark-sent-only-on-success
  pattern.** That's what prevents duplicate Telegram messages across runs.
- **Don't increase cron frequency without re-checking the API budget math.**
  Every 30 minutes was chosen specifically so a full-day Test match doesn't
  exceed the CricketData free 100-hits/day cap. See README "Why every 30
  minutes."
- **Don't trust a fixed "1 hit per call" assumption.** The API returns
  `info: {hitsToday, hitsLimit}` on every response; `ApiBudget.sync_from_api_info`
  treats that as authoritative. If you add new endpoints, keep feeding their
  responses through the same sync path.
- **`match_scorecard`'s innings list is nested at `data["scorecard"]`**, not
  `data` itself — this was a real bug caught by testing against the live
  API (the docs site was unreachable/403 when this was built). Don't
  "simplify" `fetch_match_scorecard` back to assuming `data` is the list.
- **`currentMatches` alone is not a reliable discovery source.** In
  production it completely omitted a genuinely live India match (not a
  pagination issue — the match wasn't in the feed at all across every
  page). The pattern: it had `fantasyEnabled: false` / `bbbEnabled: false`.
  Fixed by adding `refresh_watched_series`/`maybe_refresh_series`, which
  resolve matches directly from the India-tour/IPL series schedule
  (`series` search + `series_info`) and merge them with whatever
  `currentMatches` finds, deduped by match ID. **Don't remove this
  fallback and go back to trusting `currentMatches` alone** — that's the
  exact bug this fixed (2026-07-04: user reported 0 India matches detected
  during a live India vs England T20I).
- The series-schedule fallback is throttled (`SEARCH_RETRY_DAYS`) so it
  doesn't burn an API hit every 30-minute run during the ~10 months/year
  with no IPL season. If you touch `maybe_refresh_series`, keep that
  throttle — removing it reintroduces a quota-burn risk.
- **Known accepted limitation: some matches have zero live data on
  CricketData.org's free tier**, even once correctly discovered. Confirmed
  2026-07-04 on the England vs India 2nd T20I: `match_info` never populated
  toss/score (stuck on the pre-match placeholder status) and
  `match_scorecard` returned `"ERR: Scorecard ... not found"`, despite
  `matchStarted: true`. Correlates with `fantasyEnabled: false` /
  `bbbEnabled: false` on that match — the provider seems to lack a live
  feed for some fixtures (rights/tier gap on their end), though data may
  still backfill after the match ends (observed on the 1st T20I in the
  same series). **This is not a bug to chase** — the user explicitly chose
  to accept this rather than add a fallback data source (e.g. a Cricbuzz
  scraper). Don't spend time trying to "fix" missing live data for a
  specific match without re-confirming with the user first.
- Secrets (`CRICKET_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) live
  only in GitHub Actions Secrets. Never hardcode them, never log them, never
  write them into `bot/state.json` or any committed file.

## Local dev gotchas

- On this machine, Python's `requests` fails with
  `CERTIFICATE_VERIFY_FAILED` against `api.cricapi.com` because of a
  corporate SSL-inspecting proxy whose root CA isn't in `certifi`'s bundle.
  `pip install pip-system-certs` fixes it (patches the default SSL context
  to use the OS cert store, which trusts the proxy's CA); `curl` also works
  fine as a fallback for ad-hoc poking. This is local-only — GitHub Actions
  runners are unaffected.
- Tests (`pytest`, 52 tests in `tests/test_cricket_bot.py`) are fully
  mocked — no real network or Telegram calls ever happen in the test suite.
  Keep it that way; add new tests with `unittest.mock.patch`, never live
  calls.

## Manual verification tools already built in

- `DRY_RUN=true` — hits the real CricketData API but only prints Telegram
  messages instead of sending them.
- `SEND_TEST_MESSAGE=true` — sends one fixed Telegram message bypassing all
  match logic, zero CricketData API calls. Used to verify bot
  token/chat ID work. Both are wired up as `workflow_dispatch` inputs in
  `.github/workflows/cricket-alerts.yml` for manual testing from the
  Actions tab.

## Status

Feature-complete and live as of 2026-07-04. Cron is running every 30
minutes, secrets are configured, Telegram delivery confirmed via
self-test, and real-data dry-runs have been validated in the actual GitHub
Actions environment. On 2026-07-04 the user reported zero India matches
detected during a live India vs England T20I; root-caused to the
`currentMatches` discovery gap described above and fixed with the
series-schedule fallback, verified against live data (correctly found and
processed both the rained-off 1st T20I and the live 2nd T20I).
