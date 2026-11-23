import asyncio
import logging
import os
import random
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "underground.sqlite3")
PORT = int(os.getenv("PORT", "8000"))

NIGHT_SECONDS = 60
MORNING_SECONDS = 60
MIN_PLAYERS = 4
MAX_PLAYERS = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("underground")


def role_counts(n: int) -> tuple[int, int, int]:
    dolls_by_count = {
        4: 1, 5: 2, 6: 2, 7: 2, 8: 3, 9: 3, 10: 4,
        11: 4, 12: 4, 13: 4, 14: 5, 15: 5, 16: 5,
        17: 6, 18: 6, 19: 7, 20: 7,
    }
    dolls = dolls_by_count[n]
    return 2, dolls, n - 2 - dolls


LOCATIONS = [
    ("АТС", ["Ящик ГП-5", "Плакаты ГО", "Чайник", "Кружка ЧОПа", "Лом", "Ящик Нерехт"]),
    ("Поддом", ["Плед", "Журнал", "Ручка", "Чайник", "Панорамник", "Фонарик"]),
    ("Склад ГО", ["Ящик ГП-5", "Ящик ММ-1", "Ящик ГП-7", "Дозиметр ДП-5В", "Ящик АИ-2", "Фильтр ФП-100"]),
]

ROLE_GUARD = "ЧОП"
ROLE_DOLL = "Кукла"
ROLE_VISITOR = "Посетитель"

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute(
    """CREATE TABLE IF NOT EXISTS players (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        started_private INTEGER DEFAULT 0
    )"""
)
conn.commit()


@dataclass
class Player:
    user_id: int
    name: str
    role: str = ""
    alive: bool = True
    action: Optional[tuple] = None


@dataclass
class Game:
    chat_id: int
    chat_title: str
    players: Dict[int, Player] = field(default_factory=dict)
    phase: str = "lobby"  # lobby / night / morning / finished
    location: Optional[str] = None
    items: List[str] = field(default_factory=list)
    stolen: List[str] = field(default_factory=list)
    round_no: int = 0
    doll_votes: Dict[int, str] = field(default_factory=dict)
    guard_actions: Dict[int, tuple] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer_task: Optional[asyncio.Task] = None


GAMES: Dict[int, Game] = {}
BOT: Optional[Bot] = None
dp = Dispatcher()


def display_name(user) -> str:
    raw = user.full_name or user.username or str(user.id)
    return raw.replace("<", "‹").replace(">", "›")


def lobby_keyboard(chat_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="🎒 Войти в игру", callback_data=f"join:{chat_id}")
    kb.button(text="🚪 Выйти", callback_data=f"leave:{chat_id}")
    kb.adjust(2)
    return kb.as_markup()


def players_text(game: Game) -> str:
    names = [f"• {p.name}" for p in game.players.values()]
    return "\n".join(names) if names else "Пока никого нет"


async def announce(game: Game, text: str, reply_markup=None):
    assert BOT is not None
    await BOT.send_message(game.chat_id, text, reply_markup=reply_markup)


async def update_lobby(game: Game):
    await announce(
        game,
        f"🎭 <b>UNDERGROUND — набор игроков</b>\n\n"
        f"Участников: <b>{len(game.players)}/{MAX_PLAYERS}</b>\n"
        f"Минимум для старта: <b>{MIN_PLAYERS}</b>\n\n"
        f"{players_text(game)}\n\n"
        "Нажмите кнопку ниже или введите /join, чтобы войти. Старт игры: /startgame",
        reply_markup=lobby_keyboard(game.chat_id),
    )


async def private_ready(user_id: int) -> bool:
    assert BOT is not None
    try:
        await BOT.send_message(
            user_id,
            "✅ Личка с <b>UNDERGROUND</b> подключена. Роль и ночные действия будут приходить сюда.",
        )
        return True
    except Exception:
        return False


