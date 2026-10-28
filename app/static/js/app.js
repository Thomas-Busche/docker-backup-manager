// Docker Backup Manager - frontend (vanilla JS, no build step required)
(function() { const t = localStorage.getItem("dbm-theme"); if (t) document.documentElement.dataset.theme = t; })();
const root = document.getElementById("app");
const state = { route: "dashboard", user: null, jobs: {} };

// ---------- API helper ----------
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
  });
  if (res.status === 401) {
    render(loginScreen());
    throw new Error("not authenticated");
  }
  const isJson = res.headers.get("content-type")?.includes("application/json");
  const data = isJson ? await res.json() : null;
  if (!res.ok) {
    throw new Error((data && data.detail) || `Request failed (${res.status})`);
  }
  return data;
}

function fmtBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}
function fmtDate(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return d.toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
}
function fmtDateLong(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return d.toLocaleString("de-DE", { dateStyle: "full", timeStyle: "medium" });
}
function fmtDuration(sec) {
  if (sec == null) return "-";
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return `${m}m ${s}s`;
}
function escHtml(s) {
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
function fmtSpeed(bytesPerSec) {
  if (bytesPerSec == null) return null;
  return `${fmtBytes(bytesPerSec)}/s`;
}

// ---------- toasts ----------
let toastStack;
function toast(message, type = "ok") {
  if (!toastStack) {
    toastStack = document.createElement("div");
    toastStack.className = "toast-stack";
    document.body.appendChild(toastStack);
  }
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  toastStack.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

// ---------- global job tray (always visible, on every page) ----------
let jobTrayEl;
let settingsClockTimer = null;
const toastedJobIds = new Set();
const finishedJobHideAt = new Map(); // jobId -> timestamp when it should be removed from the tray

function ensureJobTray() {
  if (!jobTrayEl) {
    jobTrayEl = document.createElement("div");
    jobTrayEl.className = "job-tray";
    document.body.appendChild(jobTrayEl);
  }
  return jobTrayEl;
}

function _jobStatusBadge(status) {
  if (status === "running") return { cls: "running", label: "läuft" };
  if (status === "cancelling") return { cls: "running", label: "wird abgebrochen…" };
  if (status === "cancelled") return { cls: "neutral", label: "abgebrochen" };
  if (status === "success") return { cls: "ok", label: "fertig" };
  return { cls: "failed", label: "fehlgeschlagen" };
}

function renderJobCard(job) {
  const { cls, label } = _jobStatusBadge(job.status);
  const card = h(`
    <div class="card job-card" data-job-id="${job.id}">
      <div class="job-title"><span>${job.kind === "backup" ? "💾" : "♻️"} ${job.label}</span>
        <span class="badge ${cls}">${label}</span></div>
      <div class="muted job-step" style="font-size:.8rem">${job.step_name}</div>
      <div class="progress-wrap">
        <div class="progress-bar"><div style="width:${job.percent}%"></div></div>
        <div class="progress-meta">
          <span class="job-step-count">Schritt ${job.current_step}/${job.total_steps}</span>
          <span class="job-elapsed">${job.status === "running"
            ? "Verstrichen: " + fmtDuration(job.elapsed_seconds)
              + (job.eta_seconds != null ? " · ETA " + fmtDuration(job.eta_seconds) : "")
              + (fmtSpeed(job.speed_bytes_per_sec) ? " · Leserate: " + fmtSpeed(job.speed_bytes_per_sec) : "")
              + (fmtSpeed(job.upload_speed_bytes_per_sec) ? " · Upload: " + fmtSpeed(job.upload_speed_bytes_per_sec) : "")
            : fmtDuration(job.elapsed_seconds)}</span>
        </div>
      </div>
      ${job.error ? `<div class="error-msg">${job.error}</div>` : ""}
      <div class="job-log-wrap" style="margin-top:6px;">
        <button type="button" class="btn job-log-toggle" style="padding:2px 8px;font-size:.75rem;opacity:.7;">📋 Log</button>
        <div class="job-log-panel" style="display:none;margin-top:6px;background:#1a1a1a;color:#d0d0d0;border-radius:4px;padding:8px;max-height:200px;overflow-y:auto;font-family:monospace;font-size:.75rem;line-height:1.5;"></div>
      </div>
      ${job.cancellable ? `<div class="row-actions" style="margin-top:8px;"><button type="button" class="btn danger job-cancel-btn" style="padding:4px 10px; font-size:.8rem;">Abbrechen</button></div>` : ""}
    </div>
  `);
  _wireJobCancelButton(card, job.id);
  _wireJobLogToggle(card, job.id);
  return card;
}

function _wireJobLogToggle(card, jobId) {
  const btn = card.querySelector(".job-log-toggle");
  const panel = card.querySelector(".job-log-panel");
  if (!btn || !panel) return;
  let pollTimer = null;

  async function refreshLog() {
    try {
      const data = await api(`/api/jobs/${jobId}/logs`);
      const atBottom = panel.scrollHeight - panel.scrollTop <= panel.clientHeight + 20;
      if (!data.lines.length) {
        panel.innerHTML = '<div style="opacity:.5;padding:4px 0">Noch keine Log-Einträge — Backup läuft noch oder wurde vor diesem Update gestartet.</div>';
        return;
      }
      panel.innerHTML = data.lines.map(l =>
        `<div><span style="opacity:.5">${l.ts}</span> ${escHtml(l.msg)}</div>`
      ).join("");
      if (atBottom) panel.scrollTop = panel.scrollHeight;
    } catch (e) {
      panel.innerHTML = `<div style="color:#f66">Fehler beim Laden: ${escHtml(e.message)}</div>`;
    }
  }

  btn.addEventListener("click", () => {
    const open = panel.style.display !== "none";
    panel.style.display = open ? "none" : "block";
    btn.style.opacity = open ? ".7" : "1";
    if (!open) {
      refreshLog();
      pollTimer = setInterval(refreshLog, 2000);
    } else {
      clearInterval(pollTimer);
    }
  });
}

function _wireJobCancelButton(card, jobId) {
  const btn = card.querySelector(".job-cancel-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Breche ab…";
    try {
      await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
    } catch (e) {
      toast(e.message, "error");
      btn.disabled = false;
      btn.textContent = "Abbrechen";
    }
  });
}

function updateJobCard(card, job) {
  const { cls, label } = _jobStatusBadge(job.status);
  const badge = card.querySelector(".badge");
  badge.className = `badge ${cls}`;
  badge.textContent = label;
  card.querySelector(".job-step").textContent = job.step_name;
  card.querySelector(".progress-bar > div").style.width = `${job.percent}%`;
  card.querySelector(".job-step-count").textContent = `Schritt ${job.current_step}/${job.total_steps}`;
  card.querySelector(".job-elapsed").textContent = job.status === "running"
    ? "Verstrichen: " + fmtDuration(job.elapsed_seconds)
      + (job.eta_seconds != null ? " · ETA " + fmtDuration(job.eta_seconds) : "")
      + (fmtSpeed(job.speed_bytes_per_sec) ? " · Leserate: " + fmtSpeed(job.speed_bytes_per_sec) : "")
      + (fmtSpeed(job.upload_speed_bytes_per_sec) ? " · Upload: " + fmtSpeed(job.upload_speed_bytes_per_sec) : "")
    : fmtDuration(job.elapsed_seconds);
  const existingError = card.querySelector(".error-msg");
  if (job.error && !existingError) {
    card.appendChild(h(`<div class="error-msg">${job.error}</div>`));
  } else if (job.error && existingError) {
    existingError.textContent = job.error;
  } else if (!job.error && existingError) {
    existingError.remove();
  }
  const existingCancelWrap = card.querySelector(".job-cancel-btn")?.closest(".row-actions");
  if (job.cancellable && !existingCancelWrap) {
    const wrap = h(`<div class="row-actions" style="margin-top:8px;"><button type="button" class="btn danger job-cancel-btn" style="padding:4px 10px; font-size:.8rem;">Abbrechen</button></div>`);
    card.appendChild(wrap);
    _wireJobCancelButton(card, job.id);
  } else if (!job.cancellable && existingCancelWrap) {
    existingCancelWrap.remove();
  }
}

function _syncJobCardsInContainer(container, jobs, emptyMessage) {
  if (!jobs.length) {
    container.querySelectorAll(".job-card").forEach(c => c.remove());
    if (emptyMessage && !container.querySelector(".empty-state")) {
      container.innerHTML = `<div class="empty-state">${emptyMessage}</div>`;
    }
    return;
  }
  const emptyState = container.querySelector(".empty-state");
  if (emptyState) emptyState.remove();
  const visibleIds = new Set(jobs.map((j) => String(j.id)));
  container.querySelectorAll(".job-card").forEach((card) => {
    if (!visibleIds.has(card.dataset.jobId)) card.remove();
  });
  jobs.forEach((job) => {
    const existing = container.querySelector(`.job-card[data-job-id="${job.id}"]`);
    if (existing) updateJobCard(existing, job);
    else container.appendChild(renderJobCard(job));
  });
}

// Single shared poll for both the floating tray (every page) and the
// Dashboard's "Letzte Jobs" list (only patched if that element is currently
// on screen) - one /api/jobs request per tick, not one per consumer.
async function pollGlobalJobs() {
  let jobs;
  try {
    jobs = (await api("/api/jobs")).jobs;
  } catch (e) { return; }

  const now = Date.now();
  for (const job of jobs) {
    const terminal = job.status === "success" || job.status === "failed" || job.status === "cancelled";
    if (terminal && !toastedJobIds.has(job.id)) {
      toastedJobIds.add(job.id);
      if (job.status === "success") {
        toast(`${job.label}: erfolgreich abgeschlossen`, "ok");
      } else if (job.status === "cancelled") {
        toast(`${job.label}: abgebrochen`, "ok");
      } else {
        toast(`${job.label}: fehlgeschlagen – ${job.error}`, "error");
      }
      finishedJobHideAt.set(job.id, now + 5000);
    }
  }

  // "cancelling" is still active — keep it in the tray until it's fully done.
  const trayVisible = jobs.filter((j) => j.status === "running" || j.status === "cancelling" ||
    (finishedJobHideAt.has(j.id) && finishedJobHideAt.get(j.id) > now)).slice(0, 5);
  _syncJobCardsInContainer(ensureJobTray(), trayVisible, null);

  const dashboardContainer = document.getElementById("jobs-container");
  if (dashboardContainer) {
    _syncJobCardsInContainer(dashboardContainer, jobs.slice(0, 6), "Keine Jobs bisher");
  }
}

function startGlobalJobPoller() {
  pollGlobalJobs();
  setInterval(pollGlobalJobs, 1500);
}

// ---------- render helpers ----------
function h(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}
function render(el) {
  root.innerHTML = "";
  root.appendChild(el);
}

// ---------- Auth screens ----------
function loginScreen() {
  const wrap = h(`
    <div class="center-screen">
      <div class="auth-card">
        <h1>Docker Backup Manager</h1>
        <p class="sub">Melde dich an, um fortzufahren</p>
        <div class="field"><label>Benutzername</label><input type="text" id="login-user" /></div>
        <div class="field"><label>Passwort</label><input type="password" id="login-pass" /></div>
        <button class="btn primary block" id="login-btn">Anmelden</button>
        <div class="error-msg" id="login-error"></div>
      </div>
    </div>
  `);
  wrap.querySelector("#login-btn").addEventListener("click", async () => {
    const username = wrap.querySelector("#login-user").value.trim();
    const password = wrap.querySelector("#login-pass").value;
    try {
      await api("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
      await boot();
    } catch (e) {
      wrap.querySelector("#login-error").textContent = e.message;
    }
  });
  return wrap;
}

function setupScreen() {
  const wrap = h(`
    <div class="center-screen">
      <div class="auth-card">
        <h1>Willkommen</h1>
        <p class="sub">Erstelle das erste Administrator-Konto</p>
        <div class="field"><label>Benutzername</label><input type="text" id="su-user" /></div>
        <div class="field"><label>Passwort (min. 8 Zeichen)</label><input type="password" id="su-pass" /></div>
        <button class="btn primary block" id="su-btn">Konto erstellen</button>
        <div class="error-msg" id="su-error"></div>
      </div>
    </div>
  `);
  wrap.querySelector("#su-btn").addEventListener("click", async () => {
    const username = wrap.querySelector("#su-user").value.trim();
    const password = wrap.querySelector("#su-pass").value;
    try {
      await api("/api/auth/setup", { method: "POST", body: JSON.stringify({ username, password }) });
      await boot();
    } catch (e) {
      wrap.querySelector("#su-error").textContent = e.message;
    }
  });
  return wrap;
}

// ---------- Shell / layout ----------
const NAV_ITEMS = [
  { key: "dashboard", label: "Dashboard", icon: "▦" },
  { key: "containers", label: "Container", icon: "⚙" },
  { key: "backups", label: "Backups", icon: "⭘" },
  { key: "schedules", label: "Zeitpläne", icon: "⏰" },
  { key: "logs", label: "Logs", icon: "📜" },
  { key: "settings", label: "Einstellungen", icon: "⚙️" },
];

function shell(activeKey, contentEl) {
  const wrap = h(`
    <div style="display:flex; width:100%;">
      <div class="sidebar">
        <div class="brand">🐳 Backup Manager</div>
        <div id="nav"></div>
        <div class="spacer"></div>
        <div class="user-row" id="user-row"></div>
      </div>
      <div class="main" id="main"></div>
      <nav class="mobile-nav" id="mobile-nav"></nav>
    </div>
  `);
  const nav = wrap.querySelector("#nav");
  const mobileNav = wrap.querySelector("#mobile-nav");
  NAV_ITEMS.forEach((item) => {
    const navEl = h(`<div class="nav-item ${item.key === activeKey ? "active" : ""}">
      <span>${item.icon}</span><span>${item.label}</span></div>`);
    navEl.addEventListener("click", () => navigate(item.key));
    nav.appendChild(navEl);
    const mEl = h(`<div class="mobile-nav-item ${item.key === activeKey ? "active" : ""}">
      <span class="m-icon">${item.icon}</span><span>${item.label}</span></div>`);
    mEl.addEventListener("click", () => navigate(item.key));
    mobileNav.appendChild(mEl);
  });
  const userRow = wrap.querySelector("#user-row");
  const adminBadge = state.user && state.user.is_admin ? ' <span class="badge ok" style="font-size:10px; padding:1px 5px;">Admin</span>' : "";
  userRow.innerHTML = `${state.user ? escHtml(state.user.username) : ""}${adminBadge} &middot; <a href="#" id="logout-link">Abmelden</a>
    <div style="font-size:10px; color:var(--muted); margin-top:2px; opacity:0.6;">v${state.appVersion || ""}</div>
    <button id="theme-toggle" style="margin-top:8px; width:100%; background:none; border:1px solid var(--border); border-radius:6px; color:var(--text-dim); cursor:pointer; padding:4px 8px; font-size:.75rem;">🌙 Dark / ☀️ Light</button>`;
  userRow.querySelector("#logout-link").addEventListener("click", async (e) => {
    e.preventDefault();
    await api("/api/auth/logout", { method: "POST" });
    render(loginScreen());
  });
  (function() {
    const saved = localStorage.getItem("dbm-theme");
    if (saved) document.documentElement.dataset.theme = saved;
    userRow.querySelector("#theme-toggle").addEventListener("click", () => {
      const cur = document.documentElement.dataset.theme;
      const next = (cur === "light") ? "dark" : "light";
      document.documentElement.dataset.theme = next;
      localStorage.setItem("dbm-theme", next);
    });
  })();
  wrap.querySelector("#main").appendChild(contentEl);
  return wrap;
}

async function navigate(key) {
  state.route = key;
  if (settingsClockTimer) {
    clearInterval(settingsClockTimer);
    settingsClockTimer = null;
  }
  try {
    let content;
    if (key === "dashboard") content = await dashboardPage();
    else if (key === "containers") content = await containersPage();
    else if (key === "backups") content = await backupsPage();
    else if (key === "schedules") content = await schedulesPage();
    else if (key === "logs") content = await logsPage();
    else if (key === "settings") content = await settingsPage();
    render(shell(key, content));
  } catch (e) {
    if (e.message !== "not authenticated") toast(e.message, "error");
  }
}

// ---------- Dashboard ----------
async function dashboardPage() {
  const [overview, backupsData, jobsData, targetsData] = await Promise.all([
    api("/api/settings/overview"), api("/api/backups"), api("/api/jobs"),
    api("/api/settings/storage-targets").catch(() => ({ targets: [] })),
  ]);

  const enabledTargets = (targetsData.targets || []).filter((t) => t.enabled && t.type !== "local_path");
  const spaceResults = await Promise.all(
    enabledTargets.map((t) =>
      api(`/api/settings/storage-targets/${t.id}/space`)
        .then((s) => ({ ...t, space: s }))
        .catch(() => ({ ...t, space: null }))
    )
  );
  const targetsWithSpace = spaceResults.filter((t) => t.space && t.space.total_bytes != null);
  // Collect member container names from landscape backups so they aren't
  // double-counted in the dashboard total (1 Immich landscape = 1 backup, not 5)
  const memberNames = new Set();
  Object.values(backupsData.groups).forEach((versions) => {
    versions.forEach((v) => {
      if (v.backup_type === "landscape") (v.member_names || []).forEach((n) => memberNames.add(n));
    });
  });
  const allRecords = Object.entries(backupsData.groups).flatMap(([name, versions]) =>
    versions
      .filter((v) => v.backup_type === "landscape" || !memberNames.has(name))
      .map((v) => ({ ...v, name })));
  const totalBackups = allRecords.length;
  const lastBackup = allRecords.sort((a, b) => new Date(b.created_at) - new Date(a.created_at))[0];

  const wrap = h(`<div>
    <div class="page-header"><h2>Dashboard</h2></div>
    <div class="grid cols-3">
      <div class="card stat-card">
        <div class="label">Docker-Status</div>
        <div class="value">${overview.docker_available ? "✅ Verbunden" : "⚠️ Nicht erreichbar"}</div>
        <div class="sub">${overview.docker_available ? "" : (overview.docker_error || "")}</div>
      </div>
      <div class="card stat-card">
        <div class="label">Backups gesamt</div>
        <div class="value">${totalBackups}</div>
      </div>
      <div class="card stat-card">
        <div class="label">Letztes Backup</div>
        <div class="value" style="font-size:1.1rem">${lastBackup ? lastBackup.name : "-"}</div>
        <div class="sub">${lastBackup ? fmtDate(lastBackup.created_at) : ""}</div>
      </div>
    </div>
    ${targetsWithSpace.length ? `
    <div class="section-title">Speicherplatz Ziele</div>
    <div class="grid cols-3">
      ${targetsWithSpace.map((t) => {
        const pct = t.space.total_bytes ? Math.round((t.space.used_bytes / t.space.total_bytes) * 100) : null;
        const barColor = pct == null ? "var(--accent)" : pct >= 90 ? "var(--error, #e05)" : pct >= 75 ? "var(--warn)" : "var(--accent)";
        return `<div class="card stat-card">
          <div class="label">${t.name} <span class="muted" style="font-size:.75rem">(${t.type})</span></div>
          <div class="value" style="font-size:1.15rem">${fmtBytes(t.space.free_bytes)} frei</div>
          <div class="sub">${fmtBytes(t.space.used_bytes)} / ${fmtBytes(t.space.total_bytes)}</div>
          ${pct != null ? `<div style="margin-top:8px; background:var(--bg); border-radius:4px; height:6px; overflow:hidden;">
            <div style="width:${pct}%; height:100%; background:${barColor}; transition:width .3s;"></div>
          </div>` : ""}
        </div>`;
      }).join("")}
    </div>` : ""}
    ${overview.encryption_error
      ? `<div class="card" style="margin-top:16px; border-color: var(--warn);">
           ⚠️ <span class="mono">DBM_ENCRYPTION_KEY</span> ist ungültig: ${overview.encryption_error}
           Backups werden deshalb aktuell <strong>unverschlüsselt</strong> gespeichert (siehe Einstellungen).
         </div>`
      : overview.encryption_enabled
      ? ""
      : `<div class="card" style="margin-top:16px; border-color: var(--warn);">
           ⚠️ Backups werden aktuell <strong>unverschlüsselt</strong> gespeichert. Setze
           <span class="mono">DBM_ENCRYPTION_KEY</span>, um Verschlüsselung zu aktivieren (siehe Einstellungen).
         </div>`}
    ${overview.timezone_error
      ? `<div class="card" style="margin-top:16px; border-color: var(--warn);">⚠️ ${overview.timezone_error}</div>`
      : ""}
    <div class="section-title">Letzte Jobs</div>
    <div id="jobs-container" class="grid cols-3"></div>
  </div>`);

  // Initial render only - pollGlobalJobs() (already running every 1.5s from
  // boot()) patches #jobs-container on every tick from here on, the same
  // single /api/jobs request that also drives the floating tray.
  const jobsContainer = wrap.querySelector("#jobs-container");
  if (!jobsData.jobs.length) {
    jobsContainer.appendChild(h(`<div class="empty-state">Keine Jobs bisher</div>`));
  } else {
    jobsData.jobs.slice(0, 6).forEach((job) => jobsContainer.appendChild(renderJobCard(job)));
  }

  return wrap;
}

// ---------- Shared: pick storage targets before starting a manual backup ----------
async function pickStorageTargetsAndRun(title, runFn) {
  let storageTargets = [];
  try { storageTargets = (await api("/api/settings/storage-targets")).targets; } catch (e) {}
  const targetsHtml = storageTargets.map((t) => `
    <label style="display:flex; align-items:center; gap:8px; padding:6px 0;">
      <input type="checkbox" class="b-storage-target" value="${t.id}" style="width:auto;" ${t.enabled ? "checked" : "disabled"} />
      ${t.name} <span class="muted" style="font-size:.78rem">(${t.type})</span>
      ${t.enabled ? "" : '<span class="badge neutral">deaktiviert</span>'}
    </label>`).join("");
  const STREAMABLE_TYPES = ["local_path", "smb", "s3", "rclone"];
  const streamTargetOptionsHtml = storageTargets
    .filter((t) => STREAMABLE_TYPES.includes(t.type) && t.enabled)
    .map((t) => `<option value="${t.id}">${t.name} (${t.type})</option>`).join("");
  const overlay = h(`
    <div class="modal-overlay">
      <div class="modal">
        <h3>${title}</h3>
        ${storageTargets.length ? `
        <div class="field">
          <label>Zusätzlich hochladen nach (neben dem lokalen Speicher)</label>
          <div>${targetsHtml}</div>
        </div>
        <div class="field">
          <label>Volumes direkt streamen, ohne lokal zu speichern (optional)</label>
          <select id="b-stream-target">
            <option value="">Nein - lokal speichern (Standard)</option>
            ${streamTargetOptionsHtml}
          </select>
          <div class="muted" style="font-size:.75rem; margin-top:4px;">
            Für große Volumes bei wenig lokalem Speicherplatz. Umgeht die AES-256-Verschlüsselung
            dieser App (die greift nur bei lokal geschriebenen Dateien) - nur bei vertrauenswürdigem
            Ziel nutzen. Nur lokaler Pfad, SMB, S3 und rclone unterstützt.
          </div>
        </div>` : ""}
        <div class="field">
          <label style="display:flex; align-items:center; gap:8px;">
            <input type="checkbox" id="b-stop-containers" style="width:auto;" />
            Container(n) vor dem Backup stoppen, danach wieder starten
          </label>
          <div class="muted" style="font-size:.75rem; margin-top:4px;">
            Für ein anwendungskonsistentes statt nur crash-konsistentes Backup (z. B. bei
            Datenbanken). Bedeutet eine kurze Downtime für die Dauer des Backups.
          </div>
        </div>
        <div class="row-actions">
          <button class="btn" id="cancel-btn">Abbrechen</button>
          <button class="btn primary" id="start-btn">Backup starten</button>
        </div>
      </div>
    </div>
  `);
  overlay.querySelector("#cancel-btn").addEventListener("click", () => overlay.remove());
  overlay.querySelector("#start-btn").addEventListener("click", async () => {
    const ids = Array.from(overlay.querySelectorAll(".b-storage-target:checked")).map((el) => parseInt(el.value, 10));
    const streamTargetEl = overlay.querySelector("#b-stream-target");
    const streamTargetId = streamTargetEl && streamTargetEl.value ? parseInt(streamTargetEl.value, 10) : null;
    const stopContainers = overlay.querySelector("#b-stop-containers").checked;
    overlay.remove();
    await runFn(ids, streamTargetId, stopContainers);
  });
  document.body.appendChild(overlay);
}

// ---------- Containers ----------
async function containersPage() {
  const data = await api("/api/containers").catch((e) => { toast(e.message, "error"); return { containers: [], projects: {} }; });
  const wrap = h(`<div>
    <div class="page-header"><h2>Container</h2>
      <div class="actions"><button class="btn primary" id="backup-all-btn">Gesamte Landschaft sichern</button></div>
    </div>
    <div class="card" style="padding:0">
      <table>
        <thead><tr><th>Name</th><th>Image</th><th>Status</th><th>Projekt</th><th></th></tr></thead>
        <tbody id="containers-tbody"></tbody>
      </table>
    </div>
  </div>`);

  const tbody = wrap.querySelector("#containers-tbody");
  if (!data.containers.length) {
    tbody.appendChild(h(`<tr><td colspan="5"><div class="empty-state">Keine Container gefunden</div></td></tr>`));
  }
  data.containers.forEach((c) => {
    const row = h(`<tr>
      <td>${c.name}</td>
      <td class="mono">${c.image}</td>
      <td><span class="badge ${c.status === "running" ? "ok" : "neutral"}">${c.status}</span></td>
      <td>${c.project || "-"}</td>
      <td><button class="btn">Backup jetzt</button></td>
    </tr>`);
    row.querySelector("button").addEventListener("click", async (e) => {
      await pickStorageTargetsAndRun(`Backup für ${c.name}`, async (storageTargetIds, streamTargetId, stopContainers) => {
        e.target.disabled = true;
        try {
          await api(`/api/containers/${encodeURIComponent(c.name)}/backup`, {
            method: "POST",
            body: JSON.stringify({
              storage_target_ids: storageTargetIds, stream_volumes_target_id: streamTargetId,
              stop_container: stopContainers,
            }),
          });
          toast(`Backup für ${c.name} gestartet`);
          pollGlobalJobs();
        } catch (err) { toast(err.message, "error"); }
        e.target.disabled = false;
      });
    });
    tbody.appendChild(row);
  });

  wrap.querySelector("#backup-all-btn").addEventListener("click", async () => {
    await pickStorageTargetsAndRun("Gesamte Landschaft sichern", async (storageTargetIds, streamTargetId, stopContainers) => {
      try {
        await api("/api/backups/landscape", {
          method: "POST",
          body: JSON.stringify({
            storage_target_ids: storageTargetIds, stream_volumes_target_id: streamTargetId,
            stop_containers: stopContainers,
          }),
        });
        toast("Landschafts-Backup gestartet");
        pollGlobalJobs();
      } catch (err) { toast(err.message, "error"); }
    });
  });

  return wrap;
}

// ---------- Backups ----------
async function backupsPage() {
  const data = await api("/api/backups");
  const wrap = h(`<div>
    <div class="page-header"><h2>Backups</h2></div>
    <div id="groups"></div>
  </div>`);
  const groupsEl = wrap.querySelector("#groups");
  const names = Object.keys(data.groups);
  if (!names.length) {
    groupsEl.appendChild(h(`<div class="empty-state">Noch keine Backups vorhanden</div>`));
    return wrap;
  }

  // Split into landscapes and standalone containers
  const landscapeNames = names.filter((n) => data.groups[n].some((v) => v.backup_type === "landscape"));

  // Collect all member container names from landscape backups so they can be hidden below
  const landscapeMembers = new Set();
  landscapeNames.forEach((n) => {
    data.groups[n].forEach((v) => {
      (v.member_names || []).forEach((m) => landscapeMembers.add(m));
    });
  });

  const containerNames = names.filter((n) => !landscapeNames.includes(n) && !landscapeMembers.has(n));

  function buildAccordion(name, versions, isLandscape) {
    // For landscapes, sum the member_size_bytes across versions (the landscape
    // record itself is just a tiny metadata file — the real data is in the members)
    const totalSize = isLandscape
      ? versions.reduce((s, v) => s + (v.member_size_bytes || v.size_bytes || 0), 0)
      : versions.reduce((s, v) => s + (v.size_bytes || 0), 0);
    const icon = isLandscape ? "🗺️" : "📦";
    const acc = h(`
      <div class="accordion${isLandscape ? " accordion-landscape" : ""}">
        <div class="accordion-head">
          <div style="display:flex; align-items:center; gap:8px;">
            <span>${icon}</span>
            <strong>${escHtml(name)}</strong>
            <span class="muted">(${versions.length} Version${versions.length === 1 ? "" : "en"}, ${fmtBytes(totalSize)})</span>
          </div>
          <div style="display:flex; align-items:center; gap:10px;">
            ${isLandscape ? '<button class="btn danger delete-all-btn" style="font-size:.78rem; padding:5px 10px;">Alle löschen</button>' : ''}
            <div class="muted">${fmtDate(versions[0].created_at)}</div>
          </div>
        </div>
        <div class="accordion-body" style="display:none">
          <div class="bulk-bar" style="display:none">
            <span class="bulk-count"></span>
            <button class="btn danger bulk-delete-btn" style="font-size:.8rem; padding:4px 10px;">Ausgewählte löschen</button>
          </div>
          <table>
            <thead><tr>
              <th style="width:36px; padding-right:0;"><input type="checkbox" class="select-all-cb" style="width:auto; margin:0; cursor:pointer;" title="Alle auswählen"></th>
              <th>Erstellt</th><th>Größe</th><th>Status</th><th></th>
            </tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
    `);
    const head = acc.querySelector(".accordion-head");
    const body = acc.querySelector(".accordion-body");
    head.addEventListener("click", () => { body.style.display = body.style.display === "none" ? "block" : "none"; });

    if (isLandscape) {
      const deleteAllBtn = acc.querySelector(".delete-all-btn");
      deleteAllBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(`Alle ${versions.length} Backup-Version(en) für "${name}" wirklich löschen?\n\nDies löscht auch alle zugehörigen Container-Backups.`)) return;
        deleteAllBtn.disabled = true;
        deleteAllBtn.textContent = "Löschen …";
        try {
          for (const v of versions) {
            await api(`/api/backups/${v.id}`, { method: "DELETE" });
          }
          toast(`Alle Backups für "${name}" gelöscht`);
          navigate("backups");
        } catch (err) {
          toast(err.message, "error");
          deleteAllBtn.disabled = false;
          deleteAllBtn.textContent = "Alle löschen";
        }
      });
    }

    const tbody = acc.querySelector("tbody");
    const selectAllCb = acc.querySelector(".select-all-cb");
    const bulkBar = acc.querySelector(".bulk-bar");
    const bulkCount = acc.querySelector(".bulk-count");
    const bulkDeleteBtn = acc.querySelector(".bulk-delete-btn");

    function updateBulkUI() {
      const checked = tbody.querySelectorAll(".row-cb:checked").length;
      const total = tbody.querySelectorAll(".row-cb").length;
      bulkBar.style.display = checked > 0 ? "flex" : "none";
      bulkCount.textContent = `${checked} von ${total} ausgewählt`;
      selectAllCb.checked = checked === total && total > 0;
      selectAllCb.indeterminate = checked > 0 && checked < total;
    }

    selectAllCb.addEventListener("change", () => {
      tbody.querySelectorAll(".row-cb").forEach(cb => { cb.checked = selectAllCb.checked; });
      updateBulkUI();
    });

    bulkDeleteBtn.addEventListener("click", async () => {
      const ids = [...tbody.querySelectorAll(".row-cb:checked")].map(cb => parseInt(cb.dataset.id));
      if (!ids.length) return;
      if (!confirm(`${ids.length} Backup${ids.length === 1 ? "" : "s"} für "${name}" wirklich löschen?`)) return;
      bulkDeleteBtn.disabled = true;
      bulkDeleteBtn.textContent = "Löschen …";
      try {
        await api("/api/backups/bulk-delete", { method: "POST", body: JSON.stringify({ ids }) });
        toast(`${ids.length} Backup${ids.length === 1 ? "" : "s"} gelöscht`);
        navigate("backups");
      } catch (err) {
        toast(err.message, "error");
        bulkDeleteBtn.disabled = false;
        bulkDeleteBtn.textContent = "Ausgewählte löschen";
      }
    });

    versions.forEach((v) => {
      const isLsc = v.backup_type === "landscape";
      const displaySize = isLsc && v.member_size_bytes ? v.member_size_bytes : (v.size_bytes || 0);
      const row = h(`<tr>
        <td style="width:36px; padding-right:0; vertical-align:middle;"><input type="checkbox" class="row-cb" data-id="${v.id}" style="width:auto; margin:0; cursor:pointer;"></td>
        <td>${fmtDate(v.created_at)}</td>
        <td>${fmtBytes(displaySize)}</td>
        <td><span class="badge ${v.status === "ok" ? "ok" : "failed"}">${v.status}</span></td>
        <td style="display:flex; gap:8px;">
          ${isLsc
            ? '<button class="btn primary members-btn">🗺️ Mitglieder &amp; Restore</button>'
            : '<button class="btn restore-btn">↩ Wiederherstellen</button>'}
          <button class="btn download-btn">⬇ Download</button>
          <button class="btn danger delete-btn">Löschen</button>
        </td>
      </tr>`);
      row.querySelector(".row-cb").addEventListener("change", updateBulkUI);
      const restoreBtn = row.querySelector(".restore-btn");
      if (restoreBtn) restoreBtn.addEventListener("click", () => openRestoreModal(v));
      const membersBtn = row.querySelector(".members-btn");
      if (membersBtn) membersBtn.addEventListener("click", () => openLandscapeMembersModal(v));
      const downloadBtn = row.querySelector(".download-btn");
      if (downloadBtn) downloadBtn.addEventListener("click", () => {
        const a = document.createElement("a");
        a.href = `/api/backups/${v.id}/download`;
        a.download = "";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
      });
      row.querySelector(".delete-btn").addEventListener("click", async (e) => {
        if (!confirm(`Backup vom ${fmtDate(v.created_at)} für "${name}" wirklich löschen?`)) return;
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.textContent = "Löschen …";
        try {
          const res = await api(`/api/backups/${v.id}`, { method: "DELETE" });
          if (res.warning) toast(res.warning, "error");
          else toast("Backup gelöscht");
          navigate("backups");
        } catch (e) { toast(e.message, "error"); btn.disabled = false; btn.textContent = "Löschen"; }
      });
      tbody.appendChild(row);
    });
    return acc;
  }

  if (landscapeNames.length) {
    groupsEl.appendChild(h(`<div class="section-title" style="margin-top:0">🗺️ Landscape-Backups (Gruppen)</div>`));
    landscapeNames.forEach((name) => groupsEl.appendChild(buildAccordion(name, data.groups[name], true)));
  }
  if (containerNames.length) {
    groupsEl.appendChild(h(`<div class="section-title" style="margin-top:${landscapeNames.length ? "24px" : "0"}">📦 Einzelne Container</div>`));
    containerNames.forEach((name) => groupsEl.appendChild(buildAccordion(name, data.groups[name], false)));
  }
  return wrap;
}

