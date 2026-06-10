---
name: discord-py
description: Reference for discord.py 2.7.1 as installed in this repo's venv — bot setup, intents, cogs, hybrid/app commands, tasks loops, UI components (incl. Components V2 and modal components), webhooks, and error handling. Use when writing or debugging any discord.py code.
---

# discord.py 2.7.1 (installed)

Version installed in `venv/` (Python 3.10): **discord.py 2.7.1** by Rapptz.
Source lives at `venv/lib/python3.10/site-packages/discord/` — when unsure
about a signature or behavior, read the installed source; it is the ground
truth for this project, not docs for some other version.

> Gotcha when probing: don't run Python with the package dir as cwd —
> `discord/types/` shadows stdlib `types` and breaks imports.

## Core model

- Everything is asyncio. Never block the event loop: offload CPU/blocking work
  with `await loop.run_in_executor(None, fn, *args)` (this repo does that for
  feedparser).
- `discord.Client` is the low-level client; `discord.ext.commands.Bot`
  subclasses it and adds prefix/hybrid commands, cogs, and extensions. This
  repo uses `commands.Bot`.
- Async setup belongs in `setup_hook()` (runs after login, before connecting):
  open DBs, `await bot.load_extension(...)`, sync the app command tree. This
  repo's `RSSBot.setup_hook` in `bot.py` is the pattern to follow.
- `bot.run(token, log_handler=None)` disables discord.py's default logging
  handler so it inherits whatever `logging` config you set up yourself.

## Intents

```python
intents = discord.Intents.default()
intents.message_content = True   # privileged; needed for prefix commands
bot = commands.Bot(command_prefix="!", intents=intents)
```

`message_content`, `members`, and `presences` are privileged — they must also
be enabled in the Discord Developer Portal or the gateway connection fails.
Slash commands do NOT need `message_content`; prefix commands do.

## Cogs and extensions

- Extension = a module with `async def setup(bot): await bot.add_cog(MyCog(bot))`.
  Loaded via `await bot.load_extension("cogs.rss")`.
- Cog lifecycle hooks: `cog_load` / `cog_unload` (both may be async). Start
  `tasks.loop`s in `cog_load`, cancel them in `cog_unload`.
- `commands.Cog.listener()` decorates event listeners inside cogs.

## Commands: prefix, slash, hybrid

Three flavors:

1. **Prefix** — `@commands.command()`, gets `ctx: commands.Context`.
2. **App (slash)** — `@app_commands.command()` on `bot.tree`, gets
   `interaction: discord.Interaction`.
3. **Hybrid** — `@commands.hybrid_command()` / `@commands.hybrid_group()`:
   one function, both invocation paths, gets `ctx`. **This repo uses hybrid
   everywhere** — keep new commands hybrid for consistency.

Hybrid specifics (see `cogs/rss.py` for live examples):

- Parameters need type hints; `app_commands.describe(...)` adds slash
  descriptions. Only str/int/float/bool/Member/Role/channel types etc. are
  valid slash parameter types.
- `ctx.interaction` is `None` on the prefix path — branch on it when behavior
  must differ (this repo: ephemeral defer on slash vs. message delete on
  prefix, to hide webhook URLs).
- Slash interactions must be acknowledged within **3 seconds**: either respond
  or `await ctx.defer(ephemeral=True)` first, then `await ctx.send(...)`
  (which follows up automatically). Hybrid `ctx.send(..., ephemeral=True)` is
  ignored silently on the prefix path — fine.
- Sync: slash commands appear only after `await bot.tree.sync()`. This repo
  syncs globally in `setup_hook`. Global sync can take up to ~1h to propagate;
  for instant testing, copy to one guild:
  `bot.tree.copy_global_to(guild=obj); await bot.tree.sync(guild=obj)`.
  Don't sync on every message/command — it's heavily rate limited.

Checks: `@commands.has_guild_permissions(manage_guild=True)`,
`@commands.guild_only()`, `@commands.cooldown(...)`. App-command equivalents
live in `app_commands.checks`. Hybrid commands accept the `commands.*` checks
and translate them.

### 2.4+ niceties available here

- `app_commands.Range[int, 1, 10]` for bounded numeric/str params.
- `app_commands.Choice` / `@app_commands.choices(...)` and
  `@app_commands.autocomplete(...)` for parameter suggestions.