async def send_role(player: Player, game: Game):
    assert BOT is not None
    text = (
        f"🎭 <b>Твоя роль: {player.role}</b>\n\n"
        f"Локация: <b>{game.location}</b>\n"
        "Игра началась. Не раскрывай свою роль другим игрокам."
    )
    if player.role == ROLE_GUARD:
        text += "\n\nКаждую ночь ты можешь выбрать: проверить игрока или задержать игрока."
    elif player.role == ROLE_DOLL:
        other_dolls = [
            p.name for p in game.players.values()
            if p.role == ROLE_DOLL and p.user_id != player.user_id
        ]
        text += "\n\nТвоя задача — вместе с другими куклами украсть всё имущество."
        if other_dolls:
            text += "\nДругие куклы: " + ", ".join(other_dolls)
    else:
        text += "\n\nОбсуждай подозрения в общей группе. Победа вместе с ЧОП, если все куклы задержаны."
    await BOT.send_message(player.user_id, text)


def cancel_timer(game: Game):
    if game.timer_task and not game.timer_task.done():
        game.timer_task.cancel()
    game.timer_task = None


async def begin_game(game: Game):
    async with game.lock:
        if game.phase != "lobby":
            return
        if len(game.players) < MIN_PLAYERS:
            await announce(
                game,
                f"❌ Для старта нужно минимум {MIN_PLAYERS} игрока. Сейчас: {len(game.players)}.",
            )
            return
        if len(game.players) > MAX_PLAYERS:
            await announce(game, f"❌ Максимум игроков: {MAX_PLAYERS}.")
            return

        ready = []
        for p in game.players.values():
            if await private_ready(p.user_id):
                ready.append(p.user_id)

        if len(ready) != len(game.players):
            missing = [p.name for p in game.players.values() if p.user_id not in ready]
            await announce(
                game,
                "❌ Не удалось написать в личку всем игрокам. Каждый должен открыть бота и нажать /start.\n"
                "Не подключились: " + ", ".join(missing) + "\n"
                "Набор сохранён — нажмите /startgame снова, когда все откроют бота.",
            )
            return

        n = len(game.players)
        guards, dolls, visitors = role_counts(n)
        roles = [ROLE_GUARD] * guards + [ROLE_DOLL] * dolls + [ROLE_VISITOR] * visitors
        random.shuffle(roles)

        for player, role in zip(game.players.values(), roles):
            player.role = role
            player.alive = True
            player.action = None

        loc_name, loc_items = random.choice(LOCATIONS)
        game.location = loc_name
        game.items = list(loc_items)
        game.stolen = []
        game.round_no = 0
        game.phase = "night"

        await announce(
            game,
            f"🚨 <b>ЭКСПЕДИЦИЯ НАЧАЛАСЬ!</b>\n"
            f"Локация: <b>{loc_name}</b>\n"
            f"Участников: {n} (ЧОП: {guards}, Кукол: {dolls}, Посетителей: {visitors})\n"
            f"Роли отправлены в личные сообщения.",
        )

        for p in game.players.values():
            try:
                await send_role(p, game)
            except Exception:
                log.exception("Failed to send role to %s", p.user_id)

        await start_night(game)


