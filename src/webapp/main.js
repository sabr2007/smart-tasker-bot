document.addEventListener("DOMContentLoaded", () => {
  const FIXED_OFFSET = "+05:00";
  const FIXED_OFFSET_MINUTES = 5 * 60;

  const statusEl = document.getElementById("status");
  const tasksEl = document.getElementById("tasks");
  const btnRefresh = document.getElementById("btnRefresh");

  const sheetBackdrop = document.getElementById("sheetBackdrop");
  const sheetEl = document.getElementById("sheet");
  const sheetTaskTitleEl = document.getElementById("sheetTaskTitle");
  const sheetEditBtn = document.getElementById("sheetEdit");
  const sheetRescheduleBtn = document.getElementById("sheetReschedule");
  const sheetClearDeadlineBtn = document.getElementById("sheetClearDeadline");
  const sheetDeleteBtn = document.getElementById("sheetDelete");
  const sheetCloseBtn = document.getElementById("sheetClose");

  const tg = window.Telegram?.WebApp;

  function setStatus(msg) {
    if (!statusEl) return;
    statusEl.textContent = msg ? String(msg) : "";
  }

  if (!tg) {
    setStatus("Telegram WebApp не обнаружен");
    return;
  }

  tg.ready();
  tg.expand?.();

  function getInitData() {
    return tg.initData || "";
  }


  async function apiFetch(path, opts = {}) {
    const initData = getInitData();
    if (!initData) {
      throw new Error("Откройте WebApp внутри Telegram (initData пустой).");
    }
    const headers = Object.assign({}, opts.headers || {}, {
      "Authorization": "tma " + initData,
    });
    if (opts.body && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    const res = await fetch(path, Object.assign({}, opts, { headers }));
    if (!res.ok) {
      let detail = "";
      try {
        const data = await res.json();
        detail = data && data.detail ? String(data.detail) : JSON.stringify(data);
      } catch (_) {
        detail = await res.text();
      }
      throw new Error(`API error ${res.status}: ${detail}`);
    }
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return await res.json();
    return await res.text();
  }

  function fmtDue(iso) {
    if (!iso) return "—";
    // Ожидаем "YYYY-MM-DDTHH:MM:SS+05:00"
    const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
    if (!m) return String(iso);
    const [, , mm, dd, hh, mi] = m;
    return `${dd}.${mm} ${hh}:${mi}`;
  }

  function wait(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function dateKeyInFixedOffset(ms) {
    // Конвертируем epoch ms в "локальную" дату в фиксированной TZ (+05:00)
    const d = new Date(ms + FIXED_OFFSET_MINUTES * 60 * 1000);
    return d.toISOString().slice(0, 10); // YYYY-MM-DD
  }

  function safeParseIsoToMs(iso) {
    if (!iso) return null;
    const ms = Date.parse(String(iso));
    return Number.isFinite(ms) ? ms : null;
  }

  function parseDeadlineInput(s) {
    const raw = (s || "").trim();
    if (raw === "") return null;
    if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw;
    if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}$/.test(raw)) {
      return raw.replace(" ", "T") + ":00" + FIXED_OFFSET;
    }
    return raw; // допустим, пользователь ввёл полноценный ISO
  }

  let activeTask = null;

  function closeSheet() {
    if (sheetBackdrop) {
      sheetBackdrop.classList.add("hidden");
      sheetBackdrop.setAttribute("aria-hidden", "true");
    }
    if (sheetEl) {
      sheetEl.classList.add("hidden");
      sheetEl.setAttribute("aria-hidden", "true");
    }
    activeTask = null;
  }

  function openSheet(task) {
    activeTask = task;
    if (sheetTaskTitleEl) sheetTaskTitleEl.textContent = task && task.text ? String(task.text) : "Задача";
    if (sheetBackdrop) {
      sheetBackdrop.classList.remove("hidden");
      sheetBackdrop.setAttribute("aria-hidden", "false");
    }
    if (sheetEl) {
      sheetEl.classList.remove("hidden");
      sheetEl.setAttribute("aria-hidden", "false");
    }
  }

  function buildSection(title, rows) {
    if (!rows.length) return null;
    const section = document.createElement("div");
    const h = document.createElement("div");
    h.className = "section-title";
    h.textContent = title;
    const list = document.createElement("div");
    list.className = "list";
    for (const r of rows) list.appendChild(r);
    section.appendChild(h);
    section.appendChild(list);
    return section;
  }

  function makeTaskRow(t, { isOverdue } = {}) {
    const row = document.createElement("div");
    row.className = "task-row";

    const check = document.createElement("button");
    check.type = "button";
    check.className = "check";
    check.setAttribute("aria-label", "Отметить выполненной");
    check.innerHTML = '<span class="checkmark">✓</span>';
    check.onclick = async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();

      // UX: мгновенная реакция + затем API
      check.classList.add("is-checked");
      row.classList.add("is-removing");
      await wait(160);

      try {
        await apiFetch(`/api/tasks/${t.id}/complete`, { method: "POST" });
        await loadTasks();
      } catch (e) {
        // rollback
        check.classList.remove("is-checked");
        row.classList.remove("is-removing");
        const msg = String(e && e.message ? e.message : e);
        setStatus(msg);
        try {
          tg.showAlert?.(msg);
        } catch (_) {}
      }
    };

    const main = document.createElement("div");
    main.className = "task-main";

    const title = document.createElement("div");
    title.className = "task-text";
    title.textContent = t.text || "";

    const meta = document.createElement("div");
    meta.className = "task-meta";
    if (isOverdue) meta.classList.add("overdue");
    meta.textContent = t.due_at ? fmtDue(t.due_at) : "Без дедлайна";

    main.appendChild(title);
    main.appendChild(meta);

    const menu = document.createElement("button");
    menu.type = "button";
    menu.className = "menu-btn";
    menu.setAttribute("aria-label", "Действия");
    menu.textContent = "⋮";
    menu.onclick = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      openSheet(t);
    };

    row.onclick = () => openSheet(t);

    row.appendChild(check);
    row.appendChild(main);
    row.appendChild(menu);
    return row;
  }

  function renderTasks(items) {
    if (!tasksEl) return;
    tasksEl.innerHTML = "";

    const nowMs = Date.now();
    const todayKey = dateKeyInFixedOffset(nowMs);

    const overdue = [];
    const today = [];
    const upcoming = [];

    for (const t of items || []) {
      const dueMs = safeParseIsoToMs(t.due_at);
      const hasDue = dueMs != null;
      const isOverdue = hasDue && dueMs < nowMs;
      const isToday = hasDue && dateKeyInFixedOffset(dueMs) === todayKey && !isOverdue;

      const row = makeTaskRow(t, { isOverdue });

      if (isOverdue) overdue.push(row);
      else if (isToday) today.push(row);
      else upcoming.push(row);
    }

    const blocks = [
      buildSection("🚨 Просрочено", overdue),
      buildSection("📅 Сегодня", today),
      buildSection("🔜 Скоро", upcoming),
    ].filter(Boolean);

    if (!blocks.length) {
      setStatus("Активных задач нет.");
      return;
    }

    for (const b of blocks) tasksEl.appendChild(b);
  }

  async function loadTasks() {
    setStatus("Загрузка задач…");
    const items = await apiFetch("/api/tasks", { method: "GET" });
    renderTasks(items || []);
    setStatus(items && items.length ? "" : "Активных задач нет.");
  }

  // --- Actions sheet wiring ---
  sheetBackdrop?.addEventListener("click", closeSheet);
  sheetCloseBtn?.addEventListener("click", closeSheet);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSheet();
  });

  sheetEditBtn?.addEventListener("click", async () => {
    if (!activeTask) return;
    const nt = prompt("Новый текст задачи:", activeTask.text || "");
    if (nt == null) return;
    const trimmed = String(nt).trim();
    if (!trimmed) return;
    closeSheet();
    await apiFetch(`/api/tasks/${activeTask.id}`, {
      method: "PATCH",
      body: JSON.stringify({ text: trimmed }),
    });
    await loadTasks();
  });

  sheetRescheduleBtn?.addEventListener("click", async () => {
    if (!activeTask) return;
    const nd = prompt(
      "Новый дедлайн:\n- пусто = снять дедлайн\n- YYYY-MM-DD\n- YYYY-MM-DD HH:MM",
      activeTask.due_at ? String(activeTask.due_at) : ""
    );
    if (nd == null) return;
    const parsed = parseDeadlineInput(nd);
    closeSheet();
    await apiFetch(`/api/tasks/${activeTask.id}`, {
      method: "PATCH",
      body: JSON.stringify({ deadline_iso: parsed }),
    });
    await loadTasks();
  });

  sheetClearDeadlineBtn?.addEventListener("click", async () => {
    if (!activeTask) return;
    closeSheet();
    await apiFetch(`/api/tasks/${activeTask.id}`, {
      method: "PATCH",
      body: JSON.stringify({ deadline_iso: null }),
    });
    await loadTasks();
  });

  sheetDeleteBtn?.addEventListener("click", async () => {
    if (!activeTask) return;
    if (!confirm("Удалить задачу?")) return;
    closeSheet();
    await apiFetch(`/api/tasks/${activeTask.id}`, { method: "DELETE" });
    await loadTasks();
  });

  btnRefresh?.addEventListener("click", async () => {
    try {
      await loadTasks();
    } catch (e) {
      const msg = String(e && e.message ? e.message : e);
      setStatus(msg);
      try {
        tg.showAlert?.(msg);
      } catch (_) {}
    }
  });

  (async () => {
    try {
      await loadTasks();
    } catch (e) {
      const msg = String(e && e.message ? e.message : e);
      setStatus(msg);
      try {
        tg.showAlert?.(msg);
      } catch (_) {}
    }
  })();
})();


