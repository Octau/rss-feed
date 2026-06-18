"""RSS feed management commands and the background poller."""
import asyncio
import hashlib
import html
import logging
import os
import re
import time
from datetime import datetime, time as dtime, timedelta, timezone

import aiohttp
import discord
import feedparser
from discord import app_commands
from discord.ext import commands, tasks
from urllib.parse import urlparse

import adapters
import db

log = logging.getLogger("rss")

# Tunables. Each reads from .env at startup, falling back to the documented
# default. load_dotenv() runs in bot.py before this cog is loaded.
MIN_INTERVAL = int(os.getenv("MIN_INTERVAL", "120"))            # floor per-feed interval (s)
DEFAULT_INTERVAL = int(os.getenv("DEFAULT_INTERVAL", "14400"))  # 4 hours
MAX_ITEMS_PER_POLL = int(os.getenv("MAX_ITEMS_PER_POLL", "5"))  # cap announcements per cycle
FETCH_SPACING = float(os.getenv("FETCH_SPACING", "1.0"))        # pause between feed fetches (s)
SEND_SPACING = float(os.getenv("SEND_SPACING", "1.0"))          # pause between webhook sends (s)
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=int(os.getenv("FETCH_TIMEOUT", "20")))
USER_AGENT = "rss-feed-discord-bot/1.0 (+https://github.com/)"
RSS_COLOR = int(os.getenv("RSS_COLOR", "0xEE802F"), 0)          # orange — embeds
ERROR_COLOR = int(os.getenv("ERROR_COLOR", "0xE74C3C"), 0)      # red — failure alerts
TAG_RE = re.compile(r"<[^>]+>")
WEBHOOK_RE = re.compile(r"^https://(canary\.|ptb\.)?discord(app)?\.com/api/webhooks/\d+/\S+$")
MAX_BACKOFF = int(os.getenv("MAX_BACKOFF", "3600"))             # cap backoff at 1 hour
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "5"))                    # feeds per page in rss list

# Daily feed calibration: re-poll every feed (all guilds) at 00:01 GMT+7.
# Fixed UTC+7 offset (no DST) so a plain timezone() is exact — no tzdata needed.
GMT7 = timezone(timedelta(hours=7))
CALIBRATION_TIME = dtime(hour=0, minute=1, tzinfo=GMT7)


def _google_favicon(site_url: str) -> str | None:
    """Return a Google S2 favicon URL for the domain of site_url, or None if unparseable."""
    p = urlparse(site_url)
    if p.netloc:
        return f"https://www.google.com/s2/favicons?domain={p.netloc}&sz=64"
    return None


def extract_icon_url(parsed) -> str | None:
    """Best-effort site icon via Google S2 favicon service.

    Tries feed image domain first, then feed link.
    """
    img = parsed.feed.get("image") or {}
    href = (img.get("href") or img.get("url")) if isinstance(img, dict) else (
        getattr(img, "href", None) or getattr(img, "url", None))
    if href:
        favicon = _google_favicon(href)
        if favicon:
            return favicon
    site = parsed.feed.get("link")
    if site:
        favicon = _google_favicon(site)
        if favicon:
            return favicon
    return None


def entry_key(entry) -> str:
    """Stable identifier for a feed entry, used for new-item detection."""
    key = entry.get("id") or entry.get("link")
    if not key:
        raw = (entry.get("title", "") + entry.get("published", "")).encode()
        key = hashlib.sha256(raw).hexdigest()
    return key


def clean_summary(entry, limit: int = 300) -> str:
    text = entry.get("summary") or ""
    text = html.unescape(TAG_RE.sub("", text)).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + " …"
    return text


