(() => {
  const FIXED_OFFSET = "+05:00";

  const statusEl = document.getElementById("status");
  const tasksEl = document.getElementById("tasks");
  const newTextEl = document.getElementById("newText");
  const newDueEl = document.getElementById("newDue");
  const btnAdd = document.getElementById("btnAdd");

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) {
    try { tg.ready(); } catch (_) {}
  }

  function setStatus(msg) {
    statusEl.textContent = msg || "";
  }

  function getInitData() {
    return tg && tg.initData ? tg.initData : "";
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

  function datetimeLocalToIso(v) {
    if (!v) return null;
    // v = "YYYY-MM-DDTHH:MM"
    return v + ":00" + FIXED_OFFSET;
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

  function renderTasks(items) {
    tasksEl.innerHTML = "";
    for (const t of items) {
      const li = document.createElement("li");
      const title = document.createElement("div");
      title.className = "task-title";
      title.textContent = t.text;

      const due = document.createElement("div");
      due.className = "task-due muted";
      due.textContent = "Дедлайн: " + fmtDue(t.due_at);

      const btns = document.createElement("div");
      btns.className = "btns";

      const bComplete = document.createElement("button");
      bComplete.textContent = "Выполнено";
      bComplete.onclick = async () => {
        await apiFetch(`/api/tasks/${t.id}/complete`, { method: "POST" });
        await loadTasks();
      };

      const bEdit = document.createElement("button");
      bEdit.textContent = "Редактировать текст";
      bEdit.onclick = async () => {
        const nt = prompt("Новый текст задачи:", t.text);
        if (nt == null) return;
        await apiFetch(`/api/tasks/${t.id}`, {
          method: "PATCH",
          body: JSON.stringify({ text: nt }),
        });
        await loadTasks();
      };

      const bDue = document.createElement("button");
      bDue.textContent = "Изменить дедлайн";
      bDue.onclick = async () => {
        const nd = prompt(
          "Новый дедлайн:\n- пусто = снять дедлайн\n- YYYY-MM-DD\n- YYYY-MM-DD HH:MM",
          t.due_at ? String(t.due_at) : ""
        );
        if (nd == null) return;
        const parsed = parseDeadlineInput(nd);
        await apiFetch(`/api/tasks/${t.id}`, {
          method: "PATCH",
          body: JSON.stringify({ deadline_iso: parsed }),
        });
        await loadTasks();
      };

      const bDelete = document.createElement("button");
      bDelete.textContent = "Удалить";
      bDelete.onclick = async () => {
        if (!confirm("Удалить задачу?")) return;
        await apiFetch(`/api/tasks/${t.id}`, { method: "DELETE" });
        await loadTasks();
      };

      btns.appendChild(bComplete);
      btns.appendChild(bEdit);
      btns.appendChild(bDue);
      btns.appendChild(bDelete);

      li.appendChild(title);
      li.appendChild(due);
      li.appendChild(btns);
      tasksEl.appendChild(li);
    }
  }

  async function loadTasks() {
    setStatus("Загрузка задач…");
    const items = await apiFetch("/api/tasks", { method: "GET" });
    renderTasks(items || []);
    setStatus(items && items.length ? "" : "Активных задач нет.");
  }

  btnAdd.onclick = async () => {
    const text = (newTextEl.value || "").trim();
    if (!text) return;
    const deadlineIso = datetimeLocalToIso(newDueEl.value);
    await apiFetch("/api/tasks", {
      method: "POST",
      body: JSON.stringify({ text, deadline_iso: deadlineIso }),
    });
    newTextEl.value = "";
    newDueEl.value = "";
    await loadTasks();
  };

  (async () => {
    try {
      await loadTasks();
    } catch (e) {
      setStatus(String(e && e.message ? e.message : e));
    }
  })();
})();


