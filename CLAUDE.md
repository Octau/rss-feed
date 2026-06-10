# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Discord bot that manages RSS feed subscriptions for Discord servers. Users can add RSS/Atom feeds via Discord webhooks, and the bot polls them on configurable intervals to announce new entries in Discord channels.

**Key stack:** discord.py (bot framework), feedparser (RSS/Atom parsing), aiosqlite (async SQLite), aiohttp (HTTP client).

## Architecture

The project uses a modular discord.py cogs architecture:

- **bot.py** — Main entry point. Sets up the Discord bot, initializes the database on startup, and loads the RSS cog. Defines basic commands (ping, hello) and event handlers (on_ready).

- **db.py** — Async SQLite abstraction layer with a global `_conn` connection. Defines the schema and provides functions for managing feeds and tracking seen entries to prevent duplicates. Key tables:
  - `feeds`: One row per feed subscription, scoped by guild, tracks polling metadata (etag, last_modified, last_polled)
  - `seen_entries`: Prevents re-announcing the same feed item; bounded to 500 entries per feed to prevent unbounded growth

- **cogs/rss.py** — The RSS management cog. Contains:
  - Commands: `rss add`, `rss remove`, `rss list`, `rss interval`, `rss poll` (all require "Manage Server" permission)
  - `poller` task: Runs every 60 seconds, finds feeds due for polling, fetches them, detects new entries, and announces via Discord webhook
  - Helper functions: `entry_key()` (stable identifier), `clean_summary()` (HTML strip + truncate), `entry_timestamp()` (parse pubdate), `build_embed()` (Discord embed formatting)
  - HTTP conditional GET with ETag/Last-Modified to minimize bandwidth
  - Blocking feedparser work is offloaded to executor to keep the event loop responsive

## Configuration

Via `.env` (required):
- `DISCORD_TOKEN` — Discord bot token
- `BOT_PREFIX` — Command prefix (default: `!`)
- `DATA_DIR` — SQLite database location (default: `data/`)

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
- `DEFAULT_INTERVAL` (300s): Default polling interval for new feeds
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
