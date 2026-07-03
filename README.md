# crickbot

Telegram alerts for Indian Cricket Team (international) and IPL matches:
toss result, centuries, five-wicket hauls, and match result.

## How it works

A GitHub Actions workflow (`.github/workflows/cricket-alerts.yml`) runs
`bot/cricket_bot.py` on a cron schedule (every 30 minutes). Each run:

1. Fetches current matches from [CricketData.org](https://cricketdata.org/).
2. Filters to matches where a team is exactly "India" (international) or
   both teams are current/recent IPL franchises.
3. Checks each relevant match for new events not already recorded in
   `bot/state.json`, and sends a Telegram message for each new one.
4. Commits the updated `bot/state.json` back to the repo so dedup state
   persists across runs.

The script never loops internally — it runs once and exits — so scheduling
is entirely controlled by the GitHub Actions cron. Safety layers against
spam / runaway API usage:

- Daily API call budget tracked in `state.json` (`MAX_CALLS_PER_DAY`,
  default 90 — under the free 100/day cap).
- Per-run call cap (`MAX_CALLS_PER_RUN`, default 8).
- If the API itself reports the quota exhausted, the bot stops calling it
  for the rest of the day instead of retrying.
- Every event has a dedup key checked against `state.json` before sending;
  state is only marked "sent" after Telegram confirms delivery.
- Completed matches are skipped entirely (no further API calls).

## Required GitHub Secrets

Set these under **Settings -> Secrets and variables -> Actions**:

- `CRICKET_API_KEY` — from cricketdata.org
- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `TELEGRAM_CHAT_ID` — your Telegram chat ID

## Testing

Run unit tests (mocked, no real network calls):

```
pip install -r requirements-dev.txt
pytest
```

Dry-run against the real API without sending real Telegram messages:

```
CRICKET_API_KEY=your_key DRY_RUN=true python -m bot.cricket_bot
```

Manually trigger the workflow in dry-run mode from the **Actions** tab
("Run workflow", leave "Dry run" checked) to validate against real data in
CI before trusting the live schedule.

## Scope notes

- Only the senior India men's/women's national side matching team name
  exactly "India" is tracked — "India A", "India Women", "India U19" are
  excluded by design. Adjust `INDIA_TEAM_NAMES` in `bot/cricket_bot.py` if
  you want to widen this.
- IPL team names are hardcoded in `IPL_TEAM_NAMES` (includes recent
  historical renames). Update this set if a franchise is renamed again.
