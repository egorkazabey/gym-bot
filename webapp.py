"""Бэкенд мини-аппа: отдаёт webapp/index.html и JSON API.

Каждый запрос к API несёт initData от Telegram в заголовке X-Init-Data. Подпись
проверяется токеном бота, так что user_id берётся оттуда, подделать его нельзя.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

from db import DB
from parser import ParseError, e1rm, fmt_sets

STATIC = Path(__file__).parent / "webapp"
INIT_DATA_TTL = 24 * 3600  # initData старше суток не принимаем
ACTIVITY_WEEKS = 18

log = logging.getLogger("gymbot.web")


def create_app(
    db: DB,
    token: str,
    allowed: set[int],
    now: Callable[[], datetime],
    record: Callable[[int, str], tuple[int, str] | None],
) -> web.Application:

    @web.middleware
    async def auth(request: web.Request, handler):
        if not request.path.startswith("/api/"):
            return await handler(request)
        raw = request.headers.get("X-Init-Data", "")
        try:
            data = safe_parse_webapp_init_data(token, raw)
        except ValueError:
            return web.json_response({"error": "Открой приложение из Telegram"}, status=401)
        if data.user is None or time.time() - data.auth_date.timestamp() > INIT_DATA_TTL:
            return web.json_response({"error": "Сессия устарела, перезапусти приложение"}, status=401)
        if allowed and data.user.id not in allowed:
            return web.json_response({"error": "Нет доступа"}, status=403)
        request["uid"] = data.user.id
        return await handler(request)

    async def index(_: web.Request):
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def summary(request: web.Request):
        uid = request["uid"]
        today = now().date()
        week_start = today - timedelta(days=today.weekday())
        act_start = week_start - timedelta(weeks=ACTIVITY_WEEKS - 1)

        rows = db.all_sets(uid)
        by_day: OrderedDict[str, OrderedDict[str, list]] = OrderedDict()
        for d, ex, w, r in rows:
            by_day.setdefault(d, OrderedDict()).setdefault(ex, []).append((w, r))

        def day_stats(d: str) -> dict:
            sets = [s for ss in by_day[d].values() for s in ss]
            return {"day": d, "sets": len(sets), "tonnage": sum(w * r for w, r in sets)}

        activity = [day_stats(d) for d in by_day if d >= act_start.isoformat()]

        def week_totals(start: date, end: date) -> dict:
            days = [d for d in by_day if start.isoformat() <= d <= end.isoformat()]
            stats = [day_stats(d) for d in days]
            return {"days": len(days), "sets": sum(s["sets"] for s in stats),
                    "tonnage": sum(s["tonnage"] for s in stats)}

        recent = []
        for d in list(by_day)[-10:][::-1]:
            recent.append({
                **day_stats(d),
                "exercises": [{"name": ex, "text": fmt_sets(sets)} for ex, sets in by_day[d].items()],
            })

        exercises = []
        for ex, n, last in db.exercises(uid):
            all_s = [(s.day, w, r) for s in db.sessions(uid, ex) for w, r in s.sets]
            bd, bw, br = max(all_s, key=lambda t: (t[1], t[2]))
            item = {"name": ex, "sessions": n, "last": last, "weighted": bw > 0}
            if bw > 0:
                ed, ew, er = max(all_s, key=lambda t: e1rm(t[1], t[2]))
                item["best"] = {"weight": bw, "reps": br, "day": bd.isoformat()}
                item["e1rm"] = round(e1rm(ew, er), 1)
            else:
                rd, _, rr = max(all_s, key=lambda t: t[2])
                item["best"] = {"weight": 0, "reps": rr, "day": rd.isoformat()}
            exercises.append(item)

        return web.json_response({
            "today": today.isoformat(),
            "week": week_totals(week_start, today),
            "prev_week": week_totals(week_start - timedelta(days=7), today - timedelta(days=7)),
            "activity_start": act_start.isoformat(),
            "activity": activity,
            "recent": recent,
            "exercises": exercises,
        })

    async def exercise(request: web.Request):
        uid = request["uid"]
        name = request.query.get("name", "")
        sessions = db.sessions(uid, name)
        if not sessions:
            return web.json_response({"error": "Нет такого упражнения"}, status=404)
        return web.json_response({
            "name": name,
            "weighted": any(s.top_weight > 0 for s in sessions),
            "sessions": [{
                "day": s.day.isoformat(),
                "text": fmt_sets(s.sets),
                "top": s.top_weight,
                "e1rm": round(max(e1rm(w, r) for w, r in s.sets), 1),
                "volume": s.volume,
                "max_reps": s.max_reps,
                "total_reps": s.total_reps,
            } for s in sessions],
        })

    async def log_workout(request: web.Request):
        uid = request["uid"]
        try:
            body = await request.json()
            text = str(body.get("text", "")).strip()
        except (ValueError, AttributeError):
            return web.json_response({"error": "Плохой запрос"}, status=400)
        if not text or len(text) > 4000:
            return web.json_response({"error": "Пустая запись"}, status=400)
        try:
            result = record(uid, text)
        except ParseError as e:
            return web.json_response({"error": str(e)}, status=422)
        if not result:
            return web.json_response({"error": "Не вижу подходов. Пример: жим 80x8x3"}, status=422)
        entry_id, report = result
        return web.json_response({"entry_id": entry_id, "report": report})

    async def undo(request: web.Request):
        uid = request["uid"]
        try:
            entry_id = int((await request.json())["entry_id"])
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "Плохой запрос"}, status=400)
        return web.json_response({"ok": db.delete_entry(uid, entry_id)})

    app = web.Application(middlewares=[auth])
    app.router.add_get("/", index)
    app.router.add_get("/api/summary", summary)
    app.router.add_get("/api/exercise", exercise)
    app.router.add_post("/api/log", log_workout)
    app.router.add_post("/api/undo", undo)
    return app


async def start_server(app: web.Application, port: int) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Мини-апп слушает порт %s", port)
    return runner