def entry_timestamp(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    return None


def build_embed(feed_name: str, feed_url: str, entry) -> discord.Embed:
    embed = discord.Embed(
        title=entry.get("title", "Untitled")[:256],
        url=entry.get("link"),
        description=clean_summary(entry),
        color=RSS_COLOR,
        timestamp=entry_timestamp(entry),
    )
    embed.set_author(name=feed_name[:256])
    author = entry.get("author")
    if author:
        embed.set_footer(text=f"by {author}"[:2048])
    return embed


class FeedListView(discord.ui.View):
    """Paginated view for rss list. Persistent (no timeout)."""

    def __init__(self, feeds: list, guild_name: str):
        super().__init__(timeout=None)
        self.feeds = feeds
        self.guild_name = guild_name
        self.page = 0
        self.total_pages = max(1, (len(feeds) + PAGE_SIZE - 1) // PAGE_SIZE)
        self._update_buttons()

    def _update_buttons(self):
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        start = self.page * PAGE_SIZE
        page_feeds = self.feeds[start:start + PAGE_SIZE]
        embed = discord.Embed(title=f"RSS feeds in {self.guild_name}", color=RSS_COLOR)
        for f in page_feeds:
            last = (f"<t:{int(f['last_polled'])}:R>" if f["last_polled"] else "never")
            status = f" ⚠️ {f['fail_count']} failure(s)" if f["fail_count"] else ""
            embed.add_field(
                name=f"#{f['id']} — {f['name'][:180]}",
                value=f"{f['url']}\n{f['feed_type']} · every {f['interval_seconds']}s"
                      f" · last polled {last}{status}",
                inline=False,
            )
        embed.set_footer(text=f"Page {self.page + 1} of {self.total_pages}")
        return embed

    @discord.ui.button(label="◄ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next ►", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class RSS(commands.Cog):
    """Manage RSS feeds that get announced through Discord webhooks."""

    rss = app_commands.Group(
        name="rss",
        description="Manage RSS feed subscriptions",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT})
        self.poller.start()
        self.calibrator.start()

    async def cog_unload(self):
        self.poller.cancel()
        self.calibrator.cancel()
        if self.session:
            await self.session.close()

    # ------------------------------------------------------------------ fetch

    async def fetch_feed(self, url: str, etag: str | None = None,
                         last_modified: str | None = None):
        """Conditional GET. Returns (parsed, etag, last_modified) or
        (None, ..) when the feed is unchanged (HTTP 304)."""
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        async with self.session.get(url, headers=headers) as resp:
            if resp.status == 304:
                return None, etag, last_modified
            resp.raise_for_status()
            body = await resp.read()
            new_etag = resp.headers.get("ETag")
            new_lm = resp.headers.get("Last-Modified")
        # feedparser is blocking; keep it off the event loop.
        parsed = await asyncio.get_running_loop().run_in_executor(
            None, feedparser.parse, body)
        return parsed, new_etag, new_lm

    # ------------------------------------------------------------------ poller

    @tasks.loop(seconds=60)
    async def poller(self):
        started = time.time()
        all_feeds = await db.due_feeds(started)
        # Filter out feeds that are still in backoff.
        feeds = [f for f in all_feeds if self._is_due(f, started)]
        log.debug("[poller] cycle_start feeds_due=%d in_backoff=%d",
                  len(feeds), len(all_feeds) - len(feeds))
        for feed in feeds:
            try:
                await self.poll_feed(feed)
            except Exception as exc:
                log.exception("[poller] feed_id=%s status=error", feed["id"])
                await db.update_poll_meta(
                    feed["id"], feed["etag"], feed["last_modified"], time.time())
                fail_count = await db.record_poll_failure(feed["id"], str(exc))
                await self._maybe_alert_failure(feed, fail_count)
            await asyncio.sleep(FETCH_SPACING)
        log.debug("[poller] cycle_end elapsed=%.1fs", time.time() - started)

    @poller.before_loop
    async def before_poller(self):
        await self.bot.wait_until_ready()

    # -------------------------------------------------------------- calibration

    @tasks.loop(time=CALIBRATION_TIME)
    async def calibrator(self):
        """Daily at 00:01 GMT+7: re-poll every feed across all guilds.

        Same effect as `/rss reset`, applied globally — clears each feed's
        polling cursors so the next cycle re-fetches fresh, while keeping
        seen_entries and fail_count so only genuinely new items are announced.
        """
        count = await db.reset_all_feeds()
        log.info("[calibration] reset last-poll for %d feed(s) across all guilds", count)

    @calibrator.before_loop
    async def before_calibrator(self):
        await self.bot.wait_until_ready()

    def _is_due(self, feed, now: float) -> bool:
        """True if the feed's backoff interval has elapsed since last_polled."""
        fail_count = feed["fail_count"] or 0
        if fail_count == 0:
            return True
        effective = min(feed["interval_seconds"] * (2 ** fail_count), MAX_BACKOFF)
        return now >= feed["last_polled"] + effective

    async def _maybe_alert_failure(self, feed, fail_count: int) -> None:
        """Send a warning embed to the feed's webhook at failure #4 and each doubling."""
        if fail_count < 4:
            return
        # Fire at 4, 8, 16, 32, … (powers of 2 from 4 upward)
        if fail_count != 4 and (fail_count & (fail_count - 1)) != 0:
            return
        backoff_s = min(feed["interval_seconds"] * (2 ** fail_count), MAX_BACKOFF)
        embed = discord.Embed(
            title="⚠️ Feed polling failed",
            description=(
                f"**{feed['name']}** has failed {fail_count} consecutive poll(s).\n"
                f"URL: {feed['url']}\n"
                f"Last error: `{feed['last_error'] or 'unknown'}`"
            ),
            color=ERROR_COLOR,
        )
        embed.set_footer(text=f"Will retry. Next attempt in ~{backoff_s // 60}m.")
        try:
            webhook = discord.Webhook.from_url(feed["webhook_url"], session=self.session)
            await webhook.send(embed=embed, username=feed["name"][:80],
                               avatar_url=feed["icon_url"] or None)
        except Exception:
            log.warning("Could not send failure alert for feed %s", feed["id"])

    async def poll_feed(self, feed):
        parsed, etag, last_modified = await self.fetch_feed(
            feed["url"], feed["etag"], feed["last_modified"])
        await db.update_poll_meta(feed["id"], etag, last_modified, time.time())
        if parsed is None:  # 304 Not Modified
            recovered = await db.record_poll_success(feed["id"])
            if recovered:
                await self._send_recovery_notice(feed)
            log.info("[poller] feed_id=%s status=skipped entries_new=0", feed["id"])
            return

        seen = await db.seen_keys(feed["id"])
        entries = adapters.adapt_entries(feed["feed_type"], parsed)
        new_entries = [e for e in entries if entry_key(e) not in seen]

        # Record success before announcing so a webhook failure doesn't re-trigger backoff.
        recovered = await db.record_poll_success(feed["id"])
        if recovered:
            await self._send_recovery_notice(feed)

        if not new_entries:
            log.info("[poller] feed_id=%s status=ok entries_new=0", feed["id"])
            return

        # Record everything as seen, but only announce the newest few,
        # oldest first so the channel reads chronologically.
        await db.add_seen(feed["id"], [entry_key(e) for e in new_entries])
        to_send = list(reversed(new_entries[:MAX_ITEMS_PER_POLL]))

        webhook = discord.Webhook.from_url(feed["webhook_url"], session=self.session)
        for entry in to_send:
            log.info("[webhook] feed_id=%s name=%r title=%r",
                     feed["id"], feed["name"], entry.get("title", "Untitled"))
            await webhook.send(
                embed=build_embed(feed["name"], feed["url"], entry),
                username=feed["name"][:80],
                avatar_url=feed["icon_url"] or None,
            )
            await asyncio.sleep(SEND_SPACING)
        log.info("[poller] feed_id=%s status=ok entries_new=%d",
                 feed["id"], len(to_send))

    async def _send_recovery_notice(self, feed) -> None:
        embed = discord.Embed(
            title="✅ Feed recovered",
            description=f"**{feed['name']}** is polling successfully again.",
            color=RSS_COLOR,
        )
        try:
            webhook = discord.Webhook.from_url(feed["webhook_url"], session=self.session)
            await webhook.send(embed=embed, username=feed["name"][:80],
                               avatar_url=feed["icon_url"] or None)
        except Exception:
            log.warning("Could not send recovery notice for feed %s", feed["id"])

    # ---------------------------------------------------------------- commands

    @rss.command(name="add")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        feed_url="The RSS/Atom feed URL to poll",
        webhook_url="Discord webhook URL where new entries are announced",
        interval=f"Polling interval in seconds (min {MIN_INTERVAL})",
        feed_type="Feed type — picks the adapter used to parse entries",
    )
    @app_commands.choices(feed_type=[
        app_commands.Choice(name=t, value=t) for t in adapters.FEED_TYPES])
    async def add(self, interaction: discord.Interaction,
                  feed_url: str, webhook_url: str,
                  interval: int = DEFAULT_INTERVAL,
                  feed_type: str = adapters.GENERIC):
        """Start polling a feed and announce new items via the webhook."""
        # Defer ephemerally so the webhook URL stays private and we beat the 3s deadline.
        await interaction.response.defer(ephemeral=True)

        if not WEBHOOK_RE.match(webhook_url):
            return await interaction.followup.send(
                "❌ That doesn't look like a Discord webhook URL."
            )
        interval = max(interval, MIN_INTERVAL)

        try:
            parsed, etag, last_modified = await self.fetch_feed(feed_url)
        except Exception as exc:
            return await interaction.followup.send(f"❌ Couldn't fetch the feed: `{exc}`")
        if parsed is None or (parsed.bozo and not parsed.entries):
            return await interaction.followup.send(
                "❌ That URL doesn't look like a valid RSS/Atom feed."
            )

        name = parsed.feed.get("title") or feed_url
        icon_url = extract_icon_url(parsed)
        entries = adapters.adapt_entries(feed_type, parsed)
        feed_id = await db.add_feed(
            interaction.guild_id, feed_url, name, webhook_url, interval,
            interaction.user.id, feed_type, icon_url)
        if feed_id is None:
            return await interaction.followup.send(
                "❌ That feed is already registered in this server."
            )

        await db.add_seen(feed_id, [entry_key(e) for e in entries])
        await db.update_poll_meta(feed_id, etag, last_modified, time.time())

        if entries:
            webhook = discord.Webhook.from_url(webhook_url, session=self.session)
            try:
                log.info("[webhook] feed_id=%s name=%r title=%r (preview)",
                         feed_id, name, entries[0].get("title", "Untitled"))
                await webhook.send(
                    embed=build_embed(name, feed_url, entries[0]),
                    username=name[:80],
                    avatar_url=icon_url,
                )
            except discord.HTTPException:
                await db.remove_feed(feed_id)
                return await interaction.followup.send(
                    "❌ The webhook rejected a test message, so the feed was not "
                    "added. Check that the webhook still exists.")

        log.info("Feed %s added: %r (%s) type=%s interval=%ss in guild %s by user %s",
                 feed_id, name, feed_url, feed_type, interval,
                 interaction.guild_id, interaction.user.id)
        embed = discord.Embed(title="✅ Feed added", color=RSS_COLOR)
        if parsed.entries:
            embed.description = "The newest item was posted via the webhook as a preview."
        embed.add_field(name="Name", value=name, inline=False)
        embed.add_field(name="URL", value=feed_url, inline=False)
        embed.add_field(name="Interval", value=f"{interval}s")
        embed.add_field(name="Type", value=feed_type)
        embed.add_field(name="ID", value=str(feed_id))
        await interaction.followup.send(embed=embed)

    @rss.command(name="remove")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(ref="Feed id or URL")
    async def remove(self, interaction: discord.Interaction, ref: str):
        """Remove a feed by id or URL."""
        feed = await db.get_feed(interaction.guild_id, ref)
        if feed is None:
            return await interaction.response.send_message(
                "❌ No feed with that id/URL in this server.", ephemeral=True)
        await db.remove_feed(feed["id"])
        log.info("Feed %s removed: %r (%s) in guild %s by user %s",
                 feed["id"], feed["name"], feed["url"],
                 interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(
            f"🗑️ Removed **{feed['name']}** (`{feed['url']}`).")

    @rss.command(name="list")
    async def list_(self, interaction: discord.Interaction):
        """List feeds registered in this server."""
        feeds = await db.list_feeds(interaction.guild_id)
        if not feeds:
            return await interaction.response.send_message(
                "No feeds yet. Add one with `/rss add`.", ephemeral=True)
        view = FeedListView(feeds, interaction.guild.name)
        await interaction.response.send_message(embed=view.build_embed(), view=view)

    @rss.command(name="edit")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        ref="Feed id or URL",
        name="New display name",
        webhook="New Discord webhook URL",
        interval=f"New polling interval in seconds (min {MIN_INTERVAL})",
        feed_type="New feed adapter type",
    )
    @app_commands.choices(feed_type=[
        app_commands.Choice(name=t, value=t) for t in adapters.FEED_TYPES])
    async def edit(self, interaction: discord.Interaction, ref: str,
                   name: str | None = None,
                   webhook: str | None = None,
                   interval: int | None = None,
                   feed_type: str | None = None):
        """Edit a feed's name, webhook, interval, or type without re-adding it."""
        # Defer ephemerally when a webhook URL is being set to keep it private.
        if webhook:
            await interaction.response.defer(ephemeral=True)
            send = interaction.followup.send
        else:
            await interaction.response.defer()
            send = interaction.followup.send

        feed = await db.get_feed(interaction.guild_id, ref)
        if feed is None:
            return await send("❌ No feed with that id/URL in this server.")

        if webhook and not WEBHOOK_RE.match(webhook):
            return await send("❌ That doesn't look like a Discord webhook URL.")
        if interval is not None:
            interval = max(interval, MIN_INTERVAL)

        new_icon_url: str | None = None
        if feed_type is not None and feed_type != feed["feed_type"]:
            try:
                parsed, etag, last_modified = await self.fetch_feed(feed["url"])
            except Exception as exc:
                return await send(f"❌ Couldn't fetch the feed to verify the new type: `{exc}`")
            if parsed is None or (parsed.bozo and not parsed.entries):
                return await send("❌ Feed returned no entries for the new adapter.")
            new_icon_url = extract_icon_url(parsed)
            entries = adapters.adapt_entries(feed_type, parsed)
            effective_webhook = webhook or feed["webhook_url"]
            if entries:
                wh = discord.Webhook.from_url(effective_webhook, session=self.session)
                try:
                    await wh.send(
                        embed=build_embed(feed["name"], feed["url"], entries[0]),
                        username=(name or feed["name"])[:80],
                        avatar_url=new_icon_url or feed["icon_url"],
                    )
                except discord.HTTPException as exc:
                    return await send(
                        f"❌ Webhook rejected the preview: `{exc}` — type not changed.")
            await db.update_poll_meta(feed["id"], etag, last_modified, time.time())

        await db.update_feed(
            feed["id"],
            name=name,
            webhook_url=webhook,
            feed_type=feed_type,
            interval_seconds=interval,
            icon_url=new_icon_url,
        )
        log.info("Feed %s edited in guild %s by user %s",
                 feed["id"], interaction.guild_id, interaction.user.id)

        embed = discord.Embed(title="✅ Feed updated", color=RSS_COLOR)
        embed.add_field(name="Name", value=name or feed["name"], inline=False)
        if interval is not None:
            embed.add_field(name="Interval", value=f"{interval}s")
        if feed_type is not None:
            embed.add_field(name="Type", value=feed_type)
        if webhook:
            embed.add_field(name="Webhook", value="Updated")
        await send(embed=embed)

    @rss.command(name="status")
    @app_commands.default_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        """Show feeds with consecutive polling failures."""
        feeds = await db.unhealthy_feeds(interaction.guild_id)

        if not feeds:
            embed = discord.Embed(
                title="✅ All feeds healthy",
                description="No feeds with consecutive failures.",
                color=RSS_COLOR,
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        embed = discord.Embed(
            title=f"⚠️ Unhealthy feeds in {interaction.guild.name}",
            color=ERROR_COLOR,
        )
        for f in feeds[:25]:
            fail_count = f["fail_count"]
            backoff_s = min(f["interval_seconds"] * (2 ** fail_count), MAX_BACKOFF)
            embed.add_field(
                name=f"#{f['id']} — {f['name'][:180]} ({fail_count} failure(s))",
                value=f"URL: {f['url']}\n"
                      f"Last error: `{(f['last_error'] or 'unknown')[:200]}`\n"
                      f"Next retry in ~{backoff_s // 60}m",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rss.command(name="interval")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(ref="Feed id or URL",
                           seconds=f"New polling interval in seconds (min {MIN_INTERVAL})")
    async def interval(self, interaction: discord.Interaction, ref: str, seconds: int):
        """Change a feed's polling interval."""
        feed = await db.get_feed(interaction.guild_id, ref)
        if feed is None:
            return await interaction.response.send_message(
                "❌ No feed with that id/URL in this server.", ephemeral=True)
        seconds = max(seconds, MIN_INTERVAL)
        await db.set_interval(feed["id"], seconds)
        await interaction.response.send_message(
            f"⏱️ **{feed['name']}** now polls every {seconds}s.")

    @rss.command(name="reset")
    @app_commands.default_permissions(manage_guild=True)
    async def reset(self, interaction: discord.Interaction):
        """Reset every feed's last-poll time so they all re-poll on the next cycle."""
        count = await db.reset_feeds(interaction.guild_id)
        if count == 0:
            return await interaction.response.send_message(
                "No feeds to reset. Add one with `/rss add`.", ephemeral=True)
        log.info("Reset %d feed(s) in guild %s by user %s",
                 count, interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(
            f"🔄 Reset last-poll time for **{count}** feed(s). "
            "They'll be re-polled on the next cycle (new items only).")

    @rss.command(name="poll")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(ref="Feed id or URL")
    async def poll(self, interaction: discord.Interaction, ref: str):
        """Force-fetch a feed and push its latest entry to the webhook immediately."""
        feed = await db.get_feed(interaction.guild_id, ref)
        if feed is None:
            return await interaction.response.send_message(
                "❌ No feed with that id/URL in this server.", ephemeral=True)

        await interaction.response.defer()

        try:
            parsed, etag, last_modified = await self.fetch_feed(feed["url"])
        except Exception as exc:
            return await interaction.followup.send(f"❌ Couldn't fetch the feed: `{exc}`")
        if parsed is None or not parsed.entries:
            return await interaction.followup.send(f"⚠️ **{feed['name']}** returned no entries.")

        await db.update_poll_meta(feed["id"], etag, last_modified, time.time())

        entries = adapters.adapt_entries(feed["feed_type"], parsed)
        if not entries:
            return await interaction.followup.send(f"⚠️ **{feed['name']}** returned no entries.")

        webhook = discord.Webhook.from_url(feed["webhook_url"], session=self.session)
        try:
            log.info("[webhook] feed_id=%s name=%r title=%r (forced)",
                     feed["id"], feed["name"], entries[0].get("title", "Untitled"))
            await webhook.send(
                embed=build_embed(feed["name"], feed["url"], entries[0]),
                username=feed["name"][:80],
                avatar_url=feed["icon_url"] or None,
            )
        except discord.HTTPException as exc:
            return await interaction.followup.send(f"❌ Webhook rejected the message: `{exc}`")

        await interaction.followup.send(f"🔄 **{feed['name']}** — latest entry pushed to webhook.")

    # ------------------------------------------------------------------ errors

    async def cog_app_command_error(self, interaction: discord.Interaction,
                                    error: app_commands.AppCommandError):
        msg = None
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ You need the **Manage Server** permission for that."
        elif isinstance(error, app_commands.NoPrivateMessage):
            msg = "❌ RSS commands only work in a server."
        else:
            raise error
        if msg:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RSS(bot))