async def start_night(game: Game):
    assert BOT is not None
    if game.phase == "finished":
        return

    cancel_timer(game)
    game.phase = "night"
    game.round_no += 1
    game.doll_votes.clear()
    game.guard_actions.clear()
    for p in game.players.values():
        p.action = None

    await announce(
        game,
        f"🌙 <b>НОЧЬ №{game.round_no}</b>\n"
        f"Длительность: {NIGHT_SECONDS} сек. Все действия — в личке бота.",
    )

    for p in game.players.values():
        if not p.alive:
            continue
        try:
            if p.role == ROLE_GUARD:
                kb = InlineKeyboardBuilder()
                kb.button(text="🔎 Проверить", callback_data=f"gcheck:{game.chat_id}")
                kb.button(text="🚔 Задержать", callback_data=f"garrest:{game.chat_id}")
                kb.adjust(1)
                await BOT.send_message(
                    p.user_id,
                    f"🌙 Ночь №{game.round_no}. Выбери действие ЧОП:",
                    reply_markup=kb.as_markup(),
                )
            elif p.role == ROLE_DOLL:
                kb = InlineKeyboardBuilder()
                for idx, item in enumerate(game.items):
                    kb.button(text=f"🧤 {item}", callback_data=f"dv:{game.chat_id}:{idx}")
                kb.adjust(1)
                await BOT.send_message(
                    p.user_id,
                    f"🌙 Ночь №{game.round_no}. Проголосуй за имущество для кражи:",
                    reply_markup=kb.as_markup(),
                )
        except Exception:
            log.exception("Could not send night action to %s", p.user_id)

    game.timer_task = asyncio.create_task(phase_timer(game, NIGHT_SECONDS, "night"))


async def phase_timer(game: Game, seconds: int, expected_phase: str):
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return

    async with game.lock:
        if game.phase != expected_phase:
            return
        if expected_phase == "night":
            await resolve_night(game)
        elif expected_phase == "morning":
            await end_morning(game)


async def resolve_night(game: Game):
    assert BOT is not None
    counts: Dict[str, int] = {}
    for item in game.doll_votes.values():
        counts[item] = counts.get(item, 0) + 1

    stolen_now = None
    if counts:
        highest = max(counts.values())
        winners = [item for item, count in counts.items() if count == highest]
        if len(winners) == 1:
            stolen_now = winners[0]
            if stolen_now in game.items:
                game.items.remove(stolen_now)
                game.stolen.append(stolen_now)

    arrested_ids = set()
    checks = []
    for guard_id, action in game.guard_actions.items():
        kind, target_id = action
        target = game.players.get(target_id)
        if not target or not target.alive:
            continue
        if kind == "arrest":
            arrested_ids.add(target_id)
        elif kind == "check":
            checks.append((guard_id, target_id, target.role))

    for guard_id, target_id, role in checks:
        try:
            await BOT.send_message(
                guard_id,
                f"🔎 Результат проверки: <b>{game.players[target_id].name}</b> — роль <b>{role}</b>.",
            )
        except Exception:
            pass

    for uid in arrested_ids:
        game.players[uid].alive = False

    if stolen_now:
        theft_text = f"🧤 Куклы украли: <b>{stolen_now}</b>."
    elif counts:
        theft_text = "🧤 Куклы не смогли выбрать один предмет: ничья в голосовании. Ничего не украдено."
    else:
        theft_text = "🧤 Этой ночью куклы не совершили кражу."

    if arrested_ids:
        lines = [
            f"🚔 Задержан: <b>{game.players[uid].name}</b> — роль <b>{game.players[uid].role}</b>."
            for uid in arrested_ids
        ]
        arrest_text = "\n" + "\n".join(lines)
    else:
        arrest_text = "\n🚔 Этой ночью задержаний не было."

    total_items = len(game.stolen) + len(game.items)
    await announce(
        game,
        f"🌅 <b>НАСТУПИЛО УТРО (Раунд {game.round_no})</b>\n"
        f"{theft_text}{arrest_text}\n\n"
        f"📦 Украдено имущества: <b>{len(game.stolen)}/{total_items}</b>\n"
        f"Осталось на объекте: <b>{len(game.items)}</b>",
    )

    if not game.items:
        await finish_game(game, "🎭 <b>ПОБЕДА КУКОЛ!</b> Они украли всё имущество локации.")
        return

    if not any(p.alive and p.role == ROLE_DOLL for p in game.players.values()):
        await finish_game(game, "🛡 <b>ПОБЕДА ЧОП И ПОСЕТИТЕЛЕЙ!</b> Все куклы задержаны.")
        return

    game.phase = "morning"
    await announce(game, f"💬 Обсуждение в чате: <b>{MORNING_SECONDS} сек.</b>")
    game.timer_task = asyncio.create_task(phase_timer(game, MORNING_SECONDS, "morning"))


