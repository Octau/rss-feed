"""RSS feed management commands and the background poller."""
import asyncio
import hashlib
import html
import logging
import re
import time
from datetime import datetime, timezone

import aiohttp
import discord
import feedparser
from discord.ext import commands, tasks

import db

log = logging.getLogger("rss")

MIN_INTERVAL = 120          # floor for per-feed polling interval (seconds)
DEFAULT_INTERVAL = 300
MAX_ITEMS_PER_POLL = 5      # cap announcements per feed per cycle
FETCH_SPACING = 1.0         # pause between feed fetches within one cycle
SEND_SPACING = 1.0          # pause between webhook sends
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=20)
USER_AGENT = "rss-feed-discord-bot/1.0 (+https://github.com/)"
RSS_COLOR = 0xEE802F
TAG_RE = re.compile(r"<[^>]+>")
WEBHOOK_RE = re.compile(r"^https://(canary\.|ptb\.)?discord(app)?\.com/api/webhooks/\d+/\S+$")


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
        feeds = await db.due_feeds(time.time())
        for feed in feeds:
            try:
                await self.poll_feed(feed)
            except Exception:
                log.exception("Polling failed for feed %s (%s)", feed["id"], feed["url"])
                # Still bump last_polled so a broken feed can't hot-loop.
                await db.update_poll_meta(
                    feed["id"], feed["etag"], feed["last_modified"], time.time())
            await asyncio.sleep(FETCH_SPACING)

    @poller.before_loop
    async def before_poller(self):
        await self.bot.wait_until_ready()

    async def poll_feed(self, feed):
        parsed, etag, last_modified = await self.fetch_feed(
            feed["url"], feed["etag"], feed["last_modified"])
        await db.update_poll_meta(feed["id"], etag, last_modified, time.time())
        if parsed is None:  # 304 Not Modified
            return

        seen = await db.seen_keys(feed["id"])
        new_entries = [e for e in parsed.entries if entry_key(e) not in seen]
        if not new_entries:
            return

        # Record everything as seen, but only announce the newest few,
        # oldest first so the channel reads chronologically.
        await db.add_seen(feed["id"], [entry_key(e) for e in new_entries])
        to_send = list(reversed(new_entries[:MAX_ITEMS_PER_POLL]))

        webhook = discord.Webhook.from_url(feed["webhook_url"], session=self.session)
        for entry in to_send:
            await webhook.send(
                embed=build_embed(feed["name"], feed["url"], entry),
                username=feed["name"][:80],
            )
            await asyncio.sleep(SEND_SPACING)
        log.info("Feed %s (%s): announced %d new item(s)",
                 feed["id"], feed["name"], len(to_send))

    # ---------------------------------------------------------------- commands

    @commands.group(invoke_without_command=True)
    @commands.guild_only()
    async def rss(self, ctx: commands.Context):
        """Show RSS command help."""
        prefix = ctx.clean_prefix
        embed = discord.Embed(title="RSS commands", color=RSS_COLOR)
        embed.add_field(
            name=f"{prefix}rss add <feed_url> <webhook_url> [interval_seconds]",
            value="Start polling a feed and announce new items via the webhook.",
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
        await ctx.send(embed=embed)

    @rss.command()
    @commands.has_guild_permissions(manage_guild=True)
    async def add(self, ctx: commands.Context, feed_url: str, webhook_url: str,
                  interval: int = DEFAULT_INTERVAL):
        """Register a feed. The invoking message is deleted to hide the webhook URL."""
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            await ctx.send("⚠️ Couldn't delete your message — consider deleting it "
                           "yourself, it contains the webhook URL.")

        if not WEBHOOK_RE.match(webhook_url):
            return await ctx.send("❌ That doesn't look like a Discord webhook URL.")
        interval = max(interval, MIN_INTERVAL)

        # Validate the feed before saving it.
        try:
            parsed, etag, last_modified = await self.fetch_feed(feed_url)
        except Exception as exc:
            return await ctx.send(f"❌ Couldn't fetch the feed: `{exc}`")
        if parsed is None or (parsed.bozo and not parsed.entries):
            return await ctx.send("❌ That URL doesn't look like a valid RSS/Atom feed.")

        name = parsed.feed.get("title") or feed_url
        feed_id = await db.add_feed(
            ctx.guild.id, feed_url, name, webhook_url, interval, ctx.author.id)
        if feed_id is None:
            return await ctx.send("❌ That feed is already registered in this server.")

        # Seed seen entries so adding a feed doesn't flood the channel.
        await db.add_seen(feed_id, [entry_key(e) for e in parsed.entries])
        await db.update_poll_meta(feed_id, etag, last_modified, time.time())

        embed = discord.Embed(title="✅ Feed added", color=RSS_COLOR)
        embed.add_field(name="Name", value=name, inline=False)
        embed.add_field(name="URL", value=feed_url, inline=False)
        embed.add_field(name="Interval", value=f"{interval}s")
        embed.add_field(name="ID", value=str(feed_id))
        await ctx.send(embed=embed)

    @rss.command()
    @commands.has_guild_permissions(manage_guild=True)
    async def remove(self, ctx: commands.Context, *, ref: str):
        """Remove a feed by id or URL."""
        feed = await db.get_feed(ctx.guild.id, ref)
        if feed is None:
            return await ctx.send("❌ No feed with that id/URL in this server.")
        await db.remove_feed(feed["id"])
        await ctx.send(f"🗑️ Removed **{feed['name']}** (`{feed['url']}`).")

    @rss.command(name="list")
    async def list_(self, ctx: commands.Context):
        """List feeds registered in this server."""
        feeds = await db.list_feeds(ctx.guild.id)
        if not feeds:
            return await ctx.send(
                f"No feeds yet. Add one with `{ctx.clean_prefix}rss add`.")
        embed = discord.Embed(title=f"RSS feeds in {ctx.guild.name}", color=RSS_COLOR)
        for f in feeds[:25]:
            last = (f"<t:{int(f['last_polled'])}:R>" if f["last_polled"] else "never")
            embed.add_field(
                name=f"#{f['id']} — {f['name'][:200]}",
                value=f"{f['url']}\nevery {f['interval_seconds']}s · last polled {last}",
                inline=False)
        await ctx.send(embed=embed)

    @rss.command()
    @commands.has_guild_permissions(manage_guild=True)
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
    async def poll(self, ctx: commands.Context, *, ref: str):
        """Force a feed to be polled on the next cycle (within ~60s)."""
        feed = await db.get_feed(ctx.guild.id, ref)
        if feed is None:
            return await ctx.send("❌ No feed with that id/URL in this server.")
        await db.mark_due(feed["id"])
        await ctx.send(f"🔄 **{feed['name']}** queued — it'll be polled within a minute.")

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
