// ES5 compatible - no destructuring
var createApp = Vue.createApp;
var ref = Vue.ref;
var computed = Vue.computed;
var reactive = Vue.reactive;
var onMounted = Vue.onMounted;
var watch = Vue.watch;
var nextTick = Vue.nextTick;
var onUpdated = Vue.onUpdated;

// Minimal log function (can be removed in production)
function log(msg) { console.log(msg); }

const App = {
  setup() {
    // --- Config ---
    const tg = window.Telegram && window.Telegram.WebApp;

    // Popular timezones (shown first)
    const POPULAR_TIMEZONES = [
      { value: 'Asia/Almaty', label: 'Алматы (UTC+5)' },
      { value: 'Europe/Moscow', label: 'Москва (UTC+3)' },
      { value: 'Asia/Tashkent', label: 'Ташкент (UTC+5)' },
      { value: 'Asia/Bishkek', label: 'Бишкек (UTC+6)' },
    ];

    // All timezones
    const ALL_TIMEZONES = [
      { value: 'Asia/Almaty', label: 'Алматы (UTC+5)' },
      { value: 'Asia/Tashkent', label: 'Ташкент (UTC+5)' },
      { value: 'Asia/Bishkek', label: 'Бишкек (UTC+6)' },
      { value: 'Europe/Moscow', label: 'Москва (UTC+3)' },
      { value: 'Europe/London', label: 'Лондон (UTC+0)' },
      { value: 'Europe/Paris', label: 'Париж (UTC+1)' },
      { value: 'Europe/Berlin', label: 'Берлин (UTC+1)' },
      { value: 'Europe/Kiev', label: 'Киев (UTC+2)' },
      { value: 'Europe/Istanbul', label: 'Стамбул (UTC+3)' },
      { value: 'Asia/Tbilisi', label: 'Тбилиси (UTC+4)' },
      { value: 'Asia/Baku', label: 'Баку (UTC+4)' },
      { value: 'Asia/Dubai', label: 'Дубай (UTC+4)' },
      { value: 'Asia/Yekaterinburg', label: 'Екатеринбург (UTC+5)' },
      { value: 'Asia/Dhaka', label: 'Дакка (UTC+6)' },
      { value: 'Asia/Bangkok', label: 'Бангкок (UTC+7)' },
      { value: 'Asia/Ho_Chi_Minh', label: 'Хо Чи Мин (UTC+7)' },
      { value: 'Asia/Shanghai', label: 'Шанхай (UTC+8)' },
      { value: 'Asia/Singapore', label: 'Сингапур (UTC+8)' },
      { value: 'Asia/Tokyo', label: 'Токио (UTC+9)' },
      { value: 'Asia/Seoul', label: 'Сеул (UTC+9)' },
      { value: 'Australia/Sydney', label: 'Сидней (UTC+11)' },
      { value: 'Pacific/Auckland', label: 'Окленд (UTC+13)' },
      { value: 'America/New_York', label: 'Нью-Йорк (UTC-5)' },
      { value: 'America/Chicago', label: 'Чикаго (UTC-6)' },
      { value: 'America/Denver', label: 'Денвер (UTC-7)' },
      { value: 'America/Los_Angeles', label: 'Лос-Анджелес (UTC-8)' },
    ];

    // For backward compatibility
    const COMMON_TIMEZONES = ALL_TIMEZONES;

    // --- State ---
    const loading = ref(true);
    const tasks = ref([]);
    const activeTab = ref('tasks'); // 'tasks' | 'calendar'
    const settingsOpen = ref(false);
    const showInstructions = ref(false);

    // User Settings State
    const userTimezone = ref('Asia/Almaty');
    const selectedTimezone = ref('Asia/Almaty');
    const savingTimezone = ref(false);

    // Archive State
    const archiveOpen = ref(false);
    const archiveTasks = ref([]);
    const loadingArchive = ref(false);

    // Settings sub-screens
    const timezoneOpen = ref(false);
    const helpOpen = ref(false);
    const timezoneSearch = ref('');

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
    onMounted(async function() {
      try {
        if (tg) {
          tg.ready();
          if (tg.expand) tg.expand();

          // Setup Back Button
          if (tg.BackButton && typeof tg.BackButton.onClick === 'function') {
            tg.BackButton.onClick(function() {
              if (sheet.open) {
                closeSheet();
              } else if (helpOpen.value) {
                helpOpen.value = false;
              } else if (timezoneOpen.value) {
                timezoneOpen.value = false;
                timezoneSearch.value = '';
              } else if (archiveOpen.value) {
                archiveOpen.value = false;
              } else if (settingsOpen.value) {
                settingsOpen.value = false;
              }
            });
          }
        }

        // Load user settings first (for timezone)
        await loadUserSettings();

        // Then load tasks
        await loadTasks();

        // Hide fallback loader
        var fallbackLoader = document.getElementById('fallback-loader');
        if (fallbackLoader) fallbackLoader.style.display = 'none';

        // Init icons after everything loaded
        nextTick(function() { lucide.createIcons(); });

      } catch (e) {
        console.error('App initialization error:', e);
        loading.value = false;
        var fallbackLoader = document.getElementById('fallback-loader');
        if (fallbackLoader) fallbackLoader.style.display = 'none';
      }
    });

    // Update BackButton visibility based on state
    watch([() => sheet.open, settingsOpen, timezoneOpen, helpOpen, archiveOpen], ([sOpen, setOpen, tzOpen, hlpOpen, arcOpen]) => {
      if (!tg || !tg.BackButton) return;
      try {
        if (sOpen || setOpen || tzOpen || hlpOpen || arcOpen) {
          if (tg.BackButton.show) tg.BackButton.show();
        } else {
          if (tg.BackButton.hide) tg.BackButton.hide();
        }
      } catch (e) {
        console.warn('BackButton error:', e);
      }
    });

    // Re-render icons when loading completes or state changes
    watch(loading, function(newVal) {
      if (!newVal) {
        nextTick(function() { lucide.createIcons(); });
      }
    });

    // Re-render icons when tab, sheet, or settings changes
    watch([activeTab, function() { return sheet.mode; }, function() { return sheet.open; }, settingsOpen, showInstructions, archiveOpen, timezoneOpen, helpOpen], function() {
      nextTick(function() { lucide.createIcons(); });
    });

    // Ensure icons render after any DOM update
    onUpdated(function() {
      lucide.createIcons();
    });


    // --- Computed ---
    const headerTitle = computed(() => {
      return 'Календарь'; // Only shown on calendar tab now
    });

    // Time-based greeting
    const greeting = computed(() => {
      const tz = userTimezone.value || 'Asia/Almaty';
      let hour;
      try {
        hour = parseInt(new Intl.DateTimeFormat('en-US', {
          timeZone: tz,
          hour: 'numeric',
          hour12: false
        }).format(new Date()));
      } catch (e) {
        hour = new Date().getHours();
      }

      if (hour >= 5 && hour < 12) return 'Доброе утро! ☀️';
      if (hour >= 12 && hour < 18) return 'Добрый день! 👋';
      if (hour >= 18 && hour < 23) return 'Добрый вечер! 🌙';
      return 'Не спится? 🦉';
    });

    // Progress bar for today's tasks
    const todayStats = computed(function() {
      try {
        var tz = userTimezone.value || 'Asia/Almaty';
        var todayKey = getDateKeyInTz(new Date(), tz);
        var nowMs = Date.now();

        var total = 0;
        var completed = 0;

        if (tasks.value && tasks.value.length) {
          for (var i = 0; i < tasks.value.length; i++) {
            var t = tasks.value[i];
            // Count tasks that are for today (due today or completed today)
            var dueMs = t.due_at ? Date.parse(t.due_at) : null;
            var isOverdue = dueMs && dueMs < nowMs;
            var isDueToday = dueMs && getDateKeyInTz(new Date(dueMs), tz) === todayKey;
            var completedToday = t.completed_at && getDateKeyInTz(new Date(t.completed_at), tz) === todayKey;

            if (isDueToday || isOverdue || completedToday) {
              total++;
              if (t.completed_at) completed++;
            }
          }
        }

        var percent = total > 0 ? Math.round((completed / total) * 100) : 0;
        return { total: total, completed: completed, percent: percent };
      } catch (e) {
        log('todayStats error: ' + e.message);
        return { total: 0, completed: 0, percent: 0 };
      }
    });

    // Filtered timezones based on search
    const filteredTimezones = computed(() => {
      const query = timezoneSearch.value.toLowerCase().trim();
      if (!query) return ALL_TIMEZONES;
      return ALL_TIMEZONES.filter(tz =>
        tz.label.toLowerCase().includes(query) ||
        tz.value.toLowerCase().includes(query)
      );
    });

    // Current timezone label for display
    const currentTimezoneLabel = computed(() => {
      const tz = ALL_TIMEZONES.find(t => t.value === userTimezone.value);
      return tz ? tz.label : userTimezone.value;
    });

    // Archive count for settings display
    const archiveCount = computed(() => archiveTasks.value.length);

    // Archive tasks grouped by day (Сегодня / Вчера / Ранее)
    const archiveGroups = computed(() => {
      const tz = userTimezone.value || 'Asia/Almaty';
      const now = new Date();
      const todayKey = getDateKeyInTz(now, tz);

      // Calculate yesterday
      const yesterday = new Date(now);
      yesterday.setDate(yesterday.getDate() - 1);
      const yesterdayKey = getDateKeyInTz(yesterday, tz);

      const today = [];
      const yesterdayTasks = [];
      const earlier = [];

      archiveTasks.value.forEach(task => {
        if (!task.completed_at) return;

        const completedDate = new Date(task.completed_at);
        const completedKey = getDateKeyInTz(completedDate, tz);

        if (completedKey === todayKey) {
          today.push(task);
        } else if (completedKey === yesterdayKey) {
          yesterdayTasks.push(task);
        } else {
          earlier.push(task);
        }
      });

      const groups = [];
      if (today.length) groups.push({ title: 'Сегодня', items: today });
      if (yesterdayTasks.length) groups.push({ title: 'Вчера', items: yesterdayTasks });
      if (earlier.length) groups.push({ title: 'Ранее', items: earlier });

      return groups;
    });

    // Group tasks for List View
    const taskGroups = computed(() => {
      const nowMs = Date.now();
      const tz = userTimezone.value || 'Asia/Almaty';

      // Get today's date key in user's timezone
      const todayKey = getDateKeyInTz(new Date(), tz);

      const overdue = [];
      const today = [];
      const upcoming = [];  // Tasks with future deadline
      const noDeadline = []; // Tasks without deadline
      const completed = []; // New completed group

      tasks.value.forEach(t => {
        if (t.completed_at) {
          completed.push(t);
          return;
        }

        const dueMs = t.due_at ? Date.parse(t.due_at) : null;

        if (dueMs && dueMs < nowMs) {
          overdue.push(t);
        } else if (dueMs && getDateKeyInTz(new Date(dueMs), tz) === todayKey) {
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

      // Completed at the bottom
      if (completed.length) groups.push({ title: '✅ Выполненные', items: completed });

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
      const initData = (tg && tg.initData) || localStorage.getItem('tma_init_data_v1') || '';

      // Cache initData if valid
      if (tg && tg.initData) localStorage.setItem('tma_init_data_v1', tg.initData);

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
        if (tg && tg.showAlert) tg.showAlert(e.message);
        else alert(e.message);
        throw e;
      }
    }

    async function loadTasks() {
      try {
        loading.value = true;

        // Fetch active tasks
        const activePromise = apiFetch('/api/tasks');

        // Fetch completed tasks (since today or last 10)
        // We'll calculate "today" start ISO roughly based on user timezone or local
        let since = '';
        try {
          // Use selected timezone to define "today"
          const tz = userTimezone.value || 'Asia/Almaty';
          const todayStr = getDateKeyInTz(new Date(), tz);
          since = todayStr; // Backend accepts YYYY-MM-DD as prefix or >= comparison string
        } catch (e) {
          since = '';
        }

        const completedPromise = apiFetch(`/api/tasks/completed?since=${since}`);

        const [active, completed] = await Promise.all([activePromise, completedPromise]);

        // Merge them, handling potential duplicates if any (shouldn't be, but good practice)
        // Using Map to dedupe by ID
        const map = new Map();
        active.forEach(t => map.set(t.id, t));
        completed.forEach(t => map.set(t.id, t));

        tasks.value = Array.from(map.values());

      } catch (e) {
        console.error(e);
      } finally {
        loading.value = false;
        nextTick(() => lucide.createIcons());
      }
    }

    async function loadUserSettings() {
      try {
        const settings = await apiFetch('/api/users/me');
        userTimezone.value = settings.timezone || 'Asia/Almaty';
        selectedTimezone.value = userTimezone.value;
      } catch (e) {
        console.error('Failed to load user settings:', e);
      }
    }

    async function saveTimezone() {
      if (savingTimezone.value) return;
      savingTimezone.value = true;
      try {
        await apiFetch('/api/users/me', {
          method: 'PATCH',
          body: JSON.stringify({ timezone: selectedTimezone.value })
        });
        userTimezone.value = selectedTimezone.value;
        // Close timezone screen after save
        timezoneOpen.value = false;
        timezoneSearch.value = '';
      } catch (e) {
        console.error('Failed to save timezone:', e);
      } finally {
        savingTimezone.value = false;
      }
    }

    // Select timezone and auto-save
    async function selectTimezone(tz) {
      if (savingTimezone.value || tz === userTimezone.value) return;
      selectedTimezone.value = tz;
      await saveTimezone();
    }

    // Open timezone screen
    function openTimezone() {
      timezoneOpen.value = true;
      timezoneSearch.value = '';
    }

    // Open help screen
    function openHelp() {
      helpOpen.value = true;
    }

    async function toggleTask(task) {
      // Optimistic update
      const originalCompleted = task.completed_at;

      if (originalCompleted) {
        // Reopen
        task.completed_at = null;
        try {
          await apiFetch(`/api/tasks/${task.id}/reopen`, { method: 'POST' });
          // await loadTasks(); // Optional: reload to be sure
        } catch (e) {
          task.completed_at = originalCompleted;
        }
      } else {
        // Complete
        task.completed_at = new Date().toISOString();
        try {
          await apiFetch(`/api/tasks/${task.id}/complete`, { method: 'POST' });
          // await loadTasks();
        } catch (e) {
          task.completed_at = originalCompleted; // Revert
        }
      }
    }

    async function archiveTask(task) {
      // Optimistic remove
      const idx = tasks.value.indexOf(task);
      if (idx > -1) tasks.value.splice(idx, 1);

      try {
        await apiFetch(`/api/tasks/${task.id}/archive`, { method: 'POST' });
      } catch (e) {
        // Revert if failed
        if (idx > -1) {
          tasks.value.splice(idx, 0, task);
        }
        alert("Не удалось архивировать задачу");
      }
    }

    function openLink(url) {
      if (url) {
        window.open(url, '_blank');
      }
    }

    function openPhone(phone) {
      if (phone) {
        window.open(`tel:${phone}`, '_self');
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
      if (!editForm.date && sheet.task && sheet.task.due_at) {
        editForm.date = sheet.task.due_at.slice(0, 16);
      }
    }

    async function setDeadline(type) {
      if (!sheet.task) return;
      let iso = null;
      const now = new Date();

      // Send local datetime - backend converts to UTC
      // Format: YYYY-MM-DDTHH:mm:ss (without timezone suffix)
      if (type === 'today') {
        iso = getDateKey(now) + 'T23:59:00';
      } else if (type === 'tomorrow') {
        const d = new Date(now);
        d.setDate(d.getDate() + 1);
        iso = getDateKey(d) + 'T23:59:00';
      } else if (type === 'next_week') {
        // Next Monday
        const d = new Date(now);
        d.setDate(d.getDate() + (1 + 7 - d.getDay()) % 7 || 7);
        iso = getDateKey(d) + 'T09:00:00';
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
      // Send without timezone suffix - backend handles conversion
      let iso = null;
      if (editForm.date) {
        iso = editForm.date + ':00';
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

    // Get date key in specific timezone (for comparing dates across timezones)
    function getDateKeyInTz(date, tz) {
      try {
        const parts = new Intl.DateTimeFormat('en-CA', {
          timeZone: tz,
          year: 'numeric',
          month: '2-digit',
          day: '2-digit'
        }).formatToParts(date);
        const y = parts.find(p => p.type === 'year').value;
        const m = parts.find(p => p.type === 'month').value;
        const d = parts.find(p => p.type === 'day').value;
        return `${y}-${m}-${d}`;
      } catch (e) {
        // Fallback to browser local
        return getDateKey(date);
      }
    }

    function formatDue(iso) {
      if (!iso) return '';
      const date = new Date(iso);
      if (isNaN(date)) return iso;

      // Use user's selected timezone for display
      const tz = userTimezone.value || 'Asia/Almaty';
      try {
        const dayMonth = new Intl.DateTimeFormat('ru-RU', {
          timeZone: tz,
          day: '2-digit',
          month: '2-digit'
        }).format(date);
        const time = new Intl.DateTimeFormat('ru-RU', {
          timeZone: tz,
          hour: '2-digit',
          minute: '2-digit',
          hour12: false
        }).format(date);
        return `${dayMonth} ${time}`;
      } catch (e) {
        // Fallback to browser local if timezone invalid
        const day = String(date.getDate()).padStart(2, '0');
        const mo = String(date.getMonth() + 1).padStart(2, '0');
        const h = String(date.getHours()).padStart(2, '0');
        const m = String(date.getMinutes()).padStart(2, '0');
        return `${day}.${mo} ${h}:${m}`;
      }
    }

    function formatDate(dateStr) {
      if (!dateStr) return '';
      const [y, m, d] = dateStr.split('-');
      return `${d}.${m}.${y}`;
    }

    function formatTime(iso) {
      if (!iso) return '';
      const date = new Date(iso);
      if (isNaN(date)) return '';

      // Use user's selected timezone for display
      const tz = userTimezone.value || 'Asia/Almaty';
      try {
        return new Intl.DateTimeFormat('ru-RU', {
          timeZone: tz,
          hour: '2-digit',
          minute: '2-digit',
          hour12: false
        }).format(date);
      } catch (e) {
        // Fallback to browser local
        const h = String(date.getHours()).padStart(2, '0');
        const m = String(date.getMinutes()).padStart(2, '0');
        return `${h}:${m}`;
      }
    }

    function isOverdue(task) {
      if (!task.due_at) return false;
      return new Date(task.due_at) < new Date();
    }

    // Format completed time for archive (shows time for today/yesterday, date otherwise)
    function formatCompletedTime(iso, groupTitle) {
      if (!iso) return '';
      const date = new Date(iso);
      if (isNaN(date)) return '';

      const tz = userTimezone.value || 'Asia/Almaty';
      try {
        if (groupTitle === 'Сегодня' || groupTitle === 'Вчера') {
          // Show only time for today/yesterday
          return new Intl.DateTimeFormat('ru-RU', {
            timeZone: tz,
            hour: '2-digit',
            minute: '2-digit',
            hour12: false
          }).format(date);
        } else {
          // Show date for earlier items
          return new Intl.DateTimeFormat('ru-RU', {
            timeZone: tz,
            day: 'numeric',
            month: 'short'
          }).format(date);
        }
      } catch (e) {
        const h = String(date.getHours()).padStart(2, '0');
        const m = String(date.getMinutes()).padStart(2, '0');
        return `${h}:${m}`;
      }
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
      // Settings sub-screens
      timezoneOpen, helpOpen, timezoneSearch,
      // Timezone
      userTimezone, selectedTimezone, savingTimezone,
      COMMON_TIMEZONES, POPULAR_TIMEZONES, ALL_TIMEZONES,
      filteredTimezones, currentTimezoneLabel, archiveCount, archiveGroups,
      // Computed
      headerTitle, taskGroups, greeting, todayStats,
      calendarTitle, calendarDays, calendarTasks, selectedDate,
      // Methods
      openSheet, closeSheet, toggleTask,
      saveText, saveDeadline, deleteTask,
      initReschedule, setDeadline,
      openSettings, openArchive, clearArchive, archiveTask, openLink, openPhone,
      openTimezone, openHelp, selectTimezone,
      calPrevMonth, calNextMonth, selectDate,
      saveTimezone,
      // Formatters
      formatDue, formatDate, formatTime, isOverdue, formatCompletedTime
    };
  }
};

createApp(App).mount('#app');



