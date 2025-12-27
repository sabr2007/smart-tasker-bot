document.addEventListener("DOMContentLoaded", () => {
  const FIXED_OFFSET = "+05:00";
  const FIXED_OFFSET_MINUTES = 5 * 60;
  const FIXED_OFFSET_MS = FIXED_OFFSET_MINUTES * 60 * 1000;

  // --- DOM ---
  const statusEl = document.getElementById("status");
  const tasksListEl = document.getElementById("tasksList");

  const headerTitleEl = document.getElementById("headerTitle");
  const btnSettings = document.getElementById("btnSettings");

  const viewTasks = document.getElementById("viewTasks");
  const viewCalendar = document.getElementById("viewCalendar");
  const tabTasks = document.getElementById("tabTasks");
  const tabCalendar = document.getElementById("tabCalendar");

  const calPrev = document.getElementById("calPrev");
  const calNext = document.getElementById("calNext");
  const calTitle = document.getElementById("calTitle");
  const calGrid = document.getElementById("calGrid");
  const dayTitleEl = document.getElementById("dayTitle");
  const dayTasksEl = document.getElementById("dayTasks");

  const settingsBackdrop = document.getElementById("settingsBackdrop");
  const settingsEl = document.getElementById("settings");
  const btnSettingsClose = document.getElementById("btnSettingsClose");
  const settingsMain = document.getElementById("settingsMain");
  const settingsArchive = document.getElementById("settingsArchive");
  const btnOpenArchive = document.getElementById("btnOpenArchive");
  const btnArchiveBack = document.getElementById("btnArchiveBack");
  const archiveStatusEl = document.getElementById("archiveStatus");
  const archiveListEl = document.getElementById("archiveList");

  // Bottom sheet actions for active tasks
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

  // --- Auth (initData) ---
  const INIT_DATA_CACHE_KEY = "tma_init_data_v1";

  function getInitData() {
    const live = tg.initData || "";
    if (live) {
      try {
        localStorage.setItem(INIT_DATA_CACHE_KEY, live);
      } catch (_) {}
      return live;
    }
    try {
      return localStorage.getItem(INIT_DATA_CACHE_KEY) || "";
    } catch (_) {
      return "";
    }
  }

  async function apiFetch(path, opts = {}) {
    const initData = getInitData();
    if (!initData) {
      throw new Error(
        "initData пустой. Откройте Mini App через кнопку «Open/Меню» в Telegram хотя бы один раз, затем можно открывать и из клавиатуры."
      );
    }
    const headers = Object.assign({}, opts.headers || {}, {
      Authorization: "tma " + initData,
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
      if (res.status === 401) {
        try {
          localStorage.removeItem(INIT_DATA_CACHE_KEY);
        } catch (_) {}
      }
      throw new Error(`API error ${res.status}: ${detail}`);
    }
    if (res.status === 204) return null;
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return await res.json();
    return await res.text();
  }

  // --- Helpers ---
  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  function wait(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function fmtDue(iso) {
    if (!iso) return "—";
    const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
    if (!m) return String(iso);
    const [, , mm, dd, hh, mi] = m;
    return `${dd}.${mm} ${hh}:${mi}`;
  }

  function safeParseIsoToMs(iso) {
    if (!iso) return null;
    const ms = Date.parse(String(iso));
    return Number.isFinite(ms) ? ms : null;
  }

  function dateKeyInFixedOffset(ms) {
    const d = new Date(ms + FIXED_OFFSET_MS);
    return d.toISOString().slice(0, 10); // YYYY-MM-DD
  }

  function weekdayMon0InFixedOffset(ms) {
    const dowSun0 = new Date(ms + FIXED_OFFSET_MS).getUTCDay(); // 0=Sun..6=Sat
    return (dowSun0 + 6) % 7; // 0=Mon..6=Sun
  }

  function dateKeyToMs(key) {
    const ms = Date.parse(`${key}T00:00:00${FIXED_OFFSET}`);
    return Number.isFinite(ms) ? ms : null;
  }

  function parseDeadlineInput(s) {
    const raw = (s || "").trim();
    if (raw === "") return null;
    if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw;
    if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}$/.test(raw)) {
      return raw.replace(" ", "T") + ":00" + FIXED_OFFSET;
    }
    return raw;
  }

  // --- State ---
  let currentTab = "tasks"; // tasks | calendar
  let tasksCache = [];
  let tasksByDateKey = new Map(); // YYYY-MM-DD -> tasks[]

  const todayKey = dateKeyInFixedOffset(Date.now());
  let selectedDateKey = todayKey;

  const nowFixed = new Date(Date.now() + FIXED_OFFSET_MS);
  let calendarYear = nowFixed.getUTCFullYear();
  let calendarMonthIndex = nowFixed.getUTCMonth(); // 0..11

  // --- Bottom sheet (Task actions) ---
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

  // --- Settings overlay ---
  function openSettings() {
    closeSheet();
    settingsMain?.classList.remove("hidden");
    settingsMain?.setAttribute("aria-hidden", "false");
    settingsArchive?.classList.add("hidden");
    settingsArchive?.setAttribute("aria-hidden", "true");

    settingsBackdrop?.classList.remove("hidden");
    settingsBackdrop?.setAttribute("aria-hidden", "false");
    settingsEl?.classList.remove("hidden");
    settingsEl?.setAttribute("aria-hidden", "false");
  }

  function closeSettings() {
    settingsBackdrop?.classList.add("hidden");
    settingsBackdrop?.setAttribute("aria-hidden", "true");
    settingsEl?.classList.add("hidden");
    settingsEl?.setAttribute("aria-hidden", "true");
  }

  async function loadArchive() {
    if (archiveStatusEl) archiveStatusEl.textContent = "Загрузка…";
    if (archiveListEl) archiveListEl.innerHTML = "";
    const items = await apiFetch("/api/tasks/archive?limit=50", { method: "GET" });
    const arr = Array.isArray(items) ? items : [];
    if (!arr.length) {
      if (archiveStatusEl) archiveStatusEl.textContent = "Архив пуст 🙂";
      return;
    }
    if (archiveStatusEl) archiveStatusEl.textContent = "";

    for (const t of arr) {
      const row = document.createElement("div");
      row.className = "task-row";
      row.style.cursor = "default";

      const check = document.createElement("div");
      check.className = "check is-checked";
      check.innerHTML = '<span class="checkmark">✓</span>';

      const main = document.createElement("div");
      main.className = "task-main";

      const title = document.createElement("div");
      title.className = "task-text";
      title.textContent = t.text || "";

      const meta = document.createElement("div");
      meta.className = "task-meta";
      const parts = [];
      if (t.completed_at) parts.push(`Выполнено: ${fmtDue(t.completed_at)}`);
      if (t.due_at) parts.push(`Дедлайн: ${fmtDue(t.due_at)}`);
      meta.textContent = parts.length ? parts.join(" · ") : "Выполнено";

      main.appendChild(title);
      main.appendChild(meta);
      row.appendChild(check);
      row.appendChild(main);
      archiveListEl?.appendChild(row);
    }
  }

  function openArchiveView() {
    settingsMain?.classList.add("hidden");
    settingsMain?.setAttribute("aria-hidden", "true");
    settingsArchive?.classList.remove("hidden");
    settingsArchive?.setAttribute("aria-hidden", "false");
    loadArchive().catch((e) => {
      const msg = String(e && e.message ? e.message : e);
      if (archiveStatusEl) archiveStatusEl.textContent = msg;
      try {
        tg.showAlert?.(msg);
      } catch (_) {}
    });
  }

  function closeArchiveView() {
    settingsArchive?.classList.add("hidden");
    settingsArchive?.setAttribute("aria-hidden", "true");
    settingsMain?.classList.remove("hidden");
    settingsMain?.setAttribute("aria-hidden", "false");
  }

  // --- UI builders ---
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

      check.classList.add("is-checked");
      row.classList.add("is-removing");
      await wait(160);

      try {
        await apiFetch(`/api/tasks/${t.id}/complete`, { method: "POST" });
        await loadTasks();
      } catch (e) {
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

  function renderTasksTab(items) {
    if (!tasksListEl) return;
    tasksListEl.innerHTML = "";

    const nowMs = Date.now();
    const todayKey2 = dateKeyInFixedOffset(nowMs);

    const overdue = [];
    const today = [];
    const upcoming = [];

    for (const t of items || []) {
      const dueMs = safeParseIsoToMs(t.due_at);
      const hasDue = dueMs != null;
      const isOverdue = hasDue && dueMs < nowMs;
      const isToday = hasDue && dateKeyInFixedOffset(dueMs) === todayKey2 && !isOverdue;

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

    for (const b of blocks) tasksListEl.appendChild(b);
  }

  function rebuildTasksByDate(items) {
    const m = new Map();
    for (const t of items || []) {
      const dueMs = safeParseIsoToMs(t.due_at);
      if (dueMs == null) continue;
      const key = dateKeyInFixedOffset(dueMs);
      const arr = m.get(key) || [];
      arr.push(t);
      m.set(key, arr);
    }
    for (const [k, arr] of m.entries()) {
      arr.sort((a, b) => {
        const am = safeParseIsoToMs(a.due_at) ?? 0;
        const bm = safeParseIsoToMs(b.due_at) ?? 0;
        return am - bm;
      });
      m.set(k, arr);
    }
    tasksByDateKey = m;
  }

  function monthTitleRu(year, monthIndex) {
    const months = [
      "Январь",
      "Февраль",
      "Март",
      "Апрель",
      "Май",
      "Июнь",
      "Июль",
      "Август",
      "Сентябрь",
      "Октябрь",
      "Ноябрь",
      "Декабрь",
    ];
    return `${months[monthIndex]} ${year}`;
  }

  function renderSelectedDayTasks() {
    if (!dayTasksEl) return;
    dayTasksEl.innerHTML = "";

    const list = tasksByDateKey.get(selectedDateKey) || [];

    if (dayTitleEl) {
      if (selectedDateKey === todayKey) {
        dayTitleEl.textContent = "Сегодня";
      } else {
        const [y, m, d] = selectedDateKey.split("-").map((x) => parseInt(x, 10));
        dayTitleEl.textContent = `${pad2(d)}.${pad2(m)}.${y}`;
      }
    }

    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "task-row";
      empty.style.cursor = "default";
      empty.innerHTML =
        '<div class="task-main"><div class="task-text">Задач нет</div><div class="task-meta">На выбранную дату нет задач с дедлайном</div></div>';
      dayTasksEl.appendChild(empty);
      return;
    }

    const nowMs = Date.now();
    for (const t of list) {
      const dueMs = safeParseIsoToMs(t.due_at);
      const isOverdue = dueMs != null && dueMs < nowMs;
      dayTasksEl.appendChild(makeTaskRow(t, { isOverdue }));
    }
  }

  function renderCalendar() {
    if (!calGrid) return;
    calGrid.innerHTML = "";
    if (calTitle) calTitle.textContent = monthTitleRu(calendarYear, calendarMonthIndex);

    const firstKey = `${calendarYear}-${pad2(calendarMonthIndex + 1)}-01`;
    const firstMs = dateKeyToMs(firstKey) ?? Date.now();
    const leading = weekdayMon0InFixedOffset(firstMs);
    const daysInMonth = new Date(Date.UTC(calendarYear, calendarMonthIndex + 1, 0)).getUTCDate();

    for (let i = 0; i < leading; i++) {
      const spacer = document.createElement("div");
      spacer.className = "day muted";
      spacer.style.visibility = "hidden";
      calGrid.appendChild(spacer);
    }

    for (let day = 1; day <= daysInMonth; day++) {
      const key = `${calendarYear}-${pad2(calendarMonthIndex + 1)}-${pad2(day)}`;
      const cell = document.createElement("div");
      cell.className = "day";
      cell.textContent = String(day);

      if (key === selectedDateKey) cell.classList.add("selected");
      if (key === todayKey) cell.style.color = "var(--tg-theme-button-color)";

      const hasTasks = (tasksByDateKey.get(key) || []).length > 0;
      if (hasTasks) {
        const dot = document.createElement("div");
        dot.className = "dot";
        cell.appendChild(dot);
      }

      cell.addEventListener("click", () => {
        selectedDateKey = key;
        renderCalendar();
        renderSelectedDayTasks();
      });

      calGrid.appendChild(cell);
    }
  }

  function setTab(tab) {
    currentTab = tab;

    tabTasks?.classList.toggle("active", tab === "tasks");
    tabTasks?.setAttribute("aria-selected", tab === "tasks" ? "true" : "false");
    tabCalendar?.classList.toggle("active", tab === "calendar");
    tabCalendar?.setAttribute("aria-selected", tab === "calendar" ? "true" : "false");

    viewTasks?.classList.toggle("hidden", tab !== "tasks");
    viewTasks?.setAttribute("aria-hidden", tab === "tasks" ? "false" : "true");
    viewCalendar?.classList.toggle("hidden", tab !== "calendar");
    viewCalendar?.setAttribute("aria-hidden", tab === "calendar" ? "false" : "true");

    // По требованиям можно оставлять title статичным
    if (headerTitleEl) headerTitleEl.textContent = "My Tasks";

    if (tab === "tasks") renderTasksTab(tasksCache);
    else {
      renderCalendar();
      renderSelectedDayTasks();
    }
  }

  // --- Data loading ---
  async function loadTasks() {
    setStatus("Загрузка задач…");
    const items = await apiFetch("/api/tasks", { method: "GET" });
    tasksCache = Array.isArray(items) ? items : [];
    rebuildTasksByDate(tasksCache);

    renderTasksTab(tasksCache);
    setStatus(tasksCache.length ? "" : "Активных задач нет.");

    // обновляем календарь и список выбранного дня всегда
    renderCalendar();
    renderSelectedDayTasks();
  }

  // --- Wiring ---
  // Bottom sheet
  sheetBackdrop?.addEventListener("click", closeSheet);
  sheetCloseBtn?.addEventListener("click", closeSheet);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeSheet();
      closeSettings();
    }
  });

  sheetEditBtn?.addEventListener("click", async () => {
    if (!activeTask) return;
    const task = activeTask;
    const taskId = task.id;
    const nt = prompt("Новый текст задачи:", task.text || "");
    if (nt == null) return;
    const trimmed = String(nt).trim();
    if (!trimmed) return;
    closeSheet();
    await apiFetch(`/api/tasks/${taskId}`, { method: "PATCH", body: JSON.stringify({ text: trimmed }) });
    await loadTasks();
  });

  sheetRescheduleBtn?.addEventListener("click", async () => {
    if (!activeTask) return;
    const task = activeTask;
    const taskId = task.id;
    const nd = prompt(
      "Новый дедлайн:\n- пусто = снять дедлайн\n- YYYY-MM-DD\n- YYYY-MM-DD HH:MM",
      task.due_at ? String(task.due_at) : ""
    );
    if (nd == null) return;
    const parsed = parseDeadlineInput(nd);
    closeSheet();
    await apiFetch(`/api/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify({ deadline_iso: parsed }),
    });
    await loadTasks();
  });

  sheetClearDeadlineBtn?.addEventListener("click", async () => {
    if (!activeTask) return;
    const taskId = activeTask.id;
    closeSheet();
    await apiFetch(`/api/tasks/${taskId}`, {
      method: "PATCH",
      body: JSON.stringify({ deadline_iso: null }),
    });
    await loadTasks();
  });

  sheetDeleteBtn?.addEventListener("click", async () => {
    if (!activeTask) return;
    if (!confirm("Удалить задачу?")) return;
    const taskId = activeTask.id;
    closeSheet();
    await apiFetch(`/api/tasks/${taskId}`, { method: "DELETE" });
    await loadTasks();
  });

  // Tabs
  tabTasks?.addEventListener("click", () => setTab("tasks"));
  tabCalendar?.addEventListener("click", () => setTab("calendar"));

  // Calendar
  calPrev?.addEventListener("click", () => {
    calendarMonthIndex -= 1;
    if (calendarMonthIndex < 0) {
      calendarMonthIndex = 11;
      calendarYear -= 1;
    }
    renderCalendar();
  });
  calNext?.addEventListener("click", () => {
    calendarMonthIndex += 1;
    if (calendarMonthIndex > 11) {
      calendarMonthIndex = 0;
      calendarYear += 1;
    }
    renderCalendar();
  });

  // Settings
  btnSettings?.addEventListener("click", openSettings);
  settingsBackdrop?.addEventListener("click", closeSettings);
  btnSettingsClose?.addEventListener("click", closeSettings);
  btnOpenArchive?.addEventListener("click", openArchiveView);
  btnArchiveBack?.addEventListener("click", closeArchiveView);

  // Init
  (async () => {
    try {
      setTab("tasks");
      await loadTasks();
    } catch (e) {
      const msg = String(e && e.message ? e.message : e);
      setStatus(msg);
      try {
        tg.showAlert?.(msg);
      } catch (_) {}
    }
  })();
});


