import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

import db

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
PREFIX = os.getenv('BOT_PREFIX', '!')
DATA_DIR = os.getenv('DATA_DIR', 'data')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
)

intents = discord.Intents.default()
intents.message_content = True


class RSSBot(commands.Bot):
    async def setup_hook(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        await db.init(os.path.join(DATA_DIR, 'rss.sqlite3'))
        await self.load_extension('cogs.rss')

    async def close(self):
        await super().close()
        await db.close()


bot = RSSBot(command_prefix=PREFIX, intents=intents)


@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    print('------')


@bot.command(name='ping')
async def ping(ctx):
    """Responds with pong and the gateway latency"""
    await ctx.send(f'Pong! `{bot.latency * 1000:.0f}ms`')


@bot.command(name='hello')
async def hello(ctx):
    """Greets the user"""
    await ctx.send(f'Hello {ctx.author.name}!')


if __name__ == '__main__':
    if not TOKEN:
        print('Error: DISCORD_TOKEN not found in .env file')
        print('Please create a .env file with your Discord bot token')
        exit(1)
    bot.run(TOKEN)
