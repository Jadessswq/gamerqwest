"""
FastAPI + WebSocket бэкенд для игры «Правда или Действие» (онлайн, 18+, 2 игрока).

Сценарий хода:
  1. Активный игрок выбирает «Правда» или «Действие» → сервер отдаёт случайный вопрос/задание.
  2. У игрока 3 кнопки:
       • «Выполнить»     → +1 в счёт «выполнено», ход переходит партнёру.
       • «Отказаться»    → +1 в счёт «отказался», ход переходит партнёру.
       • «Переключиться» → сервер достаёт вопрос противоположного типа и показывает его.
                           Можно переключаться туда-обратно сколько угодно — пока игрок
                           не нажмёт «Выполнить» или «Отказаться». Один и тот же
                           вопрос «Правды» и «Действия» сохраняется в рамках хода
                           (повторный switch возвращает к нему, не перегенерируя).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import string
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from questions import QUESTIONS, get_question

app = FastAPI()


# ---------------------------------------------------------------------------
# fly.io routing: если комната живёт на другой машине, отдаём fly-replay,
# чтобы прокси повторил запрос на нужном инстансе.
# ---------------------------------------------------------------------------
class FlyReplayMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path: str = scope.get("path", "")
            method: str = scope.get("method", "")
            headers = scope.get("headers", [])
            replay_src = None
            for k, v in headers:
                if k == b"fly-replay-src":
                    replay_src = v.decode()
                    break

            code: str | None = None
            if scope["type"] == "http" and method == "GET" and path.startswith("/api/rooms/"):
                code = path[len("/api/rooms/"):].upper()
            elif scope["type"] == "websocket" and path.startswith("/ws/"):
                code = path[len("/ws/"):].upper()

            if (
                code
                and code not in ROOMS
                and not replay_src
                # Чтобы не пытаться replay в бесконечности при 1 машине
                and os.environ.get("FLY_MACHINE_ID")
            ):
                if scope["type"] == "http":
                    await send({
                        "type": "http.response.start",
                        "status": 204,
                        "headers": [
                            (b"fly-replay", b"elsewhere=true"),
                            (b"content-length", b"0"),
                        ],
                    })
                    await send({"type": "http.response.body", "body": b""})
                else:
                    await send({
                        "type": "websocket.http.response.start",
                        "status": 503,
                        "headers": [(b"fly-replay", b"elsewhere=true")],
                    })
                    await send({"type": "websocket.http.response.body", "body": b""})
                return

        await self.app(scope, receive, send)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(FlyReplayMiddleware)


# ---------------------------------------------------------------------------
# Модели состояния
# ---------------------------------------------------------------------------

@dataclass
class Player:
    pid: str
    name: str
    ws: WebSocket | None = None
    connected: bool = False
    score_done: int = 0
    score_refused: int = 0


@dataclass
class Room:
    code: str
    level: str = "medium"
    players: list[Player] = field(default_factory=list)
    current_turn_pid: str | None = None  # чей сейчас ход
    phase: str = "lobby"  # lobby | choosing | showing | finished

    # Оригинальный выбор хода
    original_kind: str | None = None
    original_text: str | None = None
    original_idx: int | None = None

    # Альтернативный (после switch) — может быть None, если игрок ещё не переключался
    alternate_kind: str | None = None
    alternate_text: str | None = None
    alternate_idx: int | None = None

    # Показывается ли сейчас альтернативный вопрос
    showing_alternate: bool = False

    # Прогрессия по тирам (скрытая от UI):
    #   turn_index   — сколько завершённых ходов было (Execute / Refuse).
    #   t1_end       — после какого хода переходим из tier1 (прелюдии) в tier2.
    #   t2_end       — после какого хода переходим из tier2 в tier3 (горячие).
    # Пороги задаются один раз при создании комнаты.
    turn_index: int = 0
    t1_end: int = 8
    t2_end: int = 16

    used: dict[str, dict[str, list[int]]] = field(
        default_factory=lambda: {lvl: {"truth": [], "dare": []} for lvl in QUESTIONS}
    )
    last_event: str = ""
    last_event_pid: str | None = None
    last_activity: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def current_kind(self) -> str | None:
        if self.phase != "showing":
            return None
        return self.alternate_kind if self.showing_alternate else self.original_kind

    @property
    def current_text(self) -> str | None:
        if self.phase != "showing":
            return None
        return self.alternate_text if self.showing_alternate else self.original_text

    def public_state(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "level": self.level,
            "phase": self.phase,
            "current_turn_pid": self.current_turn_pid,
            "current_kind": self.current_kind,
            "current_text": self.current_text,
            "original_kind": self.original_kind,
            "showing_alternate": self.showing_alternate,
            "last_event": self.last_event,
            "last_event_pid": self.last_event_pid,
            "players": [
                {
                    "pid": p.pid,
                    "name": p.name,
                    "connected": p.connected,
                    "score_done": p.score_done,
                    "score_refused": p.score_refused,
                }
                for p in self.players
            ],
        }


ROOMS: dict[str, Room] = {}


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def gen_room_code() -> str:
    """6-значный буквенно-цифровой код, без похожих символов."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(random.choices(alphabet, k=6))
        if code not in ROOMS:
            return code


