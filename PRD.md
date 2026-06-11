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

### Phase 4 — Adapter docs
14. Add adapter authoring guide to `README.md`

---

## 7. Open Questions

All resolved — none outstanding.
