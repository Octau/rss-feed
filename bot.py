import glob
import logging
import logging.handlers
import os
import time
from datetime import datetime

import discord
from discord.ext import commands
from dotenv import load_dotenv

import adapters
import db

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
DATA_DIR = os.getenv('DATA_DIR', 'data')
LOG_DIR = os.getenv('LOG_DIR', os.path.join('storage', 'logs'))
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
LOG_BACKUP_COUNT = int(os.getenv('LOG_BACKUP_COUNT', '7'))

os.makedirs(LOG_DIR, exist_ok=True)


class DailyFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Writes to bot-YYYY-MM-DD.log for the current date.
    On rollover at midnight, opens a new file for the new date."""

    def __init__(self, log_dir: str, backup_count: int = 7, encoding: str = 'utf-8'):
        self._log_dir = log_dir
        super().__init__(
            self._dated_path(),
            when='midnight',
            interval=1,
            backupCount=backup_count,
            encoding=encoding,
        )

    def _dated_path(self) -> str:
        return os.path.join(self._log_dir, datetime.now().strftime('bot-%Y-%m-%d.log'))

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        self.baseFilename = os.path.abspath(self._dated_path())
        self.stream = self._open()
        self.rolloverAt = self.computeRollover(int(time.time()))
        if self.backupCount > 0:
            files = sorted(glob.glob(os.path.join(self._log_dir, 'bot-*.log')))
            while len(files) > self.backupCount:
                os.remove(files.pop(0))


_formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(name)s: %(message)s')

_file_handler = DailyFileHandler(LOG_DIR, backup_count=LOG_BACKUP_COUNT)
_file_handler.setFormatter(_formatter)

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_formatter)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    handlers=[_file_handler, _stream_handler],
)

logger = logging.getLogger('bot')

intents = discord.Intents.default()


class RSSBot(commands.Bot):
    async def setup_hook(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        await db.init(os.path.join(DATA_DIR, 'rss.sqlite3'))
        await self.load_extension('cogs.rss')
        logger.info('Feed types: %s', ', '.join(adapters.FEED_TYPES))
        synced = await self.tree.sync()
        logger.info('Synced %d slash command(s)', len(synced))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.type == discord.InteractionType.application_command:
            cmd = interaction.command.qualified_name if interaction.command else 'unknown'
            logger.info('[command] user=%s guild=%s cmd=%s',
                        interaction.user.id, interaction.guild_id, cmd)
        return True

    async def close(self):
        await super().close()
        await db.close()


bot = RSSBot(command_prefix=commands.when_mentioned, intents=intents)


@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    print('------')


@bot.tree.command(name='ping')
async def ping(interaction: discord.Interaction):
    """Responds with pong and the gateway latency"""
    await interaction.response.send_message(f'Pong! `{bot.latency * 1000:.0f}ms`')


@bot.tree.command(name='hello')
async def hello(interaction: discord.Interaction):
    """Greets the user"""
    await interaction.response.send_message(f'Hello {interaction.user.name}!')


if __name__ == '__main__':
    if not TOKEN:
        print('Error: DISCORD_TOKEN not found in .env file')
        print('Please create a .env file with your Discord bot token')
        exit(1)
    bot.run(TOKEN, log_handler=None)