function openRestoreModal(version) {
  const overlay = h(`
    <div class="modal-overlay">
      <div class="modal" style="width:500px">
        <h3>Backup wiederherstellen</h3>
        <p style="margin-bottom:4px; font-size:14px;">Stand: <strong>${fmtDateLong(version.created_at)}</strong></p>
        <p class="muted" style="margin-bottom:16px; font-size:12px;">Der Container wird auf genau diesen Zeitpunkt zurückgesetzt.</p>
        <div class="field">
          <label>Neuer Container-Name <span class="muted">(leer = Originalname)</span></label>
          <input type="text" id="restore-name" placeholder="z.B. immich_server_01_test" />
        </div>
        <div class="field">
          <label><input type="checkbox" id="restore-rename-vols" checked style="width:auto; margin-right:6px;" />
          Volumes automatisch mit umbenennen <span class="muted">(empfohlen bei neuem Container-Namen)</span></label>
        </div>
        <div class="field">
          <label>Volumes in eigenem Verzeichnis wiederherstellen <span class="muted">(leer = Docker-Standard)</span></label>
          <input type="text" id="restore-vol-dir" placeholder="z.B. /mnt/ssd2/volumes/" />
          <p class="muted" style="font-size:11px; margin-top:4px;">Für anderes RAID / andere HDD: Volumes werden als Bind-Mounts in dieses Verzeichnis geschrieben statt als benannte Docker-Volumes.</p>
        </div>
        <div class="field">
          <label><input type="checkbox" id="restore-start" checked style="width:auto; margin-right:6px;" />Container nach Wiederherstellung starten</label>
        </div>
        <div class="row-actions">
          <button class="btn" id="cancel-btn">Abbrechen</button>
          <button class="btn primary" id="confirm-btn">Wiederherstellen</button>
        </div>
      </div>
    </div>
  `);
  overlay.querySelector("#cancel-btn").addEventListener("click", () => overlay.remove());
  overlay.querySelector("#confirm-btn").addEventListener("click", async (e) => {
    const newName = overlay.querySelector("#restore-name").value.trim();
    const renameVolumes = overlay.querySelector("#restore-rename-vols").checked;
    const volumeBaseDir = overlay.querySelector("#restore-vol-dir").value.trim();
    const start = overlay.querySelector("#restore-start").checked;
    const btn = e.currentTarget;
    btn.disabled = true; btn.textContent = "Starte …";
    try {
      await api(`/api/backups/${version.id}/restore`, {
        method: "POST", body: JSON.stringify({
          new_name: newName || null,
          rename_volumes: renameVolumes,
          volume_base_dir: volumeBaseDir || null,
          start,
          overwrite: !newName,
        }),
      });
      toast("Wiederherstellung gestartet");
      pollGlobalJobs();
      overlay.remove();
    } catch (e) { toast(e.message, "error"); btn.disabled = false; btn.textContent = "Wiederherstellen"; }
  });
  document.body.appendChild(overlay);
}

