/* Facebook Group Scraper Dashboard – front-end behaviour (vanilla JS).
 *
 *  - confirm dialogs for destructive forms
 *  - responsive sidebar toggle
 *  - global status pill (polls /api/status/)
 *  - Run Scraper page: live logs, phase, progress, group statuses, start/stop
 *  - run detail terminals, group URL tests, browser-session buttons
 *  - Stats charts (Chart.js)
 */
(() => {
  "use strict";

  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const statusUrl = document.body.dataset.statusUrl;
  const ACTIVE_POLL_MS = 1500;
  const IDLE_POLL_MS = 5000;

  const BADGES = {
    NEVER: "badge-soft-secondary", QUEUED: "badge-soft-info", RUNNING: "badge-soft-primary",
    COMPLETED: "badge-soft-success", PARTIAL: "badge-soft-warning", FAILED: "badge-soft-danger",
    STOPPED: "badge-soft-warning", SKIPPED: "badge-soft-secondary", OK: "badge-soft-success",
    AUTHENTICATED: "badge-soft-success", NOT_AUTH: "badge-soft-danger", WAITING: "badge-soft-warning",
    CHECKING: "badge-soft-info", UNKNOWN: "badge-soft-secondary", MOCK: "badge-soft-info",
    ERROR: "badge-soft-danger", WARNING: "badge-soft-warning",
  };
  const badgeClass = (status) => BADGES[status] || "badge-soft-secondary";

  // ------------------------------------------------------------------ helpers

  async function postForm(url, formData) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrfToken, "X-Requested-With": "XMLHttpRequest", Accept: "application/json" },
      body: formData || new FormData(),
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* non-JSON error page */ }
    if (!payload.message) payload.message = response.ok ? "Done." : `Request failed (${response.status}).`;
    payload.ok = response.ok && payload.ok !== false;
    return payload;
  }

  async function getJSON(url) {
    const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
    if (response.redirected || !response.ok) throw new Error(`Status request failed (${response.status})`);
    return response.json();
  }

  function toast(message, variant = "primary") {
    const container = document.getElementById("toast-container");
    if (!container || !window.bootstrap) return;
    const el = document.createElement("div");
    el.className = `toast align-items-center text-bg-${variant} border-0`;
    el.setAttribute("role", "status");
    const wrap = document.createElement("div");
    wrap.className = "d-flex";
    const body = document.createElement("div");
    body.className = "toast-body";
    body.textContent = message;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "btn-close btn-close-white me-2 m-auto";
    close.setAttribute("data-bs-dismiss", "toast");
    wrap.append(body, close);
    el.append(wrap);
    container.append(el);
    const instance = new bootstrap.Toast(el, { delay: 6000 });
    el.addEventListener("hidden.bs.toast", () => el.remove());
    instance.show();
  }

  function setBadge(el, status, label) {
    if (!el) return;
    el.className = `badge ${badgeClass(status)}${status === "RUNNING" ? " badge-running" : ""}`;
    el.textContent = label || status;
  }

  function formatDuration(seconds) {
    if (seconds == null) return "—";
    const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
    if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
    if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
    return `${s}s`;
  }

  // ------------------------------------------------------------ confirmations

  document.addEventListener("submit", (event) => {
    const message = event.target.dataset?.confirm;
    if (message && !window.confirm(message)) event.preventDefault();
  });
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-confirm-button]");
    if (button && !window.confirm(button.dataset.confirmButton)) event.preventDefault();
  });

  // ------------------------------------------------------------------ sidebar

  document.querySelectorAll("[data-sidebar-toggle]").forEach((btn) =>
    btn.addEventListener("click", () => document.body.classList.toggle("sidebar-open")));
  document.querySelectorAll("[data-sidebar-close]").forEach((el) =>
    el.addEventListener("click", () => document.body.classList.remove("sidebar-open")));

  // ------------------------------------------------------------- status pill

  const pill = document.getElementById("global-status");
  function renderGlobalStatus(data) {
    const phase = data.phase || "IDLE";
    const active = Boolean(data.active_run_id);
    if (pill) {
      pill.classList.toggle("is-running", active);
      pill.querySelector(".label").textContent = active ? `${phase} · RUN #${data.active_run_id}` : "IDLE";
    }
    const card = document.querySelector("[data-live-status-card]");
    if (card) {
      const badge = card.querySelector("[data-phase]");
      badge.textContent = phase;
      badge.classList.toggle("phase-active", active);
      const detail = card.querySelector("[data-status-detail]");
      if (active && data.run) {
        detail.textContent = `Run #${data.run.id} · ${data.run.completed_groups + data.run.failed_groups}/${data.run.total_groups} groups · ${data.run.total_posts} new posts`;
      } else {
        detail.textContent = "Scraper idle";
      }
    }
  }

  // ----------------------------------------------------------------- terminal

  class Terminal {
    constructor(root) {
      this.root = root;
      this.body = root.querySelector("[data-terminal-body]");
      this.autoscroll = root.querySelector("[data-autoscroll]");
      this.liveIndicator = root.querySelector("[data-live-indicator]");
      this.lastId = 0;
      this.hasLines = false;
      root.querySelector("[data-terminal-clear]")?.addEventListener("click", () => this.clearView());
    }

    reset() {
      this.lastId = 0;
      this.hasLines = false;
      this.body.replaceChildren(this.placeholder("Waiting for log output…"));
    }

    clearView() {
      this.hasLines = false;
      this.body.replaceChildren(this.placeholder("View cleared – new lines will appear here."));
    }

    placeholder(text) {
      const div = document.createElement("div");
      div.className = "terminal-empty";
      div.textContent = text;
      return div;
    }

    setLive(live) {
      if (this.liveIndicator) this.liveIndicator.hidden = !live;
    }

    append(entries) {
      if (!entries.length) {
        if (!this.hasLines && this.lastId === 0) this.body.replaceChildren(this.placeholder("No log output for this run yet."));
        return;
      }
      const nearBottom = this.body.scrollHeight - this.body.scrollTop - this.body.clientHeight < 60;
      if (!this.hasLines) this.body.replaceChildren();
      const fragment = document.createDocumentFragment();
      for (const entry of entries) {
        const line = document.createElement("div");
        line.className = `log-line log-${entry.level}`;
        if (/^-{10,}$/.test(entry.message)) line.classList.add("rule");
        if (entry.message === "Facebook Group Fetcher" || entry.message.startsWith("Group : ")) line.classList.add("heading");
        const ts = document.createElement("span");
        ts.className = "ts";
        ts.textContent = entry.time;
        const msg = document.createElement("span");
        msg.className = "msg";
        msg.textContent = entry.message; // textContent: log text is never interpreted as HTML
        line.append(ts, msg);
        fragment.append(line);
        this.lastId = Math.max(this.lastId, entry.id);
      }
      this.body.append(fragment);
      this.hasLines = true;
      // Keep the DOM bounded during very long runs.
      while (this.body.childElementCount > 3000) this.body.firstElementChild.remove();
      if (this.autoscroll?.checked && (nearBottom || entries.length > 50)) this.body.scrollTop = this.body.scrollHeight;
    }
  }

  // ----------------------------------------------------------- run scraper page

  function initRunPage(page) {
    const terminal = new Terminal(page.querySelector("[data-terminal]"));
    const startBtn = page.querySelector("[data-btn-start]");
    const stopBtn = page.querySelector("[data-btn-stop]");
    const phaseBadge = page.querySelector("[data-phase]");
    const summary = page.querySelector("[data-run-summary]");
    const sessionCard = document.getElementById("session-card");
    let currentRunId = page.dataset.runId ? Number(page.dataset.runId) : null;
    let wasActive = page.dataset.active === "1";
    let reloadScheduled = false;
    let timer = null;

    function updateRun(run, active) {
      phaseBadge.textContent = active ? run.phase : "IDLE";
      phaseBadge.className = active ? "phase-badge phase-active" : "phase-badge";
      startBtn.disabled = active;
      stopBtn.disabled = !active || (run && run.stop_requested);
      if (!run) return;
      summary.hidden = false;
      page.querySelector("[data-run-number]").textContent = run.id;
      page.querySelector("[data-run-link]").href = `/runs/${run.id}/`;
      setBadge(page.querySelector("[data-run-status]"), run.status, run.stop_requested && active ? "Stopping…" : run.status_label);
      page.querySelector("[data-current-group]").textContent = active && run.current_group ? `Now: ${run.current_group}` : "";
      page.querySelector('[data-count="groups"]').textContent = `${run.completed_groups}/${run.total_groups}`;
      page.querySelector('[data-count="failed"]').textContent = run.failed_groups;
      page.querySelector('[data-count="skipped"]').textContent = run.skipped_groups;
      page.querySelector('[data-count="posts"]').textContent = run.total_posts;
      page.querySelector('[data-count="duplicates"]').textContent = run.duplicate_posts;
      page.querySelector('[data-count="duration"]').textContent = formatDuration(run.duration_seconds);
      page.querySelector("[data-progress]").style.width = `${run.progress}%`;
      const errorBox = page.querySelector("[data-run-error]");
      errorBox.hidden = !run.error_message;
      errorBox.textContent = run.error_message || "";
    }

    function updateGroups(groups) {
      for (const g of groups) {
        const row = page.querySelector(`tr[data-rg-id="${g.id}"]`);
        if (!row) continue;
        const badge = row.querySelector("[data-rg-status]");
        setBadge(badge, g.status, g.status_label);
        badge.title = g.error || "";
        const posts = row.querySelector("[data-rg-posts]");
        if (posts && (g.posts || g.status !== "QUEUED")) {
          posts.textContent = g.attempts > 1 ? `${g.posts} new · try ${g.attempts}` : `${g.posts} new`;
        }
      }
    }

    function updateSession(browser) {
      if (!sessionCard || !browser) return;
      setBadge(sessionCard.querySelector("[data-session-status]"), browser.status, browser.status_label);
      sessionCard.querySelector("[data-session-message]").textContent = browser.message || "";
      sessionCard.querySelector("[data-session-checked]").textContent = browser.checked_at ? `checked ${browser.checked_at}` : "";
      const locked = browser.busy || startBtn.disabled;
      sessionCard.querySelectorAll("[data-browser-task]").forEach((b) => { b.disabled = locked; });
    }

    async function poll() {
      clearTimeout(timer);
      let active = wasActive;
      try {
        const data = await getJSON(`${statusUrl}?since=${terminal.lastId}`);
        renderGlobalStatus(data);
        active = Boolean(data.active_run_id);
        const run = data.run;
        if (run && run.id !== currentRunId) {
          // A different run became current (started here, in another tab or via the CLI).
          currentRunId = run.id;
          terminal.reset();
          if (active && !reloadScheduled) { reloadScheduled = true; window.location.reload(); return; }
          timer = setTimeout(poll, 50);
          return;
        }
        updateRun(run, active);
        updateGroups(data.groups || []);
        terminal.append(data.logs || []);
        terminal.setLive(active);
        updateSession(data.browser);
        if (wasActive && !active && !reloadScheduled) {
          // Run finished: refresh to show the next batch.
          reloadScheduled = true;
          toast(`Run #${run.id} finished: ${run.status_label}.`, run.status === "COMPLETED" ? "success" : "warning");
          setTimeout(() => window.location.reload(), 3000);
        }
        wasActive = active;
      } catch (err) {
        console.warn(err);
      }
      timer = setTimeout(poll, active ? ACTIVE_POLL_MS : IDLE_POLL_MS);
    }

    page.querySelectorAll("form[data-run-action]").forEach((form) => {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = form.querySelector("button");
        button.disabled = true;
        const result = await postForm(form.action, new FormData(form));
        toast(result.message, result.ok ? "success" : "danger");
        if (result.ok && form.dataset.runAction === "start") {
          reloadScheduled = true;
          window.location.reload();
          return;
        }
        if (!result.ok) button.disabled = false;
        poll();
      });
    });

    sessionCard?.querySelectorAll("[data-browser-task]").forEach((button) => {
      button.addEventListener("click", async () => {
        const url = button.dataset.browserTask === "auth" ? sessionCard.dataset.authUrl : sessionCard.dataset.checkUrl;
        sessionCard.querySelectorAll("[data-browser-task]").forEach((b) => { b.disabled = true; });
        const result = await postForm(url);
        toast(result.message, result.ok ? "primary" : "danger");
        poll();
      });
    });

    poll();
  }

  // --------------------------------------------------- stand-alone terminals

  function initStandaloneTerminal(root) {
    const terminal = new Terminal(root);
    const runId = root.dataset.runId;
    const live = root.hasAttribute("data-live");
    let timer = null;

    async function load() {
      clearTimeout(timer);
      let active = live;
      try {
        const query = terminal.lastId ? `since=${terminal.lastId}` : "full=1";
        const data = await getJSON(`${statusUrl}?run=${runId}&${query}`);
        renderGlobalStatus(data);
        terminal.append(data.logs || []);
        active = Boolean(data.run && data.run.is_active);
        terminal.setLive(active);
        if (live && !active) { setTimeout(() => window.location.reload(), 2500); return; }
      } catch (err) {
        console.warn(err);
      }
      if (active) timer = setTimeout(load, ACTIVE_POLL_MS);
    }
    load();
  }

  // --------------------------------------------------------------- group tests

  function waitForBrowserTask(done) {
    const check = async () => {
      try {
        const data = await getJSON(`${statusUrl}?logs=0`);
        if (data.browser && !data.browser.busy) { done(data.browser); return; }
      } catch (err) { console.warn(err); }
      setTimeout(check, 2000);
    };
    setTimeout(check, 1500);
  }

  document.querySelectorAll("[data-test-url]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      const result = await postForm(button.dataset.testUrl);
      const variant = !result.ok ? "danger" : result.status === "OK" ? "success" : result.status === "WARNING" ? "warning" : "primary";
      toast(result.message, variant);
      if (result.ok && result.async) {
        waitForBrowserTask((browser) => {
          toast(browser.message, "primary");
          setTimeout(() => window.location.reload(), 1500);
        });
      } else {
        setTimeout(() => window.location.reload(), 1500);
      }
    });
  });

  // ----------------------------------------------------------------- charts

  function initCharts() {
    const dataEl = document.getElementById("chart-data");
    if (!dataEl || !window.Chart) return;
    const data = JSON.parse(dataEl.textContent);
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.color = "#6b778c";
    const grid = { color: "#eef0f5" };
    const common = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } };

    new Chart(document.getElementById("chart-posts"), {
      type: "line",
      data: { labels: data.days, datasets: [{ label: "Posts", data: data.posts, borderColor: "#2563eb",
        backgroundColor: "rgba(37,99,235,.12)", fill: true, tension: .3, pointRadius: 2 }] },
      options: { ...common, scales: { y: { beginAtZero: true, grid, ticks: { precision: 0 } }, x: { grid: { display: false } } } },
    });

    new Chart(document.getElementById("chart-runs"), {
      type: "bar",
      data: { labels: data.days, datasets: [
        { label: "Completed", data: data.runs_ok, backgroundColor: "#16a34a" },
        { label: "Partial / stopped", data: data.runs_partial, backgroundColor: "#f59e0b" },
        { label: "Failed", data: data.runs_failed, backgroundColor: "#dc2626" },
      ] },
      options: { ...common, plugins: { legend: { display: true, position: "bottom", labels: { boxWidth: 12 } } },
        scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, beginAtZero: true, grid, ticks: { precision: 0 } } } },
    });

    new Chart(document.getElementById("chart-groups"), {
      type: "bar",
      data: { labels: data.group_names, datasets: [{ label: "Posts", data: data.group_posts, backgroundColor: "#3b82f6", borderRadius: 6 }] },
      options: { ...common, indexAxis: "y", scales: { x: { beginAtZero: true, grid, ticks: { precision: 0 } }, y: { grid: { display: false } } } },
    });
  }

  // ------------------------------------------------------------------- boot

  const runPage = document.getElementById("run-page");
  if (runPage) {
    initRunPage(runPage);
  } else {
    document.querySelectorAll("[data-terminal][data-run-id]").forEach(initStandaloneTerminal);
    if (pill && statusUrl) {
      const refresh = async () => {
        let delay = IDLE_POLL_MS;
        try {
          const data = await getJSON(`${statusUrl}?logs=0`);
          renderGlobalStatus(data);
          if (data.active_run_id) delay = 3000;
        } catch (err) { console.warn(err); }
        setTimeout(refresh, delay);
      };
      refresh();
    }
  }
  initCharts();
})();
