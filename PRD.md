# PRD — Discord RSS Feed Bot v1.1

**Date:** 2026-06-11
**Status:** Signed off — in progress

---

## 1. Problem

Server admins want to surface RSS/Atom feed updates (blog posts, security advisories,
fiction updates) directly in Discord channels without building custom integrations.
The existing bot (MVP) handles the core loop but has gaps that make it fragile and
hard to operate at scale: feeds break silently, nothing can be edited without deleting
and re-adding, and the poller has no backoff or health visibility.

---

## 2. Success Criteria

| Metric | Target |
|---|---|
| Feed failures are surfaced | Broken feeds visible via `/rss status` and notified to the feed's own webhook on 4th consecutive failure |
| Edit without re-add | `/rss edit` covers webhook, name, interval, and type |
| Poller stability | No hot-loop; failing feeds back off with `min(interval * 2^fail_count, 3600s)` |
| Feed list usable at scale | Pagination works beyond 25 feeds (10/page, prev/next buttons, persistent view) |
| Adapter extensibility | Any dev can add a new adapter by following documented steps |

---

## 3. Scope

### In scope (v1.1)
- `/rss edit` — update name, webhook URL, interval, or feed_type in-place
- `/rss status` — show feeds with consecutive failures, including last error message
- Exponential backoff for consistently failing feeds (computed per-cycle, capped at 1 h)
- Webhook alert on 4th consecutive failure, and again at each doubling milestone (8, 16, …); ✅ recovery notice on success
- Pagination for `/rss list` — 10 feeds/page, prev/next buttons, `Page N of M` footer, persistent `discord.ui.View`
- Persist `fail_count` and `last_error` per feed (two new columns in `feeds`)

### Out of scope (v1.1)
- JSON Feed support
- Authenticated feeds (HTTP Basic / OAuth)
- Per-feed keyword filtering
- Metrics / Prometheus endpoint
- Structured (JSON) logging
- Export / import of subscriptions
- Multi-guild admin dashboard
- Webhook URL aliasing / rotation
- Per-user rate limiting on commands

---

## 4. Constraints

- discord.py 2.7.1 and feedparser 6.0.11 — no major dep bumps without separate approval
- All new commands follow the hybrid (prefix + slash) pattern already established
- All mutations remain guild-scoped; no cross-guild side-effects
- SQLite schema changes go through the existing `PRAGMA table_info` + `ALTER TABLE` migration pattern in `db.init()`
- `MIN_INTERVAL = 120 s` floor stays in place
- Docker image and Compose file must remain runnable after changes

---

## 5. Design Decisions (locked)

### Backoff — Option A (transient, no `next_poll_at` column)

Backoff lives entirely in the poller's math:

```
effective_interval = min(interval_seconds * 2^fail_count, 3600)
```

A feed is skipped in a cycle when `now < last_polled + effective_interval`.
`interval_seconds` is never mutated. `fail_count` persists across restarts so backoff
survives bot restarts correctly.

**Webhook alert cadence:** fires at `fail_count == 4`, then at 8, 16, … (each doubling).
Recovery (`fail_count` resets to 0) sends a ✅ notice to the same webhook.

### `rss status` — Ephemeral + fallback DM

- Slash invocation: ephemeral reply (consistent with `rss add`)
- Prefix invocation: delete the invoking message, DM the result to the invoker
- If DM fails: reply in channel with no sensitive data (no webhook URLs), directing user to the slash command

### `rss edit` type change

Allowed. When `feed_type` changes:
1. Re-fetch immediately through the new adapter
2. Send the single newest entry as a confirmation preview (same as `rss add`)
3. `seen_entries` is **not** flushed — only the one preview goes out

If the preview send fails, the type change is rolled back.

### Pagination

10 feeds/page with `discord.ui.View` prev/next buttons. Buttons are disabled at
boundaries. Footer shows `Page N of M`. Views are persistent (no 15-minute expiry).

---

## 6. Implementation Plan

### Phase 1 — Schema & DB layer ✅
1. Add `fail_count INTEGER NOT NULL DEFAULT 0` column to `feeds`
2. Add `last_error TEXT` column to `feeds`
3. Add `db.update_feed()` — edit name, webhook_url, feed_type, interval_seconds
4. Add `db.record_poll_failure(feed_id, error)` — increment fail_count, store last_error
5. Add `db.record_poll_success(feed_id)` — reset fail_count=0, clear last_error
6. Add `db.unhealthy_feeds(guild_id)` — feeds where fail_count > 0

### Phase 2 — Poller hardening ✅
7. After success: call `db.record_poll_success`
8. After failure: call `db.record_poll_failure`, compute backoff interval, skip if not due
9. Alert webhook at fail_count == 4 and each doubling; recovery notice on reset from ≥4

### Phase 3 — New commands ✅
10. `rss edit <id|url> [name] [webhook] [interval] [type]`
11. `rss status` — paginated list of unhealthy feeds (ephemeral/DM)
12. `rss list` — paginated with `FeedListView` (10/page, persistent)
13. Update help embed with new commands

### Phase 4 — Adapter docs ✅
14. Add adapter authoring guide to `README.md`

---

## 7. Open Questions

All resolved — none outstanding.

---

## v1.2 — Icon URL Normalization (signed off 2026-06-11)

### Problem
`extract_icon_url()` stored raw `favicon.ico` paths and feed image URLs that frequently 404 in Discord embeds. Discord renders a broken image placeholder when the URL fails.