async def finish_game(game: Game, text: str):
    cancel_timer(game)
    game.phase = "finished"
    roles_reveal = "\n".join(
        f"• {p.name} — <b>{p.role}</b> ({'в игре' if p.alive else 'задержан'})"
        for p in game.players.values()
    )
    await announce(
        game,
        f"{text}\n\n"
        f"📋 <b>Итоги ролей:</b>\n{roles_reveal}\n\n"
        "Игра завершена. Для новой партии используйте /newgame.",
    )


async def end_morning(game: Game):
    if game.phase != "morning":
        return
    await announce(game, "🌙 <b>Утро закончилось. Наступает новая ночь.</b>")
    await start_night(game)


# =====================================================================
# ОБРАБОТЧИКИ КОМАНД И КНОПОК
# =====================================================================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    if not user:
        return
    conn.execute(
        "INSERT INTO players(user_id, username, first_name, started_private) VALUES(?,?,?,1) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, started_private=1",
        (user.id, user.username, user.first_name),
    )
    conn.commit()
    await message.answer(
        "👋 Добро пожаловать в <b>UNDERGROUND</b>!\n\n"
        "Личка подключена. Здесь ты будешь получать свою роль и ночные действия.\n\n"
        "Добавь меня в игровую группу, выдай права администратора и запусти набор командой /newgame."
    )


