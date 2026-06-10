import logging
import logging.handlers
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

import db

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
PREFIX = os.getenv('BOT_PREFIX', '!')
DATA_DIR = os.getenv('DATA_DIR', 'data')
LOG_DIR = os.path.join('storage', 'logs')

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.handlers.TimedRotatingFileHandler(
            os.path.join(LOG_DIR, 'bot.log'),
            when='D',
            interval=3,
            backupCount=14,
            encoding='utf-8',
        ),
    ],
)

intents = discord.Intents.default()
intents.message_content = True


class RSSBot(commands.Bot):
    async def setup_hook(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        await db.init(os.path.join(DATA_DIR, 'rss.sqlite3'))
        await self.load_extension('cogs.rss')
        synced = await self.tree.sync()
        logging.getLogger('bot').info('Synced %d slash command(s)', len(synced))

    async def close(self):
        await super().close()
        await db.close()


bot = RSSBot(command_prefix=PREFIX, intents=intents)


@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    print('------')


@bot.hybrid_command(name='ping')
async def ping(ctx: commands.Context):
    """Responds with pong and the gateway latency"""
    await ctx.send(f'Pong! `{bot.latency * 1000:.0f}ms`')


@bot.hybrid_command(name='hello')
async def hello(ctx: commands.Context):
    """Greets the user"""
    await ctx.send(f'Hello {ctx.author.name}!')


if __name__ == '__main__':
    if not TOKEN:
        print('Error: DISCORD_TOKEN not found in .env file')
        print('Please create a .env file with your Discord bot token')
        exit(1)
    bot.run(TOKEN, log_handler=None)