### Solution
Route all icon URLs through **Google S2 Favicon Service** (`https://www.google.com/s2/favicons?domain=<host>&sz=64`). The domain is extracted from the feed `<image>` href (if present) or the feed's `<link>` site URL. Result is always a valid image — Google falls back to a default globe icon for unknown domains.

### Scope
- `_google_favicon(site_url)` helper added to `cogs/rss.py`
- `extract_icon_url()` updated to call it for both code paths
- Applies on `rss add` and `rss edit` (both already call `extract_icon_url`)
- No new dependencies (stdlib `urllib.parse` only)
- No DB migration — existing rows unaffected until next edit/re-add

---

## v1.3 — Logging Reimplementation (signed off 2026-06-12; supersedes reverted PR #8)

### Problem

The current logging setup in `bot.py` is hardcoded: 3-day rotation, 14 backups, root logger only. Operators can't tune retention without editing source. Log filenames don't embed the date — the active file is always `bot.log`, so grepping or archiving by day requires waiting for rotation. There is no consistent record of which commands were invoked, when webhooks were pushed, or when the poller ran — debugging a missed announcement means scrubbing discord.py internals rather than reading a clear event log.

A first attempt (PR #8) shipped suffix-based rotation but was reverted: it kept writing the live log to `bot.log` and only renamed archives, and its `basicConfig` call could be silently skipped if another import configured the root logger first.

### Success Criteria

| Metric | Target |
|---|---|
| Date in filename at runtime | The **active** log file is `bot-YYYY-MM-DD.log` for the current date — not just archives |
| Daily rotation, 7-day retention | Rotates at midnight; latest 7 daily files kept, older deleted |
| Configurable retention | `LOG_BACKUP_COUNT` env var, default `7` |
| Configurable level | `LOG_LEVEL` env var, default `INFO` |
| Configurable directory | `LOG_DIR` env var, default `storage/logs` |
| Command events logged | Every slash command invocation (user, guild, command name) emits an INFO line |
| Webhook push events logged | Every Discord webhook send emits an INFO line (feed id, entry title — **no webhook URL**) |
| Poll cycle events logged | Every poller cycle and per-feed outcome emits a log line |
| Old handler fully removed | Hardcoded `TimedRotatingFileHandler(when='D', interval=3, backupCount=14)` replaced entirely |
| `.env.example` updated | All LOG_* vars documented with defaults |
| No code changes needed for defaults | Existing deployments work unchanged after update |

### Design Decisions (locked)

**Dated active file — `DailyFileHandler` subclass.** Stock `TimedRotatingFileHandler` always writes to the base filename and only applies the date suffix to rotated archives. To make the runtime write directly to its own date's file, subclass it: `__init__` opens `bot-<today>.log`, and `doRollover` closes the stream, repoints `baseFilename` at the new date's path, reopens, and prunes `bot-*.log` files beyond `backupCount` via glob. Based on the reference `TimedRotatingFileHandler` pattern (formatter + handler extracted, named loggers via `getLogger`).

**Root logger configured directly, not via `basicConfig`.** `basicConfig` is a no-op when the root logger already has handlers (which discord.py or any early import can cause), silently dropping our handlers — this bit PR #8. Attach the file and stream handlers to the root logger explicitly with `addHandler`.

**Command logging via `interaction_check`.** All RSS commands are app (slash) commands; an `interaction_check` override on the bot logs every application-command interaction and always returns `True`.

**Log line formats:**
- `[command] user=<id> guild=<id> cmd=<name>` — INFO; args excluded so webhook URLs passed to `rss add`/`rss edit` never reach the log
- `[webhook] feed_id=<n> name=<name> title=<entry title>` — INFO; webhook URL omitted entirely
- `[poller] feed_id=<n> status=ok|skipped|error entries_new=<n>` — INFO (`error` via `log.exception`)
- `[poller] cycle_start feeds_due=<n>` / `cycle_end elapsed=<s>` — INFO

### Scope

**In scope (v1.3)**
- Remove the hardcoded handler; add `DailyFileHandler` writing to `bot-YYYY-MM-DD.log` for the current date
- Midnight rotation; keep latest `LOG_BACKUP_COUNT` (default 7) daily files, prune older
- `LOG_DIR`, `LOG_BACKUP_COUNT`, `LOG_LEVEL` env vars with safe defaults, documented in `.env.example`
- Extracted formatter and handlers; root logger configured via `addHandler` (no `basicConfig`)
- `[command]`, `[webhook]`, `[poller]` event lines as specified above

**Out of scope (v1.3)**
- Structured (JSON) logging — already deferred from v1.1
- Log shipping / remote sinks
- Per-logger level overrides
- Size-based rotation
- Logging command args (risk of leaking URLs)

### Constraints

- No new dependencies (stdlib `logging`, `logging.handlers`, `glob` only)
- Webhook URLs must not appear in any log line
- Backwards-compatible defaults: if no env vars set — daily rotation, 7 backups, INFO level, `storage/logs`

### Implementation Plan

1. Read `LOG_DIR`, `LOG_BACKUP_COUNT`, `LOG_LEVEL` from env in `bot.py`
2. Add `DailyFileHandler(TimedRotatingFileHandler)`: dated active file, `doRollover` reopens next date's file and prunes beyond `backupCount`
3. Extract formatter and stream/file handlers; attach both to the root logger directly
4. Override `RSSBot.interaction_check` to emit `[command]` INFO lines
5. `[webhook]` log line in every webhook send path (poller, `rss add` preview, `rss poll`) — feed id and entry title only, no URL
6. `[poller]` log lines at cycle start/end and per-feed outcome (INFO / exception)
7. Update `.env.example` with all three LOG_* vars and inline comments
