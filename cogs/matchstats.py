import asyncio
import json
import logging
import time
from datetime import datetime

import discord
from discord.ext import commands, tasks

from config import CONFIG, DATA_DIR
from rcon_client import RCONError, WardogsRCON

log = logging.getLogger("matchstats")

MATCH_COLOR = 0x2EA2CC
PLACEHOLDER_TITLE = "📊 Итоги матча"


def _short(text, limit=20):
    text = str(text or "?").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _kills(p):
    return p.get("kills", 0) or 0


def _fmt_duration(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m:02d}м"
    return f"{m}м {s:02d}с"


def _bar(value, total):
    if not total:
        return ""
    return "█" * max(1, int(round((value / total) * 18)))


def _players_text(players, limit):
    if not players:
        return "На момент завершения матча игроков не было."
    header = (
        f"{'#':<3}{'Игрок':<20}{'Команда':<13}"
        f"{'K':>4}{'D':>5}{'KD':>5}{'Пинг':>6}"
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
        ping = p.get("pingMs", "")
        lines.append(
            f"{i:<3}{_short(p.get('name'), 19):<20}{_short(p.get('faction'), 13):<13}"
            f"{k:>4}{d:>5}{kd:>5}{str(ping):>6}"
        )
    if len(players) > limit:
        lines.append(f"… и ещё {len(players) - limit}")
    return "\n".join(lines)


class MatchStats(commands.Cog):
    """Мониторинг итогов матча: детектит завершение по сбросу счёта/смене карты.

    Сравнивает суммарный счёт фракций и идентификатор матча (карта+режим+свет)
    при каждом опросе. Когда матч закончился — постит эмбед с финальным счётом
    и топом игроков по K (пиковый снимок, пока счёт не сброшен).
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cfg = CONFIG.get("match_stats") or {}
        self.enabled = bool(self.cfg.get("enabled", False) and self.cfg.get("channel_id"))
        self.channel_id = int(self.cfg.get("channel_id") or 0)
        self.poll_seconds = max(10, int(self.cfg.get("poll_seconds", 20)))
        self.top_players = max(1, int(self.cfg.get("top_players", 10)))
        self.api = WardogsRCON(CONFIG.get("rcon") or {})
        self._message_id = None
        self.saved_path = DATA_DIR / "matchstats.json"
        self._load_message_id()
        self._last_key = None
        self._in_match = False
        self._session_start = None
        self._peak = None
        if self.enabled:
            self.check_loop.change_interval(seconds=self.poll_seconds)

    def _load_message_id(self):
        try:
            data = json.loads(self.saved_path.read_text(encoding="utf-8"))
            self._message_id = int(data.get("message_id") or 0) or None
        except Exception:
            self._message_id = None

    def _save_message_id(self, message_id):
        try:
            self.saved_path.parent.mkdir(parents=True, exist_ok=True)
            self.saved_path.write_text(
                json.dumps({"message_id": int(message_id)}), encoding="utf-8"
            )
        except Exception:
            log.exception("Не удалось сохранить ID эмбеда итогов матча")

    # ---------- детекция конца матча ----------

    def _total(self, status):
        return sum(int(f.get("score", 0) or 0) for f in (status.get("factionScores") or []))

    async def _poll(self):
        st = await self.api.status()
        players = []
        try:
            data = await self.api.players()
            players = data.get("players") or []
        except RCONError:
            pass
        return st, players

    def _ended(self):
        if self._peak is not None:
            self.bot.loop.create_task(self._post(self._peak))
        self._peak = None
        self._in_match = False
        self._session_start = None

    @tasks.loop(seconds=20)
    async def check_loop(self):
        try:
            st, players = await self._poll()
        except RCONError as e:
            log.warning("RCON недоступен (итоги матча): %s", e)
            return
        except Exception:
            log.exception("Ошибка опроса RCON (итоги матча)")
            return

        total = self._total(st)
        key = (
            st.get("map"),
            tuple(st.get("experiences") or []),
            st.get("lighting"),
        )

        if self._last_key is not None and key != self._last_key:
            self._ended()
        self._last_key = key

        if total > 0:
            if not self._in_match:
                self._session_start = time.time()
            self._in_match = True
            if self._peak is None or total > self._peak["total"]:
                self._peak = {
                    "total": total,
                    "ts": time.time(),
                    "start": self._session_start or time.time(),
                    "status": st,
                    "players": players,
                }
        else:
            self._ended()

    # ---------- пост итогов ----------

    def _factions_text(self, status):
        factions = sorted(
            (status.get("factionScores") or []),
            key=lambda f: f.get("score", 0) or 0,
            reverse=True,
        )
        total = sum(int(f.get("score", 0) or 0) for f in factions)
        if not factions:
            return "—"
        lines = []
        for f in factions:
            name = _short(f.get("name"), 22)
            score = f.get("score", 0) or 0
            lines.append(f"• **{name}** — {score}  `{_bar(score, total)}`")
        return "\n".join(lines)

    async def _post(self, peak):
        st = peak["status"]
        players = sorted(peak["players"] or [], key=_kills, reverse=True)
        embed = discord.Embed(
            title="🏁 Матч завершён",
            color=MATCH_COLOR,
            timestamp=datetime.fromtimestamp(peak["ts"]),
        )
        embed.add_field(name="Карта", value=_short(st.get("map")), inline=True)
        exps = st.get("experiences") or []
        if exps:
            embed.add_field(name="Режим", value=_short(", ".join(exps), 40), inline=True)
        embed.add_field(name="Свет", value=_short(st.get("lighting")), inline=True)
        players_info = st.get("players") or {}
        embed.add_field(
            name="Игроков",
            value=f"{players_info.get('current', '?')}",
            inline=True,
        )
        duration = peak["ts"] - peak["start"]
        if duration >= 30:
            embed.add_field(name="Длительность", value=_fmt_duration(duration), inline=True)

        factions = sorted(
            (st.get("factionScores") or []),
            key=lambda f: f.get("score", 0) or 0,
            reverse=True,
        )
        if factions and factions[0].get("score", 0):
            embed.add_field(
                name="Победитель",
                value=_short(factions[0].get("name"), 22),
                inline=True,
            )
        embed.add_field(name="Счёт фракций", value=self._factions_text(st), inline=False)

        if players:
            embed.add_field(
                name=f"Топ-{min(len(players), self.top_players)} по убийствам",
                value=f"```\n{_players_text(players, self.top_players)}\n```",
                inline=False,
            )
        embed.set_footer(text="Итоги матча · wardogs-monitor")

        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.channel_id)
            except Exception:
                log.warning("Канал итогов матча %s не найден", self.channel_id)
                return
        message_id = self._message_id
        if message_id:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                self._message_id = None
            except Exception:
                log.exception("Ошибка редактирования итогов матча — попробую перепост")
        try:
            msg = await channel.send(embed=embed)
            self._message_id = msg.id
            self._save_message_id(msg.id)
            log.info("Эмбед итогов матча отправлен в канал %s", self.channel_id)
        except Exception:
            log.exception("Ошибка отправки эмбеда итогов матча в канал")

    async def _ensure_placeholder(self):
        """Один постоянный эмбед-заглушка: при старте и каждом перезапуске."""
        if not self.enabled:
            return
        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.channel_id)
            except Exception:
                log.warning("Канал итогов матча %s не найден (заглушка)", self.channel_id)
                return
        embed = discord.Embed(
            title=PLACEHOLDER_TITLE,
            description="Ожидание завершения матча — сюда придёт финальная статистика.",
            color=MATCH_COLOR,
        )
        embed.set_footer(text="Итоги матча · wardogs-monitor")
        message_id = self._message_id
        if message_id:
            try:
                msg = await channel.fetch_message(message_id)
            except discord.NotFound:
                self._message_id = None
            except Exception:
                log.exception("Ошибка чтения эмбеда итогов матча — попробую пересоздать")
            else:
                # итоги уже выложенного матча не затираем заглушкой
                if msg.embeds and msg.embeds[0].title != PLACEHOLDER_TITLE:
                    log.info("Итоги прошлого матча на месте — оставляю как есть")
                    return
                await msg.edit(embed=embed)
                return
        try:
            msg = await channel.send(embed=embed)
        except Exception:
            log.exception("Не удалось создать эмбед-заглушку итогов матча")
            return
        self._message_id = msg.id
        self._save_message_id(msg.id)

    # ---------- жизненный цикл ----------

    @check_loop.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(3)

    @commands.Cog.listener()
    async def on_ready(self):
        if getattr(self, "_started", False):
            return
        self._started = True
        if self.enabled:
            self.check_loop.start()
            self.bot.loop.create_task(self._placeholder_later())
        log.info(
            "MatchStats: %s, опрос каждые %s с, канал %s, топ-%s",
            "включён" if self.enabled else "выключен",
            self.poll_seconds,
            self.channel_id or "—",
            self.top_players,
        )

    async def _placeholder_later(self):
        """Заглушка создаётся ПОСЛЕ сводки мониторинга — чтобы на свежем канале
        мониторинг был первым (вверху), а итоги матча — вторыми (снизу)."""
        await asyncio.sleep(8)
        wardogs = self.bot.get_cog("Wardogs")
        for _ in range(12):
            if wardogs is None or wardogs._summary_message_id is not None:
                break
            await asyncio.sleep(2)
        await self._ensure_placeholder()

    async def cog_unload(self):
        self.check_loop.cancel()
        await self.api.close()


async def setup(bot: commands.Bot):
    await bot.add_cog(MatchStats(bot))