async function openLandscapeMembersModal(version) {
  const data = await api(`/api/backups/${version.id}/members`);
  const restorableCount = data.members.filter((m) => m.backup_id && m.status === "ok").length;
  const overlay = h(`
    <div class="modal-overlay">
      <div class="modal" style="max-width:560px">
        <h3>Gruppe wiederherstellen</h3>
        <p style="margin-bottom:4px; font-size:14px;">Stand: <strong>${fmtDateLong(version.created_at)}</strong></p>
        <p class="muted" style="margin-bottom:12px; font-size:12px;">${restorableCount} wiederherstellbare Container</p>

        <div id="members-list" style="margin-bottom:16px;"></div>

        <div class="field" style="margin-bottom:8px;">
          <label>Modus</label>
          <select id="restore-mode">
            <option value="replace">Überschreiben — bestehende Container ersetzen</option>
            <option value="parallel">Parallel — unter neuem Namen neben dem laufenden System</option>
          </select>
        </div>
        <div id="prefix-field" class="field" style="display:none; margin-bottom:8px;">
          <label>Namenspräfix (z.B. <code>staging_</code>)</label>
          <input type="text" id="restore-prefix" placeholder="staging_" />
          <p class="muted" style="margin-top:4px; font-size:12px;">Alle Container und Volumes werden mit diesem Präfix neu erstellt und laufen unabhängig vom Produktivsystem.</p>
        </div>
        <div class="field">
          <label><input type="checkbox" id="restore-start" checked style="width:auto; margin-right:6px;" />Container nach Wiederherstellung starten</label>
        </div>

        <div class="row-actions">
          <button class="btn" id="close-btn">Abbrechen</button>
          <button class="btn primary" id="restore-all-btn" ${restorableCount ? "" : "disabled"}>Alle ${restorableCount} wiederherstellen</button>
        </div>
      </div>
    </div>
  `);

  const list = overlay.querySelector("#members-list");
  data.members.forEach((m) => {
    const ok = m.backup_id && m.status === "ok";
    const statusBadge = m.status === "ok" ? '<span class="badge ok">ok</span>' : `<span class="badge failed">${m.status || "kein Backup"}</span>`;
    const row = h(`<div style="display:flex; justify-content:space-between; align-items:center; padding:6px 0; border-bottom:1px solid var(--border); gap:8px;">
      <span style="font-weight:500">${escHtml(m.container_name)}</span>
      <div style="display:flex; align-items:center; gap:8px;">
        ${statusBadge}
        ${ok ? '<button class="btn" style="padding:2px 10px; font-size:13px;">Einzeln</button>' : ''}
      </div>
    </div>`);
    const btn = row.querySelector("button");
    if (btn) btn.addEventListener("click", () => {
      overlay.remove();
      openRestoreModal({ id: m.backup_id, created_at: m.created_at || version.created_at });
    });
    list.appendChild(row);
  });

  overlay.querySelector("#restore-mode").addEventListener("change", (e) => {
    overlay.querySelector("#prefix-field").style.display = e.target.value === "parallel" ? "block" : "none";
  });

  overlay.querySelector("#restore-all-btn").addEventListener("click", async () => {
    const mode = overlay.querySelector("#restore-mode").value;
    const prefix = mode === "parallel" ? (overlay.querySelector("#restore-prefix").value.trim() || "restored_") : "";
    const start = overlay.querySelector("#restore-start").checked;
    const restorable = data.members.filter((m) => m.backup_id && m.status === "ok");

    const names = restorable.map((m) => prefix ? `${prefix}${m.container_name}` : m.container_name).join(", ");
    if (!confirm(`${restorable.length} Container wiederherstellen?\n\n${names}\n\nDies startet ${restorable.length} separate Wiederherstellungs-Jobs.`)) return;

    overlay.remove();
    let started = 0;
    for (const m of restorable) {
      try {
        const newName = prefix ? `${prefix}${m.container_name}` : null;
        await api(`/api/backups/${m.backup_id}/restore`, {
          method: "POST",
          body: JSON.stringify({ new_name: newName, start, overwrite: mode === "replace" }),
        });
        started++;
      } catch (e) { toast(`${m.container_name}: ${e.message}`, "error"); }
    }
    if (started) toast(`${started} Wiederherstellungs-Jobs gestartet`);
    pollGlobalJobs();
  });

  overlay.querySelector("#close-btn").addEventListener("click", () => overlay.remove());
  document.body.appendChild(overlay);
}

