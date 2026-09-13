import asyncio
import json
import logging
from datetime import datetime

import discord
from discord.ext import commands, tasks

from config import CHANNEL_ID, CONFIG, DATA_DIR, GUILD_ID, RCON
from rcon_client import RCONError, WardogsRCON

log = logging.getLogger("wardogs")

WARDOGS_COLOR = 0x2EA2CC


def _short(text, limit=20):
    text = str(text or "?").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _kills(p):
    return p.get("kills", 0) or 0


class Wardogs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.api = WardogsRCON(RCON)
        self.poll_seconds = max(15, int(CONFIG.get("poll_seconds", 30)))
        self.summary_interval = max(1, int(CONFIG.get("summary_interval_minutes", 5)))
        self.players_limit = max(1, int(CONFIG.get("players_limit", 15)))
        self.presence_cfg = CONFIG.get("presence") or {}
        self.connect_url = CONFIG.get("connect_url") or ""
        self.summary_channel_id = int(CHANNEL_ID or 0)
        self._summary_message_id = None
        self.saved_summary_path = DATA_DIR / "summary.json"
        self._load_summary_id()
        self._started = False
        self.presence_loop.change_interval(seconds=self.poll_seconds)
        self.summary_loop.change_interval(minutes=self.summary_interval)

    def _load_summary_id(self):
        try:
            data = json.loads(self.saved_summary_path.read_text(encoding="utf-8"))
            self._summary_message_id = int(data.get("summary_message_id") or 0) or None
        except Exception:
            self._summary_message_id = None

    def _save_summary_id(self, message_id):
        try:
            self.saved_summary_path.parent.mkdir(parents=True, exist_ok=True)
            self.saved_summary_path.write_text(
                json.dumps({"summary_message_id": int(message_id)}), encoding="utf-8"
            )
        except Exception:
            log.exception("Не удалось сохранить ID сводки")

    # ---------- форматирование ----------

    def _presence_name(self, st=None, offline=False):
        prefix = self.presence_cfg.get("prefix", "wardogs-ru")
        if offline or st is None:
            return f"{prefix}: офлайн"
        players = st.get("players") or {}
        cur = players.get("current", 0)
        mx = players.get("max", "?")
        return f"{prefix}: {st.get('map', '?')} · {cur}/{mx}"

    def _status_embed(self, st):
        embed = discord.Embed(
            title="📡 Wardogs — статус сервера",
            color=WARDOGS_COLOR,
            timestamp=datetime.now(),
        )
        embed.add_field(name="Сервер", value=_short(st.get("serverName"), 60), inline=True)
        embed.add_field(name="Карта", value=_short(st.get("map")), inline=True)
        embed.add_field(name="Свет", value=_short(st.get("lighting")), inline=True)
        players = st.get("players") or {}
        embed.add_field(
            name="Игроки",
            value=f"{players.get('current', '?')}/{players.get('max', '?')}",
            inline=True,
        )
        experiences = st.get("experiences") or []
        if experiences:
            embed.add_field(
                name="Режим",
                value=_short(", ".join(experiences), 60),
                inline=False,
            )
        factions = st.get("factionScores") or []
        factions = sorted(factions, key=lambda f: f.get("score", 0), reverse=True)
        if factions:
            embed.add_field(
                name="Счёт фракций",
                value="\n".join(
                    f"• **{_short(f.get('name'), 20)}** — {f.get('score', 0)}"
                    for f in factions
                ),
                inline=False,
            )
        rotation = st.get("rotation") or {}
        if rotation:
            now = rotation.get("nowIndex", "?")
            nxt = rotation.get("nextIndex", "?")
            embed.add_field(name="Ротация", value=f"сейчас #{now} · следующая #{nxt}", inline=True)
        return embed

    def _map_file(self, st):
        """Карточка текущей карты (генерируется Pillow, кэшируется в data/maps)."""
        try:
            from mapcard import generate_map_card

            path = generate_map_card(st.get("map"), st.get("lighting"), DATA_DIR / "maps")
            return discord.File(path, filename="wardogs_map.png")
        except Exception:
            log.exception("Ошибка генерации карточки карты")
            return None

    def _with_map(self, embed, st):
        file = self._map_file(st)
        if file:
            embed.set_image(url=f"attachment://{file.filename}")
        return embed, file

    def _players_text(self, players, limit):
        if not players:
            return "Сервер пуст."
        header = (
            f"{'#':<3}{'Игрок':<20}{'Команда':<13}"
            f"{'K':>4}{'D':>5}{'KD':>5}{'Касса':>8}{'Пинг':>6}"
        )
        lines = [header, "-" * len(header)]
        for i, p in enumerate(players[:limit], 1):
            k = p.get("kills", 0) or 0
            d = p.get("deaths", 0) or 0
            if d:
                kd = f"{k / d:.1f}"
            elif k:
                kd = "∞"
            else:
                kd = "0.0"
            cash = p.get("cash", 0) or 0
            ping = p.get("pingMs", "")
            lines.append(
                f"{i:<3}{_short(p.get('name'), 19):<20}{_short(p.get('faction'), 13):<13}"
                f"{k:>4}{d:>5}{kd:>5}{cash:>8}{str(ping):>6}"
            )
        if len(players) > limit:
            lines.append(f"… и ещё {len(players) - limit}")
        return "\n".join(lines)

    def _build_summary_embed(self, st):
        embed = self._status_embed(st)
        embed.set_footer(text="Автосводка")
        return self._with_map(embed, st)

    # ---------- запросы к RCON ----------

    async def _fetch_players(self, attempts=2):
        """/v1/players на загруженном сервере нестабилен — делаем пару попыток."""
        last = None
        for i in range(attempts):
            try:
                return await self.api.players()
            except RCONError as e:
                last = e
                if i < attempts - 1:
                    await asyncio.sleep(2)
        raise last

    # ---------- команды в канале ----------

    _slash_prefix = str(CONFIG.get("slash_prefix", "wardogs"))

    wd = discord.app_commands.Group(
        name=_slash_prefix,
        description="Данные с RCON-API сервера WARDOGS",
        guild_ids=[GUILD_ID] if GUILD_ID else None,
    )

    @wd.command(name="status", description="Статус игрового сервера WARDOGS")
    async def wd_status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            st = await self.api.status()
        except RCONError as e:
            await interaction.followup.send(f"⚠️ RCON недоступен: {e}", ephemeral=True)
            return
        embed, file = self._with_map(self._status_embed(st), st)
        await interaction.followup.send(embed=embed, file=file, view=self._fresh_connect_view())

    @wd.command(name="players", description="Список игроков на сервере (K/D/касса/пинг)")
    async def wd_players(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            data = await self._fetch_players()
        except RCONError as e:
            await interaction.followup.send(f"⚠️ RCON недоступен: {e}", ephemeral=True)
            return
        players = sorted(data.get("players") or [], key=_kills, reverse=True)
        embed = discord.Embed(
            title=f"🎯 Игроки на сервере — {data.get('count', len(players))}",
            description=f"```\n{self._players_text(players, self.players_limit)}\n```",
            color=WARDOGS_COLOR,
            timestamp=datetime.now(),
        )
        await interaction.followup.send(embed=embed)

    @wd.command(name="caps", description="Возможности этой сборки сервера (роуты API)")
    async def wd_caps(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            caps = await self.api.capabilities()
        except RCONError as e:
            await interaction.followup.send(f"⚠️ RCON недоступен: {e}", ephemeral=True)
            return
        routes = caps.get("routes") or []
        shown = "\n".join(routes[:30]) if routes else "нет данных"
        embed = discord.Embed(
            title="🧰 Возможности сервера",
            description=f"```\n{shown}\n```" if routes else "Сборка не отдала список роутов.",
            color=WARDOGS_COLOR,
        )
        if routes and len(routes) > 30:
            embed.set_footer(text=f"… и ещё {len(routes) - 30}")
        write = (caps.get("config") or {}).get("writable", "?")
        embed.add_field(name="Конфиг доступен на запись", value=str(write), inline=True)
        await interaction.followup.send(embed=embed)

    # ---------- фон: статус бота + сводка в канал ----------

    @tasks.loop(seconds=60)
    async def presence_loop(self):
        try:
            st = await self.api.status()
        except RCONError as e:
            log.warning("RCON недоступен: %s", e)
            await self._update_presence(offline=True)
            return
        except Exception:
            log.exception("Ошибка опроса RCON (status)")
            await self._update_presence(offline=True)
            return
        await self._update_presence(st)

    @tasks.loop(minutes=5)
    async def summary_loop(self):
        if not self.summary_channel_id:
            return
        try:
            st = await self.api.status()
        except RCONError as e:
            log.warning("RCON недоступен (сводка): %s", e)
            return
        except Exception:
            log.exception("Ошибка опроса RCON (сводка)")
            return
        await self._update_summary(st)

    async def _update_presence(self, st=None, offline=False):
        if not self.presence_cfg.get("enabled", True):
            return
        name = self._presence_name(st, offline=offline)[:128]
        try:
            await self.bot.change_presence(activity=discord.Game(name=name))
        except Exception:
            log.exception("Не удалось обновить статус бота")

    def _fresh_connect_view(self):
        view = discord.ui.View()
        if self.connect_url:
            view.add_item(
                discord.ui.Button(
                    label="🎮 Подключиться к серверу",
                    style=discord.ButtonStyle.url,
                    url=self.connect_url,
                )
            )
        return view

    async def _update_summary(self, st):
        channel = self.bot.get_channel(self.summary_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.summary_channel_id)
            except Exception:
                log.warning("Канал сводки %s не найден", self.summary_channel_id)
                return
        embed, file = self._build_summary_embed(st)

        message_id = self._summary_message_id
        if message_id:
            try:
                msg = await channel.fetch_message(message_id)
                kwargs = {"embed": embed, "view": self._fresh_connect_view()}
                if file:
                    kwargs["attachments"] = [file]
                await msg.edit(**kwargs)
                return
            except discord.NotFound:
                self._summary_message_id = None
            except Exception:
                log.exception("Ошибка редактирования сводки — попробую перепост")
        try:
            msg = await channel.send(embed=embed, file=file)
            self._summary_message_id = msg.id
            self._save_summary_id(msg.id)
        except Exception:
            log.exception("Ошибка отправки сводки в канал")

    @presence_loop.before_loop
    async def before_presence_loop(self):
        await self.bot.wait_until_ready()

    @summary_loop.before_loop
    async def before_summary_loop(self):
        await self.bot.wait_until_ready()
        # первая сводка — чуть позже, чтобы присутствие уже обновилось
        await asyncio.sleep(5)

    @commands.Cog.listener()
    async def on_ready(self):
        if self._started:
            return
        self._started = True
        self.presence_loop.start()
        self.summary_loop.start()
        log.info(
            "Wardogs: опрос %s://%s:%s, присутствие каждые %s с, сводка в канал %s каждые %s мин",
            RCON.get("scheme", "http"),
            RCON.get("host"),
            RCON.get("port"),
            self.poll_seconds,
            self.summary_channel_id or "выключена",
            self.summary_interval,
        )

    async def cog_unload(self):
        self.presence_loop.cancel()
        self.summary_loop.cancel()
        await self.api.close()


async def setup(bot: commands.Bot):
    await bot.add_cog(Wardogs(bot))