def gen_pid() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=12))


async def broadcast(room: Room) -> None:
    state = room.public_state()
    payload = json.dumps({"type": "state", "state": state}, ensure_ascii=False)
    for p in room.players:
        if p.ws is not None and p.connected:
            try:
                await p.ws.send_text(payload)
            except Exception:
                p.connected = False


def other_player(room: Room, pid: str) -> Player | None:
    for p in room.players:
        if p.pid != pid:
            return p
    return None


# ---------------------------------------------------------------------------
# REST: создать / проверить комнату
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/rooms")
async def create_room(payload: dict[str, Any] | None = None) -> dict[str, str]:
    level = "medium"
    if payload and isinstance(payload.get("level"), str) and payload["level"] in QUESTIONS:
        level = payload["level"]
    code = gen_room_code()
    # Случайные пороги перехода между скрытыми тирами:
    #   1-я подгруппа (прелюдии): 4-6 ходов
    #   горячие начинаются с хода 9-12
    t1_end = random.randint(4, 6)
    t2_end = random.randint(9, 12)
    if t2_end <= t1_end:
        t2_end = t1_end + 3
    ROOMS[code] = Room(code=code, level=level, t1_end=t1_end, t2_end=t2_end)
    return {"code": code, "level": level}


@app.get("/api/rooms/{code}")
async def get_room(code: str) -> JSONResponse:
    code = code.upper()
    room = ROOMS.get(code)
    if room is None:
        return JSONResponse({"exists": False}, status_code=404)
    return JSONResponse(
        {
            "exists": True,
            "players": len(room.players),
            "phase": room.phase,
            "level": room.level,
        }
    )


# ---------------------------------------------------------------------------
# WebSocket: игровая сессия
# ---------------------------------------------------------------------------

@app.websocket("/ws/{code}")
async def ws_room(ws: WebSocket, code: str) -> None:
    await ws.accept()
    code = code.upper()
    room = ROOMS.get(code)
    if room is None:
        await ws.send_text(json.dumps({"type": "error", "message": "Комната не найдена"}))
        await ws.close()
        return

    player: Player | None = None

    try:
        # Первое сообщение должно быть "join"
        first = await ws.receive_text()
        msg = json.loads(first)
        if msg.get("type") != "join":
            await ws.send_text(json.dumps({"type": "error", "message": "Ожидался join"}))
            await ws.close()
            return

        name = (msg.get("name") or "").strip()[:30] or "Игрок"
        pid = msg.get("pid") or ""

        async with room.lock:
            # 1) Точное совпадение по pid — это тот же игрок вернулся.
            existing = next((p for p in room.players if p.pid == pid), None) if pid else None
            # 2) Если pid не совпал, но кто-то из слотов отключён —
            #    пробуем подобрать слот по совпадению имени, иначе
            #    (если в комнате есть вообще отключённый слот и она полна)
            #    отдаём его пришедшему — он явно возвращается в игру.
            if existing is None:
                by_name = next(
                    (p for p in room.players if not p.connected and name and p.name == name),
                    None,
                )
                if by_name is not None:
                    existing = by_name
                elif len(room.players) >= 2:
                    disconnected = next((p for p in room.players if not p.connected), None)
                    if disconnected is not None:
                        existing = disconnected
            if existing is not None:
                existing.ws = ws
                existing.connected = True
                existing.name = name or existing.name
                player = existing
            else:
                if len(room.players) >= 2:
                    await ws.send_text(json.dumps({"type": "error", "message": "Комната заполнена"}))
                    await ws.close()
                    return
                pid = gen_pid()
                player = Player(pid=pid, name=name, ws=ws, connected=True)
                room.players.append(player)
                # При появлении 2-го игрока — переходим в choosing, ход у создателя
                if len(room.players) == 2 and room.phase == "lobby":
                    room.phase = "choosing"
                    room.current_turn_pid = room.players[0].pid

            await ws.send_text(json.dumps({"type": "joined", "pid": player.pid}))
            await broadcast(room)

        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await handle_message(room, player, msg)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_text(json.dumps({"type": "error", "message": f"Ошибка: {e}"}))
        except Exception:
            pass
    finally:
        if player is not None:
            player.connected = False
            await broadcast(room)


