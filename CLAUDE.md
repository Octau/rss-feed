# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Prompt Instruction
Draft an "Operating Instructions" doc for my Claude Cowork preferences. Make you a sharp thinking partner, not a yes-machine. Cover:

About Me – Pull from past conversations: name, role, what my company/team does, public work or side projects with specifics, biggest pain points, tools I use. Missing something? Ask – don't guess.

Building anything – PRD first (problem, success criteria, scope, constraints, plan, open questions); get sign-off before building. Check what already exists before proposing custom work.

Pushback – Interrogate vague requests. Disagree when something's off. Flag contradictions before acting – never silently overwrite. No sycophancy.

Reversibility – Before anything destructive (deleting, overwriting, comms in my name, financial actions, mass ops): show the plan, flag what's irreversible, wait for explicit "proceed."

Note-taking – Capture context, decisions, and open threads continuously. Checkpoint before switching domains or when a chat runs long.

Working style – Show reasoning, not just conclusions. Breadth and rigor. Skip filler. If I say "things changed," re-interview me.
Show me the draft, then we'll revise.

Always refer to PRD.md and propose the changes on PRD.md first.

Always update CLAUDE.md and README.md to reflect a version changes.


## Project Overview

This is a Discord bot that manages RSS feed subscriptions for Discord servers. Users can add RSS/Atom feeds via Discord webhooks, and the bot polls them on configurable intervals to announce new entries in Discord channels.

**Key stack:** discord.py (bot framework), feedparser (RSS/Atom parsing), aiosqlite (async SQLite), aiohttp (HTTP client).

## Architecture

The project uses a modular discord.py cogs architecture:

- **bot.py** — Main entry point. Sets up the Discord bot, initializes the database on startup, and loads the RSS cog. Defines basic commands (ping, hello) and event handlers (on_ready).

- **adapters/** — Per-source feed adapters. `adapters/__init__.py` auto-discovers any module in the package exporting `ADAPTER` and exposes `FEED_TYPES` plus `adapt_entries(feed_type, parsed)`; `adapters/f5.py` handles the F5 Support (NGINX) feed shape (extra `copyright`/`ttl` channel fields); `adapters/royalroad.py` handles Royal Road fiction feeds (`generator` channel field, `guid` with an `isPermaLink` attribute, HTML-rich descriptions). Each feed stores a `feed_type` (default `generic` = raw feedparser entries) chosen via the `rss add` type option, and the poller parses entries through the matching adapter on every cycle.

- **db.py** — Async SQLite abstraction layer with a global `_conn` connection. Defines the schema and provides functions for managing feeds and tracking seen entries to prevent duplicates. `init` patches missing columns into pre-existing DBs via `PRAGMA table_info` + `ALTER TABLE`. Key tables:
  - `feeds`: One row per feed subscription, scoped by guild, tracks the parsing adapter (`feed_type`) and polling metadata (etag, last_modified, last_polled)
  - `seen_entries`: Prevents re-announcing the same feed item; bounded to 500 entries per feed to prevent unbounded growth

- **cogs/rss.py** — The RSS management cog. Contains:
  - Commands: `rss add`, `rss remove`, `rss list`, `rss interval`, `rss poll` (all require "Manage Server" permission). All commands are hybrid (`hybrid_group`/`hybrid_command`): usable both as prefix commands (`!rss add`) and slash commands (`/rss add`). The app command tree is synced globally in `setup_hook` (bot.py). `/rss add` defers ephemerally to keep the webhook URL private and survive the 3s interaction deadline; the prefix path deletes the invoking message instead. On a successful add, the feed's newest entry is pushed through the webhook as a preview (also validates the webhook works; the feed is removed again if the send fails).
  - `poller` task: Runs every 60 seconds, finds feeds due for polling, fetches them, detects new entries, and announces via Discord webhook
  - Helper functions: `entry_key()` (stable identifier), `clean_summary()` (HTML strip + truncate), `entry_timestamp()` (parse pubdate), `build_embed()` (Discord embed formatting)
  - HTTP conditional GET with ETag/Last-Modified to minimize bandwidth
  - Blocking feedparser work is offloaded to executor to keep the event loop responsive

## Configuration

Via `.env` (required):
- `DISCORD_TOKEN` — Discord bot token
- `BOT_PREFIX` — Command prefix (default: `!`)
- `DATA_DIR` — SQLite database location (default: `data/`)

## Logging

Logs go to stdout and to `storage/logs/bot.log` (directory created at startup; rotation every 3 days, 14 backups kept). Configured via `logging.basicConfig` in [bot.py](bot.py); `bot.run(..., log_handler=None)` routes discord.py's logs through the same handlers. The directory is gitignored.

## Running

**Start the bot:**
```bash
python bot.py
```

**Install dependencies (first run):**
```bash
pip install -r requirements.txt
```

## Key Constants

In [cogs/rss.py](cogs/rss.py):
- `MIN_INTERVAL` (120s): Floor for per-feed polling intervals
- `DEFAULT_INTERVAL` (14400s / 4h): Default polling interval for new feeds
- `MAX_ITEMS_PER_POLL` (5): Cap new announcements per feed per cycle to avoid floods
- `FETCH_SPACING` (1.0s): Pause between feed fetches in one poller cycle (rate limiting)
- `SEND_SPACING` (1.0s): Pause between webhook sends (Discord rate limiting)
- `FETCH_TIMEOUT` (20s): HTTP request timeout
- `RSS_COLOR` (0xEE802F): Orange color for embeds
- `WEBHOOK_RE`: Regex validation for Discord webhook URLs

## Database

SQLite is stored at `{DATA_DIR}/rss.sqlite3`. Foreign keys are enabled.

- `feeds` table: Per-feed subscription state, includes HTTP caching headers and polling timestamps
- `seen_entries` table: Composite key (feed_id, entry_key), cascading deletes on feed removal

The poller queries `due_feeds(now)` to find feeds where `last_polled + interval_seconds <= now`.

## Guild Scoping

All feed operations are scoped by `guild_id`. Users in one server cannot access or modify feeds from another server. Feed removal by URL is scoped to the invoking guild (no cross-guild interference).
