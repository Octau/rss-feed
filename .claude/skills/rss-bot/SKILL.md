---
name: rss-bot
description: Working guide for this Discord RSS feed bot — architecture, data flow, database schema, conventions, and how to run/extend it. Use when adding features, fixing bugs, or answering questions about bot.py, db.py, or cogs/rss.py.
---

# RSS Feed Discord Bot

A Discord bot that lets servers subscribe to RSS/Atom feeds. Feeds are polled on
per-feed intervals and new entries are announced through Discord webhooks.

**Stack:** Python 3.10+, discord.py (slash commands + cogs), feedparser,
aiosqlite, aiohttp, python-dotenv.

## File map

| File | Role |
|---|---|
| `bot.py` | Entry point. Configures logging: stdout + daily file `{LOG_DIR}/bot-YYYY-MM-DD.log` via `DailyFileHandler` (midnight rotation, newest `LOG_BACKUP_COUNT` files kept; handlers attached to the root logger with `addHandler`, NOT `basicConfig`; `bot.run(..., log_handler=None)` so discord.py uses the same handlers). `LoggingCommandTree.interaction_check` logs a `[command]` line for every app-command invocation (args excluded — they can contain webhook URLs). `RSSBot.setup_hook` creates `DATA_DIR`, opens the DB, loads `cogs.rss`, syncs slash commands. Basic `/ping`/`/hello` slash commands. No prefix commands (`command_prefix=when_mentioned`, default intents). |
| `db.py` | All SQLite access through a module-global `_conn`. Schema lives in the `SCHEMA` string at the top. |
| `cogs/rss.py` | The whole feature: `rss` command group, the 60s `poller` task loop, fetch/parse/announce helpers. |
| `adapters/__init__.py` | Adapter registry. Scans the package at import; any module exporting `ADAPTER` is registered under its `feed_type`. Exposes `FEED_TYPES` (`("generic", *sorted(ADAPTERS))`) and `adapt_entries(feed_type, parsed)` — the one entry point the cog uses; `"generic"`/unknown types fall back to raw feedparser entries. |
| `adapters/f5.py` | Adapter for the F5 Support (NGINX) RSS feed (`feed_type = "f5"`): typed `F5RSSFeed`/`F5RSSItem` dataclasses capturing F5's extra `copyright`/`ttl` channel fields, built via `from_parsed` (feedparser) or `from_dict` (raw `{channel: {items}}` shape); `entries()` yields dicts compatible with `entry_key`/`build_embed`. |
| `adapters/royalroad.py` | Adapter for Royal Road fiction feeds (`feed_type = "royalroad"`): same dataclass/`from_parsed`/`from_dict` pattern as f5. Captures the `generator` channel field (no `copyright`/`ttl`) and the `guid` element's `isPermaLink` attribute (feedparser: `entry.guidislink`); HTML-rich `description` is passed through untouched for `clean_summary()` to strip at embed time. |

## Data flow (poll cycle)

1. `poller` (tasks.loop, every 60s) calls `db.due_feeds(now)` — feeds where
   `last_polled + interval_seconds <= now` — then filters out feeds still in
   backoff via `_is_due`: a failing feed is due only after
   `min(interval_seconds * 2^fail_count, MAX_BACKOFF)` has elapsed
   (`interval_seconds` itself is never mutated).
2. `poll_feed` does a conditional GET (`fetch_feed`) sending stored
   ETag/Last-Modified; HTTP 304 returns `parsed=None` and we stop early.