# ---------------------------------------------------------------------------
# Игровая логика
# ---------------------------------------------------------------------------

def _kind_label(kind: str) -> str:
    return "Правда" if kind == "truth" else "Действие"


def _other_kind(kind: str) -> str:
    return "dare" if kind == "truth" else "truth"


def _reset_turn_state(room: Room) -> None:
    room.original_kind = None
    room.original_text = None
    room.original_idx = None
    room.alternate_kind = None
    room.alternate_text = None
    room.alternate_idx = None
    room.showing_alternate = False


def _pass_turn(room: Room, event: str, event_pid: str | None) -> None:
    other = other_player(room, room.current_turn_pid or "")
    room.current_turn_pid = other.pid if other else room.current_turn_pid
    _reset_turn_state(room)
    room.phase = "choosing"
    room.last_event = event
    room.last_event_pid = event_pid


async def handle_message(room: Room, player: Player, msg: dict[str, Any]) -> None:
    mtype = msg.get("type")

    async with room.lock:
        room.last_activity = time.time()

        if mtype == "choose":
            # Активный игрок выбирает Правда / Действие
            if room.current_turn_pid != player.pid or room.phase != "choosing":
                return
            kind = msg.get("kind")
            if kind not in ("truth", "dare"):
                return
            idx, text = get_question(
                room.level,
                kind,
                room.used[room.level][kind],
                turn_index=room.turn_index,
                t1_end=room.t1_end,
                t2_end=room.t2_end,
            )
            room.used[room.level][kind].append(idx)
            _reset_turn_state(room)
            room.original_kind = kind
            room.original_text = text
            room.original_idx = idx
            room.showing_alternate = False
            room.phase = "showing"
            room.last_event = _kind_label(kind)
            room.last_event_pid = player.pid

        elif mtype == "switch_kind":
            # Переключиться на противоположный тип (или вернуться к оригинальному)
            if room.current_turn_pid != player.pid or room.phase != "showing":
                return
            if room.original_kind is None:
                return
            if room.showing_alternate:
                # Возвращаемся к оригиналу
                room.showing_alternate = False
                room.last_event = f"{player.name}: вернулся к — {_kind_label(room.original_kind)}"
                room.last_event_pid = player.pid
            else:
                # Идём на альтернативный тип
                if room.alternate_kind is None:
                    other_kind = _other_kind(room.original_kind)
                    idx, text = get_question(
                        room.level,
                        other_kind,
                        room.used[room.level][other_kind],
                        turn_index=room.turn_index,
                        t1_end=room.t1_end,
                        t2_end=room.t2_end,
                    )
                    room.used[room.level][other_kind].append(idx)
                    room.alternate_kind = other_kind
                    room.alternate_text = text
                    room.alternate_idx = idx
                room.showing_alternate = True
                room.last_event = f"{player.name}: переключился на — {_kind_label(room.alternate_kind)}"
                room.last_event_pid = player.pid

        elif mtype == "complete":
            # Активный игрок выполнил → +1 done, ход партнёру
            if room.current_turn_pid != player.pid or room.phase != "showing":
                return
            player.score_done += 1
            room.turn_index += 1
            _pass_turn(room, event=f"{player.name}: выполнено", event_pid=player.pid)

        elif mtype == "refuse":
            # Активный игрок отказался → +1 refused, ход партнёру
            if room.current_turn_pid != player.pid or room.phase != "showing":
                return
            player.score_refused += 1
            room.turn_index += 1
            _pass_turn(room, event=f"{player.name}: отказ", event_pid=player.pid)

        elif mtype == "reset":
            # Сброс игры (включая прогрессию по тирам и пороги)
            room.phase = "choosing" if len(room.players) == 2 else "lobby"
            _reset_turn_state(room)
            room.used = {lvl: {"truth": [], "dare": []} for lvl in QUESTIONS}
            room.turn_index = 0
            room.t1_end = random.randint(4, 6)
            room.t2_end = random.randint(9, 12)
            if room.t2_end <= room.t1_end:
                room.t2_end = room.t1_end + 3
            for p in room.players:
                p.score_done = 0
                p.score_refused = 0
            if room.players:
                room.current_turn_pid = room.players[0].pid
            room.last_event = "Игра сброшена"
            room.last_event_pid = player.pid

        await broadcast(room)


# ---------------------------------------------------------------------------
# Статика (фронт)
# ---------------------------------------------------------------------------

FRONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

if os.path.isdir(FRONT_DIR):
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(os.path.join(FRONT_DIR, "index.html"))

    app.mount("/static", StaticFiles(directory=FRONT_DIR), name="static")
