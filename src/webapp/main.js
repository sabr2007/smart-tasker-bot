const { createApp, ref, computed, reactive, onMounted, watch, nextTick } = Vue;

const App = {
  setup() {
    // --- Config ---
    const FIXED_OFFSET = "+05:00"; // Hardcoded offset
    const tg = window.Telegram?.WebApp;

    // --- State ---
    const loading = ref(true);
    const tasks = ref([]);
    const activeTab = ref('tasks'); // 'tasks' | 'calendar'
    const settingsOpen = ref(false);
    const showInstructions = ref(false);

    // Archive State
    const archiveOpen = ref(false);
    const archiveTasks = ref([]);
    const loadingArchive = ref(false);

    // Calendar State
    const now = new Date();
    const calendarYear = ref(now.getFullYear());
    const calendarMonth = ref(now.getMonth()); // 0-11
    const selectedDate = ref(getDateKey(now)); // YYYY-MM-DD

    // Bottom Sheet State
    const sheet = reactive({
      open: false,
      mode: 'menu', // 'menu' | 'edit' | 'reschedule' | 'delete'
      task: null
    });

    const editForm = reactive({
      text: '',
      date: '' // YYYY-MM-DDTHH:mm
    });

    // --- Telegram Integration ---
    onMounted(() => {
      if (tg) {
        tg.ready();
        tg.expand?.();
        // Setup Back Button
        tg.BackButton.onClick(() => {
          if (sheet.open) {
            closeSheet();
          } else if (settingsOpen.value) {
            settingsOpen.value = false;
          }
        });
      }
      loadTasks();

      // Init Lucide icons
      nextTick(() => lucide.createIcons());
    });

    // Update BackButton visibility based on state
    watch([() => sheet.open, settingsOpen], ([sOpen, setOpen]) => {
      if (!tg) return;
      if (sOpen || setOpen) {
        tg.BackButton.show();
      } else {
        tg.BackButton.hide();
      }
    });

    // Re-render icons when tab, sheet, or settings changes
    watch([activeTab, () => sheet.mode, () => sheet.open, settingsOpen, showInstructions, archiveOpen], () => {
      nextTick(() => lucide.createIcons());
    });


    // --- Computed ---
    const headerTitle = computed(() => {
      return activeTab.value === 'tasks' ? 'My Tasks' : 'Календарь';
    });

    // Group tasks for List View
    const taskGroups = computed(() => {
      const nowMs = Date.now();
      const todayKey = getDateKey(new Date());

      const overdue = [];
      const today = [];
      const upcoming = [];  // Tasks with future deadline
      const noDeadline = []; // Tasks without deadline

      tasks.value.forEach(t => {
        if (t.completed_at) return; // Skip completed in main list

        const dueMs = t.due_at ? Date.parse(t.due_at) : null;

        if (dueMs && dueMs < nowMs) {
          overdue.push(t);
        } else if (dueMs && getDateKey(new Date(dueMs)) === todayKey) {
          today.push(t);
        } else if (dueMs) {
          upcoming.push(t);  // Has future deadline
        } else {
          noDeadline.push(t);  // No deadline
        }
      });

      const groups = [];
      if (overdue.length) groups.push({ title: '🚨 Просрочено', items: overdue });
      if (today.length) groups.push({ title: '📅 Сегодня', items: today });
      if (upcoming.length) groups.push({ title: '🔜 Скоро', items: upcoming });
      if (noDeadline.length) groups.push({ title: '📋 Без срока', items: noDeadline });

      return groups;
    });

    // Calendar Grid Logic
    const calendarTitle = computed(() => {
      const months = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];
      return `${months[calendarMonth.value]} ${calendarYear.value}`;
    });

    const calendarDays = computed(() => {
      const year = calendarYear.value;
      const month = calendarMonth.value;
      const daysInMonth = new Date(year, month + 1, 0).getDate();
      const firstDay = new Date(year, month, 1).getDay(); // 0=Sun

      // Shift to Mon=0 ... Sun=6
      const startOffset = (firstDay + 6) % 7;

      const res = [];
      // Empty slots
      for (let i = 0; i < startOffset; i++) res.push({ day: null });

      // Days
      const todayKey = getDateKey(new Date());
      for (let d = 1; d <= daysInMonth; d++) {
        const dateKey = `${year}-${String(month + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
        // Check tasks
        const hasTasks = tasks.value.some(t => {
          if (t.completed_at || !t.due_at) return false;
          return t.due_at.startsWith(dateKey);
        });

        res.push({
          day: d,
          date: dateKey,
          isToday: dateKey === todayKey,
          hasTasks
        });
      }
      return res;
    });

    const calendarTasks = computed(() => {
      return tasks.value.filter(t => {
        if (t.completed_at) return false;
        if (!t.due_at) return false;
        return t.due_at.startsWith(selectedDate.value);
      });
    });


    // --- Actions ---

    // API Wrapper
    async function apiFetch(path, opts = {}) {
      const initData = tg?.initData || localStorage.getItem('tma_init_data_v1') || '';

      // Cache initData if valid
      if (tg?.initData) localStorage.setItem('tma_init_data_v1', tg.initData);

      if (!initData) {
        console.warn("No initData found");
        // For testing locally without Telegram, you might want to mock this or allow bypass
      }

      const headers = { ...opts.headers, Authorization: `tma ${initData}` };
      if (opts.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';

      try {
        const res = await fetch(path, { ...opts, headers });
        if (!res.ok) throw new Error(`API Error ${res.status}`);
        if (res.status === 204) return null;
        return await res.json();
      } catch (e) {
        if (tg?.showAlert) tg.showAlert(e.message);
        else alert(e.message);
        throw e;
      }
    }

    async function loadTasks() {
      try {
        loading.value = true;
        tasks.value = await apiFetch('/api/tasks');
      } catch (e) {
        console.error(e);
      } finally {
        loading.value = false;
        nextTick(() => lucide.createIcons());
      }
    }

    async function toggleTask(task) {
      // Optimistic update
      const originalCompleted = task.completed_at;
      task.completed_at = originalCompleted ? null : new Date().toISOString();

      // Remove from list immediately (visual feedback)
      // Actually, let's keep it but formatted as done? 
      // Current logic: filter out completed tasks from main list.
      // So it will disappear. 

      try {
        await apiFetch(`/api/tasks/${task.id}/complete`, { method: 'POST' });
        await loadTasks(); // Reload to sync
      } catch (e) {
        task.completed_at = originalCompleted; // Revert
      }
    }

    // --- Bottom Sheet Logic ---

    function openSheet(task) {
      sheet.task = task;
      sheet.mode = 'menu';
      sheet.open = true;

      // Init form
      editForm.text = task.text;
      editForm.date = task.due_at ? task.due_at.slice(0, 16) : ''; // YYYY-MM-DDTHH:mm
    }

    function closeSheet() {
      sheet.open = false;
      setTimeout(() => {
        sheet.mode = 'menu';
        sheet.task = null;
      }, 300); // Wait for transition
    }

    async function saveText() {
      if (!sheet.task) return;
      await apiFetch(`/api/tasks/${sheet.task.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ text: editForm.text })
      });
      closeSheet();
      loadTasks();
    }

    function initReschedule() {
      sheet.mode = 'reschedule';
      if (!editForm.date && sheet.task?.due_at) {
        editForm.date = sheet.task.due_at.slice(0, 16);
      }
    }

    async function setDeadline(type) {
      if (!sheet.task) return;
      let iso = null;
      const now = new Date();

      // Simple offset helpers
      if (type === 'today') {
        // preserve time or set to end of day? 
        // Let's set end of day for convenience? Or just date?
        // Backend expects ISO. Let's just send YYYY-MM-DDT23:59:59+05:00 ideally
        // But here we use a simple approach:
        // Actually, let's just pick the date part and keep time?
        // No, "Today" implies TODAY.
        iso = getDateKey(now) + 'T23:59:00' + FIXED_OFFSET;
      } else if (type === 'tomorrow') {
        const d = new Date(now);
        d.setDate(d.getDate() + 1);
        iso = getDateKey(d) + 'T23:59:00' + FIXED_OFFSET;
      } else if (type === 'next_week') {
        // Next Monday
        const d = new Date(now);
        d.setDate(d.getDate() + (1 + 7 - d.getDay()) % 7 || 7);
        iso = getDateKey(d) + 'T09:00:00' + FIXED_OFFSET;
      } else {
        iso = null; // Clear
      }

      await apiFetch(`/api/tasks/${sheet.task.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ deadline_iso: iso })
      });
      closeSheet();
      loadTasks();
    }

    async function saveDeadline() {
      if (!sheet.task) return;
      // editForm.date is YYYY-MM-DDTHH:mm from input
      let iso = null;
      if (editForm.date) {
        iso = editForm.date + ':00' + FIXED_OFFSET;
      }
      await apiFetch(`/api/tasks/${sheet.task.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ deadline_iso: iso })
      });
      closeSheet();
      loadTasks();
    }

    async function deleteTask() {
      if (!sheet.task) return;
      await apiFetch(`/api/tasks/${sheet.task.id}`, { method: 'DELETE' });
      closeSheet();
      loadTasks();
    }

    // --- Archive Logic ---
    function openArchive() {
      archiveOpen.value = true;
      loadArchive();
    }

    async function loadArchive() {
      try {
        loadingArchive.value = true;
        archiveTasks.value = await apiFetch('/api/tasks/archive?limit=50');
      } catch (e) {
        console.error(e);
      } finally {
        loadingArchive.value = false;
        nextTick(() => lucide.createIcons());
      }
    }

    async function clearArchive() {
      if (!confirm('Очистить весь архив выполненных задач?')) return;
      try {
        await apiFetch('/api/tasks/archive', { method: 'DELETE' });
        archiveTasks.value = [];
      } catch (e) {
        alert(e.message);
      }
    }


    // --- Helpers ---
    function getDateKey(date) {
      const y = date.getFullYear();
      const m = String(date.getMonth() + 1).padStart(2, '0');
      const d = String(date.getDate()).padStart(2, '0');
      return `${y}-${m}-${d}`;
    }

    function formatDue(iso) {
      if (!iso) return '';
      const date = new Date(iso);
      if (isNaN(date)) return iso;

      // Simple format: DD.MM HH:mm
      const day = String(date.getDate()).padStart(2, '0');
      const mo = String(date.getMonth() + 1).padStart(2, '0');
      const h = String(date.getHours()).padStart(2, '0');
      const m = String(date.getMinutes()).padStart(2, '0');
      return `${day}.${mo} ${h}:${m}`;
    }

    function formatDate(dateStr) {
      if (!dateStr) return '';
      const [y, m, d] = dateStr.split('-');
      return `${d}.${m}.${y}`;
    }

    function formatTime(iso) {
      if (!iso) return '';
      const d = new Date(iso);
      const h = String(d.getHours()).padStart(2, '0');
      const m = String(d.getMinutes()).padStart(2, '0');
      return `${h}:${m}`;
    }

    function isOverdue(task) {
      if (!task.due_at) return false;
      return new Date(task.due_at) < new Date();
    }

    // Settings
    function openSettings() {
      settingsOpen.value = true;
    }

    // Calendar Nav
    function calPrevMonth() {
      if (calendarMonth.value === 0) {
        calendarMonth.value = 11;
        calendarYear.value--;
      } else {
        calendarMonth.value--;
      }
    }
    function calNextMonth() {
      if (calendarMonth.value === 11) {
        calendarMonth.value = 0;
        calendarYear.value++;
      } else {
        calendarMonth.value++;
      }
    }
    function selectDate(dateKey) {
      selectedDate.value = dateKey;
    }


    return {
      // Logic
      loading, tasks, activeTab, settingsOpen,
      sheet, editForm, showInstructions,
      archiveOpen, archiveTasks, loadingArchive,
      // Computed
      headerTitle, taskGroups,
      calendarTitle, calendarDays, calendarTasks, selectedDate,
      // Methods
      openSheet, closeSheet, toggleTask,
      saveText, saveDeadline, deleteTask,
      initReschedule, setDeadline,
      openSettings, openArchive, clearArchive,
      calPrevMonth, calNextMonth, selectDate,
      // Formatters
      formatDue, formatDate, formatTime, isOverdue
    };
  }
};

createApp(App).mount('#app');



