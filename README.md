# rss-feed

A Discord bot that polls RSS/Atom feeds and announces **new** items to channels
via webhooks.

## Features

- Add, edit, and remove feeds per server with slash commands
- Feed adapters for vendor-specific shapes (F5/NGINX support, Royal Road, Ubuntu Security Notices); generic feedparser fallback for everything else
- Background poller with per-feed intervals, conditional HTTP requests
  (ETag / Last-Modified), and built-in rate limiting
- Only announces items it hasn't seen before (state kept in SQLite)
- Failure handling: exponential backoff for broken feeds, webhook alerts after
  repeated failures, recovery notices, and `/rss status` for health visibility
- Delivery via Discord webhooks with formatted embeds and per-feed favicon avatars
- Daily log files with configurable retention and level
- Container-ready: Dockerfile + docker-compose, state on a volume
- Push-to-deploy CI/CD: GitHub Actions lints, builds, and publishes a Docker
  image to GHCR, then rolls the container on the VPS over SSH

## Commands

All commands are slash commands. `add`, `remove`, `edit`, `status`, `interval`,
`reset`, and `poll` require the **Manage Server** permission by default.

| Command | Description |
| --- | --- |
| `/ping` | Liveness check, replies with gateway latency |
| `/hello` | Greets the invoking user |
| `/rss add <feed_url> <webhook_url> [interval] [feed_type]` | Register a feed (default interval 4 h, default type `generic`) |
| `/rss remove <id\|url>` | Stop polling a feed |
| `/rss list` | List this server's feeds (paginated, 5 per page, prev/next buttons) |
| `/rss edit <id\|url> [name] [webhook] [interval] [type]` | Update a feed in place without re-adding it |
| `/rss status` | Show feeds with consecutive polling failures and their last error |
| `/rss interval <id\|url> <seconds>` | Change polling interval (min 120 s) |
| `/rss reset` | Mark every feed in this server due now so they re-poll on the next cycle (only new items are announced) |
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
| `f5` | F5 / NGINX Support feed shape — **announces security advisories only**: entries without a `CVE-YYYY-NNNN` reference in the title or description are dropped |
| `royalroad` | Royal Road fiction feeds |
| `ubuntu` | Ubuntu Security Notices (USN) feed — every item is a security advisory, so all are announced (no filtering) |

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
| `MIN_INTERVAL` | `120` | Floor for per-feed polling interval (seconds) |
| `DEFAULT_INTERVAL` | `14400` | Default polling interval for new feeds (seconds, 4h) |
| `MAX_ITEMS_PER_POLL` | `5` | Max new entries announced per feed per cycle |
| `FETCH_SPACING` | `1.0` | Pause between feed fetches within a cycle (seconds) |
| `SEND_SPACING` | `1.0` | Pause between webhook sends (seconds) |
| `FETCH_TIMEOUT` | `20` | HTTP request timeout (seconds) |
| `MAX_BACKOFF` | `3600` | Cap on the exponential backoff interval (seconds) |
| `PAGE_SIZE` | `5` | Feeds per page in `/rss list` |
| `RSS_COLOR` | `0xEE802F` | Embed color for announcements (hex) |
| `ERROR_COLOR` | `0xE74C3C` | Embed color for failure alerts (hex) |

