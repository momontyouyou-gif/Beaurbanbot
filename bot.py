"""UNDERGROUND — Telegram-бот (aiogram 3). Запуск: BOT_TOKEN=... python bot.py"""
import asyncio
import html
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from engine import CHOP, DOLL, MAX_PLAYERS, MIN_PLAYERS, ROLE_TITLES, VISITOR, Game, Phase

NIGHT_SECONDS = 60
MORNING_SECONDS = 60

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()
games: dict[int, Game] = {}
tasks: dict[int, asyncio.Task] = {}
night_done: dict[int, asyncio.Event] = {}
lobby_msgs: dict[int, int] = {}


def esc(s: str) -> str:
    return html.escape(s)


def mention(p) -> str:
    return f'<a href="tg://user?id={p.id}">{esc(p.name)}</a>'


# ---------- лобби ----------
def lobby_text(g: Game) -> str:
    names = "\n".join(f"{i}. {esc(p.name)}" for i, p in enumerate(g.players.values(), 1)) or "—"
    return (f"☢️ <b>UNDERGROUND</b> — набор игроков ({len(g.players)}/{MAX_PLAYERS})\n\n{names}\n\n"
            f"Минимум {MIN_PLAYERS}. Когда все в сборе — /begin")


def lobby_kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Войти", callback_data=f"join:{chat_id}"),
        InlineKeyboardButton(text="➖ Выйти", callback_data=f"leave:{chat_id}"),
    ]])


async def refresh_lobby(bot: Bot, g: Game) -> None:
    mid = lobby_msgs.get(g.chat_id)
    if mid:
        try:
            await bot.edit_message_text(lobby_text(g), chat_id=g.chat_id, message_id=mid,
                                        reply_markup=lobby_kb(g.chat_id))
        except TelegramAPIError:
            pass