// ---------- Schedules ----------
const WEEKDAY_LABELS = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];

function describeCron(cron) {
  const parts = (cron || "").trim().split(/\s+/);
  if (parts.length !== 5) return cron;
  const [minute, hour, dayOfMonth, , dayOfWeek] = parts;
  const hourlyMatch = /^\*\/(\d+)$/.exec(hour);
  if (hourlyMatch && dayOfMonth === "*" && dayOfWeek === "*") {
    return `Alle ${hourlyMatch[1]} Stunden`;
  }
  const time = `${hour.padStart(2, "0")}:${minute.padStart(2, "0")} Uhr`;
  if (dayOfMonth !== "*") return `Monatlich am ${dayOfMonth}. um ${time}`;
  if (dayOfWeek !== "*") {
    const days = dayOfWeek.split(",").map((d) => WEEKDAY_LABELS[parseInt(d, 10)] || d).join(", ");
    return `Wöchentlich (${days}) um ${time}`;
  }
  return `Täglich um ${time}`;
}

async function schedulesPage() {
  const [data, targetsData] = await Promise.all([api("/api/schedules"), api("/api/settings/storage-targets")]);
  const targetById = Object.fromEntries(targetsData.targets.map((t) => [t.id, t.name]));
  const wrap = h(`<div>
    <div class="page-header"><h2>Zeitpläne</h2>
      <div class="actions"><button class="btn primary" id="new-schedule-btn">Neuer Zeitplan</button></div>
    </div>
    <div class="card" style="padding:0">
      <table>
        <thead><tr><th>Name</th><th>Quelle</th><th>Zeitplan</th><th>Aufbewahrung</th><th>Speicherziele</th><th>Letzter Lauf</th><th>Status</th><th></th></tr></thead>
        <tbody id="sched-tbody"></tbody>
      </table>
    </div>
  </div>`);
  const tbody = wrap.querySelector("#sched-tbody");
  if (!data.schedules.length) tbody.appendChild(h(`<tr><td colspan="8"><div class="empty-state">Keine Zeitpläne konfiguriert</div></td></tr>`));
  data.schedules.forEach((s) => {
    const targetNames = (s.storage_target_ids || []).map((id) => targetById[id] || `#${id}`);
    const row = h(`<tr>
      <td>${s.name}</td>
      <td>${s.target_type === "container" ? "Container: " + s.target_ref
        : s.name_contains ? `Name enthält: ${s.name_contains}`
        : s.project_filter ? `Projekt: ${s.project_filter}` : "Gesamte Landschaft"}</td>
      <td>${describeCron(s.cron_expression)}</td>
      <td>${s.retention_count > 0 ? s.retention_count + " Versionen" : ""}${s.retention_days > 0 ? " / " + s.retention_days + " Tage" : ""}</td>
      <td>${targetNames.length ? targetNames.join(", ") : '<span class="muted">nur lokal</span>'}</td>
      <td>${fmtDate(s.last_run_at)}</td>
      <td>${s.last_status ? `<span class="badge ${s.last_status === "ok" ? "ok" : "failed"}">${s.last_status}</span>` : '<span class="badge neutral">nie ausgeführt</span>'}
          ${s.enabled ? "" : '<span class="badge neutral">deaktiviert</span>'}</td>
      <td style="display:flex; gap:8px;">
        <button class="btn edit-btn">Bearbeiten</button>
        <button class="btn run-btn">Jetzt ausführen</button>
        <button class="btn danger del-btn">Löschen</button>
      </td>
    </tr>`);
    row.querySelector(".edit-btn").addEventListener("click", () => openScheduleModal(s));
    row.querySelector(".run-btn").addEventListener("click", async () => {
      try { await api(`/api/schedules/${s.id}/run-now`, { method: "POST" }); toast("Zeitplan gestartet"); }
      catch (e) { toast(e.message, "error"); }
    });
    row.querySelector(".del-btn").addEventListener("click", async () => {
      if (!confirm(`Zeitplan "${s.name}" löschen?`)) return;
      await api(`/api/schedules/${s.id}`, { method: "DELETE" });
      navigate("schedules");
    });
    tbody.appendChild(row);
  });
  wrap.querySelector("#new-schedule-btn").addEventListener("click", () => openScheduleModal(null));
  return wrap;
}

function parseCronToFrequencyFields(cron) {
  const parts = (cron || "").trim().split(/\s+/);
  const fallback = { freq: "daily", hour: 3, minute: 0, weekdays: ["1"], monthday: "1", hourInterval: 6 };
  if (parts.length !== 5) return fallback;
  const [minuteStr, hourStr, dayOfMonth, , dayOfWeek] = parts;
  const hourlyMatch = /^\*\/(\d+)$/.exec(hourStr);
  if (hourlyMatch && dayOfMonth === "*" && dayOfWeek === "*") {
    return { ...fallback, freq: "hourly", hourInterval: parseInt(hourlyMatch[1], 10) };
  }
  const hour = parseInt(hourStr, 10) || 0;
  const minute = parseInt(minuteStr, 10) || 0;
  if (dayOfMonth !== "*") return { ...fallback, freq: "monthly", hour, minute, monthday: dayOfMonth };
  if (dayOfWeek !== "*") return { ...fallback, freq: "weekly", hour, minute, weekdays: dayOfWeek.split(",") };
  return { ...fallback, freq: "daily", hour, minute };
}

