// Truth-or-Dare 18+ — клиентская логика

(() => {
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const LEVEL_NAME = {
    soft: "Лёгкий флирт",
    medium: "Средний 18+",
    hot: "Hard 18+",
  };

  const state = {
    name: localStorage.getItem("tod_name") || "",
    pid: localStorage.getItem("tod_pid") || "",
    code: "",
    ws: null,
    server: location.origin,
  };

  // -------------------------------------------------------------------------
  // Навигация между экранами
  // -------------------------------------------------------------------------
  function showScreen(id) {
    $$(".screen").forEach((s) => s.classList.toggle("active", s.id === id));
  }

  // -------------------------------------------------------------------------
  // Меню
  // -------------------------------------------------------------------------
  $("#input-name").value = state.name;
  // Запомненный уровень при создании
  const lastLevel = localStorage.getItem("tod_level") || "medium";
  if (LEVEL_NAME[lastLevel]) $("#select-level-create").value = lastLevel;

  $("#btn-create").addEventListener("click", async () => {
    const name = $("#input-name").value.trim();
    if (!name) return toast("Введи имя");
    const level = $("#select-level-create").value;
    state.name = name;
    localStorage.setItem("tod_name", name);
    localStorage.setItem("tod_level", level);

    try {
      const res = await fetch(`${state.server}/api/rooms`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ level }),
      });
      const data = await res.json();
      if (!data.code) throw new Error("no code");
      state.code = data.code;
      state.pid = "";
      enterLobby(data.code, data.level || level);
      connectWs();
    } catch (e) {
      toast("Не удалось создать комнату");
    }
  });

  $("#btn-join").addEventListener("click", async () => {
    const name = $("#input-name").value.trim();
    const code = $("#input-code").value.trim().toUpperCase();
    if (!name) return toast("Введи имя");
    if (code.length !== 6) return toast("Код должен быть 6 символов");
    state.name = name;
    localStorage.setItem("tod_name", name);

    try {
      const res = await fetch(`${state.server}/api/rooms/${code}`);
      if (res.status === 404) return toast("Комната не найдена");
      const data = await res.json();
      if (!data.exists) return toast("Комната не найдена");
      state.code = code;
      // Не сбрасываем pid: если это тот же игрок, сервер подхватит его бывший слот.
      $("#game-code").textContent = code;
      $("#game-level").textContent = "Уровень: " + (LEVEL_NAME[data.level] || "—");
      showScreen("screen-game");
      connectWs();
    } catch (e) {
      toast("Сеть недоступна");
    }
  });

  // Авто-вход по ?room=CODE из ссылки
  const urlParams = new URLSearchParams(location.search);
  const sharedCode = (urlParams.get("room") || "").toUpperCase();
  if (sharedCode) $("#input-code").value = sharedCode;

  // -------------------------------------------------------------------------
  // Лобби
  // -------------------------------------------------------------------------
  function enterLobby(code, level) {
    $("#lobby-code").textContent = code;
    $("#lobby-level").textContent = LEVEL_NAME[level] || "—";
    $("#game-code").textContent = code;
    $("#game-level").textContent = "Уровень: " + (LEVEL_NAME[level] || "—");
    showScreen("screen-lobby");
  }

  $("#btn-copy-code").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(state.code);
      toast("Код скопирован");
    } catch {
      toast(state.code);
    }
  });

  $("#btn-share-link").addEventListener("click", async () => {
    const url = `${location.origin}/?room=${state.code}`;
    const text = `Сыграем в Правда или Действие 18+? Код: ${state.code} → ${url}`;
    if (navigator.share) {
      try {
        await navigator.share({ title: "Правда или Действие", text, url });
        return;
      } catch {}
    }
    try {
      await navigator.clipboard.writeText(url);
      toast("Ссылка скопирована");
    } catch {
      toast(url);
    }
  });

  $("#btn-leave-lobby").addEventListener("click", () => {
    closeWs();
    showScreen("screen-menu");
  });

  // -------------------------------------------------------------------------
  // WebSocket
  // -------------------------------------------------------------------------
  function connectWs() {
    closeWs();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/${state.code}`);
    state.ws = ws;

    ws.addEventListener("open", () => {
      ws.send(JSON.stringify({ type: "join", name: state.name, pid: state.pid }));
    });

    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "joined") {
        state.pid = msg.pid;
        localStorage.setItem("tod_pid", msg.pid);
      } else if (msg.type === "state") {
        renderState(msg.state);
      } else if (msg.type === "error") {
        toast(msg.message || "Ошибка");
      }
    });

    ws.addEventListener("close", () => {
      // Авто-реконнект, если мы всё ещё в игре
      if ($("#screen-game").classList.contains("active") || $("#screen-lobby").classList.contains("active")) {
        setTimeout(() => {
          if (state.code) connectWs();
        }, 1500);
      }
    });
  }

  function closeWs() {
    if (state.ws) {
      try { state.ws.close(); } catch {}
      state.ws = null;
    }
  }

  function send(obj) {
    if (state.ws && state.ws.readyState === 1) {
      state.ws.send(JSON.stringify(obj));
    }
  }

  // -------------------------------------------------------------------------
  // Рендер состояния
  // -------------------------------------------------------------------------
  function renderState(s) {
    const me = s.players.find((p) => p.pid === state.pid);
    const them = s.players.find((p) => p.pid !== state.pid);

    // Если игроков двое и мы не в лобби — показываем игру
    if (s.players.length === 2 && s.phase !== "lobby") {
      if (!$("#screen-game").classList.contains("active")) {
        showScreen("screen-game");
      }
    } else if (s.phase === "lobby" || s.players.length < 2) {
      if (!$("#screen-lobby").classList.contains("active") && !$("#screen-menu").classList.contains("active")) {
        enterLobby(s.code, s.level);
      } else if ($("#screen-lobby").classList.contains("active")) {
        $("#lobby-code").textContent = s.code;
        $("#lobby-level").textContent = LEVEL_NAME[s.level] || "—";
      }
    }

    $("#game-code").textContent = s.code;
    $("#game-level").textContent = "Уровень: " + (LEVEL_NAME[s.level] || "—");

    // Игроки
    const meEl = $("#player-me");
    const themEl = $("#player-them");
    if (me) {
      meEl.querySelector(".player-name").textContent = me.name + " (ты)";
      meEl.querySelector(".ok").textContent = me.score_done;
      meEl.querySelector(".bad").textContent = me.score_refused;
      meEl.classList.toggle("active", s.current_turn_pid === me.pid);
      meEl.classList.toggle("disconnected", !me.connected);
    } else {
      meEl.querySelector(".player-name").textContent = state.name + " (ты)";
    }
    if (them) {
      themEl.querySelector(".player-name").textContent = them.name;
      themEl.querySelector(".ok").textContent = them.score_done;
      themEl.querySelector(".bad").textContent = them.score_refused;
      themEl.classList.toggle("active", s.current_turn_pid === them.pid);
      themEl.classList.toggle("disconnected", !them.connected);
    } else {
      themEl.querySelector(".player-name").textContent = "Ждём…";
      themEl.querySelector(".ok").textContent = "0";
      themEl.querySelector(".bad").textContent = "0";
      themEl.classList.remove("active");
    }

    // Индикатор хода
    const myTurn = me && s.current_turn_pid === me.pid;
    if (s.players.length < 2) {
      $("#turn-indicator").textContent = "Ждём партнёра…";
    } else if (myTurn) {
      $("#turn-indicator").textContent = "Твой ход";
    } else {
      $("#turn-indicator").textContent = `Ход: ${them ? them.name : "партнёр"}`;
    }

    // Фазы
    const choosing = $("#phase-choosing");
    const showing = $("#phase-showing");
    choosing.classList.remove("active");
    showing.classList.remove("active");

    if (s.players.length < 2) {
      // Никаких фаз пока
    } else if (s.phase === "choosing") {
      choosing.classList.add("active");
      $("#choose-prompt").textContent = myTurn
        ? "Что выбираешь?"
        : `Партнёр выбирает: Правда или Действие…`;
      $("#btn-truth").disabled = !myTurn;
      $("#btn-dare").disabled = !myTurn;
      $("#btn-truth").style.opacity = myTurn ? "1" : "0.45";
      $("#btn-dare").style.opacity = myTurn ? "1" : "0.45";
    } else if (s.phase === "showing") {
      showing.classList.add("active");
      const label = $("#kind-label");
      label.textContent = s.current_kind === "truth" ? "Правда" : "Действие";
      label.classList.toggle("truth", s.current_kind === "truth");
      label.classList.toggle("dare", s.current_kind === "dare");
      $("#question-text").textContent = s.current_text || "";

      const myActions = $("#my-actions");
      const waitingNote = $("#waiting-note");
      myActions.classList.toggle("hidden", !myTurn);
      waitingNote.classList.toggle("show", !myTurn);
      waitingNote.textContent = myTurn
        ? ""
        : `Партнёр ${s.current_kind === "truth" ? "отвечает на правду" : "выполняет действие"}…`;

      // Текст кнопки переключения зависит от того, показан оригинал или альтернатива
      const switchBtn = $("#btn-switch");
      const otherKind = s.current_kind === "truth" ? "dare" : "truth";
      const otherLabel = otherKind === "truth" ? "на Правду" : "на Действие";
      const backLabel = s.original_kind === "truth" ? "к Правде" : "к Действию";
      if (s.showing_alternate) {
        switchBtn.textContent = "Вернуться " + backLabel + " ↺";
        switchBtn.classList.add("back");
      } else {
        switchBtn.textContent = "Переключиться " + otherLabel + " ⇆";
        switchBtn.classList.remove("back");
      }
    }

    // Лог события
    $("#event-log").textContent = s.last_event || "";
  }

  // -------------------------------------------------------------------------
  // Игровые действия
  // -------------------------------------------------------------------------
  $("#btn-truth").addEventListener("click", () => send({ type: "choose", kind: "truth" }));
  $("#btn-dare").addEventListener("click", () => send({ type: "choose", kind: "dare" }));
  $("#btn-complete").addEventListener("click", () => send({ type: "complete" }));
  $("#btn-refuse").addEventListener("click", () => send({ type: "refuse" }));
  $("#btn-switch").addEventListener("click", () => send({ type: "switch_kind" }));
  $("#btn-reset").addEventListener("click", () => {
    if (confirm("Сбросить игру? Счёт обнулится.")) send({ type: "reset" });
  });

  // -------------------------------------------------------------------------
  // Toast
  // -------------------------------------------------------------------------
  let toastTimer = null;
  function toast(msg) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
  }
})();