3. `update_poll_meta` is ALWAYS called right after the fetch (and also in the
   poller's exception handler) so a broken feed bumps `last_polled` and can't
   hot-loop. Success calls `db.record_poll_success` (resets `fail_count`,
   sends a ✅ recovery notice to the feed's webhook if it was ≥4); failure
   calls `db.record_poll_failure` and `_maybe_alert_failure` sends a ⚠️
   warning embed to the feed's webhook at `fail_count == 4` and each
   doubling (8, 16, …).
4. Entries are normalized first: `adapters.adapt_entries(feed["feed_type"],
   parsed)` parses through the feed's adapter (raw feedparser entries for
   `generic`). New entries = entries whose `entry_key()` is not in
   `db.seen_keys(feed_id)`.
   ALL new keys are recorded as seen, but only the newest `MAX_ITEMS_PER_POLL`
   (5) are announced, reversed so the channel reads oldest-first.
5. Announcements go through `discord.Webhook.from_url(feed["webhook_url"])`,
   one embed per entry, `SEND_SPACING` (1s) apart.

`feedparser.parse` is blocking — it is always run via
`run_in_executor`; keep it that way for any new parsing code.

## Database (`{DATA_DIR}/rss.sqlite3`)

- `feeds(id, guild_id, url, name, webhook_url, feed_type, interval_seconds,
  added_by, etag, last_modified, last_polled, fail_count, last_error,
  icon_url)` — `UNIQUE (guild_id, url)`; the
  unique violation in `add_feed` returns `None` (means "already registered").
  `feed_type` defaults to `'generic'` and selects the parsing adapter.
  `fail_count`/`last_error` track consecutive poll failures (drive backoff and
  `/rss status`); `icon_url` is the Google S2 favicon used as the webhook
  avatar.
- `seen_entries(feed_id, entry_key)` — PK on both columns,
  `ON DELETE CASCADE` from feeds. `add_seen` trims to the newest 500 rows per
  feed (`keep=500`) to bound growth.
- Foreign keys are enabled with `PRAGMA foreign_keys = ON` in `db.init`.
- `mark_due` (sets `last_polled = 0`) is currently dead code — `rss poll`
  fetches the feed directly and pushes the latest entry immediately instead
  of waiting for the next cycle.

Adding a column? Update `SCHEMA` in `db.py` AND add a patch in `db.init`:
`CREATE TABLE IF NOT EXISTS` won't alter existing DBs, so `init` checks
`PRAGMA table_info(feeds)` and `ALTER TABLE`s in missing columns (see the
`feed_type` patch there for the pattern).

## Key constants (top of `cogs/rss.py`)

All values below are defaults; each reads from a same-named `.env` variable at
module load (`int`/`float`/`int(..., 0)` for hex colors). Unset = the default.

- `MIN_INTERVAL` 120s (enforced with `max()` in `add`, `edit`, and `interval`)
- `DEFAULT_INTERVAL` 14400s (4h) · `MAX_ITEMS_PER_POLL` 5
- `FETCH_SPACING` / `SEND_SPACING` 1.0s · `FETCH_TIMEOUT` 20s
- `MAX_BACKOFF` 3600s (cap on exponential backoff) · `PAGE_SIZE` 5 (rss list)
- `RSS_COLOR` 0xEE802F (orange — use for all embeds in this bot);
  `ERROR_COLOR` 0xE74C3C for failure alerts
- `WEBHOOK_RE` validates Discord webhook URLs (incl. canary/ptb/discordapp)

## Conventions & invariants

- **New feed source?** Add a module under `adapters/` exporting `ADAPTER`
  (class with `feed_type`, `from_parsed`, `entries()`); it is auto-discovered
  and appears in the `rss add` type choices — no cog changes needed. Entry
  dicts must carry `id`/`link`/`title`/`published`/`published_parsed`/`summary`
  so `entry_key`/`build_embed` keep working, and `id` must stay stable or
  existing feeds of that type re-announce history.
- **Guild scoping is a security boundary.** Every read/write that takes user
  input goes through `db.get_feed(guild_id, ref)` or is filtered by
  `guild_id`. Never let a command touch another guild's rows.
- Mutating commands (and `status`) use
  `@app_commands.default_permissions(manage_guild=True)`; `rss list` is open
  to everyone.
- Commands are **slash-only**: the `rss` group is an `app_commands.Group`
  with `guild_only=True`; new params need `@app_commands.describe(...)`.
- **Webhook URLs are secrets.** `rss add` defers ephemerally, and `rss edit`
  defers ephemerally whenever a webhook URL is supplied — preserve this for
  any new command that accepts a webhook URL. Never echo webhook URLs back in
  output, and never log them (command args are excluded from `[command]` log
  lines for this reason).
- `entry_key()` precedence: `id` → `link` → sha256(title+published). Changing
  this re-announces history for existing feeds — don't, unless intentional.
- When a feed is added, its current entries are seeded into `seen_entries` so
  the channel isn't flooded; the newest entry is then pushed through the
  webhook as a preview/webhook test. If that send fails, the feed is removed
  again and the user is told the add failed.
- User-facing replies use emoji prefixes: ❌ errors, ✅ success, ⚠️ warnings,
  🗑️ removal, ⏱️ interval, 🔄 forced poll. Embed text is truncated to Discord
  limits (title 256, footer 2048, ≤25 fields).
- Errors funnel through `cog_app_command_error`; unknown errors are re-raised.
- Style: stdlib logging per-module (`log = logging.getLogger("rss")`),
  4-space indent, double quotes in `db.py`/`cogs/rss.py`, type hints with
  `X | None` unions, section divider comments like
  `# ------ fetch`.

## Config & running

`.env` (see `.env.example`): `DISCORD_TOKEN` (required), `DATA_DIR`
(default `data/`), `LOG_DIR` (default `storage/logs`), `LOG_BACKUP_COUNT`
(default `7`), `LOG_LEVEL` (default `INFO`).

```bash
source venv/bin/activate        # project venv at ./venv
pip install -r requirements.txt
python bot.py
```

Docker: `Dockerfile` + `docker-compose.yml` exist at the repo root.
There are currently **no tests** in this repo.

## Maintaining this skill

This file is the living reference for the repo. When architecture, schema,
constants, or conventions change, update this SKILL.md in the same change.
