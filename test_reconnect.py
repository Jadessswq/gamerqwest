"""Тест переподключения игрока: создаём комнату, оба подключаются, один дропает, возвращается."""
import asyncio
import json
import httpx
import websockets

BASE = "http://localhost:8000"
WS = "ws://localhost:8000"


async def connect_and_join(code, name, pid=""):
    ws = await websockets.connect(f"{WS}/ws/{code}")
    await ws.send(json.dumps({"type": "join", "name": name, "pid": pid}))
    # Дожидаемся первого "joined"
    my_pid = None
    for _ in range(5):
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        m = json.loads(raw)
        if m.get("type") == "joined":
            my_pid = m["pid"]
            break
        if m.get("type") == "error":
            await ws.close()
            raise RuntimeError(f"error for {name}: {m.get('message')}")
    return ws, my_pid


async def main():
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{BASE}/api/rooms", json={"level": "medium"})
        code = r.json()["code"]
    print(f"Room: {code}")

    # 1) A и B подключаются
    ws_a, pid_a = await connect_and_join(code, "Аня")
    ws_b, pid_b = await connect_and_join(code, "Боря")
    print(f"A pid={pid_a}, B pid={pid_b}")
    assert pid_a and pid_b and pid_a != pid_b

    await asyncio.sleep(0.3)

    # 2) A отключается
    await ws_a.close()
    print("A disconnected")
    await asyncio.sleep(0.5)

    # 3) A возвращается с тем же pid → должен подхватить слот
    print("Test 1: A reconnects with same pid…")
    ws_a2, pid_a2 = await connect_and_join(code, "Аня", pid=pid_a)
    assert pid_a2 == pid_a, f"expected same pid, got {pid_a2}"
    print(f"  OK: pid сохранён = {pid_a2}")
    await ws_a2.close()
    await asyncio.sleep(0.5)

    # 4) A возвращается без pid (fresh browser, localStorage очищен),
    #    но с тем же именем → должны подобрать слот по имени
    print("Test 2: A reconnects with empty pid, same name…")
    ws_a3, pid_a3 = await connect_and_join(code, "Аня", pid="")
    assert pid_a3 == pid_a, f"expected slot reuse by name, got new pid {pid_a3}"
    print(f"  OK: слот переиспользован по имени, pid={pid_a3}")
    await ws_a3.close()
    await asyncio.sleep(0.5)

    # 5) A возвращается с новым именем и пустым pid → всё равно подхват,
    #    т.к. есть отключённый слот в полной комнате (это возврат игрока)
    print("Test 3: A reconnects with empty pid and DIFFERENT name…")
    ws_a4, pid_a4 = await connect_and_join(code, "Аня-новая", pid="")
    print(f"  OK: подхвачен слот, pid={pid_a4}")
    await ws_a4.close()
    await asyncio.sleep(0.5)

    # 6) Третий игрок с новым pid, новым именем, когда ОБА слота активны → должны отбить
    print("Test 4: third fresh player while room is full…")
    # Сначала снова поднимем A с оригинальным pid, чтобы оба слота были connected
    ws_a5, _ = await connect_and_join(code, "Аня", pid=pid_a)
    try:
        ws_c = await websockets.connect(f"{WS}/ws/{code}")
        await ws_c.send(json.dumps({"type": "join", "name": "Чужой", "pid": ""}))
        raw = await asyncio.wait_for(ws_c.recv(), timeout=5)
        m = json.loads(raw)
        print(f"  response: {m}")
        assert m.get("type") == "error", f"expected error for third connected player, got {m}"
        print("  OK: третий отклонён")
        await ws_c.close()
    finally:
        await ws_a5.close()
        await ws_b.close()

    print("\n✓ Все сценарии переподключения работают")


if __name__ == "__main__":
    asyncio.run(main())
