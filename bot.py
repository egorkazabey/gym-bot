"""Telegram-бот: трекер тренировок.

Пишешь «жим 80x8x3», бот сохраняет, сравнивает с прошлым разом, ловит рекорды,
рисует графики прогресса.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import re
from collections import OrderedDict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
from html import escape

from charts import progress_chart
from db import DB
from parser import ParseError, e1rm, fmt_sets, fmt_w, normalize_name, parse_message

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Prague"))
DB_PATH = os.getenv("DB_PATH", "data/gym.db")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x}

db = DB(DB_PATH)
router = Router()
log = logging.getLogger("gymbot")

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

HELP = """<b>Как записывать</b>
<code>жим лежа 80x8x3</code>  3 подхода по 8 с 80 кг
<code>жим 80x8,8,7</code>  разные повторы по подходам
<code>жим 80x8x2 85x6</code>  несколько групп
<code>подтягивания x10x3</code>  свой вес (или <code>бв10x3</code>)
<code>подтягивания +10x8x3</code>  с доп. весом
Можно несколько упражнений в одном сообщении, каждое с новой строки.
Забыл записать вовремя? Первой строкой напиши <code>вчера</code> или дату <code>28.09</code>.

<b>Команды</b>
/today  тренировка за сегодня
/week  сводка за неделю
/last жим  что было в прошлый раз
/history жим  последние 10 тренировок
/progress жим  график прогресса
/pr  личные рекорды
/list  все упражнения
/alias жим = жим лежа  склеить названия
/undo  удалить последнюю запись
/export  выгрузка в CSV"""


# ---------- доступ ----------
class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if ALLOWED and (user is None or user.id not in ALLOWED):
            return None
        return await handler(event, data)


# ---------- утилиты ----------
def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def fmt_day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"


def title(name: str) -> str:
    return escape(name[:1].upper() + name[1:])


DATE_LINE = re.compile(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?$")


def extract_date(text: str) -> tuple[date | None, str]:
    """Если первая строка «вчера» / «позавчера» / «28.09», возвращает эту дату и остальной текст."""
    lines = text.strip().splitlines()
    first = lines[0].strip().lower().rstrip(":")
    today = now().date()
    if first == "вчера":
        return today - timedelta(days=1), "\n".join(lines[1:])
    if first == "позавчера":
        return today - timedelta(days=2), "\n".join(lines[1:])
    m = DATE_LINE.match(first)
    if m:
        d, mth, y = int(m[1]), int(m[2]), m[3]
        year = today.year if not y else (int(y) + 2000 if len(y) == 2 else int(y))
        try:
            dt = date(year, mth, d)
        except ValueError:
            return None, text
        if dt > today and not y:  # «28.12», написанное в январе, значит прошлый год
            dt = date(year - 1, mth, d)
        return dt, "\n".join(lines[1:])
    return None, text


def find_exercise(user_id: int, query: str) -> tuple[str | None, list[str]]:
    """Ищет упражнение по запросу: точное имя, алиас, потом подстрока.
    Возвращает (найденное, кандидаты)."""
    q = normalize_name(query)
    names = [e for e, _, _ in db.exercises(user_id)]
    resolved = db.resolve(user_id, q)
    if resolved in names:
        return resolved, []
    cands = [n for n in names if q in n]
    if not cands:
        words = q.split()
        cands = [n for n in names if all(w in n for w in words)]
    if len(cands) == 1:
        return cands[0], []
    return None, cands


def pick_keyboard(action: str, names: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for n in names[:20]:
        data = f"{action}:{n}"
        if len(data.encode()) > 64:  # лимит Telegram на callback_data
            continue
        rows.append([InlineKeyboardButton(text=n, callback_data=data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def with_exercise(message: Message, cmd: CommandObject, action: str, run):
    """Общая логика для /last, /history, /progress: выбор упражнения по аргументу или кнопкой."""
    uid = message.from_user.id
    if not cmd.args:
        names = [e for e, _, _ in db.exercises(uid)]
        if not names:
            await message.answer("Пока пусто. Запиши первую тренировку, например: <code>жим 80x8x3</code>")
            return
        await message.answer("Какое упражнение?", reply_markup=pick_keyboard(action, names))
        return
    found, cands = find_exercise(uid, cmd.args)
    if found:
        await run(message, uid, found)
    elif cands:
        await message.answer("Уточни:", reply_markup=pick_keyboard(action, cands))
    else:
        await message.answer(f"Не нашёл «{escape(cmd.args)}». Список: /list")


# ---------- запись тренировки ----------
@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    uid = message.from_user.id
    when_date, body = extract_date(message.text)
    try:
        parsed = parse_message(body)
    except ParseError as e:
        await message.answer(f"⚠️ {escape(str(e))}\n\nФорматы: /help")
        return
    if not parsed:
        await message.answer("Не вижу подходов. Пример: <code>жим 80x8x3</code>\nВсе форматы: /help")
        return

    ts = now()
    if when_date:
        ts = datetime.combine(when_date, ts.time())
    day = ts.date()
    known = {e for e, _, _ in db.exercises(uid)}

    # склеиваем одинаковые упражнения внутри сообщения
    items: OrderedDict[str, list[tuple[float, int]]] = OrderedDict()
    for p in parsed:
        items.setdefault(db.resolve(uid, p.exercise), []).extend(p.sets)

    # до сохранения считаем прошлые рекорды и прошлую тренировку
    report = []
    for ex, sets in items.items():
        history = db.sessions(uid, ex)
        prev_sets = [s for sess in history for s in sess.sets if sess.day <= day]
        before = [sess for sess in history if sess.day < day]

        lines = [f"✅ <b>{title(ex)}</b>: {fmt_sets(sets)}"]
        vol = sum(w * r for w, r in sets)
        best_e = max(e1rm(w, r) for w, r in sets)
        stats = []
        if vol:
            stats.append(f"тоннаж {vol:g} кг")
        if best_e:
            stats.append(f"1ПМ ≈ {best_e:.0f} кг")
        if stats:
            lines.append("   " + " · ".join(stats))
        if before:
            last = before[-1]
            lines.append(f"   прошлый раз {last.day:%d.%m}: {fmt_sets(last.sets)}")

        if ex not in known:
            similar = [k for k in known if ex in k or k in ex]
            if similar:
                lines.append(
                    f"   🆕 новое упражнение. Если это «{escape(similar[0])}», напиши "
                    f"<code>/alias {escape(ex)} = {escape(similar[0])}</code>"
                )
            else:
                lines.append("   🆕 новое упражнение")
        elif prev_sets:
            top = max(w for w, _ in sets)
            old_top = max(w for w, _ in prev_sets)
            if top > 0 and top > old_top:
                lines.append(f"   🏆 рекорд веса! было {fmt_w(old_top)} кг")
            else:
                old_e = max(e1rm(w, r) for w, r in prev_sets)
                if best_e and best_e > old_e + 0.05:
                    lines.append(f"   🏆 рекорд 1ПМ! было ≈ {old_e:.0f} кг")
                elif top == 0 and old_top == 0:
                    mr, old_mr = max(r for _, r in sets), max(r for _, r in prev_sets)
                    if mr > old_mr:
                        lines.append(f"   🏆 рекорд повторов! было {old_mr}")
        report.append("\n".join(lines))

    entry_id = db.add_entry(uid, message.text, ts, list(items.items()))
    header = f"📅 {fmt_day(day)}\n" if when_date else ""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="↩️ Отменить", callback_data=f"undo:{entry_id}")]]
    )
    await message.answer(header + "\n\n".join(report), reply_markup=kb)


@router.callback_query(F.data.startswith("undo:"))
async def cb_undo(call: CallbackQuery):
    entry_id = int(call.data.split(":", 1)[1])
    ok = db.delete_entry(call.from_user.id, entry_id)
    await call.answer("Удалено" if ok else "Уже удалено")
    if ok:
        await call.message.edit_text(call.message.html_text + "\n\n<s>запись удалена</s>")


@router.message(Command("undo"))
async def cmd_undo(message: Message):
    last = db.last_entry(message.from_user.id)
    if not last:
        await message.answer("Нечего удалять.")
        return
    db.delete_entry(message.from_user.id, last[0])
    await message.answer(f"Удалил последнюю запись:\n<code>{escape(last[1])}</code>")


# ---------- просмотр ----------
@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message):
    await message.answer("Привет! Я записываю тренировки.\n\n" + HELP)


def render_days(rows) -> str:
    by_day: OrderedDict[str, OrderedDict[str, list]] = OrderedDict()
    for d, ex, w, r in rows:
        by_day.setdefault(d, OrderedDict()).setdefault(ex, []).append((w, r))
    out = []
    for d, exs in by_day.items():
        dd = date.fromisoformat(d)
        tonnage = sum(w * r for sets in exs.values() for w, r in sets)
        n_sets = sum(len(s) for s in exs.values())
        block = [f"<b>{fmt_day(dd)}</b>  ·  {n_sets} подх." + (f" · {tonnage:g} кг" if tonnage else "")]
        block += [f"• {title(ex)}: {fmt_sets(sets)}" for ex, sets in exs.items()]
        out.append("\n".join(block))
    return "\n\n".join(out)


@router.message(Command("today"))
async def cmd_today(message: Message):
    today = now().date()
    rows = db.day_sets(message.from_user.id, today, today)
    await message.answer(render_days(rows) if rows else "Сегодня ещё ничего не записано.")


@router.message(Command("week"))
async def cmd_week(message: Message):
    today = now().date()
    start = today - timedelta(days=today.weekday())
    rows = db.day_sets(message.from_user.id, start, today)
    if not rows:
        await message.answer("На этой неделе тренировок пока нет.")
        return
    days = len({r[0] for r in rows})
    tonnage = sum(w * r for _, _, w, r in rows)
    prev = db.day_sets(message.from_user.id, start - timedelta(days=7), today - timedelta(days=7))
    prev_t = sum(w * r for _, _, w, r in prev)
    cmp = ""
    if prev_t:
        diff = (tonnage - prev_t) / prev_t * 100
        cmp = f" ({diff:+.0f}% к прошлой неделе на тот же день)"
    head = f"<b>Неделя с {start:%d.%m}</b>: {days} трен., тоннаж {tonnage:g} кг{cmp}\n\n"
    await message.answer(head + render_days(rows))


async def show_last(message: Message, uid: int, ex: str):
    sessions = db.sessions(uid, ex)
    s = sessions[-1]
    await message.answer(f"<b>{title(ex)}</b>, {fmt_day(s.day)}:\n{fmt_sets(s.sets)}")


async def show_history(message: Message, uid: int, ex: str):
    sessions = db.sessions(uid, ex)[-10:]
    lines = [f"<b>{title(ex)}</b>, последние {len(sessions)}:"]
    for s in reversed(sessions):
        extra = f"  ({s.volume:g} кг)" if s.volume else ""
        lines.append(f"{fmt_day(s.day)}: {fmt_sets(s.sets)}{extra}")
    await message.answer("\n".join(lines))


async def show_progress(message: Message, uid: int, ex: str):
    sessions = db.sessions(uid, ex)
    if len(sessions) < 2:
        await message.answer(f"По «{escape(ex)}» пока одна тренировка, график будет со второй.")
        return
    png = await asyncio.to_thread(progress_chart, ex, sessions)
    first, last = sessions[0], sessions[-1]
    if last.top_weight > 0:
        e0 = max(e1rm(w, r) for w, r in first.sets)
        e1 = max(e1rm(w, r) for w, r in last.sets)
        cap = (f"<b>{title(ex)}</b>: {len(sessions)} трен. с {first.day:%d.%m.%y}\n"
               f"Рабочий вес {fmt_w(first.top_weight)} → {fmt_w(last.top_weight)} кг")
        if e0 > 0:
            cap += f", 1ПМ ≈ {e0:.0f} → {e1:.0f} кг"
    else:
        cap = (f"<b>{title(ex)}</b>: {len(sessions)} трен. с {first.day:%d.%m.%y}\n"
               f"Лучший подход {first.max_reps} → {last.max_reps} повт.")
    await message.answer_photo(BufferedInputFile(png, "progress.png"), caption=cap)


@router.message(Command("last"))
async def cmd_last(message: Message, command: CommandObject):
    await with_exercise(message, command, "last", show_last)


@router.message(Command("history"))
async def cmd_history(message: Message, command: CommandObject):
    await with_exercise(message, command, "hist", show_history)


@router.message(Command("progress"))
async def cmd_progress(message: Message, command: CommandObject):
    await with_exercise(message, command, "prog", show_progress)


@router.callback_query(F.data.regexp(r"^(last|hist|prog):"))
async def cb_pick(call: CallbackQuery):
    action, ex = call.data.split(":", 1)
    run = {"last": show_last, "hist": show_history, "prog": show_progress}[action]
    await call.answer()
    await run(call.message, call.from_user.id, ex)


@router.message(Command("pr"))
async def cmd_pr(message: Message):
    uid = message.from_user.id
    lines = ["<b>Личные рекорды</b>"]
    for ex, _, _ in db.exercises(uid):
        sessions = db.sessions(uid, ex)
        all_sets = [(s.day, w, r) for s in sessions for w, r in s.sets]
        d, w, r = max(all_sets, key=lambda t: (t[1], t[2]))
        if w > 0:
            ed, ew, er = max(all_sets, key=lambda t: e1rm(t[1], t[2]))
            lines.append(
                f"• {title(ex)}: <b>{fmt_w(w)}×{r}</b> ({d:%d.%m}), "
                f"1ПМ ≈ {e1rm(ew, er):.0f} кг ({fmt_w(ew)}×{er})"
            )
        else:
            d, _, r = max(all_sets, key=lambda t: t[2])
            lines.append(f"• {title(ex)}: <b>{r} повт.</b> ({d:%d.%m})")
    await message.answer("\n".join(lines) if len(lines) > 1 else "Рекордов пока нет.")


@router.message(Command("list"))
async def cmd_list(message: Message):
    rows = db.exercises(message.from_user.id)
    if not rows:
        await message.answer("Пока пусто.")
        return
    lines = ["<b>Упражнения</b> (трен. · последняя)"]
    lines += [f"• {title(ex)}: {n} · {date.fromisoformat(d):%d.%m}" for ex, n, d in rows]
    aliases = db.list_aliases(message.from_user.id)
    if aliases:
        lines.append("\n<b>Алиасы</b>")
        lines += [f"{escape(a)} → {escape(e)}" for a, e in aliases]
    await message.answer("\n".join(lines))


@router.message(Command("alias"))
async def cmd_alias(message: Message, command: CommandObject):
    if not command.args or "=" not in command.args:
        await message.answer("Формат: <code>/alias жим = жим лежа</code>\n"
                             "Слева короткое название, справа основное. Старые записи тоже перенесутся.")
        return
    left, right = (normalize_name(x) for x in command.args.split("=", 1))
    if not left or not right or left == right:
        await message.answer("Нужны два разных названия.")
        return
    moved = db.set_alias(message.from_user.id, left, right)
    extra = f" Перенёс {moved} подх." if moved else ""
    await message.answer(f"Теперь «{escape(left)}» = «{escape(right)}».{extra}")


@router.message(Command("export"))
async def cmd_export(message: Message):
    rows = db.all_sets(message.from_user.id)
    if not rows:
        await message.answer("Нечего выгружать.")
        return
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "exercise", "weight_kg", "reps"])
    w.writerows(rows)
    data = buf.getvalue().encode("utf-8-sig")  # BOM, чтобы Excel открыл кириллицу
    await message.answer_document(BufferedInputFile(data, f"gym_{now():%Y%m%d}.csv"))


# ---------- запуск ----------
async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.outer_middleware(AccessMiddleware())
    dp.callback_query.outer_middleware(AccessMiddleware())
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="today", description="Тренировка за сегодня"),
        BotCommand(command="week", description="Сводка за неделю"),
        BotCommand(command="progress", description="График прогресса"),
        BotCommand(command="last", description="Что было в прошлый раз"),
        BotCommand(command="history", description="История упражнения"),
        BotCommand(command="pr", description="Личные рекорды"),
        BotCommand(command="list", description="Все упражнения"),
        BotCommand(command="undo", description="Удалить последнюю запись"),
        BotCommand(command="export", description="Выгрузка в CSV"),
        BotCommand(command="help", description="Как записывать"),
    ])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
