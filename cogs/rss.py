"""RSS feed management commands and the background poller."""
import asyncio
import hashlib
import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
import feedparser
from discord import app_commands
from discord.ext import commands, tasks

import adapters
import db

log = logging.getLogger("rss")

MIN_INTERVAL = 120          # floor for per-feed polling interval (seconds)
DEFAULT_INTERVAL = 14400    # 4 hours
MAX_ITEMS_PER_POLL = 5      # cap announcements per feed per cycle
FETCH_SPACING = 1.0         # pause between feed fetches within one cycle
SEND_SPACING = 1.0          # pause between webhook sends
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=20)
USER_AGENT = "rss-feed-discord-bot/1.0 (+https://github.com/)"
RSS_COLOR = 0xEE802F
ERROR_COLOR = 0xE74C3C
TAG_RE = re.compile(r"<[^>]+>")
WEBHOOK_RE = re.compile(r"^https://(canary\.|ptb\.)?discord(app)?\.com/api/webhooks/\d+/\S+$")
MAX_BACKOFF = 3600          # cap backoff at 1 hour
PAGE_SIZE = 10              # feeds per page in rss list


class FeedTypeConverter(commands.Converter[str]):
    """Feed type argument for prefix commands. Raising on unknown tokens lets
    an Optional[FeedTypeConverter] parameter backtrack: when the first word
    isn't a feed type, discord.py passes None and re-reads the token as the
    next parameter — so the type can be omitted in `rss add`."""

    async def convert(self, ctx: commands.Context, argument: str) -> str:
        argument = argument.lower()
        if argument not in adapters.FEED_TYPES:
            raise commands.BadArgument(
                f"Unknown feed type `{argument}` "
                f"(available: {', '.join(adapters.FEED_TYPES)})")
        return argument


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

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT})
        self.poller.start()

    async def cog_unload(self):
        self.poller.cancel()
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
        log.info("Poll cycle started: %d feed(s) due (%d in backoff)",
                 len(feeds), len(all_feeds) - len(feeds))
        for feed in feeds:
            try:
                await self.poll_feed(feed)
            except Exception as exc:
                log.exception("Polling failed for feed %s (%s)", feed["id"], feed["url"])
                await db.update_poll_meta(
                    feed["id"], feed["etag"], feed["last_modified"], time.time())
                fail_count = await db.record_poll_failure(feed["id"], str(exc))
                await self._maybe_alert_failure(feed, fail_count)
            await asyncio.sleep(FETCH_SPACING)
        log.info("Poll cycle finished in %.1fs", time.time() - started)

    @poller.before_loop
    async def before_poller(self):
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
            await webhook.send(embed=embed, username=feed["name"][:80])
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
            return

        seen = await db.seen_keys(feed["id"])
        entries = adapters.adapt_entries(feed["feed_type"], parsed)
        new_entries = [e for e in entries if entry_key(e) not in seen]

        # Record success before announcing so a webhook failure doesn't re-trigger backoff.
        recovered = await db.record_poll_success(feed["id"])
        if recovered:
            await self._send_recovery_notice(feed)

        if not new_entries:
            return

        # Record everything as seen, but only announce the newest few,
        # oldest first so the channel reads chronologically.
        await db.add_seen(feed["id"], [entry_key(e) for e in new_entries])
        to_send = list(reversed(new_entries[:MAX_ITEMS_PER_POLL]))

        webhook = discord.Webhook.from_url(feed["webhook_url"], session=self.session)
        for entry in to_send:
            log.info("Webhook send: feed %s (%s) entry %r",
                     feed["id"], feed["name"], entry_key(entry))
            await webhook.send(
                embed=build_embed(feed["name"], feed["url"], entry),
                username=feed["name"][:80],
            )
            await asyncio.sleep(SEND_SPACING)
        log.info("Feed %s (%s): announced %d new item(s)",
                 feed["id"], feed["name"], len(to_send))

    async def _send_recovery_notice(self, feed) -> None:
        embed = discord.Embed(
            title="✅ Feed recovered",
            description=f"**{feed['name']}** is polling successfully again.",
            color=RSS_COLOR,
        )
        try:
            webhook = discord.Webhook.from_url(feed["webhook_url"], session=self.session)
            await webhook.send(embed=embed, username=feed["name"][:80])
        except Exception:
            log.warning("Could not send recovery notice for feed %s", feed["id"])

    # ---------------------------------------------------------------- commands

    @commands.hybrid_group(invoke_without_command=True, fallback="help")
    @commands.guild_only()
    async def rss(self, ctx: commands.Context):
        """Show RSS command help."""
        prefix = ctx.clean_prefix
        embed = discord.Embed(title="RSS commands", color=RSS_COLOR)
        embed.add_field(
            name=f"{prefix}rss add [type] <feed_url> <webhook_url> [interval_seconds]",
            value="Start polling a feed and announce new items via the webhook. "
                  f"Types: {', '.join(adapters.FEED_TYPES)}.",
            inline=False)
        embed.add_field(name=f"{prefix}rss remove <id|url>",
                        value="Stop polling a feed.", inline=False)
        embed.add_field(name=f"{prefix}rss list",
                        value="List feeds registered in this server.", inline=False)
        embed.add_field(name=f"{prefix}rss interval <id|url> <seconds>",
                        value=f"Change the polling interval (min {MIN_INTERVAL}s).",
                        inline=False)
        embed.add_field(name=f"{prefix}rss poll <id|url>",
                        value="Queue a feed to be polled on the next cycle.", inline=False)
        embed.add_field(
            name=f"{prefix}rss edit <id|url> [name] [webhook] [interval] [type]",
            value="Edit a feed's name, webhook, interval, or type without re-adding it.",
            inline=False)
        embed.add_field(name=f"{prefix}rss status",
                        value="Show feeds with consecutive polling failures.", inline=False)
        await ctx.send(embed=embed)

    @rss.command()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(
        feed_type="Feed type — picks the adapter used to parse entries",
        feed_url="The RSS/Atom feed URL to poll",
        webhook_url="Discord webhook URL where new entries are announced",
        interval=f"Polling interval in seconds (min {MIN_INTERVAL})",
    )
    @app_commands.choices(feed_type=[
        app_commands.Choice(name=t, value=t) for t in adapters.FEED_TYPES])
    async def add(self, ctx: commands.Context,
                  feed_type: Optional[FeedTypeConverter], feed_url: str,
                  webhook_url: str, interval: int = DEFAULT_INTERVAL):
        """Start polling a feed and announce new items via the webhook."""
        if ctx.interaction:
            # Slash invocation: options aren't shown in the channel, so the
            # webhook URL stays private — just reply ephemerally. Deferring
            # also buys time past the 3s interaction deadline for the fetch.
            await ctx.defer(ephemeral=True)
        else:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                await ctx.send("⚠️ Couldn't delete your message — consider deleting it "
                               "yourself, it contains the webhook URL.")

        if not WEBHOOK_RE.match(webhook_url):
            return await ctx.send("❌ That doesn't look like a Discord webhook URL.")
        interval = max(interval, MIN_INTERVAL)
        # The converter (prefix) and choices (slash) both constrain the value;
        # None means the type was omitted.
        feed_type = feed_type or adapters.GENERIC

        # Validate the feed before saving it.
        try:
            parsed, etag, last_modified = await self.fetch_feed(feed_url)
        except Exception as exc:
            return await ctx.send(f"❌ Couldn't fetch the feed: `{exc}`")
        if parsed is None or (parsed.bozo and not parsed.entries):
            return await ctx.send("❌ That URL doesn't look like a valid RSS/Atom feed.")

        name = parsed.feed.get("title") or feed_url
        entries = adapters.adapt_entries(feed_type, parsed)
        feed_id = await db.add_feed(
            ctx.guild.id, feed_url, name, webhook_url, interval, ctx.author.id,
            feed_type)
        if feed_id is None:
            return await ctx.send("❌ That feed is already registered in this server.")

        # Seed seen entries so adding a feed doesn't flood the channel.
        await db.add_seen(feed_id, [entry_key(e) for e in entries])
        await db.update_poll_meta(feed_id, etag, last_modified, time.time())

        # Push the newest entry through the webhook as immediate feedback;
        # this also proves the webhook actually accepts messages.
        if entries:
            webhook = discord.Webhook.from_url(webhook_url, session=self.session)
            try:
                log.info("Webhook send: preview for new feed %r (%s)", name, feed_url)
                await webhook.send(
                    embed=build_embed(name, feed_url, entries[0]),
                    username=name[:80],
                )
            except discord.HTTPException:
                await db.remove_feed(feed_id)
                return await ctx.send(
                    "❌ The webhook rejected a test message, so the feed was not "
                    "added. Check that the webhook still exists.")

        log.info("Feed %s added: %r (%s) type=%s interval=%ss in guild %s by user %s",
                 feed_id, name, feed_url, feed_type, interval,
                 ctx.guild.id, ctx.author.id)
        embed = discord.Embed(title="✅ Feed added", color=RSS_COLOR)
        if parsed.entries:
            embed.description = "The newest item was posted via the webhook as a preview."
        embed.add_field(name="Name", value=name, inline=False)
        embed.add_field(name="URL", value=feed_url, inline=False)
        embed.add_field(name="Interval", value=f"{interval}s")
        embed.add_field(name="Type", value=feed_type)
        embed.add_field(name="ID", value=str(feed_id))
        await ctx.send(embed=embed)

    @rss.command()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(ref="Feed id or URL")
    async def remove(self, ctx: commands.Context, *, ref: str):
        """Remove a feed by id or URL."""
        feed = await db.get_feed(ctx.guild.id, ref)
        if feed is None:
            return await ctx.send("❌ No feed with that id/URL in this server.")
        await db.remove_feed(feed["id"])
        log.info("Feed %s removed: %r (%s) in guild %s by user %s",
                 feed["id"], feed["name"], feed["url"],
                 ctx.guild.id, ctx.author.id)
        await ctx.send(f"🗑️ Removed **{feed['name']}** (`{feed['url']}`).")        

    @rss.command(name="list")
    async def list_(self, ctx: commands.Context):
        """List feeds registered in this server."""
        feeds = await db.list_feeds(ctx.guild.id)
        if not feeds:
            return await ctx.send(
                f"No feeds yet. Add one with `{ctx.clean_prefix}rss add`.")
        view = FeedListView(feeds, ctx.guild.name)
        await ctx.send(embed=view.build_embed(), view=view)

    @rss.command()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(
        ref="Feed id or URL",
        name="New display name",
        webhook="New Discord webhook URL",
        interval=f"New polling interval in seconds (min {MIN_INTERVAL})",
        feed_type="New feed adapter type",
    )
    @app_commands.choices(feed_type=[
        app_commands.Choice(name=t, value=t) for t in adapters.FEED_TYPES])
    async def edit(self, ctx: commands.Context, ref: str,
                   name: str | None = None,
                   webhook: str | None = None,
                   interval: int | None = None,
                   feed_type: Optional[FeedTypeConverter] = None):
        """Edit a feed's name, webhook, interval, or type without re-adding it."""
        # Slash path: defer ephemerally if a new webhook URL is being set.
        if ctx.interaction and webhook:
            await ctx.defer(ephemeral=True)
        elif not ctx.interaction and webhook:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                await ctx.send("⚠️ Couldn't delete your message — it contains a webhook URL.")

        feed = await db.get_feed(ctx.guild.id, ref)
        if feed is None:
            return await ctx.send("❌ No feed with that id/URL in this server.")

        if webhook and not WEBHOOK_RE.match(webhook):
            return await ctx.send("❌ That doesn't look like a Discord webhook URL.")
        if interval is not None:
            interval = max(interval, MIN_INTERVAL)

        # When feed_type changes, re-fetch and send a preview through the new adapter.
        if feed_type is not None and feed_type != feed["feed_type"]:
            try:
                parsed, etag, last_modified = await self.fetch_feed(feed["url"])
            except Exception as exc:
                return await ctx.send(f"❌ Couldn't fetch the feed to verify the new type: `{exc}`")
            if parsed is None or (parsed.bozo and not parsed.entries):
                return await ctx.send("❌ Feed returned no entries for the new adapter.")
            entries = adapters.adapt_entries(feed_type, parsed)
            effective_webhook = webhook or feed["webhook_url"]
            if entries:
                wh = discord.Webhook.from_url(effective_webhook, session=self.session)
                try:
                    await wh.send(
                        embed=build_embed(feed["name"], feed["url"], entries[0]),
                        username=(name or feed["name"])[:80],
                    )
                except discord.HTTPException as exc:
                    return await ctx.send(
                        f"❌ Webhook rejected the preview: `{exc}` — type not changed.")
            await db.update_poll_meta(feed["id"], etag, last_modified, time.time())

        await db.update_feed(
            feed["id"],
            name=name,
            webhook_url=webhook,
            feed_type=feed_type,
            interval_seconds=interval,
        )
        log.info("Feed %s edited in guild %s by user %s", feed["id"], ctx.guild.id, ctx.author.id)

        embed = discord.Embed(title="✅ Feed updated", color=RSS_COLOR)
        embed.add_field(name="Name", value=name or feed["name"], inline=False)
        if interval is not None:
            embed.add_field(name="Interval", value=f"{interval}s")
        if feed_type is not None:
            embed.add_field(name="Type", value=feed_type)
        if webhook:
            embed.add_field(name="Webhook", value="Updated")
        await ctx.send(embed=embed)

    @rss.command()
    @commands.has_guild_permissions(manage_guild=True)
    async def status(self, ctx: commands.Context):
        """Show feeds with consecutive polling failures."""
        feeds = await db.unhealthy_feeds(ctx.guild.id)

        if not feeds:
            embed = discord.Embed(
                title="✅ All feeds healthy",
                description="No feeds with consecutive failures.",
                color=RSS_COLOR,
            )
            if ctx.interaction:
                return await ctx.send(embed=embed, ephemeral=True)
            return await ctx.send(embed=embed)

        embed = discord.Embed(
            title=f"⚠️ Unhealthy feeds in {ctx.guild.name}",
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

        if ctx.interaction:
            return await ctx.send(embed=embed, ephemeral=True)

        # Prefix path: delete the invoking message then DM the result.
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        try:
            await ctx.author.send(embed=embed)
        except discord.HTTPException:
            await ctx.send(
                "⚠️ Couldn't DM you the status report. "
                "Use the `/rss status` slash command for an ephemeral reply."
            )

    @rss.command()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(ref="Feed id or URL",
                           seconds=f"New polling interval in seconds (min {MIN_INTERVAL})")
    async def interval(self, ctx: commands.Context, ref: str, seconds: int):
        """Change a feed's polling interval."""
        feed = await db.get_feed(ctx.guild.id, ref)
        if feed is None:
            return await ctx.send("❌ No feed with that id/URL in this server.")
        seconds = max(seconds, MIN_INTERVAL)
        await db.set_interval(feed["id"], seconds)
        await ctx.send(f"⏱️ **{feed['name']}** now polls every {seconds}s.")

    @rss.command()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.describe(ref="Feed id or URL")
    async def poll(self, ctx: commands.Context, *, ref: str):
        """Force-fetch a feed and push its latest entry to the webhook immediately."""
        feed = await db.get_feed(ctx.guild.id, ref)
        if feed is None:
            return await ctx.send("❌ No feed with that id/URL in this server.")

        try:
            # Unconditional fetch — skip etag/last_modified so we always get entries back.
            parsed, etag, last_modified = await self.fetch_feed(feed["url"])
        except Exception as exc:
            return await ctx.send(f"❌ Couldn't fetch the feed: `{exc}`")
        if parsed is None or not parsed.entries:
            return await ctx.send(f"⚠️ **{feed['name']}** returned no entries.")

        await db.update_poll_meta(feed["id"], etag, last_modified, time.time())

        entries = adapters.adapt_entries(feed["feed_type"], parsed)
        if not entries:
            return await ctx.send(f"⚠️ **{feed['name']}** returned no entries.")

        webhook = discord.Webhook.from_url(feed["webhook_url"], session=self.session)
        try:
            log.info("Webhook send: forced poll preview for feed %s (%s)", feed["id"], feed["name"])
            await webhook.send(
                embed=build_embed(feed["name"], feed["url"], entries[0]),
                username=feed["name"][:80],
            )
        except discord.HTTPException as exc:
            return await ctx.send(f"❌ Webhook rejected the message: `{exc}`")

        await ctx.send(f"🔄 **{feed['name']}** — latest entry pushed to webhook.")

    # ------------------------------------------------------------------ errors

    async def cog_command_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You need the **Manage Server** permission for that.")
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.send(f"❌ {error} — see `{ctx.clean_prefix}rss` for usage.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ RSS commands only work in a server.")
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(RSS(bot))
