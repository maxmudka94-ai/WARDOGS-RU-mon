import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

import discord
from discord.ext import commands

from config import CONFIG, GUILD_ID, PROXY_URL, TOKEN
from cogs.wardogs import Wardogs
from cogs.matchstats import MatchStats

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("main")

intents = discord.Intents.default()
_bot_opts = {}
_proxy = PROXY_URL or ""
if _proxy.lower() in ("", "none", "system", "off", "0"):
    _proxy = ""
if _proxy:
    _bot_opts["proxy"] = _proxy

bot = commands.Bot(command_prefix="!", intents=intents, **_bot_opts)


@bot.event
async def on_ready():
    print(f"Бот запущен: {bot.user} (ID: {bot.user.id})", flush=True)
    for g in bot.guilds:
        print(f"СЕРВЕР: {g.name} | ID: {g.id}", flush=True)
    if not getattr(bot, "_commands_synced", False):
        try:
            if GUILD_ID:
                g = bot.get_guild(GUILD_ID) or discord.Object(id=GUILD_ID)
                guild_synced = await bot.tree.sync(guild=g)
                print(f"Синхронизировано команд для гильды: {len(guild_synced)}", flush=True)
            else:
                synced = await bot.tree.sync()
                print(f"Синхронизировано глобальных команд: {len(synced)}", flush=True)
        except Exception as e:
            print(f"Ошибка синхронизации команд: {e}", flush=True)
        bot._commands_synced = True
    log.info("Wardogs-monitor готов к работе.")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    cmd = interaction.command.name if interaction.command else "?"
    log.error("Ошибка команды %s: %s", cmd, error, exc_info=error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send("Произошла ошибка.", ephemeral=True)
        else:
            await interaction.response.send_message("Произошла ошибка.", ephemeral=True)
    except Exception:
        pass


async def main():
    async with bot:
        await bot.add_cog(Wardogs(bot))
        await bot.add_cog(MatchStats(bot))
        if config.PANEL_ENABLED:
            import webpanel

            await webpanel.start(bot)
        await bot.start(TOKEN)


def _acquire_single_instance_mutex():
    try:
        if os.name == "nt":
            import msvcrt

            fh = open(os.devnull, "a")
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                return False
            _acquire_single_instance_mutex._lock_handle = fh
            return True
        else:
            import fcntl

            fh = open(os.devnull, "a")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return False
            _acquire_single_instance_mutex._lock_handle = fh
            return True
    except Exception:
        log.exception("Сбой блокировки одиночного инстанса — запуск запрещён.")
        return False


if __name__ == "__main__":
    if not _acquire_single_instance_mutex():
        log.warning("Уже запущен другой инстанс бота — выход.")
        sys.exit(0)
    asyncio.run(main())