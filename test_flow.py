"""Локальный smoke-test новой логики (switch_kind)."""

import asyncio
import json

import httpx
import websockets

BASE = "http://localhost:8000"
WS = "ws://localhost:8000"


async def player_loop(name, code, plan, inbox, label):
    pid = ""
    async with websockets.connect(f"{WS}/ws/{code}") as ws:
        await ws.send(json.dumps({"type": "join", "name": name, "pid": pid}))
        plan_idx = 0
        last_phase = None
        while plan_idx < len(plan):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                print(f"[{label}] timeout; plan_idx={plan_idx}")
                return
            msg = json.loads(raw)
            if msg.get("type") == "joined":
                pid = msg["pid"]
            elif msg.get("type") == "state":
                s = msg["state"]
                inbox.append((label, s["phase"], s["current_kind"], s.get("showing_alternate"), (s["current_text"] or "")[:50], s["last_event"]))
                if s["current_turn_pid"] == pid and len(s["players"]) == 2:
                    action = plan[plan_idx]
                    if action.get("when_phase") and s["phase"] != action["when_phase"]:
                        continue
                    print(f"[{label}] -> {action['send']}")
                    await ws.send(json.dumps(action["send"]))
                    plan_idx += 1
                    await asyncio.sleep(0.15)


async def main():
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{BASE}/api/rooms", json={"level": "soft"})
        code = r.json()["code"]
    print(f"room: {code}")

    inbox = []

    plan_a = [
        {"when_phase": "choosing", "send": {"type": "choose", "kind": "truth"}},
        # Аня видит Правду → переключается на Действие
        {"when_phase": "showing", "send": {"type": "switch_kind"}},
        # Видит Действие → возвращается к Правде
        {"when_phase": "showing", "send": {"type": "switch_kind"}},
        # Снова Правда — выполняет
        {"when_phase": "showing", "send": {"type": "complete"}},
    ]
    plan_b = [
        {"when_phase": "choosing", "send": {"type": "choose", "kind": "dare"}},
        {"when_phase": "showing", "send": {"type": "switch_kind"}},  # → Правда
        {"when_phase": "showing", "send": {"type": "refuse"}},  # отказ от Правды
    ]

    a = asyncio.create_task(player_loop("Аня", code, plan_a, inbox, "A"))
    await asyncio.sleep(0.5)
    b = asyncio.create_task(player_loop("Боря", code, plan_b, inbox, "B"))

    await asyncio.gather(a, b)
    print("\n--- TRACE ---")
    for x in inbox:
        print("  ", x)


if __name__ == "__main__":
    asyncio.run(main())