async function openScheduleModal(existing) {
  let containers = [];
  let projects = {};
  let storageTargets = [];
  try {
    const data = await api("/api/containers");
    containers = data.containers;
    projects = data.projects;
  } catch (e) {}
  try { storageTargets = (await api("/api/settings/storage-targets")).targets; } catch (e) {}

  const targetsHtml = storageTargets.length
    ? storageTargets.map((t) => `
        <label style="display:flex; align-items:center; gap:8px; padding:6px 0;">
          <input type="checkbox" class="s-storage-target" value="${t.id}" style="width:auto;" ${t.enabled ? "" : "disabled"} />
          ${t.name} <span class="muted" style="font-size:.78rem">(${t.type})</span>
          ${t.enabled ? "" : '<span class="badge neutral">deaktiviert</span>'}
        </label>`).join("")
    : `<p class="muted" style="font-size:.85rem">Noch keine Speicherziele konfiguriert. Unter <strong>Einstellungen</strong> anlegen (SMB, S3, Google Drive/OneDrive via rclone, ...).</p>`;

  // Only these target types can receive a live byte-stream; Google Drive/OneDrive
  // need a known size/seekable content up front, so they're not offered here.
  const STREAMABLE_TYPES = ["local_path", "smb", "s3", "rclone"];
  const streamableTargets = storageTargets.filter((t) => STREAMABLE_TYPES.includes(t.type) && t.enabled);
  const streamTargetOptionsHtml = streamableTargets.map((t) =>
    `<option value="${t.id}">${t.name} (${t.type})</option>`).join("");

  const cronFields = parseCronToFrequencyFields(existing ? existing.cron_expression : null);
  const timeValue = `${String(cronFields.hour).padStart(2, "0")}:${String(cronFields.minute).padStart(2, "0")}`;

  const overlay = h(`
    <div class="modal-overlay">
      <div class="modal">
        <h3>${existing ? "Zeitplan bearbeiten" : "Neuer Zeitplan"}</h3>
        <div class="field"><label>Name</label><input type="text" id="s-name" value="${existing ? existing.name : ""}" /></div>
        <div class="field"><label>Sicherungsquelle (was wird gesichert)</label>
          <select id="s-target-type">
            <option value="landscape">Gesamte Docker-Landschaft</option>
            <option value="container">Einzelner Container</option>
          </select>
        </div>
        <div class="field" id="s-container-field" style="display:none">
          <label>Container</label>
          <select id="s-target-ref">${containers.map((c) => `<option value="${c.name}">${c.name}</option>`).join("")}</select>
        </div>
        <div class="field" id="s-project-field">
          <label>Was sichern?</label>
          <select id="s-project-filter">
            <option value="">Alle Container (gesamte Landschaft)</option>
            ${Object.keys(projects).sort().map((p) => `<option value="${p}">Nur Projekt „${p}" (${projects[p].length} Container, z. B. Immich/Nextcloud-Stack)</option>`).join("")}
          </select>
          <div class="muted" style="font-size:.75rem; margin-top:6px;">
            Setups ohne Docker-Compose-Projekt (z. B. Nextcloud AIO) tauchen hier nicht auf.
            Stattdessen unten einen Namensbestandteil eintragen, den alle zugehörigen Container gemeinsam haben.
          </div>
          <label style="margin-top:8px; display:block;">Oder: Name enthält (überschreibt die Auswahl oben)</label>
          <input type="text" id="s-name-contains" placeholder="z. B. nextcloud-aio" />
          <label style="margin-top:8px; display:block;">Container ausschließen (kommagetrennt)</label>
          <input type="text" id="s-exclude-names" placeholder="z. B. borgbackup,watchtower" />
          <div class="muted" style="font-size:.75rem; margin-top:4px;">Container deren Name einen dieser Begriffe enthält werden übersprungen.</div>
        </div>
        <div class="field"><label>Wie oft?</label>
          <select id="s-freq">
            <option value="hourly">Alle X Stunden</option>
            <option value="daily">Täglich</option>
            <option value="weekly">Wöchentlich</option>
            <option value="monthly">Monatlich</option>
          </select>
        </div>
        <div class="field" id="s-hourly-field" style="display:none">
          <label>Alle wie viele Stunden?</label>
          <input type="number" id="s-hour-interval" value="${cronFields.hourInterval}" min="1" max="23" />
        </div>
        <div class="field" id="s-time-field"><label>Uhrzeit</label><input type="time" id="s-time" value="${timeValue}" /></div>
        <div class="field" id="s-weekdays-field" style="display:none">
          <label>An welchen Tagen?</label>
          <div style="display:flex; gap:10px; flex-wrap:wrap;">
            ${[["Mo", 1], ["Di", 2], ["Mi", 3], ["Do", 4], ["Fr", 5], ["Sa", 6], ["So", 0]].map(([label, cronDow]) => `
              <label style="display:flex; align-items:center; gap:4px;">
                <input type="checkbox" class="s-weekday" value="${cronDow}" style="width:auto;" ${cronFields.weekdays.includes(String(cronDow)) ? "checked" : ""} /> ${label}
              </label>`).join("")}
          </div>
        </div>
        <div class="field" id="s-monthday-field" style="display:none">
          <label>An welchem Tag im Monat?</label>
          <select id="s-monthday">
            ${Array.from({ length: 28 }, (_, i) => i + 1).map((d) => `<option value="${d}" ${String(d) === cronFields.monthday ? "selected" : ""}>${d}.</option>`).join("")}
          </select>
        </div>
        <div class="field"><label>Aufbewahrung: Anzahl Versionen (0 = unbegrenzt)</label><input type="text" id="s-ret-count" value="${existing ? existing.retention_count : 7}" /></div>
        <div class="field"><label>Aufbewahrung: Tage (0 = deaktiviert)</label><input type="text" id="s-ret-days" value="${existing ? existing.retention_days : 0}" /></div>
        <div class="field">
          <label>Speicherziele für diesen Zeitplan (wohin zusätzlich hochgeladen wird)</label>
          <div id="s-storage-targets">${targetsHtml}</div>
        </div>
        <div class="field">
          <label>Volumes direkt streamen, ohne lokal zu speichern (optional)</label>
          <select id="s-stream-target">
            <option value="">Nein - lokal speichern (Standard)</option>
            ${streamTargetOptionsHtml}
          </select>
          <div class="muted" style="font-size:.75rem; margin-top:4px;">
            Für große Volumes (z. B. Immich), wenn lokal nicht genug Speicherplatz frei ist: die
            Volume-Daten gehen direkt an das gewählte Ziel, ohne je auf der lokalen Platte zu landen.
            <strong>Wichtig:</strong> dabei wird die AES-256-Verschlüsselung dieser App umgangen (die
            greift nur bei lokal geschriebenen Dateien) - nur nutzen, wenn du dem Zielsystem selbst
            vertraust (z. B. eigenes NAS im LAN). Nur lokaler Pfad, SMB, S3 und rclone unterstützt.
          </div>
        </div>
        <div class="field">
          <label style="display:flex; align-items:center; gap:8px;">
            <input type="checkbox" id="s-stop-containers" style="width:auto;" />
            Container(n) vor dem Backup stoppen, danach wieder starten
          </label>
          <div class="muted" style="font-size:.75rem; margin-top:4px;">
            Für ein anwendungskonsistentes statt nur crash-konsistentes Backup (z. B. bei
            Datenbanken). Bedeutet eine kurze Downtime während jedes Laufs dieses Zeitplans.
            Standardmäßig aus, damit bestehende Zeitpläne sich nicht ändern.
          </div>
        </div>
        <div class="row-actions">
          <button class="btn" id="cancel-btn">Abbrechen</button>
          <button class="btn primary" id="save-btn">${existing ? "Speichern" : "Erstellen"}</button>
        </div>
      </div>
    </div>
  `);
  overlay.querySelector("#s-freq").value = cronFields.freq;
  if (existing) {
    overlay.querySelector("#s-target-type").value = existing.target_type;
    if (existing.target_type === "container") overlay.querySelector("#s-target-ref").value = existing.target_ref || "";
    overlay.querySelector("#s-project-filter").value = existing.project_filter || "";
    overlay.querySelector("#s-name-contains").value = existing.name_contains || "";
    overlay.querySelector("#s-exclude-names").value = existing.exclude_names || "";
    (existing.storage_target_ids || []).forEach((id) => {
      const cb = overlay.querySelector(`.s-storage-target[value="${id}"]`);
      if (cb) cb.checked = true;
    });
    if (existing.stream_volumes_target_id) {
      overlay.querySelector("#s-stream-target").value = String(existing.stream_volumes_target_id);
    }
    overlay.querySelector("#s-stop-containers").checked = !!existing.stop_containers;
  }
  overlay.querySelector("#s-target-type").addEventListener("change", (e) => {
    overlay.querySelector("#s-container-field").style.display = e.target.value === "container" ? "block" : "none";
    overlay.querySelector("#s-project-field").style.display = e.target.value === "landscape" ? "block" : "none";
  });
  overlay.querySelector("#s-target-type").dispatchEvent(new Event("change"));
  function updateFrequencyFields() {
    const freq = overlay.querySelector("#s-freq").value;
    overlay.querySelector("#s-hourly-field").style.display = freq === "hourly" ? "block" : "none";
    overlay.querySelector("#s-time-field").style.display = freq === "hourly" ? "none" : "block";
    overlay.querySelector("#s-weekdays-field").style.display = freq === "weekly" ? "block" : "none";
    overlay.querySelector("#s-monthday-field").style.display = freq === "monthly" ? "block" : "none";
  }
  overlay.querySelector("#s-freq").addEventListener("change", updateFrequencyFields);
  updateFrequencyFields();

  function buildCronExpression() {
    const freq = overlay.querySelector("#s-freq").value;
    if (freq === "hourly") {
      const interval = Math.min(23, Math.max(1, parseInt(overlay.querySelector("#s-hour-interval").value || "6", 10)));
      return `0 */${interval} * * *`;
    }
    const [hour, minute] = overlay.querySelector("#s-time").value.split(":").map((n) => parseInt(n, 10));
    if (freq === "weekly") {
      const days = Array.from(overlay.querySelectorAll(".s-weekday:checked")).map((el) => el.value);
      return `${minute} ${hour} * * ${days.length ? days.join(",") : "0"}`;
    }
    if (freq === "monthly") {
      const day = overlay.querySelector("#s-monthday").value;
      return `${minute} ${hour} ${day} * *`;
    }
    return `${minute} ${hour} * * *`;
  }

  overlay.querySelector("#cancel-btn").addEventListener("click", () => overlay.remove());
  overlay.querySelector("#save-btn").addEventListener("click", async () => {
    const storageTargetIds = Array.from(overlay.querySelectorAll(".s-storage-target:checked")).map((el) => parseInt(el.value, 10));
    const payload = {
      name: overlay.querySelector("#s-name").value.trim() || "Backup",
      target_type: overlay.querySelector("#s-target-type").value,
      target_ref: overlay.querySelector("#s-target-ref").value || null,
      project_filter: overlay.querySelector("#s-name-contains").value.trim()
        ? null : (overlay.querySelector("#s-project-filter").value || null),
      name_contains: overlay.querySelector("#s-name-contains").value.trim() || null,
      cron_expression: buildCronExpression(),
      retention_count: parseInt(overlay.querySelector("#s-ret-count").value || "0", 10),
      retention_days: parseInt(overlay.querySelector("#s-ret-days").value || "0", 10),
      storage_target_ids: storageTargetIds,
      stream_volumes_target_id: overlay.querySelector("#s-stream-target").value
        ? parseInt(overlay.querySelector("#s-stream-target").value, 10) : null,
      exclude_names: overlay.querySelector("#s-exclude-names").value.trim() || null,
      stop_containers: overlay.querySelector("#s-stop-containers").checked,
      enabled: existing ? existing.enabled : true,
    };
    try {
      if (existing) {
        await api(`/api/schedules/${existing.id}`, { method: "PUT", body: JSON.stringify(payload) });
      } else {
        await api("/api/schedules", { method: "POST", body: JSON.stringify(payload) });
      }
      overlay.remove();
      navigate("schedules");
    } catch (e) { toast(e.message, "error"); }
  });
  document.body.appendChild(overlay);
}

// ---------- Logs ----------
const LOG_CATEGORY_LABEL = {
  backup:    "Backup",
  restore:   "Restore",
  schedule:  "Zeitplan",
  scheduler: "Zeitplan",
  cleanup:   "Bereinigung",
  restic:    "Restic",
  space:     "Speicher",
};

const LOG_CATEGORY_COLOR = {
  backup:    "#3b82f6",
  restore:   "#8b5cf6",
  schedule:  "#0ea5e9",
  scheduler: "#0ea5e9",
  cleanup:   "#f59e0b",
  restic:    "#10b981",
  space:     "#6366f1",
};

function logCategoryBadge(cat) {
  const label = LOG_CATEGORY_LABEL[cat] || cat;
  const color = LOG_CATEGORY_COLOR[cat] || "var(--accent)";
  return `<span style="display:inline-block;padding:2px 8px;border-radius:99px;font-size:.72rem;font-weight:600;background:${color}22;color:${color};border:1px solid ${color}44">${label}</span>`;
}

function logLevelBadge(level) {
  if (level === "error")   return `<span style="color:#ef4444;font-weight:700;font-size:.8rem;">● Fehler</span>`;
  if (level === "warning") return `<span style="color:#f59e0b;font-weight:700;font-size:.8rem;">● Warnung</span>`;
  return `<span style="color:var(--muted);font-size:.8rem;">● Info</span>`;
}

async function logsPage() {
  const [data, settings] = await Promise.all([
    api("/api/logs?limit=2000").catch((e) => { toast(e.message, "error"); return { entries: [], total: 0 }; }),
    api("/api/logs/settings").catch(() => ({ retention_days: 90 })),
  ]);

  const catsPresent = [...new Set(data.entries.map((e) => e.category))].sort();
  let activeCategory = "all";
  let activeLevel = "all";
  let liveMode = false;
  let liveTimer = null;
  let maxId = data.entries.reduce((m, e) => Math.max(m, e.id), 0);

  const wrap = h(`<div>
    <div class="page-header">
      <h2>Logs</h2>
      <div class="actions">
        <span class="muted" style="font-size:.82rem;" id="log-count">${data.total} Einträge gesamt</span>
        <button class="btn" id="live-btn" title="Neue Einträge alle 3 s automatisch laden">▶ Live</button>
        <button class="btn" id="log-settings-btn" style="font-size:.82rem;">Aufbewahrung: ${settings.retention_days > 0 ? settings.retention_days + " Tage" : "unbegrenzt"}</button>
        <button class="btn danger" id="log-purge-btn" style="font-size:.82rem;">Jetzt bereinigen</button>
      </div>
    </div>

    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;align-items:center;">
      <span class="muted" style="font-size:.8rem;margin-right:4px;">Kategorie:</span>
      <button class="log-filter-cat btn active-filter" data-cat="all">Alle</button>
      ${catsPresent.map((c) => `<button class="log-filter-cat btn" data-cat="${c}">${LOG_CATEGORY_LABEL[c] || c}</button>`).join("")}
      <span class="muted" style="font-size:.8rem;margin-left:12px;margin-right:4px;">Level:</span>
      <button class="log-filter-lvl btn active-filter" data-lvl="all">Alle</button>
      <button class="log-filter-lvl btn" data-lvl="warning" style="color:#f59e0b;">Warnungen</button>
      <button class="log-filter-lvl btn" data-lvl="error" style="color:#ef4444;">Fehler</button>
    </div>

    <div class="card" style="padding:0;overflow-x:auto;">
      <table style="table-layout:fixed;width:100%;">
        <colgroup>
          <col style="width:155px">
          <col style="width:115px">
          <col style="width:90px">
          <col>
        </colgroup>
        <thead><tr>
          <th>Zeitpunkt</th>
          <th>Kategorie</th>
          <th>Level</th>
          <th>Meldung</th>
        </tr></thead>
        <tbody id="logs-tbody"></tbody>
      </table>
    </div>
    <div id="log-empty" class="empty-state" style="display:none;">Keine Einträge für diesen Filter</div>
  </div>`);

  const tbody = wrap.querySelector("#logs-tbody");
  const emptyEl = wrap.querySelector("#log-empty");
  const countEl = wrap.querySelector("#log-count");
  const liveBtn = wrap.querySelector("#live-btn");
  const catBar  = wrap.querySelector(".log-filter-cat[data-cat='all']").parentElement;

  function makeRow(entry) {
    const levelStyle = entry.level === "error"
      ? "background:rgba(239,68,68,.06);"
      : entry.level === "warning" ? "background:rgba(245,158,11,.06);" : "";
    return h(`<tr style="${levelStyle}">
      <td class="mono" style="white-space:nowrap;font-size:.78rem;">${fmtDate(entry.created_at)}</td>
      <td>${logCategoryBadge(entry.category)}</td>
      <td>${logLevelBadge(entry.level)}</td>
      <td style="word-break:break-word;font-size:.85rem;">${escHtml(entry.message)}</td>
    </tr>`);
  }

  function entryVisible(e) {
    if (activeCategory !== "all" && e.category !== activeCategory) return false;
    if (activeLevel === "warning" && !["warning", "error"].includes(e.level)) return false;
    if (activeLevel === "error"   && e.level !== "error") return false;
    return true;
  }

  function renderRows() {
    tbody.innerHTML = "";
    const filtered = data.entries.filter(entryVisible);
    emptyEl.style.display = filtered.length ? "none" : "";
    countEl.textContent = `${filtered.length} / ${data.entries.length} Einträge`;
    filtered.forEach((e) => tbody.appendChild(makeRow(e)));
  }

  function addCatFilterBtn(cat) {
    if (wrap.querySelector(`.log-filter-cat[data-cat="${cat}"]`)) return;
    const btn = h(`<button class="log-filter-cat btn" data-cat="${cat}">${LOG_CATEGORY_LABEL[cat] || cat}</button>`);
    btn.addEventListener("click", () => activateCat(cat, btn));
    // Insert before the level label (find first non-cat child after cat buttons)
    const levelSpan = catBar.querySelector(`span[style*="margin-left"]`);
    catBar.insertBefore(btn, levelSpan);
  }

  function activateCat(cat, btn) {
    wrap.querySelectorAll(".log-filter-cat").forEach((b) => b.classList.remove("active-filter"));
    btn.classList.add("active-filter");
    activeCategory = cat;
    renderRows();
  }

  // Live polling — only fetches new entries (since_id), prepends to table
  async function pollOnce() {
    if (!liveMode || !tbody.isConnected) { stopLive(); return; }
    try {
      const fresh = await api(`/api/logs?since_id=${maxId}&limit=200`);
      if (fresh.entries.length) {
        maxId = fresh.entries.reduce((m, e) => Math.max(m, e.id), maxId);
        data.entries.unshift(...fresh.entries);
        fresh.entries.forEach((e) => {
          if (!catsPresent.includes(e.category)) { catsPresent.push(e.category); addCatFilterBtn(e.category); }
          if (entryVisible(e)) tbody.insertBefore(makeRow(e), tbody.firstChild);
        });
        countEl.textContent = `${data.entries.filter(entryVisible).length} / ${data.entries.length} Einträge`;
        emptyEl.style.display = data.entries.filter(entryVisible).length ? "none" : "";
      }
    } catch (_) { /* ignore poll errors */ }
    if (liveMode && tbody.isConnected) liveTimer = setTimeout(pollOnce, 3000);
  }

  function startLive() {
    liveMode = true;
    liveBtn.textContent = "⏹ Live läuft";
    liveBtn.classList.add("active-filter");
    pollOnce();
  }

  function stopLive() {
    liveMode = false;
    if (liveTimer) { clearTimeout(liveTimer); liveTimer = null; }
    liveBtn.textContent = "▶ Live";
    liveBtn.classList.remove("active-filter");
  }

  liveBtn.addEventListener("click", () => liveMode ? stopLive() : startLive());

  renderRows();

  wrap.querySelectorAll(".log-filter-cat").forEach((btn) => {
    btn.addEventListener("click", () => activateCat(btn.dataset.cat, btn));
  });

  wrap.querySelectorAll(".log-filter-lvl").forEach((btn) => {
    btn.addEventListener("click", () => {
      wrap.querySelectorAll(".log-filter-lvl").forEach((b) => b.classList.remove("active-filter"));
      btn.classList.add("active-filter");
      activeLevel = btn.dataset.lvl;
      renderRows();
    });
  });

  wrap.querySelector("#log-settings-btn").addEventListener("click", async () => {
    const input = prompt(`Log-Aufbewahrung in Tagen (0 = unbegrenzt):`, settings.retention_days);
    if (input === null) return;
    const days = parseInt(input, 10);
    if (isNaN(days) || days < 0) { toast("Ungültige Eingabe", "error"); return; }
    await api("/api/logs/settings", { method: "PUT", body: JSON.stringify({ retention_days: days }) });
    toast(`Aufbewahrung auf ${days > 0 ? days + " Tage" : "unbegrenzt"} gesetzt`);
    navigate("logs");
  });

  wrap.querySelector("#log-purge-btn").addEventListener("click", async () => {
    if (settings.retention_days <= 0) { toast("Aufbewahrung ist unbegrenzt — zuerst Tage einstellen", "error"); return; }
    if (!confirm(`Alle Log-Einträge älter als ${settings.retention_days} Tage löschen?`)) return;
    const res = await api("/api/logs/purge", { method: "POST" });
    toast(`${res.deleted} Einträge gelöscht`);
    navigate("logs");
  });

  return wrap;
}

// ---------- Settings ----------
async function settingsPage() {
  const [overview, targetsData] = await Promise.all([
    api("/api/settings/overview"), api("/api/settings/storage-targets"),
  ]);
  const wrap = h(`<div>
    <div class="page-header">
      <h2>Einstellungen</h2>
      <span class="muted" style="font-size:13px;">Version ${overview.app_version}</span>
    </div>

    <div class="section-title">Software-Update</div>
    <div class="card" id="update-card">
      <span class="muted" style="font-size:.88rem;">Wird geprüft …</span>
    </div>

    <div class="section-title">Konfiguration sichern &amp; wiederherstellen</div>
    <div class="card" style="display:flex; flex-wrap:wrap; gap:12px; align-items:center;">
      <div style="flex:1; min-width:200px;">
        <div style="font-size:.88rem; margin-bottom:4px;">Exportiert Zeitpläne, Speicherziele und Benutzer als JSON-Datei.</div>
        <div class="muted" style="font-size:.78rem;">Backup-Daten selbst sind nicht enthalten — nur die Programmeinstellungen.</div>
      </div>
      <div style="display:flex; gap:8px; flex-wrap:wrap;">
        <button class="btn primary" id="config-export-btn">↓ Einstellungen exportieren</button>
        <label class="btn" style="cursor:pointer;">
          ↑ Einstellungen importieren
          <input type="file" id="config-import-file" accept=".json" style="display:none">
        </label>
      </div>
    </div>

    <div class="section-title">Sitzung &amp; Sicherheit</div>
    <div class="card">
      <p style="margin:0 0 4px">Sitzungs-Timeout: <strong>${overview.session_max_age_hours} Stunden</strong>
        <span class="muted">(Umgebungsvariable <span class="mono">DBM_SESSION_MAX_AGE</span> in Sekunden)</span></p>
      <p class="muted" style="margin:0; font-size:12px;">Nach Ablauf der Sitzung wird automatisch abgemeldet.
        Standard: 168 Stunden (7 Tage). Für erhöhte Sicherheit z. B. auf 3600 (1 Stunde) setzen.</p>
    </div>

    <div class="section-title">Serverzeit</div>
    <div class="card">
      <span class="mono" id="server-clock" style="font-size:1.1rem"></span>
      <span class="muted">(Zeitzone: <span class="mono">${overview.timezone}</span> — maßgeblich für Zeitpläne)</span>
      ${overview.timezone_error
        ? `<div style="font-size:.8rem; margin-top:6px; color: var(--warn);">⚠️ ${overview.timezone_error}</div>`
        : overview.timezone === "UTC" ? `<div class="muted" style="font-size:.8rem; margin-top:6px;">
        Läuft ein Zeitplan nicht zur erwarteten Uhrzeit: die Standard-Zeitzone ist UTC. Setze die
        Umgebungsvariable <span class="mono">DBM_TZ</span> auf deine Zeitzone (z. B. <span class="mono">Europe/Berlin</span>)
        und starte den Container neu.</div>` : ""}
    </div>

    <div class="section-title">Speicherort</div>
    <div class="card mono">${overview.backups_dir}</div>

    <div class="section-title">Verschlüsselung</div>
    <div class="card">
      ${overview.encryption_error
        ? `<span class="badge failed">⚠️ Ungültiger Schlüssel</span> <span class="muted"><span class="mono">DBM_ENCRYPTION_KEY</span> ${overview.encryption_error}
           Erzeuge einen echten Schlüssel mit <span class="mono">openssl rand -base64 32</span> (nicht abtippen, sondern das Kommando ausführen und die Ausgabe kopieren) und starte den Container neu.</span>`
        : overview.encryption_enabled
        ? `<span class="badge ok">🔒 Aktiv</span> <span class="muted">Backups werden mit AES-256 verschlüsselt abgelegt (Schlüssel aus <span class="mono">DBM_ENCRYPTION_KEY</span>).</span>`
        : `<span class="badge failed">⚠️ Inaktiv</span> <span class="muted">Backups werden unverschlüsselt gespeichert. Setze die Umgebungsvariable
           <span class="mono">DBM_ENCRYPTION_KEY</span> (z. B. <span class="mono">openssl rand -base64 32</span>) und starte den Container neu.
           Wichtig: Schlüssel sicher aufbewahren – ohne ihn sind bestehende Backups nicht wiederherstellbar.</span>`}
    </div>

    <div class="section-title">Passwort ändern</div>
    <div class="card">
      <div class="grid cols-3">
        <div class="field"><label>Aktuelles Passwort</label><input type="password" id="cur-pass" /></div>
        <div class="field"><label>Neues Passwort</label><input type="password" id="new-pass" /></div>
        <div class="field" style="display:flex; align-items:flex-end;"><button class="btn primary" id="change-pass-btn">Ändern</button></div>
      </div>
    </div>

    <div id="user-mgmt-section" style="display:none">
      <div class="section-title">Benutzerverwaltung</div>
      <div class="card" style="padding:0; margin-bottom:8px;">
        <table>
          <thead><tr><th>Benutzername</th><th>Rolle</th><th>Erstellt</th><th>Status</th><th></th></tr></thead>
          <tbody id="users-tbody"></tbody>
        </table>
      </div>
      <div class="toolbar"><button class="btn primary" id="new-user-btn">Neuen Benutzer anlegen</button></div>
    </div>

    <div id="netpulse-section" style="display:none">
      <div class="section-title">NetPulse-Überwachung (optional)</div>
      <div class="card" id="netpulse-card"><span class="muted">Lade …</span></div>
    </div>

    <div class="section-title">Externe Speicherziele (SMB / NFS / S3 / Google Drive / OneDrive / ...)</div>
    <p class="muted" style="margin-top:-4px">Nach jedem Backup wird zusätzlich auf alle aktivierten Ziele hochgeladen/repliziert.</p>
    <div class="toolbar"><button class="btn primary" id="new-target-btn">Neues Ziel</button></div>
    <div class="card" style="padding:0">
      <table>
        <thead><tr><th>Name</th><th>Typ</th><th>Status</th><th>Letzter Sync</th><th></th></tr></thead>
        <tbody id="targets-tbody"></tbody>
      </table>
    </div>
  </div>`);

  // Live-ticking server clock: compute the offset between server and browser time once,
  // then keep displaying server-time-equivalent using the browser's own clock (no repeated polling).
  const serverTimeOffsetMs = new Date(overview.server_time).getTime() - Date.now();
  const clockEl = wrap.querySelector("#server-clock");
  function tickClock() {
    const now = new Date(Date.now() + serverTimeOffsetMs);
    clockEl.textContent = now.toLocaleString("de-DE", { dateStyle: "medium", timeStyle: "medium" });
  }
  tickClock();
  settingsClockTimer = setInterval(tickClock, 1000);

  // ── Update check (non-blocking) ──────────────────────────────────────────
  const updateCard = wrap.querySelector("#update-card");

  function showReconnecting(version) {
    updateCard.innerHTML = `
      <div style="display:flex; align-items:center; gap:12px;">
        <span class="badge running" style="font-size:.82rem; padding:4px 10px; animation: pulse 1s infinite;">↻ Update wird eingespielt …</span>
        <span class="muted" style="font-size:.88rem;">Container startet neu – Seite lädt automatisch neu …</span>
      </div>`;
    // Poll until server is back up, then reload
    const poll = setInterval(async () => {
      try {
        await fetch("/api/settings/overview", { credentials: "same-origin" });
        clearInterval(poll);
        window.location.reload();
      } catch (_) { /* still restarting */ }
    }, 2000);
  }

  api("/api/settings/update-check").then((upd) => {
    if (upd.error) {
      updateCard.innerHTML = `<span class="muted" style="font-size:.88rem;">Update-Prüfung fehlgeschlagen: ${escHtml(upd.error)}</span>`;
      return;
    }
    const repoUrl = `https://github.com/${upd.github_repo}`;
    if (upd.update_available) {
      updateCard.innerHTML = `
        <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
          <span class="badge running" style="font-size:.82rem; padding:4px 10px;">▲ Update verfügbar</span>
          <span>Version <strong>${escHtml(upd.latest_version)}</strong> ist verfügbar
            <span class="muted">(aktuell: ${escHtml(upd.current_version)})</span></span>
          <a href="${repoUrl}/releases/tag/${encodeURIComponent(upd.tag || upd.latest_version)}" target="_blank" class="btn" style="font-size:.8rem; padding:5px 10px;">Release-Notes</a>
          <button class="btn primary" id="do-update-btn" style="padding:5px 14px;">Jetzt installieren</button>
        </div>
        <div class="muted" style="margin-top:8px; font-size:.8rem;">Der Container lädt das Update von GitHub herunter und startet automatisch neu (~10–20 s).</div>`;
      updateCard.querySelector("#do-update-btn").addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.textContent = "Wird geladen …";
        try {
          await api("/api/settings/apply-update", { method: "POST" });
          showReconnecting(upd.latest_version);
        } catch (err) {
          toast(err.message, "error");
          btn.disabled = false;
          btn.textContent = "Jetzt installieren";
        }
      });
    } else {
      updateCard.innerHTML = `
        <div style="display:flex; align-items:center; gap:10px;">
          <span class="badge ok" style="font-size:.82rem; padding:4px 10px;">✓ Aktuell</span>
          <span class="muted" style="font-size:.88rem;">Version ${escHtml(upd.current_version)} ist die neueste
            — <a href="${repoUrl}/releases" target="_blank">GitHub Releases</a></span>
        </div>`;
    }
  }).catch(() => {
    updateCard.innerHTML = `<span class="muted" style="font-size:.88rem;">Update-Prüfung nicht möglich (kein Internetzugang?).</span>`;
  });

  wrap.querySelector("#config-export-btn").addEventListener("click", async () => {
    try {
      const resp = await fetch("/api/settings/export", { credentials: "same-origin" });
      if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail || "Export fehlgeschlagen"); }
      const cd = resp.headers.get("Content-Disposition") || "";
      const fnMatch = cd.match(/filename="([^"]+)"/);
      const filename = fnMatch ? fnMatch[1] : "dbm-config.json";
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = filename; a.click();
      URL.revokeObjectURL(url);
    } catch (e) { toast(e.message, "error"); }
  });

  wrap.querySelector("#config-import-file").addEventListener("change", async (evt) => {
    const file = evt.target.files[0];
    if (!file) return;
    evt.target.value = "";
    let data;
    try { data = JSON.parse(await file.text()); }
    catch { toast("Ungültige JSON-Datei", "error"); return; }
    const overwrite = confirm("Bereits vorhandene Einträge (gleicher Name) überschreiben?\n\nOK = überschreiben, Abbrechen = überspringen");
    try {
      const result = await api("/api/settings/import", { method: "POST", body: JSON.stringify({ data, overwrite }) });
      toast(`Import abgeschlossen: ${result.targets} Ziele, ${result.schedules} Zeitpläne, ${result.users} Nutzer importiert (${result.skipped} übersprungen)`);
    } catch (e) { toast(e.message, "error"); }
  });

  wrap.querySelector("#change-pass-btn").addEventListener("click", async () => {
    const current_password = wrap.querySelector("#cur-pass").value;
    const new_password = wrap.querySelector("#new-pass").value;
    try {
      await api("/api/auth/change-password", { method: "POST", body: JSON.stringify({ current_password, new_password }) });
      toast("Passwort geändert");
      wrap.querySelector("#cur-pass").value = ""; wrap.querySelector("#new-pass").value = "";
    } catch (e) { toast(e.message, "error"); }
  });

  // User management (admin only)
  if (state.user && state.user.is_admin) {
    wrap.querySelector("#user-mgmt-section").style.display = "";
    async function refreshUsers() {
      const data = await api("/api/auth/users");
      const tbody = wrap.querySelector("#users-tbody");
      tbody.innerHTML = "";
      data.users.forEach((u) => {
        const isSelf = u.username === state.user.username;
        const row = h(`<tr>
          <td><strong>${escHtml(u.username)}</strong>${isSelf ? ' <span class="muted">(ich)</span>' : ""}</td>
          <td>${u.is_admin ? '<span class="badge ok">Admin</span>' : '<span class="badge neutral">Nutzer</span>'}</td>
          <td class="muted">${fmtDate(u.created_at)}</td>
          <td>${u.locked ? '<span class="badge failed">Gesperrt</span>' : '<span class="badge ok">Aktiv</span>'}</td>
          <td style="display:flex; gap:6px;">
            ${u.locked ? `<button class="btn" data-unlock="${u.id}">Entsperren</button>` : ""}
            ${!isSelf ? `<button class="btn danger" data-del="${u.id}">Löschen</button>` : ""}
          </td>
        </tr>`);
        const unlockBtn = row.querySelector("[data-unlock]");
        if (unlockBtn) unlockBtn.addEventListener("click", async () => {
          try { await api(`/api/auth/users/${u.id}/unlock`, { method: "POST" }); toast("Entsperrt"); refreshUsers(); }
          catch (e) { toast(e.message, "error"); }
        });
        const delBtn = row.querySelector("[data-del]");
        if (delBtn) delBtn.addEventListener("click", async () => {
          if (!confirm(`Benutzer "${u.username}" wirklich löschen?`)) return;
          try { await api(`/api/auth/users/${u.id}`, { method: "DELETE" }); toast("Benutzer gelöscht"); refreshUsers(); }
          catch (e) { toast(e.message, "error"); }
        });
        tbody.appendChild(row);
      });
    }
    refreshUsers();
    wrap.querySelector("#new-user-btn").addEventListener("click", () => {
      const overlay = h(`
        <div class="modal-overlay">
          <div class="modal">
            <h3>Neuen Benutzer anlegen</h3>
            <div class="field"><label>Benutzername</label><input type="text" id="nu-name" /></div>
            <div class="field"><label>Passwort (mind. 8 Zeichen)</label><input type="password" id="nu-pass" /></div>
            <div class="field">
              <label><input type="checkbox" id="nu-admin" style="width:auto; margin-right:6px;" />Administrator-Rechte</label>
            </div>
            <div class="row-actions">
              <button class="btn" id="nu-cancel">Abbrechen</button>
              <button class="btn primary" id="nu-save">Anlegen</button>
            </div>
          </div>
        </div>`);
      overlay.querySelector("#nu-cancel").addEventListener("click", () => overlay.remove());
      overlay.querySelector("#nu-save").addEventListener("click", async () => {
        const username = overlay.querySelector("#nu-name").value.trim();
        const password = overlay.querySelector("#nu-pass").value;
        const is_admin = overlay.querySelector("#nu-admin").checked;
        try {
          await api("/api/auth/users", { method: "POST", body: JSON.stringify({ username, password, is_admin }) });
          toast(`Benutzer "${username}" angelegt`);
          overlay.remove();
          refreshUsers();
        } catch (e) { toast(e.message, "error"); }
      });
      document.body.appendChild(overlay);
    });
  }

  const tbody = wrap.querySelector("#targets-tbody");
  const typeLabels = {
    local_path: "SMB/NFS-Pfad (lokal gemountet)",
    smb: "SMB/CIFS (Benutzername/Passwort)",
    s3: "S3-kompatibel",
    rclone: "rclone (SFTP, WebDAV, B2, ...)",
    google_drive: "Google Drive",
    onedrive: "OneDrive",
  };
  if (!targetsData.targets.length) tbody.appendChild(h(`<tr><td colspan="5"><div class="empty-state">Keine externen Ziele konfiguriert</div></td></tr>`));
  targetsData.targets.forEach((t) => {
    const row = h(`<tr>
      <td>${t.name}${t.enabled ? "" : ' <span class="badge neutral">deaktiviert</span>'}</td>
      <td>${typeLabels[t.type] || t.type}</td>
      <td>${t.last_sync_status ? `<span class="badge ${t.last_sync_status === "ok" ? "ok" : "failed"}">${t.last_sync_status}</span>` : '<span class="badge neutral">noch nicht synchronisiert</span>'}</td>
      <td>${fmtDate(t.last_sync_at)}</td>
      <td style="display:flex; gap:8px;">
        <button class="btn edit-btn">Bearbeiten</button>
        <button class="btn test-btn">Testen</button>
        ${["local_path", "smb", "s3", "rclone"].includes(t.type) ? '<button class="btn import-btn">Katalog importieren</button>' : ""}
        <button class="btn danger del-btn">Löschen</button>
      </td>
    </tr>`);
    row.querySelector(".edit-btn").addEventListener("click", () => openStorageTargetModal(t));
    row.querySelector(".test-btn").addEventListener("click", async () => {
      try { await api(`/api/settings/storage-targets/${t.id}/test`, { method: "POST" }); toast("Verbindung erfolgreich"); }
      catch (e) { toast(e.message, "error"); }
    });
    const importBtn = row.querySelector(".import-btn");
    if (importBtn) importBtn.addEventListener("click", async () => {
      if (!confirm(`Speicherziel "${t.name}" nach vorhandenen Backups durchsuchen und in den Katalog übernehmen?`)) return;
      importBtn.disabled = true;
      importBtn.textContent = "Durchsuche...";
      try {
        const res = await api(`/api/settings/storage-targets/${t.id}/import-catalog`, { method: "POST" });
        toast(`${res.found} Backup(s) gefunden, ${res.imported} neu übernommen, ${res.skipped} bereits bekannt`);
        if (res.imported > 0 && state.route === "backups") navigate("backups");
      } catch (e) { toast(e.message, "error"); }
      importBtn.disabled = false;
      importBtn.textContent = "Katalog importieren";
    });
    row.querySelector(".del-btn").addEventListener("click", async () => {
      if (!confirm(`Speicherziel "${t.name}" löschen?`)) return;
      await api(`/api/settings/storage-targets/${t.id}`, { method: "DELETE" });
      navigate("settings");
    });
    tbody.appendChild(row);
  });
  wrap.querySelector("#new-target-btn").addEventListener("click", () => openStorageTargetModal());
  if (state.user && state.user.is_admin) {
    wrap.querySelector("#netpulse-section").style.display = "";
    renderNetPulseCard(wrap.querySelector("#netpulse-card"));
  }
  return wrap;
}