### Run locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python bot.py
```

### Development setup (one-time per clone)

Activate the pre-commit hook that auto-fixes and lints all staged `.py` files before each commit:

```bash
git config core.hooksPath .githooks
```

### Run in a container

```bash
docker compose up -d --build
```

Locally, Compose auto-merges `docker-compose.override.yml` on top of
`docker-compose.yml`, so the command above builds from source, bind-mounts the
working tree, and (with `docker compose watch`) live-reloads on changes. Feed
state is stored in the `bot-data` volume (`/data/rss.sqlite3`) and logs in
`bot-logs` (`/logs`), so history survives restarts and redeploys.

## Deployment (CI/CD)

Pushing to `main` runs `.github/workflows/deploy.yml`, a three-stage pipeline:

1. **lint** — `pycodestyle` (PEP 8, max-line-length from `setup.cfg`). A failure
   blocks the build and deploy.
2. **build-push** — builds the image (on pushes **and** pull requests, so a
   broken `Dockerfile` is caught before merge) and, **on push to `main` only**,
   publishes it to `ghcr.io/<owner>/rss-feed`, tagged `latest` and
   `sha-<commit>`, with GitHub Actions layer caching.
3. **deploy** — SSHes into the VPS and runs `docker compose pull && up -d
   --remove-orphans` followed by `docker image prune -f`. The VPS only ever
   pulls the pre-built image; it never builds.

The production `docker-compose.yml` references the published image
(`image: ghcr.io/<owner>/rss-feed:latest`); the build/bind-mount/watch settings
for local development live in `docker-compose.override.yml`, which is **not**
copied to the VPS.

### Required repository secrets

Set these in **Settings → Secrets and variables → Actions**:

| Secret | Purpose |
| --- | --- |
| `VPS_HOST` | VPS hostname or IP |
| `VPS_USER` | SSH login user (must be in the `docker` group) |
| `VPS_SSH_KEY` | Private key whose public half is in the VPS `authorized_keys` |
| `VPS_PORT` | SSH port (optional, defaults to `22`) |
| `VPS_APP_DIR` | App dir holding `docker-compose.yml` + `.env` (optional, defaults to `~/rss-feed`) |

`GITHUB_TOKEN` is provided automatically and authenticates the GHCR push — no
setup needed.

### One-time VPS setup

1. Install Docker Engine + the Compose plugin; add the deploy user to the
   `docker` group.
2. `mkdir ~/rss-feed` and copy **only** the production `docker-compose.yml`
   there (not the override).
3. Create `~/rss-feed/.env` with `DISCORD_TOKEN=...` (and any tunables from the
   table above).
4. Add the deploy key's public half to the VPS user's
   `~/.ssh/authorized_keys`; store the private half as the `VPS_SSH_KEY` secret.

After that, every push to `main` deploys automatically.

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

### Daily calibration

Once a day, at **00:01 GMT+7**, a background calibration task re-polls every
feed across **all** servers automatically — the global, scheduled equivalent of
running `/rss reset` in each guild. It clears each feed's polling cursors
(`last_polled`, ETag, Last-Modified) so the next cycle fetches fresh, while
preserving the seen-item history and failure/backoff state, so only genuinely
new items are announced. The run is silent in Discord (it only writes a log
line); any resulting announcements come from the normal poller cycle that
follows. To force a re-poll for a single server on demand, use `/rss reset`.

## Database schema

State lives in a single SQLite database at `{DATA_DIR}/rss.sqlite3`
(`/data/rss.sqlite3` in Docker). Foreign keys are enabled. Two tables:

### `feeds` — one row per feed subscription

| Column | Type | Description |
| --- | --- | --- |
| `id` | `INTEGER PRIMARY KEY` | Feed id, shown in `/rss list` and accepted everywhere a `<id\|url>` ref is |
| `guild_id` | `INTEGER NOT NULL` | Discord server the feed belongs to (all operations are scoped to it) |
| `url` | `TEXT NOT NULL` | Feed URL; `UNIQUE (guild_id, url)` prevents duplicates per server |
| `name` | `TEXT NOT NULL` | Display name (feed title at add time, editable via `/rss edit`) |
| `webhook_url` | `TEXT NOT NULL` | Discord webhook that announcements are sent to |
| `feed_type` | `TEXT NOT NULL DEFAULT 'generic'` | Which parser adapter to use |
| `interval_seconds` | `INTEGER NOT NULL DEFAULT 14400` | Polling interval (min 120 s) |
| `added_by` | `INTEGER NOT NULL` | Discord user id of whoever added the feed |
| `etag` / `last_modified` | `TEXT` | HTTP caching headers for conditional GETs |
| `last_polled` | `REAL NOT NULL DEFAULT 0` | Unix timestamp of the last poll attempt |
| `fail_count` | `INTEGER NOT NULL DEFAULT 0` | Consecutive poll failures; drives backoff, alerts, and `/rss status` |
| `last_error` | `TEXT` | Message from the most recent poll failure |
| `icon_url` | `TEXT` | Favicon URL used as the webhook avatar |

### `seen_entries` — items already announced

| Column | Type | Description |
| --- | --- | --- |
| `feed_id` | `INTEGER NOT NULL` | References `feeds(id)`, `ON DELETE CASCADE` |
| `entry_key` | `TEXT NOT NULL` | Stable entry identifier (`id` → `link` → hash fallback) |

Primary key is `(feed_id, entry_key)`. Bounded to the newest 500 entries per
feed so it can't grow without limit.

Schema migrations are handled in `db.init()`: columns added after the first
release are patched into pre-existing databases via `PRAGMA table_info` +
`ALTER TABLE`, so upgrading the bot never requires manual migration steps.

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

See `adapters/f5.py`, `adapters/royalroad.py`, and `adapters/ubuntu.py` for
real examples.

## Logs

Logs go to stdout and to a daily file `{LOG_DIR}/bot-YYYY-MM-DD.log` (default
`storage/logs/`). The active file always carries the current date; rotation
happens at midnight and the latest `LOG_BACKUP_COUNT` (default 7) daily files
are kept. Every command invocation, webhook push, and poll outcome is logged —
webhook URLs are never written to the log.