- User-installable apps: `@app_commands.allowed_installs(guilds=True, users=True)`
  and `@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)`
  (`AppInstallationType` / `AppCommandContext`).

## Background tasks (`discord.ext.tasks`)

```python
from discord.ext import tasks

@tasks.loop(seconds=60)
async def poller(self): ...

@poller.before_loop
async def before_poller(self):
    await self.bot.wait_until_ready()
```

- Start with `.start()`, stop with `.cancel()` (in `cog_unload`).
- An unhandled exception **stops the loop silently** unless you wrap the body
  in try/except or add an `@poller.error` handler. This repo wraps each feed's
  processing so one bad feed can't kill the cycle — preserve that property.
- `change_interval(seconds=...)` adjusts at runtime; `tasks.loop(time=...)`
  runs at fixed times of day.

## Webhooks

This repo announces via webhooks, not bot sends:

```python
wh = discord.Webhook.from_url(url, session=aiohttp_session)
await wh.send(embed=embed, username="...", avatar_url="...")
```

- Requires an `aiohttp.ClientSession` you own; create it in `setup_hook`/`cog_load`
  and close it on unload.
- `Webhook.from_url` raises `ValueError` on malformed URLs; sends raise
  `discord.NotFound` (deleted webhook), `discord.HTTPException`. The repo also
  pre-validates with `WEBHOOK_RE`.
- Webhooks can't be ratelimit-bucketed per-bot — keep manual spacing between
  sends (repo: `SEND_SPACING = 1.0`).

## Embeds

`discord.Embed(title=..., url=..., description=..., colour=..., timestamp=datetime)`
plus `.add_field(name=, value=, inline=)`, `.set_footer(...)`, `.set_author(...)`.
Hard limits: title 256, description 4096, field value 1024, total 6000 chars,
25 fields. Truncate before building (repo: `clean_summary()`).

## UI components

### Classic views (max 5 action rows)

`discord.ui.View` with `@discord.ui.button(...)`, `@discord.ui.select(...)`;
select variants: `Select`, `UserSelect`, `RoleSelect`, `ChannelSelect`,
`MentionableSelect`. Views time out after 180s by default (`timeout=None` for
persistent views — those need `custom_id`s on every item and re-registration
via `bot.add_view(...)` at startup, or use `ui.DynamicItem` for stateless
persistence with regex-matched custom_ids).

### Components V2 (added 2.6 — available here)

`discord.ui.LayoutView` with layout items: `Container`, `Section`,
`TextDisplay`, `MediaGallery`, `Separator`, `Thumbnail`, `File`, `ActionRow`.
Sent as the view itself (`await channel.send(view=layout_view)`) — a CV2
message **cannot** also have `content` or `embeds`; text goes in
`TextDisplay`. Up to 40 components per message.

### Modals (2.7 expanded these)

`discord.ui.Modal` subclass with items; 2.7 adds `Label` (wraps an input with
label/description), `FileUpload`, `Checkbox`, `CheckboxGroup`, `RadioGroup`,
and allows `TextDisplay` and selects inside modals — older docs claiming
"TextInput only" are outdated for this version. Handle in
`async def on_submit(self, interaction)`.

## Other 2.x features present in this install

- **Polls** — `discord.Poll(question=..., duration=...)`, `.add_answer(...)`,
  send via `channel.send(poll=poll)`.
- **Soundboard** (`SoundboardSound`), **SKUs/subscriptions** (monetization),
  scheduled events, automod, onboarding — all present under `discord/*.py` if
  ever needed.

## Error handling

- Prefix/hybrid: `on_command_error(ctx, error)` (global) or
  `cog_command_error`. App commands: `bot.tree.on_error`.
- Unwrap `commands.CommandInvokeError` / `app_commands.CommandInvokeError`
  via `error.original`.
- Common: `commands.MissingPermissions`, `commands.NoPrivateMessage`,
  `commands.CommandOnCooldown`, `app_commands.CheckFailure`.
- HTTP errors: `discord.HTTPException` base; `discord.Forbidden` (403),
  `discord.NotFound` (404). discord.py auto-handles 429 rate limits by
  sleeping — don't retry manually on top of it.

## Quick checks against the installed lib

```bash
venv/bin/python -c "import discord; print(discord.__version__)"
venv/bin/python -c "import discord.ui as ui; help(ui.LayoutView)"
grep -rn "def sync" venv/lib/python3.10/site-packages/discord/app_commands/tree.py
```
