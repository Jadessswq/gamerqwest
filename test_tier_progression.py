"""25 ходов через WS — отслеживаем тир каждого вопроса."""

import asyncio
import json
import httpx
import websockets

BASE = "http://localhost:8000"
WS = "ws://localhost:8000"


async def player(code, name, kind, log, peer_ready, my_ready):
    pid = ""
    questions_seen = []
    async with websockets.connect(f"{WS}/ws/{code}") as ws:
        await ws.send(json.dumps({"type": "join", "name": name, "pid": ""}))
        my_ready.set()
        await peer_ready.wait()
        last_phase = None
        last_text = None
        turns = 0
        while turns < 25:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            m = json.loads(raw)
            if m.get("type") == "joined":
                pid = m["pid"]
                continue
            if m.get("type") != "state":
                continue
            s = m["state"]
            if s["current_turn_pid"] != pid:
                continue
            if s["phase"] == "choosing" and last_phase != "choosing":
                last_phase = "choosing"
                await asyncio.sleep(0.05)
                await ws.send(json.dumps({"type": "choose", "kind": kind}))
            elif s["phase"] == "showing" and s["current_text"] != last_text:
                last_text = s["current_text"]
                last_phase = "showing"
                questions_seen.append((s["current_text"], s["current_kind"]))
                await asyncio.sleep(0.05)
                await ws.send(json.dumps({"type": "complete"}))
                turns += 1
        log[name] = questions_seen


async def main():
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{BASE}/api/rooms", json={"level": "medium"})
        code = r.json()["code"]
    print(f"room: {code}")
    log = {}
    a_ready = asyncio.Event()
    b_ready = asyncio.Event()
    # Both choose truth; one will execute, but turn alternates between players
    ta = asyncio.create_task(player(code, "Аня", "truth", log, b_ready, a_ready))
    await asyncio.sleep(0.3)
    tb = asyncio.create_task(player(code, "Боря", "truth", log, a_ready, b_ready))
    try:
        await asyncio.wait_for(asyncio.gather(ta, tb), timeout=60)
    except asyncio.TimeoutError:
        print("TIMEOUT")

    from backend.questions import QUESTIONS

    pool = QUESTIONS["medium"]
    # Объединяем оба плеера в общий порядок (чередующийся)
    a_q = log.get("Аня", [])
    b_q = log.get("Боря", [])
    combined = []
    for i in range(max(len(a_q), len(b_q))):
        if i < len(a_q):
            combined.append(("A", *a_q[i]))
        if i < len(b_q):
            combined.append(("B", *b_q[i]))

    print("\n# | Игрок | Тир | Вопрос")
    print("-" * 100)
    for n, (player_label, text, kind) in enumerate(combined):
        tier = "?"
        for ti, tlist in enumerate(pool[kind]):
            if text in tlist:
                tier = ti
                break
        print(f"{n:2d} |  {player_label}   |  {tier}   | {text[:75]}")


if __name__ == "__main__":
    asyncio.run(main())
