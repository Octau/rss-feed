# rss-feed

A Discord bot that polls RSS/Atom feeds and announces **new** items to channels
via webhooks.

## Features

- Add, edit, and remove feeds per server with slash commands
- Feed adapters for vendor-specific shapes (F5/NGINX support, Royal Road); generic feedparser fallback for everything else
- Background poller with per-feed intervals, conditional HTTP requests
  (ETag / Last-Modified), and built-in rate limiting
- Only announces items it hasn't seen before (state kept in SQLite)
- Failure handling: exponential backoff for broken feeds, webhook alerts after
  repeated failures, recovery notices, and `/rss status` for health visibility
- Delivery via Discord webhooks with formatted embeds and per-feed favicon avatars
- Daily log files with configurable retention and level
- Container-ready: Dockerfile + docker-compose, state on a volume

## Commands

All commands are slash commands. `add`, `remove`, `edit`, `status`, `interval`,
and `poll` require the **Manage Server** permission by default.

| Command | Description |
| --- | --- |
| `/ping` | Liveness check, replies with gateway latency |
| `/hello` | Greets the invoking user |
| `/rss add <feed_url> <webhook_url> [interval] [feed_type]` | Register a feed (default interval 4 h, default type `generic`) |
| `/rss remove <id\|url>` | Stop polling a feed |
| `/rss list` | List this server's feeds (paginated, 10 per page, prev/next buttons) |
| `/rss edit <id\|url> [name] [webhook] [interval] [type]` | Update a feed in place without re-adding it |
| `/rss status` | Show feeds with consecutive polling failures and their last error |
| `/rss interval <id\|url> <seconds>` | Change polling interval (min 120 s) |
| `/rss poll <id\|url>` | Fetch a feed immediately and push its latest entry to the webhook |

`/rss add` responds ephemerally so the webhook URL stays private; `/rss edit`
does the same whenever a new webhook URL is supplied. `/rss status` replies
ephemerally too.

On a successful add the newest entry is posted as a preview embed so you can
confirm the webhook is wired up correctly (if the webhook rejects it, the feed
is not added). Changing a feed's type via `/rss edit` re-fetches the feed
through the new adapter and sends the same kind of preview; the change is
rolled back if the send fails.

### Feed types

The `feed_type` option on `rss add` / `rss edit` selects a parser adapter:

| Type | Description |
| --- | --- |
| `generic` | Raw feedparser (works for most RSS/Atom feeds) |
| `f5` | F5 / NGINX Support feed shape |
| `royalroad` | Royal Road fiction feeds |

## Setup

1. Create an application + bot at the
   [Discord Developer Portal](https://discord.com/developers/applications)
   and copy the token. No privileged intents are required — the bot only uses
   slash commands.
2. Invite the bot with the `bot` and `applications.commands` OAuth2 scopes.
3. Create a webhook in the target channel
   (*Channel settings → Integrations → Webhooks*) and copy its URL.
4. Configure the environment:

   ```bash
   cp .env.example .env   # then paste your token into .env
   ```

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | Discord bot token (required) |
| `DATA_DIR` | `data` | Where the SQLite database is stored (`/data` in Docker) |
| `LOG_DIR` | `storage/logs` | Directory for daily log files |
| `LOG_BACKUP_COUNT` | `7` | Daily log files to keep; older ones are deleted at rotation |
| `LOG_LEVEL` | `INFO` | Minimum log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |

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
1 s apart, at most 5 new items are announced per feed per cycle, and webhook
sends are spaced 1 s apart to stay well inside Discord's rate limits. Newly
added feeds have their current entries marked as seen so a channel is never
flooded on registration.

### Failure handling

When a poll fails, the feed backs off exponentially: the effective interval is
`min(interval × 2^failures, 1 hour)`, so a broken feed never hot-loops. On the
4th consecutive failure — and again at each doubling (8, 16, …) — a warning
embed is sent to the feed's own webhook with the last error and the next retry
time. When the feed succeeds again, a ✅ recovery notice follows. `/rss status`
lists all currently unhealthy feeds.

## Writing a feed adapter

If a feed source has a non-standard shape (extra channel fields, unusual guid
format, HTML-heavy descriptions, etc.) you can add a typed adapter so the bot
handles it cleanly.

### 1. Create the module

Add a file at `adapters/<name>.py`. The only thing the registry needs is a
module-level `ADAPTER` name pointing to your adapter class:

```python
# adapters/mysite.py

ADAPTER = MySiteFeed
```

The registry in `adapters/__init__.py` scans the package at import time, so
no other file needs to change. The new type immediately appears as a choice in
`/rss add`.

### 2. Implement the adapter class

Your class must provide:

| Member | Description |
| --- | --- |
| `feed_type: ClassVar[str]` | The identifier users pass as the `type` option (e.g. `"mysite"`). Must be unique across adapters. |
| `from_parsed(cls, parsed) -> Self` | Classmethod that receives a feedparser result and returns an instance of your class. |
| `entries(self) -> list[dict]` | Returns a list of entry dicts (see shape below). |

Optionally add `from_dict(cls, data: dict)` if you want to construct from a
raw `{channel: {items: [...]}}` dict (useful for tests).

### 3. Entry dict shape

Each dict returned by `entries()` must carry these keys so `entry_key()` and
`build_embed()` in `cogs/rss.py` keep working:

| Key | Type | Notes |
| --- | --- | --- |
| `id` | `str` | Stable, unique identifier. Changing it re-announces history — don't. |
| `link` | `str` | URL of the item. |
| `title` | `str` | Item title. |
| `published` | `str` | Publication date string (e.g. RFC 822). |
| `published_parsed` | `time.struct_time \| None` | UTC struct_time for embed timestamp; `None` is safe. |
| `summary` | `str` | Body text or HTML — `clean_summary()` strips tags at embed time. |

`entry_key()` tries `id`, then `link`, then `sha256(title + published)` as
fallback. Any of those being stable and unique is sufficient.

### Example skeleton

```python
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import ClassVar


def _parse_date(value: str) -> time.struct_time | None:
    try:
        return parsedate_to_datetime(value).utctimetuple()
    except (TypeError, ValueError):
        return None


@dataclass
class MySiteItem:
    title: str
    link: str
    guid: str
    pub_date: str
    summary: str

    def as_entry(self) -> dict:
        return {
            "id": self.guid,
            "link": self.link,
            "title": self.title,
            "published": self.pub_date,
            "published_parsed": _parse_date(self.pub_date),
            "summary": self.summary,
        }


@dataclass
class MySiteFeed:
    feed_type: ClassVar[str] = "mysite"
    items: list[MySiteItem] = field(default_factory=list)

    def entries(self) -> list[dict]:
        return [item.as_entry() for item in self.items]

    @classmethod
    def from_parsed(cls, parsed) -> "MySiteFeed":
        return cls(items=[
            MySiteItem(
                title=e.get("title", ""),
                link=e.get("link", ""),
                guid=e.get("id") or e.get("link", ""),
                pub_date=e.get("published", ""),
                summary=e.get("summary", ""),
            )
            for e in parsed.entries
        ])


ADAPTER = MySiteFeed
```

See `adapters/f5.py` and `adapters/royalroad.py` for real examples.

## Logs

Logs go to stdout and to a daily file `{LOG_DIR}/bot-YYYY-MM-DD.log` (default
`storage/logs/`). The active file always carries the current date; rotation
happens at midnight and the latest `LOG_BACKUP_COUNT` (default 7) daily files
are kept. Every command invocation, webhook push, and poll outcome is logged —
webhook URLs are never written to the log.
