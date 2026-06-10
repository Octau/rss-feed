---
name: rss-bot
description: Working guide for this Discord RSS feed bot — architecture, data flow, database schema, conventions, and how to run/extend it. Use when adding features, fixing bugs, or answering questions about bot.py, db.py, or cogs/rss.py.
---

# RSS Feed Discord Bot

A Discord bot that lets servers subscribe to RSS/Atom feeds. Feeds are polled on
per-feed intervals and new entries are announced through Discord webhooks.

**Stack:** Python 3.10+, discord.py (hybrid commands + cogs), feedparser,
aiosqlite, aiohttp, python-dotenv.

## File map

| File | Role |
|---|---|
| `bot.py` | Entry point. Configures logging (stdout + `storage/logs/bot.log` rotating every 3 days, 14 backups; `bot.run(..., log_handler=None)` so discord.py uses the same handlers). `RSSBot.setup_hook` creates `DATA_DIR`, opens the DB, loads `cogs.rss`, syncs slash commands. Basic `ping`/`hello` hybrid commands. |
| `db.py` | All SQLite access through a module-global `_conn`. Schema lives in the `SCHEMA` string at the top. |
| `cogs/rss.py` | The whole feature: `rss` command group, the 60s `poller` task loop, fetch/parse/announce helpers. |
| `adapters/__init__.py` | Adapter registry. Scans the package at import; any module exporting `ADAPTER` is registered under its `feed_type`. Exposes `FEED_TYPES` (`("generic", *sorted(ADAPTERS))`) and `adapt_entries(feed_type, parsed)` — the one entry point the cog uses; `"generic"`/unknown types fall back to raw feedparser entries. |
| `adapters/f5.py` | Adapter for the F5 Support (NGINX) RSS feed (`feed_type = "f5"`): typed `F5RSSFeed`/`F5RSSItem` dataclasses capturing F5's extra `copyright`/`ttl` channel fields, built via `from_parsed` (feedparser) or `from_dict` (raw `{channel: {items}}` shape); `entries()` yields dicts compatible with `entry_key`/`build_embed`. |
| `adapters/royalroad.py` | Adapter for Royal Road fiction feeds (`feed_type = "royalroad"`): same dataclass/`from_parsed`/`from_dict` pattern as f5. Captures the `generator` channel field (no `copyright`/`ttl`) and the `guid` element's `isPermaLink` attribute (feedparser: `entry.guidislink`); HTML-rich `description` is passed through untouched for `clean_summary()` to strip at embed time. |

## Data flow (poll cycle)

1. `poller` (tasks.loop, every 60s) calls `db.due_feeds(now)` — feeds where
   `last_polled + interval_seconds <= now`.
2. `poll_feed` does a conditional GET (`fetch_feed`) sending stored
   ETag/Last-Modified; HTTP 304 returns `parsed=None` and we stop early.
3. `update_poll_meta` is ALWAYS called right after the fetch (and also in the
   poller's exception handler) so a broken feed bumps `last_polled` and can't
   hot-loop.
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
  added_by, etag, last_modified, last_polled)` — `UNIQUE (guild_id, url)`; the
  unique violation in `add_feed` returns `None` (means "already registered").
  `feed_type` defaults to `'generic'` and selects the parsing adapter.
- `seen_entries(feed_id, entry_key)` — PK on both columns,
  `ON DELETE CASCADE` from feeds. `add_seen` trims to the newest 500 rows per
  feed (`keep=500`) to bound growth.
- Foreign keys are enabled with `PRAGMA foreign_keys = ON` in `db.init`.
- `mark_due` sets `last_polled = 0`, which is how `rss poll` forces a fetch
  on the next cycle.

Adding a column? Update `SCHEMA` in `db.py` AND add a patch in `db.init`:
`CREATE TABLE IF NOT EXISTS` won't alter existing DBs, so `init` checks
`PRAGMA table_info(feeds)` and `ALTER TABLE`s in missing columns (see the
`feed_type` patch there for the pattern).

## Key constants (top of `cogs/rss.py`)

- `MIN_INTERVAL` 120s (enforced with `max()` in `add` and `interval` commands)
- `DEFAULT_INTERVAL` 14400s (4h) · `MAX_ITEMS_PER_POLL` 5
- `FETCH_SPACING` / `SEND_SPACING` 1.0s · `FETCH_TIMEOUT` 20s
- `RSS_COLOR` 0xEE802F (orange — use for all embeds in this bot)
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
- Mutating commands require `@commands.has_guild_permissions(manage_guild=True)`;
  `rss list` and the help group are open to everyone.
- Commands are **hybrid** (prefix + slash); new params need
  `@app_commands.describe(...)`. The `rss` group uses `fallback="help"`.
- `rss add` prefix syntax is `[type] <feed_url> <webhook_url> [interval]` —
  the leading optional type works because `Optional[FeedTypeConverter]`
  raises on unknown tokens, making discord.py backtrack and re-read the token
  as `feed_url`. Slash ordering is unaffected (app_commands sorts required
  options first).
- **Webhook URLs are secrets.** `rss add` deletes the prefix-command message
  and replies ephemerally on slash invocation — preserve this for any new
  command that accepts a webhook URL. Never echo webhook URLs back in output.
- `entry_key()` precedence: `id` → `link` → sha256(title+published). Changing
  this re-announces history for existing feeds — don't, unless intentional.
- When a feed is added, its current entries are seeded into `seen_entries` so
  the channel isn't flooded; the newest entry is then pushed through the
  webhook as a preview/webhook test. If that send fails, the feed is removed
  again and the user is told the add failed.
- User-facing replies use emoji prefixes: ❌ errors, ✅ success, 🗑️ removal,
  ⏱️ interval, 🔄 queued. Embed text is truncated to Discord limits
  (title 256, footer 2048, ≤25 list fields).
- Errors funnel through `cog_command_error`; unknown errors are re-raised.
- Style: stdlib logging per-module (`log = logging.getLogger("rss")`),
  4-space indent, double quotes in `db.py`/`cogs/rss.py`, type hints with
  `X | None` unions, section divider comments like
  `# ------ fetch`.

## Config & running

`.env` (see `.env.example`): `DISCORD_TOKEN` (required), `BOT_PREFIX`
(default `!`), `DATA_DIR` (default `data/`).

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