@dp.message(Command("start"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(m: Message):
    await m.answer("☢️ <b>UNDERGROUND</b>\nЯ веду подземную мафию. Добавьте меня в группу и напишите "
                   "/newgame. Здесь, в личке, вы получите роль и будете делать ночные действия.\n"
                   "Правила: /rules")


@dp.message(Command("rules"))
async def cmd_rules(m: Message):
    await m.answer(
        "🛡️ <b>ЧОП</b> (всегда 2) — каждую ночь проверяет роль игрока ИЛИ задерживает подозреваемого.\n"
        "🎭 <b>Куклы</b> — голосуют, какое имущество украсть (не больше 1 предмета за ночь).\n"
        "🔦 <b>Посетители</b> — помогают ЧОПу в обсуждении.\n\n"
        "ЧОП и посетители побеждают, если задержаны все куклы. Куклы побеждают, если украдено всё имущество.\n"
        "Ночь — 60 сек, утро — 60 сек.")


@dp.message(Command("newgame"))
async def cmd_newgame(m: Message, bot: Bot):
    if m.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return await m.answer("Игру нужно создавать в группе.")
    if m.chat.id in games and games[m.chat.id].phase != Phase.FINISHED:
        return await m.answer("В этом чате игра уже идёт. /stopgame — остановить.")
    g = Game(m.chat.id, m.from_user.id)
    games[m.chat.id] = g
    msg = await m.answer(lobby_text(g), reply_markup=lobby_kb(m.chat.id))
    lobby_msgs[m.chat.id] = msg.message_id


@dp.callback_query(F.data.startswith(("join:", "leave:")))
async def cb_lobby(cq: CallbackQuery, bot: Bot):
    action, chat_id = cq.data.split(":")
    g = games.get(int(chat_id))
    if not g or g.phase != Phase.LOBBY:
        return await cq.answer("Набор закрыт", show_alert=True)
    u = cq.from_user
    if action == "join":
        try:
            await bot.send_message(u.id, "✅ Вы в игре UNDERGROUND. Роль придёт сюда после старта.")
        except TelegramForbiddenError:
            me = await bot.get_me()
            return await cq.answer(f"Сначала откройте @{me.username} и нажмите Start, потом войдите снова.",
                                   show_alert=True)
        g.add_player(u.id, u.full_name)
    else:
        g.remove_player(u.id)
    await cq.answer()
    await refresh_lobby(bot, g)


@dp.message(Command("stopgame"))
async def cmd_stop(m: Message):
    g = games.get(m.chat.id)
    if not g or g.phase == Phase.FINISHED:
        return await m.answer("Нет активной игры.")
    if m.from_user.id != g.creator_id:
        return await m.answer("Остановить игру может только её создатель.")
    cleanup(m.chat.id)
    await m.answer("🛑 Игра остановлена.")


def cleanup(chat_id: int) -> None:
    t = tasks.pop(chat_id, None)
    if t and t is not asyncio.current_task():
        t.cancel()
    g = games.get(chat_id)
    if g:
        g.finish()
    night_done.pop(chat_id, None)


@dp.message(Command("begin"))
async def cmd_begin(m: Message, bot: Bot):
    g = games.get(m.chat.id)
    if not g or g.phase != Phase.LOBBY:
        return await m.answer("Сначала /newgame.")
    if m.from_user.id not in g.players:
        return await m.answer("Сначала войдите в игру кнопкой.")
    if len(g.players) < MIN_PLAYERS:
        return await m.answer(f"Нужно минимум {MIN_PLAYERS} игрока.")
    g.start()
    # раздача ролей в личку
    failed = []
    dolls = [p for p in g.players.values() if p.role == DOLL]
    for p in g.players.values():
        text = f"🎲 Ваша роль: <b>{ROLE_TITLES[p.role]}</b>\nОбъект: <b>{esc(g.location['name'])}</b>"
        if p.role == DOLL and len(dolls) > 1:
            text += "\nВаши сообщники: " + ", ".join(esc(d.name) for d in dolls if d.id != p.id)
        try:
            await bot.send_message(p.id, text)
        except TelegramAPIError:
            failed.append(p.name)
    if failed:
        g.finish()
        return await m.answer("Не могу написать в личку: " + ", ".join(map(esc, failed)) + ". Нажмите Start у бота "
                              "и начните заново (/newgame).")
    await m.answer(f"📍 Объект: <b>{esc(g.location['name'])}</b>\n{esc(g.location['intro'])}\n"
                   f"📦 Имущество: {len(g.items_left)} предметов.\nРоли разосланы в личку!")
    tasks[m.chat.id] = asyncio.create_task(run_game(bot, g))


# ---------- игровой цикл ----------
def chop_kb(g: Game, uid: int) -> InlineKeyboardMarkup:
    rows = []
    for p in g.alive():
        if p.id == uid:
            continue
        rows.append([InlineKeyboardButton(text=f"🔍 {p.name}", callback_data=f"c:{g.chat_id}:{p.id}"),
                     InlineKeyboardButton(text=f"⛓ {p.name}", callback_data=f"d:{g.chat_id}:{p.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def doll_kb(g: Game) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📦 {it}", callback_data=f"v:{g.chat_id}:{i}")]
            for i, it in enumerate(g.items_left)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def safe_send(bot: Bot, chat_id: int, text: str, **kw):
    try:
        return await bot.send_message(chat_id, text, **kw)
    except TelegramAPIError as e:
        logging.warning("send failed to %s: %s", chat_id, e)


async def run_game(bot: Bot, g: Game) -> None:
    try:
        while True:
            g.begin_night()
            ev = night_done[g.chat_id] = asyncio.Event()
            await safe_send(bot, g.chat_id,
                            f"🌑 Ночь {g.night_no}. На объекте «{esc(g.location['name'])}» объявлена тревога!\n"
                            f"Охрана проверяет объект. Посетители укрываются в бомбоубежище.\n"
                            f"У вас {NIGHT_SECONDS} секунд.")
            for p in g.alive(CHOP):
                await safe_send(bot, p.id, "🛡️ Ночь. Проверить роль (🔍) или задержать (⛓)? Выбор один и окончательный.",
                                reply_markup=chop_kb(g, p.id))
            for p in g.alive(DOLL):
                await safe_send(bot, p.id, "🎭 Ночь. Что выносим? Голос можно менять до конца ночи.",
                                reply_markup=doll_kb(g))
            try:
                await asyncio.wait_for(ev.wait(), NIGHT_SECONDS)
            except asyncio.TimeoutError:
                pass
            res = g.resolve_night()

            lines = ["☀️ <b>Наступило утро!</b>"]
            lines.append(f"🎭 За ночь куклы украли: <b>{esc(res.stolen)}</b>." if res.stolen
                         else "🎭 Куклы ничего не украли.")
            if res.detained:
                for p in res.detained:
                    lines.append(f"🛡️ ЧОП задержал {mention(p)} — это был(а) <b>{ROLE_TITLES[p.role]}</b>!")
            else:
                lines.append("🛡️ Никто не задержан.")
            lines.append(f"📦 Осталось имущества: <b>{len(g.items_left)} из {g.total_items()}</b>.")

            if res.winner:
                await safe_send(bot, g.chat_id, "\n".join(lines))
                await announce_winner(bot, g, res.winner)
                break
            lines.append(f"\nНачинается обсуждение. У вас {MORNING_SECONDS} секунд.")
            await safe_send(bot, g.chat_id, "\n".join(lines))
            await asyncio.sleep(MORNING_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("game crashed")
        await safe_send(bot, g.chat_id, "⚠️ Ошибка в игре, партия остановлена.")
    finally:
        cleanup(g.chat_id)


async def announce_winner(bot: Bot, g: Game, winner: str) -> None:
    if winner == CHOP:
        head = "🛡️🔦 <b>Победа ЧОПа и посетителей!</b> Все куклы задержаны."
    else:
        head = "🎭 <b>Победа кукол!</b> " + ("Имущество вынесено полностью." if not g.items_left
                                             else "Охрана выведена из строя.")
    roles = "\n".join(f"{ROLE_TITLES[p.role]}: {esc(p.name)}" for p in
                      sorted(g.players.values(), key=lambda p: p.role))
    await safe_send(bot, g.chat_id, f"{head}\n\n<b>Роли:</b>\n{roles}")


# ---------- ночные действия в личке ----------
@dp.callback_query(F.data.regexp(r"^[cd]:-?\d+:\d+$"))
async def cb_chop(cq: CallbackQuery, bot: Bot):
    kind, chat_id, target = cq.data.split(":")
    g = games.get(int(chat_id))
    if not g:
        return await cq.answer("Игра не найдена", show_alert=True)
    try:
        role = g.chop_act(cq.from_user.id, "check" if kind == "c" else "detain", int(target))
    except ValueError as e:
        return await cq.answer(str(e), show_alert=True)
    t = g.players[int(target)]
    await cq.message.edit_reply_markup(reply_markup=None)
    if kind == "c":
        await cq.message.answer(f"🔍 {esc(t.name)} — <b>{ROLE_TITLES[role]}</b>.")
    else:
        await cq.message.answer(f"⛓ Вы задерживаете: {esc(t.name)}. Решится утром.")
    await cq.answer()
    check_complete(g)


@dp.callback_query(F.data.regexp(r"^v:-?\d+:\d+$"))
async def cb_doll(cq: CallbackQuery):
    _, chat_id, idx = cq.data.split(":")
    g = games.get(int(chat_id))
    if not g:
        return await cq.answer("Игра не найдена", show_alert=True)
    try:
        item = g.items_left[int(idx)]
        g.doll_vote(cq.from_user.id, item)
    except (ValueError, IndexError) as e:
        return await cq.answer(str(e) or "Нельзя", show_alert=True)
    await cq.answer(f"Голос за: {item}")
    check_complete(g)


def check_complete(g: Game) -> None:
    if g.night_complete() and g.chat_id in night_done:
        night_done[g.chat_id].set()


async def main() -> None:
    token = os.environ["BOT_TOKEN"].strip()
    port = int(os.getenv("PORT", "8000"))

    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()

    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML"))

    while True:
        try:
            await dp.start_polling(bot, handle_signals=True)
            break
        except TelegramRetryAfter as e:
            wait_sec = int(e.retry_after) + 2
            logging.warning("Лимит Telegram при старте, ждём %s сек...", wait_sec)
            await asyncio.sleep(wait_sec)
        except (TelegramNetworkError, TelegramAPIError) as e:
            logging.warning("Ошибка сети (%s), повтор через 5 сек...", e)
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
