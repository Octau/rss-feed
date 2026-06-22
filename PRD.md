# PRD — Discord RSS Feed Bot v1.8

**Date:** 2026-06-22
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
| Feed list usable at scale | Pagination works beyond 25 feeds (5/page, prev/next buttons, persistent view) |
| Adapter extensibility | Any dev can add a new adapter by following documented steps |

---

## 3. Scope

### In scope (v1.1)
- `/rss edit` — update name, webhook URL, interval, or feed_type in-place
- `/rss status` — show feeds with consecutive failures, including last error message
- Exponential backoff for consistently failing feeds (computed per-cycle, capped at 1 h)
- Webhook alert on 4th consecutive failure, and again at each doubling milestone (8, 16, …); ✅ recovery notice on success
- Pagination for `/rss list` — 5 feeds/page, prev/next buttons, `Page N of M` footer, persistent `discord.ui.View`
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

5 feeds/page with `discord.ui.View` prev/next buttons. Buttons are disabled at
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
12. `rss list` — paginated with `FeedListView` (5/page, persistent)
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

**Command logging via a custom `CommandTree`.** All RSS commands are app (slash) commands. `commands.Bot` has no `interaction_check` hook — that method only exists on `app_commands.CommandTree` — so the bot is constructed with `tree_cls=LoggingCommandTree`, a tree subclass whose `interaction_check` logs every application-command interaction and returns `True`. (Guarded on `InteractionType.application_command` so autocomplete and component interactions aren't logged.)

**Log line formats:**
- `[command] user=<id> guild=<id> cmd=<name>` — INFO; args excluded so webhook URLs passed to `rss add`/`rss edit` never reach the log
- `[webhook] feed_id=<n> name=<name> title=<entry title>` — INFO; webhook URL omitted entirely
- `[poller] feed_id=<n> status=ok|skipped|error entries_new=<n>` — INFO (`error` via `log.exception`)
- `[poller] cycle_start feeds_due=<n>` / `cycle_end elapsed=<s>` — DEBUG

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
4. Add `LoggingCommandTree(app_commands.CommandTree)` with an `interaction_check` that emits `[command]` INFO lines; pass `tree_cls=LoggingCommandTree` to the bot
5. `[webhook]` log line in every webhook send path (poller, `rss add` preview, `rss poll`) — feed id and entry title only, no URL
6. `[poller]` log lines at cycle start/end and per-feed outcome (INFO / exception)
7. Update `.env.example` with all three LOG_* vars and inline comments

---

## v1.4 — Manual reset & F5 CVE filtering (signed off 2026-06-18)

### Problem

Two operational gaps:

1. **No way to force a re-poll of every feed at once.** After fixing a broken
   webhook, changing channels, or recovering from an outage, an operator can
   only force one feed at a time (`/rss poll`) or wait out each feed's interval.
   There is no "poll everything now" control.
2. **The F5 feed is too noisy.** F5's NGINX support feed carries a mix of
   product announcements, EoL notices, and security advisories. Most servers
   subscribe to it only for the security content. Every non-security item is
   currently announced, drowning out the CVE advisories operators actually care
   about.

### Success Criteria

| Metric | Target |
|---|---|
| Force re-poll of all feeds | `/rss reset` marks every feed in the guild due on the next cycle |
| Reset is non-destructive | `seen_entries` is preserved — no historical items are re-announced |
| Reset is guild-scoped | Only the invoking server's feeds are touched |
| F5 noise reduction | Only F5 entries that reference a CVE are announced; non-CVE items are silently dropped |

### Design Decisions (locked)

**`/rss reset` — clears polling cursors, keeps seen history.** Resets
`last_polled = 0`, `etag = NULL`, `last_modified = NULL` for every feed in the
guild (the same fields `db.mark_due()` clears for a single feed). Because
`seen_entries` is untouched, the next cycle fetches each feed fresh but only
announces genuinely new items. Requires the **Manage Server** permission, like
the other mutating commands. Replies with the number of feeds reset. Backoff
state (`fail_count`) is left intact so a broken feed doesn't lose its backoff.

**F5 CVE filter — applied in the adapter.** Filtering lives in
`F5RSSFeed.entries()`, so only CVE entries flow through the rest of the
pipeline. This keeps the poller, the `/rss add` preview, and `/rss poll`
consistent: all three see the same filtered view. An entry qualifies when its
title or description matches `CVE-\d{4}-\d{4,}` (case-insensitive). Non-CVE
items never reach `seen_entries`, so if such an item later gains a CVE
reference it would still be announced.

### Scope

**In scope (v1.4)**
- `/rss reset` slash command (Manage Server) + `db.reset_feeds(guild_id)`
- CVE-only filtering in the F5 adapter

**Out of scope (v1.4)**
- Configurable / per-feed keyword filtering for arbitrary feed types (still
  deferred from v1.1)
- A `/rss reset <id|url>` single-feed variant (use `/rss poll` instead)
- Flushing `seen_entries` on reset

### Implementation Plan

1. Add `db.reset_feeds(guild_id) -> int` — clear `last_polled`/`etag`/
   `last_modified` for all feeds in the guild, return affected row count
2. Add `/rss reset` command in `cogs/rss.py` (Manage Server, guild-scoped)
3. Add a CVE regex + filter in `adapters/f5.py`'s `entries()`
4. Update `CLAUDE.md` and `README.md` to document both changes

---

## v1.5 — Ubuntu Security Notices adapter (signed off 2026-06-18)

### Problem

Operators want to surface Ubuntu Security Notices (USN) in Discord, but the
feed (`https://ubuntu.com/security/notices/rss.xml`) has a vendor-specific
shape the `generic` parser handles only loosely: a Feedgen `generator`, a
`copyright` and `docs` channel field, and item `guid`s that are the notice URL
carrying an `isPermaLink="false"` attribute. A typed adapter makes the source
first-class and documents its shape, like F5 and Royal Road.

### Success Criteria

| Metric | Target |
|---|---|
| Ubuntu feed is a first-class type | `ubuntu` appears as a `feed_type` choice in `/rss add` and `/rss edit` |
| Correct parsing | USN items are normalized to the standard entry dict (stable `guid` id, link, title, pubDate, plain-text description) |
| Pipeline parity | The adapter flows through the poller, `/rss add` preview, and `/rss poll` unchanged |

### Design Decisions (locked)

**No filtering — the whole feed is advisories.** Unlike F5 (which mixes
product news with security advisories and so filters to CVE items), every item
in the Ubuntu feed is a USN security notice. `entries()` returns all items;
nothing is dropped. The plain-text description (which inlines the affected
CVEs) is kept as-is — `clean_summary()` truncates it at embed time, exactly as
for generic entries.

**Auto-registration.** The adapter exports `ADAPTER = UbuntuRSSFeed`; the
registry in `adapters/__init__.py` discovers it at import time, so no cog or
registry edits are needed. `feed_type = "ubuntu"`.

### Scope

**In scope (v1.5)**
- `adapters/ubuntu.py` — `UbuntuRSSFeed` adapter (`feed_type = "ubuntu"`) with
  `from_parsed`, `from_dict`, and `entries`
- Capture the `copyright`, `generator`, and `docs` channel fields and the item
  `guid` `isPermaLink` attribute

**Out of scope (v1.5)**
- CVE-only or per-keyword filtering of Ubuntu items (the feed is already
  all-advisories; arbitrary keyword filtering stays deferred from v1.1)
- Parsing the inline CVE list into structured fields

### Implementation Plan

1. Add `adapters/ubuntu.py` modeled on `adapters/royalroad.py` (guid +
   isPermaLink) plus F5's `copyright` channel field, exporting `ADAPTER`
2. Update `CLAUDE.md` and `README.md` to document the new feed type

---

## v1.6 — Env-configurable poller tunables (signed off 2026-06-18)

### Problem

The poller/RSS tunables (`MIN_INTERVAL`, `DEFAULT_INTERVAL`,
`MAX_ITEMS_PER_POLL`, `FETCH_SPACING`, `SEND_SPACING`, `FETCH_TIMEOUT`,
`MAX_BACKOFF`, `PAGE_SIZE`, `RSS_COLOR`, `ERROR_COLOR`) were hard-coded
module constants in `cogs/rss.py`. Tuning rate limits, intervals, page size, or
embed colors for a deployment meant editing source. Operators want to adjust
these per-environment the same way they already set `DATA_DIR` and the `LOG_*`
vars — via `.env`.

### Success Criteria

| Metric | Target |
|---|---|
| Operator-tunable without code edits | Each constant is read from a matching `.env` variable at startup |
| Safe defaults | With no env vars set, behavior is identical to before (same numeric defaults) |
| Documented | All ten vars appear in `.env.example`, `README.md`, and `CLAUDE.md` with their defaults |

### Design Decisions (locked)

**Same names, same defaults.** The env variable for each constant has the
identical name and the documented value as its fallback, e.g.
`MIN_INTERVAL = int(os.getenv("MIN_INTERVAL", "120"))`. No behavior changes
when the vars are unset.

**Read once at module load.** Values are resolved at import time in
`cogs/rss.py`, consistent with how `bot.py` reads its config; `load_dotenv()`
already runs in `bot.py` before the cog loads. Integers parse via `int(...)`,
spacings via `float(...)`, and colors via `int(..., 0)` so `0x`-prefixed hex
works.

**`WEBHOOK_RE` stays in code.** The webhook-URL validation regex is not a
deployment tunable and remains a hard-coded constant.

### Scope

**In scope (v1.6)**
- Make the ten tunables above read from `.env` with their current values as
  defaults
- Document them in `.env.example`, `README.md`, and `CLAUDE.md`

**Out of scope (v1.6)**
- Runtime reconfiguration (values are read once at startup, not hot-reloaded)
- Per-guild overrides of any tunable
- Validation/clamping beyond Python's `int`/`float` parsing

### Implementation Plan

1. In `cogs/rss.py`, wrap each constant in `os.getenv(...)` with the existing
   value as the string default (add `import os`)
2. Add all ten vars (commented, with defaults) to `.env.example`
3. Document them in `README.md` (env table) and `CLAUDE.md` (Configuration +
   Key Constants)

---

## v1.7 — Daily feed calibration cronjob (signed off 2026-06-18)

### Problem

`/rss reset` lets an operator force every feed in **one** guild to re-poll on
the next cycle (clears `last_polled`/`etag`/`last_modified`, keeps
`seen_entries`). But it is a manual, per-guild action. After a multi-guild
outage — a webhook host hiccup, a deploy that missed a cycle, or a feed source
that briefly served stale conditional-GET headers — there is no hands-off way to
guarantee every feed across every server re-fetches fresh. Operators want a
scheduled "calibration" that does the same thing automatically, once a day,
globally.

### Success Criteria

| Metric | Target |
|---|---|
| Scheduled re-poll of all feeds | A background task clears polling cursors for every feed in every guild once per day |
| Runs at a fixed local time | Fires at **00:01 GMT+7** every day |
| Non-destructive | `seen_entries` and `fail_count` are preserved — only genuinely new items are announced; backoff state is intact |
| Global, not per-guild | Unlike `/rss reset`, the cron is not guild-scoped — it touches all feeds (a cron has no invoking guild) |
| Observable | Each run logs how many feeds were calibrated |

### Design Decisions (locked)

**Same effect as `/rss reset`, applied globally.** The calibration clears
`last_polled = 0`, `etag = NULL`, `last_modified = NULL` for **all** feeds
(every guild), via a new `db.reset_all_feeds()` that mirrors `db.reset_feeds()`
without the `WHERE guild_id = ?` filter. `seen_entries` and `fail_count` are
untouched, so the next poller cycle re-fetches each feed but announces only new
items — identical semantics to the manual command. Because a scheduled job has
no invoking guild, global scope is the only sensible interpretation of "same
functionality as `/rss reset`."

**Scheduled with `tasks.loop(time=...)`.** A second `discord.ext.tasks` loop on
the cog (alongside `poller`) is scheduled with a tz-aware `datetime.time` of
`00:01` at a fixed `UTC+7` offset (`timezone(timedelta(hours=7))`). discord.py
runs a `time=`-scheduled loop once per day at that wall-clock time, so no manual
"is it 00:01 yet?" math is needed. GMT+7 is a fixed offset with no DST, so a
plain `timezone(timedelta(hours=7))` is exact and needs no `zoneinfo`/`tzdata`
dependency. The loop starts in `cog_load` and is cancelled in `cog_unload`,
exactly like `poller`; `before_loop` waits for `bot.wait_until_ready()`.

**Silent except for a log line.** The calibration sends nothing to Discord on
its own — like `/rss reset`, any announcements come from the subsequent normal
poller cycle and are limited to new items. Each run emits one
`[calibration] reset last-poll for <n> feed(s) across all guilds` INFO line.

### Scope

**In scope (v1.7)**
- `db.reset_all_feeds() -> int` — clear `last_polled`/`etag`/`last_modified`
  for all feeds in all guilds, return affected row count
- A `calibrator` `tasks.loop(time=00:01 GMT+7)` task on the RSS cog, started in
  `cog_load`, cancelled in `cog_unload`
- Log line per run

**Out of scope (v1.7)**
- A configurable calibration time (the 00:01 GMT+7 schedule is fixed for now)
- Per-guild scheduling or opt-out
- Flushing `seen_entries` on calibration (kept, same as `/rss reset`)
- A manual "calibrate all guilds now" command (operators use `/rss reset`
  per guild, or wait for the daily run)

### Implementation Plan

1. Add `db.reset_all_feeds() -> int` mirroring `db.reset_feeds()` without the
   guild filter
2. Add `CALIBRATION_TIME = time(0, 1, tzinfo=timezone(timedelta(hours=7)))` and
   a `calibrator` `tasks.loop(time=CALIBRATION_TIME)` task to `cogs/rss.py`;
   start/cancel it alongside `poller`
3. Update `CLAUDE.md` and `README.md` to document the daily calibration

---

## v1.8 — CI/CD deployment to VPS via GitHub Actions + GHCR (signed off 2026-06-22)

### Problem

The bot ships with a `Dockerfile` and a single `docker-compose.yml`, but every
deploy to the VPS is manual: SSH in, `git pull`, `docker compose up -d --build`,
hope the host has enough RAM to build. There is no automated build, no published
image, no lint gate, and no reproducible "this exact commit is what's running"
artifact. Operators want a push-to-deploy pipeline: merge to `main`, CI builds
and publishes an image, the VPS pulls and restarts.

### Success Criteria

| Metric | Target |
|---|---|
| Push-to-deploy | A push to `main` builds, publishes, and rolls the VPS container with no manual SSH |
| Lint gate | A PEP 8 (`pycodestyle`) failure blocks the build and deploy |
| Reproducible artifact | Each build is published to GHCR tagged `latest` and `sha-<commit>` |
| No build on the VPS | The VPS pulls a pre-built image; it never compiles the image itself |
| State survives deploys | SQLite (`/data`) and logs (`/logs`) live on named volumes, untouched by image swaps |
| Docker build checked on PRs | Every PR builds the image (no push, no deploy), so a broken `Dockerfile` is caught before merge |

### Design Decisions (locked)

**Registry: GitHub Container Registry (GHCR).** Image published to
`ghcr.io/<owner>/rss-feed`. GHCR needs no extra account and authenticates with
the built-in `GITHUB_TOKEN` (scope `packages: write`) — no third-party registry
secrets. Image name is lowercased in CI because GHCR rejects uppercase paths
(the owner is `Octau`).

**Three-stage pipeline (`lint` → `build-push` → `deploy`).** `lint` runs
`pycodestyle bot.py db.py adapters cogs` (max-line-length from `setup.cfg`) and
**gates** everything after it — a style failure stops the run before any image
is built. `build-push` uses `docker/build-push-action` with GitHub Actions layer
cache (`type=gha`) and runs on **both push and PR**: it always builds the image
(so PRs verify the `Dockerfile`), but only *pushes* to GHCR on push to `main`
(`push: ${{ github.event_name == 'push' }}`; the GHCR login step is likewise
`push`-only). `deploy` SSHes to the VPS and rolls the container, and is guarded
with `if: github.event_name == 'push'` so it never runs on a PR. Net: PRs run
**lint + build** (no publish, no deploy); push to `main` runs all three stages.

**Deploy mechanism: SSH + `docker compose pull && up -d`.** `appleboy/ssh-action`
connects with a deploy key, then in the app dir runs `docker login ghcr.io`,
`docker compose pull`, `docker compose up -d --remove-orphans`, and
`docker image prune -f`. The VPS holds only the production `docker-compose.yml`
(pulls the published image) and an `.env`; it never builds.

**Two compose files split build-vs-pull.** `docker-compose.yml` (production,
lives on the VPS) references `image: ghcr.io/<owner>/rss-feed:latest`.
`docker-compose.override.yml` (local dev, auto-merged by Compose, NOT copied to
the VPS) restores `build: .`, the source bind-mount, and `develop.watch`. This
keeps the prod host pull-only while local dev stays build-from-source.

**Logs persist on a named volume.** The `Dockerfile` adds `ENV LOG_DIR=/logs`
and `VOLUME /logs`; prod compose mounts a `bot-logs` volume so daily log files
survive image swaps, mirroring the existing `/data` SQLite volume.

**`concurrency` guard.** One pipeline run per ref at a time
(`cancel-in-progress: true`) so a rapid second push doesn't race two deploys
onto the VPS.

### Scope

**In scope (v1.8)**
- `.github/workflows/deploy.yml` — `lint` → `build-push` → `deploy` pipeline
- `.dockerignore` — keep `.env`, `venv/`, `data/`, `storage/`, VCS, and docs out
  of the build context/image
- `Dockerfile` — add `LOG_DIR=/logs` env + `VOLUME /logs`
- `docker-compose.yml` — switch to `image:` (pull) for production, add `bot-logs`
  volume
- `docker-compose.override.yml` — local dev overrides (build, bind-mount, watch)
- Documented GitHub secrets and one-time VPS setup

**Out of scope (v1.8)**
- Automated tests in the pipeline (repo has none yet)
- Multi-arch (arm64) image builds
- Staging environment / blue-green or zero-downtime rollout
- Container health checks / auto-rollback on a crashing image
- Tag/release-triggered deploys (deploy is `push`-to-`main` only)
- Secret management beyond GitHub Actions secrets (no Vault/SOPS)

### Required GitHub secrets

| Secret | Purpose |
|---|---|
| `VPS_HOST` | VPS hostname or IP |
| `VPS_USER` | SSH login user (must be in the `docker` group) |
| `VPS_SSH_KEY` | Private key whose public half is in the VPS `authorized_keys` |
| `VPS_PORT` | SSH port (optional; defaults to `22`) |
| `VPS_APP_DIR` | App dir on the VPS holding `docker-compose.yml` + `.env` (optional; defaults to `~/rss-feed`) |

`GITHUB_TOKEN` is provided automatically by Actions — no setup needed.

### One-time VPS setup

1. Install Docker Engine + the Compose plugin; add the deploy user to the
   `docker` group.
2. `mkdir ~/rss-feed` and copy **only** the production `docker-compose.yml` there
   (not the override).
3. Create `~/rss-feed/.env` with `DISCORD_TOKEN=...` (and any tunables).
4. Add the deploy key's public half to `~/.ssh/authorized_keys`; store the
   private half as the `VPS_SSH_KEY` repo secret.

### Implementation Plan

1. Add `.dockerignore`; extend `Dockerfile` with `LOG_DIR`/`VOLUME /logs`.
2. Convert `docker-compose.yml` to pull the GHCR image + add `bot-logs`; move
   build/bind-mount/watch into `docker-compose.override.yml`.
3. Add `.github/workflows/deploy.yml` (`lint` → `build-push` → `deploy`).
4. Update `CLAUDE.md` and `README.md` to document the pipeline, secrets, and VPS
   setup.
