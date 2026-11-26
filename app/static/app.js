/* Jarvis PWA client.
 *
 * No framework, no build step — this ships as-is and installs to the iPhone
 * home screen. Phase 2 replaces it with Ionic + Angular against the same API.
 *
 * The passcode is exchanged once for a session token, which lives in
 * localStorage: sent as a bearer token on REST calls, and as a query
 * parameter on the WebSocket (browsers can't set headers on a WS handshake).
 * The passcode itself is not stored.
 */

(() => {
  "use strict";

  const TOKEN_KEY = "jarvis.token";
  const TOKEN_EXPIRES_KEY = "jarvis.token_expires";
  // Older builds stored the passcode itself. It is exchanged for a token on
  // the next load and then removed.
  const PASSCODE_KEY = "jarvis.passcode";
  const SESSION_KEY = "jarvis.session";

  const $ = (id) => document.getElementById(id);
  const el = {
    lock: $("lock"), lockForm: $("lock-form"), lockInput: $("lock-input"), lockError: $("lock-error"),
    app: $("app"), greeting: $("greeting"), subtitle: $("subtitle"), banner: $("banner"),
    moodScale: $("mood-scale"), moodNote: $("mood-note"), moodSubmit: $("mood-submit"),
    moodTrend: $("mood-trend"), trendBars: $("trend-bars"), trendAvg: $("trend-avg"),
    messages: $("messages"), chatForm: $("chat-form"), chatInput: $("chat-input"),
    logsToggle: $("logs-toggle"), logsPanel: $("logs-panel"), logs: $("logs"),
    logoutBtn: $("logout-btn"),
    stats: $("stats"), exportBtn: $("export-btn"),
    engineBadge: $("engine-badge"), storageBadge: $("storage-badge"),
    missionForm: $("mission-form"), missionInput: $("mission-input"), missionList: $("mission-list"),
    taskForm: $("task-form"), taskTitle: $("task-title"), taskDue: $("task-due"), taskList: $("task-list"),
    alertList: $("alert-list"),
    approvals: $("approvals"), approvalList: $("approval-list"),
    memoryForm: $("memory-search-form"), memoryQuery: $("memory-query"),
    memoryScopes: $("memory-scopes"), memoryMethod: $("memory-method"),
    memoryList: $("memory-list"), memoryAddForm: $("memory-add-form"),
    memoryNewKey: $("memory-new-key"), memoryNewValue: $("memory-new-value"),
    todoVerdict: $("todo-verdict"), todoNext: $("todo-next"), todoAddForm: $("todo-add-form"),
    todoTitle: $("todo-title"), todoList: $("todo-list"),
    gatewayList: $("gateway-list"), providerSettings: $("provider-settings"),
  };

  let token = localStorage.getItem(TOKEN_KEY) || "";
  let sessionId = null;
  let selectedMood = null;
  let socket = null;
  let keepSocket = false;

  // ---------------------------------------------------------------- utils

  const escapeHtml = (s) =>
    String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function authHeaders() {
    return token ? { Authorization: `Bearer ${token}` } : {};
  }

  async function api(path, { method = "GET", body } = {}) {
    const res = await fetch(path, {
      method,
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(),
      },
      body: body ? JSON.stringify(body) : undefined,
    });

    if (res.status === 401) {
      const err = new Error("Unauthorized");
      err.unauthorized = true;
      throw err;
    }
    if (res.status === 429) {
      const err = new Error("Too many attempts. Try again in a little while.");
      err.locked = true;
      throw err;
    }
    if (!res.ok) {
      let detail = `Request failed (${res.status})`;
      try {
        const data = await res.json();
        if (data.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      } catch (_) { /* non-JSON error body */ }
      throw new Error(detail);
    }
    return res.json();
  }

  // ----------------------------------------------------------- lock screen

  function showLock(message = "") {
    el.lockError.textContent = message;
    el.lock.classList.remove("hidden");
    el.app.classList.add("hidden");
    el.lockInput.value = "";
    el.lockInput.focus();
  }

  function clearToken() {
    token = "";
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(TOKEN_EXPIRES_KEY);
    localStorage.removeItem(PASSCODE_KEY);
    stopSocket();
  }

  async function login(candidate) {
    const res = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passcode: candidate }),
    });
    if (res.status === 429) {
      const err = new Error("Too many attempts. Try again in a little while.");
      err.locked = true;
      throw err;
    }
    if (res.status === 401) {
      const err = new Error("Unauthorized");
      err.unauthorized = true;
      throw err;
    }
    if (!res.ok) throw new Error(`Couldn't reach Jarvis (${res.status})`);
    const data = await res.json();
    token = data.token || "";
    localStorage.setItem(TOKEN_KEY, token);
    if (data.expires_at) localStorage.setItem(TOKEN_EXPIRES_KEY, data.expires_at);
    localStorage.removeItem(PASSCODE_KEY);
    return data;
  }

  el.lockForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const candidate = el.lockInput.value.trim();
    if (!candidate) return;
    el.lockError.textContent = "";
    try {
      await login(candidate);
      el.lock.classList.add("hidden");
      el.app.classList.remove("hidden");
      await boot();
    } catch (err) {
      clearToken();
      const message = err.locked
        ? err.message
        : err.unauthorized
          ? "Wrong passcode."
          : `Couldn't reach Jarvis: ${err.message}`;
      showLock(message);
    }
  });

  el.logoutBtn.addEventListener("click", async () => {
    try {
      await api("/api/v1/auth/logout", { method: "POST" });
    } catch (_) { /* drop the local token either way */ }
    clearToken();
    showLock();
  });

  // ------------------------------------------------------------- messages

  function addMessage(text, role) {
    const node = document.createElement("div");
    node.className = `msg ${role}`;
    node.textContent = text;
    el.messages.appendChild(node);
    el.messages.scrollTop = el.messages.scrollHeight;
    return node;
  }

  function addMeta(trajectoryId, provider, latencyMs) {
    const meta = document.createElement("div");
    meta.className = "msg-meta";
    meta.innerHTML =
      `<span>${escapeHtml(provider)} · ${latencyMs}ms</span>` +
      `<button class="rate-btn" data-rating="1" aria-label="Good reply">👍</button>` +
      `<button class="rate-btn" data-rating="-1" aria-label="Bad reply">👎</button>`;

    meta.querySelectorAll(".rate-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const rating = Number(btn.dataset.rating);
        try {
          await api("/api/v1/feedback", { method: "POST", body: { trajectory_id: trajectoryId, rating } });
          meta.querySelectorAll(".rate-btn").forEach((b) => b.classList.remove("rated"));
          btn.classList.add("rated");
          refreshStats();
        } catch (err) {
          pushLog("error", `Feedback failed: ${err.message}`);
        }
      });
    });

    el.messages.appendChild(meta);
    el.messages.scrollTop = el.messages.scrollHeight;
  }

  // ----------------------------------------------------------------- mood

  function renderMoodScale(scale) {
    el.moodScale.innerHTML = "";
    scale.forEach(({ score, label, emoji }) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mood-btn";
      btn.innerHTML = `${emoji}<span>${escapeHtml(label)}</span>`;
      btn.setAttribute("aria-label", `${label} (${score} of 5)`);
      btn.addEventListener("click", () => {
        selectedMood = score;
        [...el.moodScale.children].forEach((c) => c.classList.remove("selected"));
        btn.classList.add("selected");
        el.moodSubmit.disabled = false;
      });
      el.moodScale.appendChild(btn);
    });
  }

  el.moodSubmit.addEventListener("click", async () => {
    if (selectedMood === null) return;
    const note = el.moodNote.value.trim();
    const score = selectedMood;

    el.moodSubmit.disabled = true;
    el.moodSubmit.textContent = "Checking in…";
    addMessage(note ? `Mood ${score}/5 — ${note}` : `Mood ${score}/5`, "user");
    const pending = addMessage("…", "jarvis pending");

    try {
      const data = await api("/api/v1/mood", { method: "POST", body: { session_id: sessionId, score, note } });
      pending.remove();
      addMessage(data.response, "jarvis");
      addMeta(data.trajectory_id, data.provider, data.latency_ms);
      el.moodNote.value = "";
      selectedMood = null;
      [...el.moodScale.children].forEach((c) => c.classList.remove("selected"));
      loadTrend();
      refreshStats();
    } catch (err) {
      pending.remove();
      addMessage(`Check-in failed: ${err.message}`, "error");
    } finally {
      el.moodSubmit.textContent = "Check in";
      el.moodSubmit.disabled = selectedMood === null;
    }
  });

  async function loadTrend() {
    try {
      const data = await api("/api/v1/mood/history?limit=14");
      if (!data.count) { el.moodTrend.classList.add("hidden"); return; }

      el.trendBars.innerHTML = "";
      // API returns newest first; show oldest -> newest left to right.
      data.entries.slice().reverse().forEach((entry) => {
        const bar = document.createElement("div");
        bar.className = "trend-bar";
        bar.style.height = `${(entry.mood_score / 5) * 100}%`;
        bar.title = `${entry.mood_label} (${entry.mood_score}/5)`;
        el.trendBars.appendChild(bar);
      });
      el.trendAvg.textContent = `avg ${data.average}`;
      el.moodTrend.classList.remove("hidden");
    } catch (_) { /* trend is decorative */ }
  }

  // ----------------------------------------------------------------- chat

  el.chatForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const prompt = el.chatInput.value.trim();
    if (!prompt) return;

    el.chatInput.value = "";
    addMessage(prompt, "user");
    const pending = addMessage("…", "jarvis pending");

    try {
      const data = await api("/api/v1/command", { method: "POST", body: { session_id: sessionId, prompt } });
      pending.remove();
      addMessage(data.response, "jarvis");
      addMeta(data.trajectory_id, data.provider, data.latency_ms);
      refreshStats();
    } catch (err) {
      pending.remove();
      addMessage(`Failed: ${err.message}`, "error");
    }
  });

  // ------------------------------------------------------------- missions

  const MISSION_POLL_MS = 2500;
  let missionPollTimer = null;

  el.missionForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const goal = el.missionInput.value.trim();
    if (!goal) return;

    el.missionInput.value = "";
    pushLog("mission", `Creating mission: ${goal}`);
    try {
      await api("/api/v1/missions", { method: "POST", body: { session_id: sessionId, goal } });
      refreshMissions();
      // Missions run in the background, so poll while any is still active.
      startMissionPolling();
    } catch (err) {
      pushLog("error", `Mission failed to start: ${err.message}`);
    }
  });

  function startMissionPolling() {
    if (missionPollTimer) return;
    missionPollTimer = setInterval(async () => {
      const active = await refreshMissions();
      if (!active) { clearInterval(missionPollTimer); missionPollTimer = null; }
    }, MISSION_POLL_MS);
  }

  const ACTIVE_STATUSES = new Set(["PLANNING", "RUNNING", "WAITING", "NEEDS_APPROVAL"]);

  async function refreshMissions() {
    let anyActive = false;
    try {
      const list = await api("/api/v1/missions?limit=5");
      el.missionList.innerHTML = "";

      for (const summary of list.missions) {
        const full = await api(`/api/v1/missions/${encodeURIComponent(summary.mission_id)}`);
        if (ACTIVE_STATUSES.has(full.status)) anyActive = true;
        el.missionList.appendChild(renderMission(full));
      }
    } catch (err) {
      pushLog("error", `Could not load missions: ${err.message}`);
    }
    refreshApprovals();
    refreshTasks();
    refreshAlerts();
    return anyActive;
  }

  el.taskForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const title = el.taskTitle.value.trim();
    if (!title) return;
    // datetime-local has no zone. The field is labelled IST, and a zone-less
    // value is read as IST by the API. Sending it with a Z would make it UTC.
    const due = el.taskDue.value ? el.taskDue.value : null;
    try {
      await api("/api/v1/tasks", { method: "POST", body: { title, due_at: due } });
      el.taskTitle.value = "";
      el.taskDue.value = "";
      refreshTasks();
    } catch (err) {
      pushLog("error", `Could not add task: ${err.message}`);
    }
  });

  async function refreshTasks() {
    try {
      const list = await api("/api/v1/tasks");
      el.taskList.innerHTML = "";
      if (!list.tasks.length) {
        el.taskList.textContent = "No open tasks.";
        return;
      }
      for (const task of list.tasks) {
        el.taskList.appendChild(renderTask(task));
      }
    } catch (err) {
      pushLog("error", `Could not load tasks: ${err.message}`);
    }
  }

  function renderTask(task) {
    const item = document.createElement("article");
    item.className = "task-item";
    if (task.status === "suggested") {
      const flag = document.createElement("span");
      flag.className = "task-suggested";
      flag.textContent = "Suggested";
      item.appendChild(flag);
    }
    const title = document.createElement("strong");
    title.textContent = task.title;
    item.appendChild(title);
    const meta = document.createElement("p");
    const due = task.due_at_ist ? `Due ${task.due_at_ist}` : "No due time";
    const tags = (task.tags || []).join(", ");
    meta.textContent = tags ? `${due} · ${tags}` : due;
    item.appendChild(meta);
    const actions = document.createElement("div");
    actions.className = "task-actions";
    if (task.status === "suggested") {
      actions.appendChild(taskButton("Accept", () => postTask(task.task_id, "accept")));
    } else if (task.status === "open") {
      actions.appendChild(taskButton("Complete", () => postTask(task.task_id, "complete")));
    }
    item.appendChild(actions);
    return item;
  }

  function taskButton(label, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "primary";
    button.textContent = label;
    button.addEventListener("click", onClick);
    return button;
  }

  async function postTask(taskId, action) {
    try {
      await api(`/api/v1/tasks/${encodeURIComponent(taskId)}/${action}`, { method: "POST" });
      refreshTasks();
    } catch (err) {
      pushLog("error", `Could not ${action} task: ${err.message}`);
    }
  }

  async function refreshAlerts() {
    try {
      const list = await api("/api/v1/alerts");
      el.alertList.innerHTML = "";
      if (!list.alerts.length) {
        el.alertList.textContent = "No alerts.";
        return;
      }
      for (const alert of list.alerts) {
        const item = document.createElement("article");
        item.className = "alert-item";
        const sev = document.createElement("span");
        sev.className = `sev sev-${alert.severity || "info"}`;
        sev.textContent = alert.severity || "info";
        const title = document.createElement("strong");
        title.textContent = alert.title || "";
        const body = document.createElement("p");
        body.textContent = alert.body || "";
        item.append(sev, title, body);
        el.alertList.appendChild(item);
      }
    } catch (err) {
      pushLog("error", `Could not load alerts: ${err.message}`);
    }
  }

  function renderMission(mission) {
    const node = document.createElement("div");
    node.className = "mission";

    const status = (mission.status || "").toLowerCase();
    const percent = mission.progress ? mission.progress.percent : 0;

    const head = document.createElement("div");
    head.className = "mission-head";
    head.innerHTML =
      `<span class="mission-goal">${escapeHtml(mission.goal)}</span>` +
      `<span class="mission-status ${status}">${escapeHtml(mission.status)}</span>`;
    node.appendChild(head);

    const track = document.createElement("div");
    track.className = "progress-track";
    // Width goes through the CSSOM. An HTML style attribute would need
    // style-src 'unsafe-inline', which the page policy does not allow.
    const fill = document.createElement("div");
    fill.className = `progress-fill ${status}`;
    fill.style.width = `${Number(percent) || 0}%`;
    track.appendChild(fill);
    node.appendChild(track);

    if (mission.tasks && mission.tasks.length) {
      const list = document.createElement("div");
      list.className = "task-list";
      mission.tasks.forEach((task) => {
        const row = document.createElement("div");
        row.className = `task ${(task.status || "").toLowerCase()}`;
        row.innerHTML =
          `<span class="task-dot"></span>` +
          `<span class="agent">${escapeHtml(task.agent_id)}</span>` +
          `<span>${escapeHtml(task.objective)}</span>`;
        list.appendChild(row);
      });
      node.appendChild(list);
    }

    if (mission.summary) {
      const summary = document.createElement("div");
      summary.className = "mission-summary";
      summary.textContent = mission.summary;
      node.appendChild(summary);
    }
    return node;
  }

  // ------------------------------------------------------------ approvals

  async function refreshApprovals() {
    try {
      const data = await api("/api/v1/approvals");
      if (!data.count) { el.approvals.classList.add("hidden"); return; }

      el.approvalList.innerHTML = "";
      data.approvals.forEach((approval) => {
        const node = document.createElement("div");
        node.className = "approval";
        node.innerHTML =
          `<div><strong>${escapeHtml(approval.agent_id)}</strong> wants to run ` +
          `<strong>${escapeHtml(approval.tool_name)}</strong></div>` +
          `<code>${escapeHtml(JSON.stringify(approval.tool_arguments, null, 2))}</code>`;

        const actions = document.createElement("div");
        actions.className = "approval-actions";
        [["Approve", true, "btn-approve"], ["Deny", false, "btn-deny"]].forEach(
          ([label, approve, cls]) => {
            const btn = document.createElement("button");
            btn.className = cls;
            btn.textContent = label;
            btn.addEventListener("click", async () => {
              actions.querySelectorAll("button").forEach((b) => (b.disabled = true));
              try {
                await api(`/api/v1/approvals/${encodeURIComponent(approval.approval_id)}`, {
                  method: "POST", body: { approve },
                });
                pushLog("approval", `${approval.tool_name} ${approve ? "approved" : "denied"}`);
                refreshApprovals();
              } catch (err) {
                pushLog("error", err.message);
              }
            });
            actions.appendChild(btn);
          }
        );
        node.appendChild(actions);
        el.approvalList.appendChild(node);
      });
      el.approvals.classList.remove("hidden");
    } catch (_) { /* approvals are best-effort */ }
  }

  // --------------------------------------------------------------- todos
  async function loadTodos() {
    try {
      const [summary, nextData, listData] = await Promise.all([
        api("/api/v1/todos/summary"),
        api("/api/v1/todos/next"),
        api("/api/v1/todos?active_only=true"),
      ]);
      el.todoVerdict.textContent = summary.verdict || "";
      if (nextData.todo) {
        const t = nextData.todo;
        el.todoNext.classList.remove("hidden");
        el.todoNext.innerHTML =
          `<strong>${escapeHtml(t.title)}</strong>` +
          `<span class="hint"> · ${escapeHtml(t.track)} · ${escapeHtml(t.status)}</span>`;
      } else {
        el.todoNext.classList.add("hidden");
      }
      el.todoList.innerHTML = "";
      (listData.todos || []).slice(0, 20).forEach((todo) => {
        const row = document.createElement("div");
        row.className = "todo-row";
        row.innerHTML =
          `<span>${escapeHtml(todo.title)}</span>` +
          `<span class="hint">${escapeHtml(todo.track)}</span>` +
          `<button type="button" class="ghost" data-done="${escapeHtml(todo.todo_id)}">Done</button>`;
        row.querySelector("button")?.addEventListener("click", async () => {
          await api(`/api/v1/todos/${encodeURIComponent(todo.todo_id)}`, {
            method: "PATCH",
            body: { status: "done" },
          });
          loadTodos();
        });
        el.todoList.appendChild(row);
      });
    } catch (err) {
      pushLog("error", `Todos load failed: ${err.message}`);
    }
  }

  el.todoAddForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const title = el.todoTitle.value.trim();
    if (!title) return;
    try {
      await api("/api/v1/todos", { method: "POST", body: { title, track: "jarvis" } });
      el.todoTitle.value = "";
      loadTodos();
    } catch (err) {
      pushLog("error", err.message);
    }
  });

  // --------------------------------------------------------------- memory

  const SCOPES = ["all", "user", "project", "mission"];
  let memoryScope = "all";
  let memoryQuery = "";

  function renderScopeChips() {
    el.memoryScopes.innerHTML = "";
    SCOPES.forEach((scope) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `scope-chip${scope === memoryScope ? " active" : ""}`;
      btn.textContent = scope;
      btn.addEventListener("click", () => {
        memoryScope = scope;
        renderScopeChips();
        loadMemory();
      });
      el.memoryScopes.appendChild(btn);
    });
  }

  async function loadMemory() {
    const scopeParam = memoryScope === "all" ? "" : `scope=${encodeURIComponent(memoryScope)}`;
    try {
      let entries;
      if (memoryQuery) {
        const params = new URLSearchParams({ q: memoryQuery });
        if (scopeParam) params.set("scope", memoryScope);
        const found = await api(`/api/v1/memory/search?${params}`);
        entries = found.results;
        // Say plainly when semantic matching was unavailable, so a thin result
        // set isn't mistaken for an empty memory.
        el.memoryMethod.textContent = found.semantic
          ? `${entries.length} match(es) — semantic + keyword`
          : `${entries.length} match(es) — keyword only (semantic search unavailable)`;
        el.memoryMethod.classList.remove("hidden");
      } else {
        const listing = await api(`/api/v1/memory${scopeParam ? "?" + scopeParam : ""}`);
        entries = listing.entries;
        el.memoryMethod.classList.add("hidden");
      }
      renderMemory(entries);
    } catch (err) {
      pushLog("error", `Memory load failed: ${err.message}`);
    }
  }

  function renderMemory(entries) {
    el.memoryList.innerHTML = "";
    if (!entries.length) {
      const empty = document.createElement("div");
      empty.className = "memory-empty";
      empty.textContent = memoryQuery
        ? `Nothing matches "${memoryQuery}".`
        : "Nothing stored yet. Jarvis learns as you talk to it.";
      el.memoryList.appendChild(empty);
      return;
    }
    entries.forEach((entry) => el.memoryList.appendChild(renderMemoryEntry(entry)));
  }

  function renderMemoryEntry(entry) {
    const node = document.createElement("div");
    node.className = "memory-entry";

    const head = document.createElement("div");
    head.className = "memory-head";
    head.innerHTML =
      `<span class="memory-scope">${escapeHtml(entry.scope)}</span>` +
      `<span class="memory-key">${escapeHtml(entry.mem_key)}</span>`;
    node.appendChild(head);

    const value = document.createElement("div");
    value.className = "memory-value";
    value.textContent = entry.value;
    node.appendChild(value);

    const meta = document.createElement("div");
    meta.className = "memory-meta";
    const learned = (entry.created_at || "").slice(0, 10);
    const source = entry.source || "unknown";
    let provenance = `learned ${learned} · from ${source}`;
    if (entry.source_ref) provenance += ` · ${entry.source_ref}`;
    if (entry.sensitivity === "private") provenance += " · private";
    if (entry.supersedes) provenance += " · corrected";
    if (entry.similarity !== undefined) provenance += ` · ${Math.round(entry.similarity * 100)}% match`;
    meta.innerHTML = `<span>${escapeHtml(provenance)}</span>`;

    const actions = document.createElement("div");
    actions.className = "memory-actions";

    const edit = document.createElement("button");
    edit.textContent = "edit";
    edit.addEventListener("click", async () => {
      if (value.isContentEditable) {
        value.contentEditable = "false";
        edit.textContent = "edit";
        try {
          await api(`/api/v1/memory/${encodeURIComponent(entry.entry_id)}`, {
            method: "PUT", body: { value: value.textContent.trim() },
          });
          pushLog("system", `Memory updated: ${entry.mem_key}`);
          loadMemory();
        } catch (err) {
          pushLog("error", err.message);
        }
      } else {
        value.contentEditable = "true";
        value.focus();
        edit.textContent = "save";
      }
    });

    const remove = document.createElement("button");
    remove.className = "danger";
    remove.textContent = "delete";
    remove.addEventListener("click", async () => {
      // Direct deletion by the user needs no approval gate -- that gate exists
      // to hold agents accountable to the human, and this is the human.
      if (!confirm(`Delete "${entry.mem_key}" from ${entry.scope} memory?`)) return;
      try {
        await api(`/api/v1/memory/${encodeURIComponent(entry.entry_id)}`, { method: "DELETE" });
        pushLog("system", `Memory deleted: ${entry.mem_key}`);
        loadMemory();
      } catch (err) {
        pushLog("error", err.message);
      }
    });

    actions.append(edit, remove);
    meta.appendChild(actions);
    node.appendChild(meta);
    return node;
  }

  el.memoryForm.addEventListener("submit", (e) => {
    e.preventDefault();
    memoryQuery = el.memoryQuery.value.trim();
    loadMemory();
  });

  el.memoryAddForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const key = el.memoryNewKey.value.trim();
    const value = el.memoryNewValue.value.trim();
    if (!key || !value) return;
    try {
      await api("/api/v1/memory", {
        method: "POST",
        body: { scope: memoryScope === "all" ? "user" : memoryScope, key, value },
      });
      el.memoryNewKey.value = "";
      el.memoryNewValue.value = "";
      memoryQuery = "";
      el.memoryQuery.value = "";
      loadMemory();
    } catch (err) {
      pushLog("error", err.message);
    }
  });

  // ----------------------------------------------------------------- logs

  function pushLog(kind, message) {
    const line = document.createElement("div");
    line.className = `log-line ${kind}`;
    const time = new Date().toLocaleTimeString([], { hour12: false });
    line.innerHTML = `<span class="log-time">${time}</span><span class="log-msg">${escapeHtml(message)}</span>`;
    el.logs.appendChild(line);
    while (el.logs.children.length > 200) el.logs.removeChild(el.logs.firstChild);
    el.logs.scrollTop = el.logs.scrollHeight;
  }

  function stopSocket() {
    keepSocket = false;
    if (!socket) return;
    const current = socket;
    socket = null;
    current.close();
  }

  function connectSocket() {
    if (!keepSocket) return;
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const qs = token ? `?token=${encodeURIComponent(token)}` : "";
    const url = `${scheme}://${location.host}/ws/logs${qs}`;

    const current = new WebSocket(url);
    socket = current;
    current.addEventListener("message", (evt) => {
      try {
        const data = JSON.parse(evt.data);
        pushLog(data.event || "system", data.message || "");
        // Server-pushed updates keep the panel live; the poll is only a
        // fallback for events that arrive while the socket is reconnecting.
        if (["mission", "task", "approval"].includes(data.event)) refreshMissions();
      } catch (_) {
        pushLog("system", evt.data);
      }
    });
    current.addEventListener("close", () => {
      if (!keepSocket) return;
      pushLog("system", "Log stream disconnected — retrying in 5s.");
      setTimeout(connectSocket, 5000);
    });
    current.addEventListener("error", () => current.close());
  }

  el.logsToggle.addEventListener("click", () => {
    el.logsPanel.classList.toggle("hidden");
    el.logsToggle.classList.toggle("active", !el.logsPanel.classList.contains("hidden"));
  });

  el.exportBtn.addEventListener("click", async () => {
    // fetch (not a plain link) so the session token travels with the request.
    el.exportBtn.disabled = true;
    try {
      const res = await fetch("/api/v1/dataset/export", { headers: authHeaders() });
      if (!res.ok) throw new Error(`Export failed (${res.status})`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `jarvis-dataset-${Date.now()}.jsonl`;
      a.click();
      URL.revokeObjectURL(url);
      pushLog("system", "Dataset exported.");
    } catch (err) {
      pushLog("error", err.message);
    } finally {
      el.exportBtn.disabled = false;
    }
  });

  async function refreshStats() {
    try {
      const s = await api("/api/v1/telemetry/stats");
      el.stats.textContent =
        `${s.turns_logged} turns · ${s.mood_checkins} check-ins · ${s.training_examples_ready} training-ready`;
    } catch (_) { /* stats are decorative */ }
  }

  // --- provider settings (values stay in this page's inputs; not stored) ---

  const SOURCE_TEXT = {
    environment: "From the environment",
    saved: "Saved in this app",
    default: "Default",
    unset: "Not set",
    unreadable: "Saved value can't be read. Enter it again.",
  };

  const CHECK_TEXT = {
    reachable: "Connection ok.",
    auth_failed: "The provider rejected the credentials.",
    missing: "Nothing is configured to check.",
    unreachable: "Could not reach the provider.",
    invalid: "That address cannot be used.",
    disabled: "Ollama is turned off.",
  };

  function fieldLabel(name) {
    if (name === "api_key") return "API key";
    if (name === "base_url") return "Base URL";
    if (name === "model") return "Model";
    if (name === "enabled") return "Enabled";
    return name;
  }

  function primaryField(provider) {
    const fields = provider.fields || [];
    if (provider.id === "ollama") return fields.find((field) => field.name === "enabled") || fields[0];
    return fields.find((field) => field.name === "api_key") || fields[0];
  }

  function renderGateway(providers) {
    el.gatewayList.replaceChildren();
    const echo = document.createElement("div");
    echo.className = "gateway-row";
    const echoName = document.createElement("span");
    echoName.textContent = "Echo";
    const echoState = document.createElement("span");
    echoState.className = "badge good";
    echoState.textContent = "configured · local fallback";
    echo.append(echoName, echoState);
    el.gatewayList.append(echo);

    providers.forEach((provider) => {
      const row = document.createElement("div");
      row.className = "gateway-row";
      const label = document.createElement("span");
      label.textContent = provider.label;
      const state = document.createElement("span");
      state.className = `badge ${provider.configured ? "good" : "warn"}`;
      const field = primaryField(provider);
      const source = field ? SOURCE_TEXT[field.source] || field.source : "";
      state.textContent = `${provider.configured ? "configured" : "not configured"}${source ? ` · ${source}` : ""}`;
      row.append(label, state);
      el.gatewayList.append(row);
    });
  }

  function renderField(provider, field, note) {
    const block = document.createElement("div");
    block.className = "field-block";
    const label = document.createElement("span");
    label.className = "field-label";
    label.textContent = `${fieldLabel(field.name)} — ${SOURCE_TEXT[field.source] || field.source}`;

    const input = document.createElement("input");
    input.type = field.secret ? "password" : "text";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.maxLength = field.secret ? 512 : 200;
    if (!field.secret && typeof field.value === "string") input.value = field.value;
    if (field.name === "enabled") input.placeholder = "true or false";
    if (field.managed) input.readOnly = true;

    const actions = document.createElement("div");
    actions.className = "key-actions";
    const save = document.createElement("button");
    save.type = "button";
    save.className = "primary";
    save.textContent = "Save";
    save.disabled = !!field.managed;
    save.addEventListener("click", async () => {
      const value = input.value.trim();
      if (!value) {
        note.textContent = "Enter a value to save, or use Clear.";
        return;
      }
      try {
        await api(`/api/v1/settings/providers/${encodeURIComponent(provider.id)}`, {
          method: "PUT",
          body: { fields: { [field.name]: value } },
        });
        if (field.secret) input.value = "";
        note.textContent = "Saved.";
        await loadProviderSettings();
      } catch (err) {
        note.textContent = err.message;
      }
    });

    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "ghost";
    clear.textContent = "Clear";
    clear.disabled = !!field.managed;
    clear.addEventListener("click", async () => {
      try {
        await api(
          `/api/v1/settings/providers/${encodeURIComponent(provider.id)}?field=${encodeURIComponent(field.name)}`,
          { method: "DELETE" },
        );
        input.value = "";
        note.textContent = "Cleared.";
        await loadProviderSettings();
      } catch (err) {
        note.textContent = err.message;
      }
    });
    actions.append(save, clear);

    if (field.secret) {
      const reveal = document.createElement("button");
      reveal.type = "button";
      reveal.className = "ghost";
      reveal.textContent = "Reveal";
      const hide = document.createElement("button");
      hide.type = "button";
      hide.className = "ghost";
      hide.textContent = "Hide";
      hide.disabled = true;
      reveal.addEventListener("click", async () => {
        if (!input.value) {
          try {
            const data = await api(`/api/v1/settings/providers/${encodeURIComponent(provider.id)}/reveal`, {
              method: "POST",
              body: { field: field.name },
            });
            input.value = data.value || "";
          } catch (err) {
            note.textContent = err.message;
            return;
          }
        }
        input.type = "text";
        hide.disabled = false;
      });
      hide.addEventListener("click", () => {
        input.type = "password";
        hide.disabled = true;
      });
      actions.append(reveal, hide);
    }

    block.append(label, input, actions);
    return block;
  }

  function renderKeys(providers) {
    el.providerSettings.replaceChildren();
    providers.forEach((provider) => {
      const card = document.createElement("article");
      card.className = "provider-card";
      const head = document.createElement("div");
      head.className = "provider-head";
      const title = document.createElement("h3");
      title.textContent = provider.label;
      const badge = document.createElement("span");
      badge.className = `badge ${provider.configured ? "good" : "warn"}`;
      badge.textContent = provider.configured ? "configured" : "not configured";
      head.append(title, badge);

      const note = document.createElement("p");
      note.className = "provider-note";
      note.setAttribute("role", "status");
      card.append(head);
      (provider.fields || []).forEach((field) => card.append(renderField(provider, field, note)));

      const check = document.createElement("button");
      check.type = "button";
      check.className = "ghost";
      check.textContent = "Check connection";
      check.addEventListener("click", async () => {
        note.textContent = "Checking…";
        try {
          const result = await api(`/api/v1/settings/providers/${encodeURIComponent(provider.id)}/check`, {
            method: "POST",
          });
          let text = CHECK_TEXT[result.status] || "Check finished.";
          if (result.model_present === false) text += " The configured model is not on that server.";
          note.textContent = text;
        } catch (err) {
          note.textContent = err.message;
        }
      });
      card.append(check, note);
      el.providerSettings.append(card);
    });
  }

  async function loadProviderSettings() {
    try {
      const data = await api("/api/v1/settings/providers");
      const providers = data.providers || [];
      renderGateway(providers);
      renderKeys(providers);
    } catch (err) {
      el.gatewayList.textContent = `Couldn't load provider status: ${err.message}`;
    }
  }

  // --- end provider settings ---

  // ----------------------------------------------------------------- boot

  function renderHealth(health) {
    const engines = health.engines || {};
    const degraded = engines.degraded;
    const live = degraded ? "echo (no key)" : (engines.available || []).filter((e) => e !== "echo").join(" + ");

    el.engineBadge.textContent = live;
    el.engineBadge.className = `badge ${degraded ? "warn" : "good"}`;

    el.storageBadge.textContent = health.storage_durable ? "postgres" : "sqlite (ephemeral)";
    el.storageBadge.className = `badge ${health.storage_durable ? "good" : "warn"}`;

    const warnings = [];
    if (degraded) {
      warnings.push("No model key configured — replies come from the local fallback. Add a Groq or Gemini key under Keys.");
    }
    if (!health.storage_durable) {
      warnings.push("SQLite on an ephemeral host: check-ins are wiped on redeploy. Set DATABASE_URL to keep them.");
    }
    if (warnings.length) {
      el.banner.textContent = warnings.join(" ");
      el.banner.classList.remove("hidden");
    } else {
      el.banner.classList.add("hidden");
    }
  }

  async function boot() {
    try {
      const [greetData, health] = await Promise.all([
        api("/api/v1/greeting"),
        fetch("/health").then((r) => r.json()),
      ]);

      el.greeting.textContent = greetData.greeting + ".";
      renderMoodScale(greetData.mood_scale);
      renderHealth(health);

      // Reuse the stored session so a reload doesn't fragment the trajectory
      // log. Validated with a cheap existence check — never an LLM call.
      sessionId = localStorage.getItem(SESSION_KEY);
      let reused = false;
      if (sessionId) {
        const check = await api(`/api/v1/session/${encodeURIComponent(sessionId)}`);
        reused = check.exists;   // false once ephemeral storage wipes the row
      }
      if (!reused) {
        const s = await api("/api/v1/session/start", {
          method: "POST",
          body: { client_type: window.matchMedia("(display-mode: standalone)").matches ? "IOS_PWA" : "WEB_PWA", user_agent: navigator.userAgent },
        });
        sessionId = s.session_id;
        localStorage.setItem(SESSION_KEY, sessionId);
      }

      el.subtitle.textContent = "Jarvis is listening.";
      keepSocket = true;
      connectSocket();
      loadTrend();
      refreshStats();
      refreshMissions();
      renderScopeChips();
      loadMemory();
      loadTodos();
      loadProviderSettings();
    } catch (err) {
      if (err.unauthorized) {
        clearToken();
        showLock("Session expired — enter your passcode.");
        return;
      }
      el.subtitle.textContent = `Couldn't connect: ${err.message}`;
    }
  }

  async function start() {
    // A build that stored the passcode exchanges it once, then forgets it.
    const legacy = localStorage.getItem(PASSCODE_KEY);
    if (!token && legacy) {
      try {
        await login(legacy);
      } catch (_) {
        localStorage.removeItem(PASSCODE_KEY);
      }
    }
    // Probe auth: the loopback dev exception lets anyone through, in which
    // case the lock screen would be pointless friction.
    try {
      await api("/api/v1/greeting");
      el.app.classList.remove("hidden");
      await boot();
    } catch (err) {
      if (err.unauthorized) {
        clearToken();
        showLock();
      } else if (err.locked) {
        showLock(err.message);
      } else {
        el.app.classList.remove("hidden");
        el.subtitle.textContent = `Couldn't connect: ${err.message}`;
      }
    }
  }

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
  }

  start();
})();