@dp.message(Command("newgame"))
async def cmd_newgame(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Команда /newgame работает только в игровой группе.")
        return
    old = GAMES.get(message.chat.id)
    if old and old.phase not in ("finished", "lobby"):
        await message.answer("Сначала закончите текущую игру (/stopgame).")
        return
    if old:
        cancel_timer(old)
    game = Game(chat_id=message.chat.id, chat_title=message.chat.title or "Группа")
    GAMES[message.chat.id] = game
    await update_lobby(game)


@dp.message(Command("stopgame"))
async def cmd_stopgame(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    game = GAMES.get(message.chat.id)
    if not game or game.phase == "finished":
        await message.answer("Активной игры сейчас нет.")
        return
    async with game.lock:
        cancel_timer(game)
        game.phase = "finished"
    await message.answer("🛑 Текущая игра принудительно остановлена. Новый набор: /newgame")


@dp.message(Command("join"))
async def cmd_join(message: Message):
    game = GAMES.get(message.chat.id)
    if not game or game.phase != "lobby":
        await message.answer("Сейчас нет открытого набора. Создать набор: /newgame")
        return
    await add_player(game, message.from_user, message)


async def add_player(game: Game, user, message_or_callback):
    if game.phase != "lobby":
        return
    if user.id in game.players:
        text = "Ты уже в списке участников."
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(text)
        else:
            await message_or_callback.answer(text, show_alert=False)
        return

    if len(game.players) >= MAX_PLAYERS:
        text = f"Достигнут лимит — {MAX_PLAYERS} игроков."
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(text)
        else:
            await message_or_callback.answer(text, show_alert=True)
        return

    game.players[user.id] = Player(user_id=user.id, name=display_name(user))
    text = f"✅ {display_name(user)} присоединился к игре. Игроков: {len(game.players)}."
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text)
    else:
        await message_or_callback.answer("Ты в игре!")
        await announce(game, text)


@dp.callback_query(F.data.startswith("join:"))
async def cb_join(callback: CallbackQuery):
    chat_id = int(callback.data.split(":", 1)[1])
    game = GAMES.get(chat_id)
    if not game or game.phase != "lobby":
        await callback.answer("Набор уже закрыт.", show_alert=True)
        return
    await add_player(game, callback.from_user, callback)


@dp.callback_query(F.data.startswith("leave:"))
async def cb_leave(callback: CallbackQuery):
    chat_id = int(callback.data.split(":", 1)[1])
    game = GAMES.get(chat_id)
    if game and game.phase == "lobby":
        removed = game.players.pop(callback.from_user.id, None)
        await callback.answer("Ты вышел из списка.")
        if removed:
            await announce(
                game,
                f"🚪 {display_name(callback.from_user)} вышел из набора. Участников: {len(game.players)}.",
            )
    else:
        await callback.answer("Сейчас выйти нельзя.", show_alert=True)


@dp.message(Command("startgame"))
async def cmd_startgame(message: Message):
    game = GAMES.get(message.chat.id)
    if not game:
        await message.answer("Сначала откройте набор командой /newgame.")
        return
    await begin_game(game)


@dp.message(Command("status"))
async def cmd_status(message: Message):
    game = GAMES.get(message.chat.id)
    if not game:
        await message.answer("В этой группе сейчас нет игры. Создать: /newgame")
        return
    alive_count = sum(1 for p in game.players.values() if p.alive)
    await message.answer(
        f"📊 <b>Статус игры</b>\n"
        f"Фаза: <b>{game.phase}</b> (Раунд {game.round_no})\n"
        f"Участников: <b>{alive_count}/{len(game.players)}</b>\n"
        f"Локация: <b>{game.location or 'ещё не выбрана'}</b>\n"
        f"Украдено: <b>{len(game.stolen)}</b> | Осталось: <b>{len(game.items)}</b>"
    )


@dp.callback_query(F.data.startswith("gcheck:"))
async def cb_guard_check(callback: CallbackQuery):
    await guard_target_menu(callback, "check")


@dp.callback_query(F.data.startswith("garrest:"))
async def cb_guard_arrest(callback: CallbackQuery):
    await guard_target_menu(callback, "arrest")


async def guard_target_menu(callback: CallbackQuery, kind: str):
    chat_id = int(callback.data.split(":", 1)[1])
    game = GAMES.get(chat_id)
    guard = game.players.get(callback.from_user.id) if game else None
    if not game or game.phase != "night" or not guard or not guard.alive or guard.role != ROLE_GUARD:
        await callback.answer("Действие сейчас недоступно.", show_alert=True)
        return
    if callback.from_user.id in game.guard_actions:
        await callback.answer("Ты уже сделал действие этой ночью.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for p in game.players.values():
        if p.alive and p.user_id != callback.from_user.id:
            kb.button(text=p.name, callback_data=f"gt:{chat_id}:{kind}:{p.user_id}")
    kb.adjust(1)
    if callback.message:
        await callback.message.answer("Выбери игрока:", reply_markup=kb.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("gt:"))
async def cb_guard_target(callback: CallbackQuery):
    _, chat_id_s, kind, target_s = callback.data.split(":", 3)
    chat_id, target_id = int(chat_id_s), int(target_s)
    game = GAMES.get(chat_id)
    guard = game.players.get(callback.from_user.id) if game else None
    target = game.players.get(target_id) if game else None
    if not game or game.phase != "night" or not guard or guard.role != ROLE_GUARD or not guard.alive:
        await callback.answer("Действие недоступно.", show_alert=True)
        return
    if callback.from_user.id in game.guard_actions:
        await callback.answer("Ты уже сделал действие этой ночью.", show_alert=True)
        return
    if not target or not target.alive or target_id == callback.from_user.id:
        await callback.answer("Игрок недоступен.", show_alert=True)
        return
    game.guard_actions[callback.from_user.id] = (kind, target_id)
    if callback.message:
        await callback.message.edit_text("✅ Действие принято. Результат будет утром.")
    await callback.answer()


@dp.callback_query(F.data.startswith("dv:"))
async def cb_doll_vote(callback: CallbackQuery):
    _, chat_id_s, idx_s = callback.data.split(":", 2)
    chat_id, idx = int(chat_id_s), int(idx_s)
    game = GAMES.get(chat_id)
    player = game.players.get(callback.from_user.id) if game else None
    if not game or game.phase != "night" or not player or not player.alive or player.role != ROLE_DOLL:
        await callback.answer("Голосование недоступно.", show_alert=True)
        return
    if not (0 <= idx < len(game.items)):
        await callback.answer("Этот предмет уже недоступен.", show_alert=True)
        return

    item = game.items[idx]
    game.doll_votes[callback.from_user.id] = item
    await callback.answer("Голос принят!")
    if callback.message:
        try:
            await callback.message.edit_text(
                f"✅ Твой голос за предмет «<b>{item}</b>» принят.\n"
                "Можно изменить выбор до конца ночи, нажав /vote в личке."
            )
        except Exception:
            pass


@dp.message(Command("vote"))
async def cmd_vote(message: Message):
    if message.chat.type != "private" or not message.from_user:
        return
    player_id = message.from_user.id
    game = next(
        (
            g for g in GAMES.values()
            if g.phase == "night"
            and player_id in g.players
            and g.players[player_id].alive
            and g.players[player_id].role == ROLE_DOLL
        ),
        None,
    )
    if not game:
        await message.answer("Сейчас у тебя нет активного голосования.")
        return
    kb = InlineKeyboardBuilder()
    for idx, item in enumerate(game.items):
        kb.button(text=f"🧤 {item}", callback_data=f"dv:{game.chat_id}:{idx}")
    kb.adjust(1)
    await message.answer("Выбери имущество:", reply_markup=kb.as_markup())


@dp.message(F.chat.type == "private")
async def private_fallback(message: Message):
    await message.answer(
        "Игровые действия появляются здесь автоматически во время ночи. "
        "Команда /vote позволяет куклам изменить голос."
    )


# =====================================================================
# ВЕБ-СЕРВЕР ДЛЯ RENDER И ТОЧКА ВХОДА
# =====================================================================

async def health_check_handler(request: web.Request) -> web.Response:
    return web.Response(text="UNDERGROUND bot is running", status=200)


async def start_health_server(port: int) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health_check_handler)
    app.router.add_get("/health", health_check_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info("Health-check server listening on 0.0.0.0:%s", port)
    return runner


async def main():
    global BOT
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Добавь токен в переменные окружения.")

    BOT = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    web_runner = await start_health_server(PORT)

    try:
        await BOT.set_my_commands([
            BotCommand(command="start", description="Подключить личку для ролей"),
            BotCommand(command="newgame", description="Открыть набор игроков в группе"),
            BotCommand(command="join", description="Присоединиться к набору"),
            BotCommand(command="startgame", description="Начать игру"),
            BotCommand(command="status", description="Текущий статус партии"),
            BotCommand(command="vote", description="Изменить голос куклы (в личке)"),
            BotCommand(command="stopgame", description="Остановить текущую игру"),
        ])
    except Exception:
        log.warning("Не удалось обновить список команд при старте")

    log.info("UNDERGROUND bot starting")
    retry_delay = 5
    try:
        while True:
            try:
                await BOT.delete_webhook(drop_pending_updates=True)
                await dp.start_polling(BOT, handle_signals=True)
                break
            except TelegramRetryAfter as exc:
                delay = max(float(exc.retry_after), 1.0) + 3.0
                log.warning("Telegram rate limit during startup: waiting %.0f seconds", delay)
                await asyncio.sleep(delay)
            except TelegramUnauthorizedError:
                log.error("BOT_TOKEN rejected by Telegram.")
                raise
            except (TelegramNetworkError, TelegramAPIError) as exc:
                log.warning("Telegram connection error: %s; retrying in %s seconds", exc, retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 120)
    finally:
        await web_runner.cleanup()
        await BOT.session.close()


if __name__ == "__main__":
    asyncio.run(main())