// ---------- NetPulse-Kopplung ----------
async function renderNetPulseCard(card) {
  let cfg;
  try { cfg = await api("/api/netpulse"); } catch (e) { card.innerHTML = `<span class="muted">${escHtml(e.message)}</span>`; return; }
  const st = cfg.status || {};
  card.innerHTML = `
    <p style="margin:0 0 10px; font-size:.88rem;">Verbindet den Backup Manager mit <b>NetPulse</b>: Solange Container für ein Backup oder eine
      Wiederherstellung gestoppt sind, pausiert NetPulse die Überwachung der zugehörigen Dienste – <b>keine Fehlalarme, keine Ausfälle im Verlauf</b>.
      Nach dem Neustart überwacht NetPulse nach einer kurzen Nachlaufzeit wieder. Außerdem meldet der Backup Manager jedes Backup-Ergebnis an NetPulse
      (dort gibt es Alarme für „Backup fehlgeschlagen“ und „Backup überfällig“). Den Schlüssel erzeugst du in NetPulse unter
      <i>Verwaltung → Verbundene Programme → Programm verbinden</i>.</p>
    <label style="display:flex; gap:8px; align-items:center; margin-bottom:12px;"><input type="checkbox" id="np-enabled" style="width:auto;" ${cfg.enabled ? "checked" : ""}/> Kopplung eingeschaltet</label>
    <div class="grid cols-3">
      <div class="field"><label>NetPulse-Adresse</label><input type="text" id="np-url" placeholder="z. B. http://10.10.10.15:18081" value="${escHtml(cfg.url || "")}" /></div>
      <div class="field"><label>Schlüssel (npi_…)</label><input type="password" id="np-token" autocomplete="off" placeholder="${cfg.token_set ? "gespeichert – leer lassen zum Behalten" : "aus NetPulse kopieren"}" /></div>
      <div class="field"><label>Nachlaufzeit nach dem Neustart (Sekunden)</label><input type="number" id="np-grace" min="0" max="1800" value="${cfg.grace_s}" /></div>
      <div class="field"><label>Pause endet spätestens nach (Minuten)</label><input type="number" id="np-max" min="10" max="720" value="${cfg.max_minutes}" /></div>
    </div>
    <div style="display:flex; flex-wrap:wrap; gap:18px; margin-bottom:12px; font-size:.88rem;">
      <label><input type="checkbox" id="np-pause" style="width:auto; margin-right:6px;" ${cfg.pause ? "checked" : ""}/>Überwachung während Backup/Restore pausieren</label>
      <label><input type="checkbox" id="np-report" style="width:auto; margin-right:6px;" ${cfg.report ? "checked" : ""}/>Backup-Ergebnisse melden</label>
      <label><input type="checkbox" id="np-verify" style="width:auto; margin-right:6px;" ${cfg.verify_tls ? "checked" : ""}/>Zertifikat prüfen (bei https mit eigenem Zertifikat ausschalten)</label>
    </div>
    <div class="toolbar" style="margin-bottom:6px;"><button class="btn primary" id="np-save">Speichern</button><button class="btn" id="np-test">Verbindung testen</button>
      <span class="muted" style="font-size:.8rem;">${st.last_error ? `<span class="badge failed">Fehler</span> ${escHtml(st.last_error)} (${fmtDate(st.last_error_at)})`
        : st.last_ok_at ? `<span class="badge ok">verbunden</span> zuletzt ${fmtDate(st.last_ok_at)}` : ""}</span></div>
    <div id="np-result"></div>`;
  const payload = () => ({
    enabled: card.querySelector("#np-enabled").checked,
    url: card.querySelector("#np-url").value.trim(),
    token: card.querySelector("#np-token").value.trim() || null,
    verify_tls: card.querySelector("#np-verify").checked,
    pause: card.querySelector("#np-pause").checked,
    report: card.querySelector("#np-report").checked,
    grace_s: Number(card.querySelector("#np-grace").value || 0),
    max_minutes: Number(card.querySelector("#np-max").value || 240),
  });
  card.querySelector("#np-save").addEventListener("click", async () => {
    try {
      await api("/api/netpulse", { method: "PUT", body: JSON.stringify(payload()) });
      toast("NetPulse-Einstellungen gespeichert");
      renderNetPulseCard(card);
    } catch (e) { toast(e.message, "error"); }
  });
  card.querySelector("#np-test").addEventListener("click", async () => {
    const out = card.querySelector("#np-result");
    out.innerHTML = '<span class="muted">Teste …</span>';
    try {
      const r = await api("/api/netpulse/test", { method: "POST", body: JSON.stringify(payload()) });
      const rows = r.subjects.map((s) => `<tr><td class="mono">${escHtml(s.name)}</td><td>${s.matches.length
        ? s.matches.map((m) => `<span class="badge neutral" title="${escHtml(m.why)}">${m.kind === "device" ? "Gerät" : "Dienst"}: ${escHtml(m.label)}</span>`).join(" ")
        : '<span class="muted">nichts zugeordnet</span>'}</td></tr>`).join("");
      out.innerHTML = `<p style="font-size:.88rem;"><span class="badge ok">✓ Verbunden</span> mit ${escHtml(r.hello.app)} ${escHtml(r.hello.version || "")} als „${escHtml(r.hello.integration)}“.
        So ordnet NetPulse deine Container zu (anpassen kannst du das in NetPulse unter <i>Verbundene Programme</i>):</p>
        <div style="max-height:320px; overflow:auto;"><table><thead><tr><th>Container</th><th>Wird während des Backups pausiert</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    } catch (e) {
      out.innerHTML = `<span class="badge failed">Fehler</span> <span style="font-size:.88rem;">${escHtml(e.message)}</span>`;
    }
  });
}

function openStorageTargetModal(existing) {
  const overlay = h(`
    <div class="modal-overlay">
      <div class="modal">
        <h3>${existing ? "Speicherziel bearbeiten" : "Neues Speicherziel"}</h3>
        <div class="field"><label>Name</label><input type="text" id="t-name" /></div>
        <div class="field"><label>Typ</label>
          <select id="t-type">
            <option value="smb">SMB/CIFS (Server + Benutzername/Passwort)</option>
            <option value="local_path">Bereits gemounteter Pfad (SMB/NFS am Host)</option>
            <option value="s3">S3-kompatibel (AWS S3, MinIO, Wasabi, ...)</option>
            <option value="google_drive">Google Drive (Anmelden per Browser)</option>
            <option value="onedrive">OneDrive (Anmelden per Browser)</option>
            <option value="rclone">rclone-Remote (SFTP, WebDAV, B2, ...)</option>
          </select>
        </div>
        <div id="t-config-fields"></div>
        <div class="row-actions">
          <button class="btn" id="cancel-btn">Abbrechen</button>
          <button class="btn" id="test-btn">Verbindung testen</button>
          <button class="btn primary" id="save-btn">Speichern</button>
        </div>
      </div>
    </div>
  `);
  let oauthPending = null; // { provider, state } once a Google/OneDrive login popup succeeds
  const fieldsEl = overlay.querySelector("#t-config-fields");
  function renderFields(type, cfg) {
    cfg = cfg || {};
    if (type === "smb") {
      fieldsEl.innerHTML = `
        <div class="field"><label>Server (IP oder Hostname)</label><input type="text" id="cfg-server" placeholder="192.168.1.50" value="${cfg.server || ""}" /></div>
        <div class="field"><label>Benutzername</label><input type="text" id="cfg-username" value="${cfg.username || ""}" /></div>
        <div class="field"><label>Passwort</label><input type="password" id="cfg-password" value="${cfg.password || ""}" /></div>
        <div class="field">
          <label>Freigabename (Share)</label>
          <div style="display:flex; gap:8px;">
            <input type="text" id="cfg-share" placeholder="backups" value="${cfg.share || ""}" style="flex:1" />
            <button type="button" class="btn" id="load-shares-btn">Freigaben anzeigen</button>
          </div>
          <div id="cfg-share-results" style="display:flex; flex-wrap:wrap; gap:6px; margin-top:6px;"></div>
          <div class="muted" style="font-size:.75rem; margin-top:4px;">Server, Benutzername und Passwort oben ausfüllen, dann auf "Freigaben anzeigen" klicken.</div>
        </div>
        <div class="field"><label>Unterordner (optional)</label><input type="text" id="cfg-base-path" placeholder="docker-backup-manager" value="${cfg.base_path || ""}" /></div>
        <div class="field"><label>Domain (optional)</label><input type="text" id="cfg-domain" value="${cfg.domain || ""}" /></div>
        <div class="field"><label>Port</label><input type="text" id="cfg-port" value="${cfg.port || "445"}" /></div>`;
      fieldsEl.querySelector("#load-shares-btn").addEventListener("click", async () => {
        const btn = fieldsEl.querySelector("#load-shares-btn");
        btn.disabled = true;
        btn.textContent = "Lade...";
        try {
          const res = await api("/api/settings/smb/shares", {
            method: "POST",
            body: JSON.stringify({
              server: fieldsEl.querySelector("#cfg-server").value.trim(),
              username: fieldsEl.querySelector("#cfg-username").value.trim(),
              password: fieldsEl.querySelector("#cfg-password").value,
              domain: fieldsEl.querySelector("#cfg-domain").value.trim(),
              port: fieldsEl.querySelector("#cfg-port").value.trim() || "445",
            }),
          });
          const results = fieldsEl.querySelector("#cfg-share-results");
          results.innerHTML = "";
          res.shares.forEach((s) => {
            const chip = h(`<button type="button" class="btn" style="padding:4px 10px; font-size:.85rem;">${s}</button>`);
            chip.addEventListener("click", () => { fieldsEl.querySelector("#cfg-share").value = s; });
            results.appendChild(chip);
          });
          if (res.shares.length) toast(`${res.shares.length} Freigabe(n) gefunden - anklicken zum Übernehmen`);
          else toast("Keine Freigaben gefunden", "error");
        } catch (e) {
          toast(e.message, "error");
        } finally {
          btn.disabled = false;
          btn.textContent = "Freigaben anzeigen";
        }
      });
    } else if (type === "local_path") {
      fieldsEl.innerHTML = `
        <div class="field"><label>Pfad im Container (z.B. gemountete SMB/NFS-Freigabe)</label>
          <input type="text" id="cfg-path" placeholder="/mnt/remote-backup" value="${cfg.path || ""}" /></div>`;
    } else if (type === "s3") {
      fieldsEl.innerHTML = `
        <div class="field"><label>Bucket</label><input type="text" id="cfg-bucket" value="${cfg.bucket || ""}" /></div>
        <div class="field"><label>Endpoint-URL (leer = AWS S3)</label><input type="text" id="cfg-endpoint" placeholder="https://s3.eu-central-1.amazonaws.com" value="${cfg.endpoint_url || ""}" /></div>
        <div class="field"><label>Region</label><input type="text" id="cfg-region" placeholder="eu-central-1" value="${cfg.region || ""}" /></div>
        <div class="field"><label>Access Key</label><input type="text" id="cfg-access" value="${cfg.access_key || ""}" /></div>
        <div class="field"><label>Secret Key</label><input type="password" id="cfg-secret" value="${cfg.secret_key || ""}" /></div>
        <div class="field"><label>Präfix (optional)</label><input type="text" id="cfg-prefix" value="${cfg.prefix || ""}" /></div>`;
    } else if (type === "google_drive" || type === "onedrive") {
      const provider = type === "google_drive" ? "google" : "onedrive";
      const providerLabel = type === "google_drive" ? "Google" : "Microsoft";
      const connected = cfg.connected || (oauthPending && oauthPending.state);
      fieldsEl.innerHTML = `
        <div class="field">
          <div id="oauth-status" class="muted" style="margin-bottom:8px;">
            ${connected
              ? `✅ Verbunden${cfg.account ? " als <strong>" + cfg.account + "</strong>" : ""}`
              : "Noch nicht verbunden."}
          </div>
          <button type="button" class="btn" id="oauth-connect-btn">${connected ? "Neu verbinden" : "Mit " + providerLabel + " anmelden"}</button>
        </div>
        <div class="field"><label>Zielordner (optional, wird angelegt falls nötig)</label>
          <input type="text" id="cfg-folder-path" placeholder="docker-backups" value="${cfg.folder_path || ""}" /></div>`;
      fieldsEl.querySelector("#oauth-connect-btn").addEventListener("click", () => {
        const popup = window.open(`/api/settings/oauth/${provider}/start`, "dbm-oauth", "width=520,height=650");
        const onMessage = (event) => {
          if (event.origin !== window.location.origin || !event.data || !event.data.dbmOAuth) return;
          window.removeEventListener("message", onMessage);
          if (!event.data.ok) { toast(`Anmeldung fehlgeschlagen: ${event.data.error}`, "error"); return; }
          oauthPending = { provider, state: event.data.state };
          toast("Erfolgreich verbunden - Speichern nicht vergessen");
          renderFields(type, cfg);
        };
        window.addEventListener("message", onMessage);
      });
    } else {
      fieldsEl.innerHTML = `
        <div class="field"><label>rclone Remote-Name (aus rclone.conf)</label><input type="text" id="cfg-remote" placeholder="gdrive" value="${cfg.remote || ""}" /></div>
        <div class="field"><label>Remote-Pfad</label><input type="text" id="cfg-remote-path" placeholder="docker-backups" value="${cfg.remote_path || ""}" /></div>
        <p class="muted" style="font-size:.8rem">Der Remote muss vorher per <span class="mono">rclone config</span> in der gemounteten rclone.conf eingerichtet sein (unterstützt SFTP, WebDAV, B2, u.v.m. - für Google Drive/OneDrive die eigenen Optionen oben verwenden).</p>`;
    }
  }
  if (existing) {
    overlay.querySelector("#t-name").value = existing.name;
    overlay.querySelector("#t-type").value = existing.type;
    renderFields(existing.type, existing.config);
  } else {
    renderFields("smb");
  }
  overlay.querySelector("#t-type").addEventListener("change", (e) => renderFields(e.target.value));

  function readConfig() {
    const type = overlay.querySelector("#t-type").value;
    let config = {};
    if (type === "smb") config = {
      server: overlay.querySelector("#cfg-server").value.trim(),
      share: overlay.querySelector("#cfg-share").value.trim(),
      base_path: overlay.querySelector("#cfg-base-path").value.trim(),
      username: overlay.querySelector("#cfg-username").value.trim(),
      password: overlay.querySelector("#cfg-password").value,
      domain: overlay.querySelector("#cfg-domain").value.trim(),
      port: overlay.querySelector("#cfg-port").value.trim() || "445",
    };
    else if (type === "local_path") config = { path: overlay.querySelector("#cfg-path").value.trim() };
    else if (type === "s3") config = {
      bucket: overlay.querySelector("#cfg-bucket").value.trim(),
      endpoint_url: overlay.querySelector("#cfg-endpoint").value.trim(),
      region: overlay.querySelector("#cfg-region").value.trim(),
      access_key: overlay.querySelector("#cfg-access").value.trim(),
      secret_key: overlay.querySelector("#cfg-secret").value,
      prefix: overlay.querySelector("#cfg-prefix").value.trim(),
    };
    else if (type === "google_drive" || type === "onedrive") config = {
      folder_path: overlay.querySelector("#cfg-folder-path").value.trim(),
    };
    else config = {
      remote: overlay.querySelector("#cfg-remote").value.trim(),
      remote_path: overlay.querySelector("#cfg-remote-path").value.trim(),
    };
    return { type, config };
  }

  function updateTestButtonVisibility() {
    const isOAuth = ["google_drive", "onedrive"].includes(overlay.querySelector("#t-type").value);
    overlay.querySelector("#test-btn").style.display = isOAuth ? "none" : "";
  }
  updateTestButtonVisibility();
  overlay.querySelector("#t-type").addEventListener("change", updateTestButtonVisibility);

  overlay.querySelector("#cancel-btn").addEventListener("click", () => overlay.remove());
  overlay.querySelector("#test-btn").addEventListener("click", async () => {
    const { type, config } = readConfig();
    const btn = overlay.querySelector("#test-btn");
    btn.disabled = true;
    btn.textContent = "Teste...";
    try {
      await api("/api/settings/storage-targets/test", { method: "POST", body: JSON.stringify({ type, config }) });
      toast("Verbindung erfolgreich");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Verbindung testen";
    }
  });
  overlay.querySelector("#save-btn").addEventListener("click", async () => {
    const { type, config } = readConfig();
    const name = overlay.querySelector("#t-name").value.trim() || type;

    if (type === "google_drive" || type === "onedrive") {
      if (oauthPending) {
        try {
          await api("/api/settings/storage-targets/oauth-complete", {
            method: "POST",
            body: JSON.stringify({
              state: oauthPending.state, name, folder_path: config.folder_path,
              target_id: existing ? existing.id : null,
            }),
          });
          overlay.remove();
          navigate("settings");
        } catch (e) { toast(e.message, "error"); }
        return;
      }
      if (!existing || !existing.config.connected) {
        toast("Bitte zuerst über den Button oben anmelden", "error");
        return;
      }
    }

    const payload = { name, type, config, enabled: existing ? existing.enabled : true };
    try {
      if (existing) {
        await api(`/api/settings/storage-targets/${existing.id}`, { method: "PUT", body: JSON.stringify(payload) });
      } else {
        await api("/api/settings/storage-targets", { method: "POST", body: JSON.stringify(payload) });
      }
      overlay.remove();
      navigate("settings");
    } catch (e) { toast(e.message, "error"); }
  });
  document.body.appendChild(overlay);
}

// ---------- Boot ----------
async function boot() {
  try {
    const authStatus = await fetch("/api/auth/status").then((r) => r.json());
    if (authStatus.setup_required) { render(setupScreen()); return; }
    const me = await api("/api/auth/me").catch(() => null);
    if (!me) { render(loginScreen()); return; }
    state.user = me;
    // Fetch version once at boot so the sidebar always shows it
    api("/api/settings/overview").then((o) => { state.appVersion = o.app_version; }).catch(() => {});
    await navigate("dashboard");
    startGlobalJobPoller();
  } catch (e) {
    render(loginScreen());
  }
}
boot();
