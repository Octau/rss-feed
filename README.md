# rss-feed

A Discord bot that polls RSS/Atom feeds and announces **new** items to channels
via webhooks.

## Features

- Add/remove feeds per server with bot commands
- Background poller with per-feed intervals, conditional HTTP requests
  (ETag / Last-Modified), and built-in rate limiting
- Only announces items it hasn't seen before (state kept in SQLite)
- Delivery via Discord webhooks with formatted embeds
- Container-ready: Dockerfile + docker-compose, state on a volume

## Commands

| Command | Description |
| --- | --- |
| `!ping` | Liveness check, replies with gateway latency |
| `!rss add <feed_url> <webhook_url> [interval_s]` | Register a feed (default 300s) |
| `!rss remove <id\|url>` | Stop polling a feed |
| `!rss list` | List this server's feeds |
| `!rss interval <id\|url> <seconds>` | Change polling interval (min 120s) |
| `!rss poll <id\|url>` | Force a poll on the next cycle |

`add`, `remove`, `interval`, and `poll` require the **Manage Server** permission.
The bot deletes the `!rss add` message because it contains the webhook URL —
prefer running it in an admin-only channel.

## Setup

1. Create an application + bot at the
   [Discord Developer Portal](https://discord.com/developers/applications),
   enable the **Message Content Intent** under *Bot → Privileged Gateway
   Intents*, and copy the token.
2. Create a webhook in the target channel
   (*Channel settings → Integrations → Webhooks*) and copy its URL.
3. Configure the environment:

   ```bash
   cp .env.example .env   # then paste your token into .env
   ```

### Run locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python bot.py
```

### Run in a container

```bash
docker compose up -d --build
```

Feed state is stored in the `bot-data` volume (`/data/rss.sqlite3`), so polling
history survives restarts and redeploys.

## How polling works

A scheduler wakes every 60 seconds and polls feeds whose interval has elapsed.
Requests are conditional (304 responses are skipped cheaply), fetches are spaced
1s apart, at most 5 new items are announced per feed per cycle, and webhook
sends are spaced 1s apart to stay well inside Discord's rate limits. Newly
added feeds have their current entries marked as seen so a channel is never
flooded on registration.
