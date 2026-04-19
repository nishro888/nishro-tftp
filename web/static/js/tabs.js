/* =====================================================================
   tabs.js - tab switching + admin/view mode + auth gating
   ---------------------------------------------------------------------
   Admin tabs (data-admin="1" on the nav button AND the matching section)
   are hidden when body[data-mode="view"] via CSS. Switching to admin
   mode first checks /api/auth/status; if not authenticated the login
   overlay is shown.

   The admin can configure which non-admin tabs are visible in view mode
   via checkboxes in the "View tab visibility" panel. The setting is
   persisted centrally at web.visible_tabs in config.yaml and pushed
   live to every connected browser over the WebSocket.
   ===================================================================== */
(function () {
  "use strict";
  const { $ } = window.Nishro;

  const STORAGE_KEY      = "nishro.mode";
  const THEME_KEY        = "nishro.theme";
  const ALL_VIEW_TABS    = ["dashboard", "sessions", "files", "stats", "logs"];
  const onShowHooks      = {};
  let authenticated      = false;
  let authRequired       = false;
  let hasPassword        = false;
  // Central server-side setting. Seeded from /api/ui_settings on init
  // and refreshed by WS ticks whenever the admin saves a new setting.
  let viewTabs      = ALL_VIEW_TABS.slice();
  let chartColors   = { completed: "#3fb950", failed: "#1f6feb" };
  let recentWindow  = 60;
  let longWindow    = 12;

  function getViewTabs()    { return viewTabs.slice(); }
  function getChartColors() { return Object.assign({}, chartColors); }

  async function saveUiSettings(patch) {
    // Optimistically apply visible_tabs first so the UI feels instant.
    if (Array.isArray(patch.visible_tabs)) {
      viewTabs = patch.visible_tabs.slice();
      applyViewTabVisibility();
    }
    if (patch.daily_chart_colors) {
      chartColors = Object.assign({}, chartColors, patch.daily_chart_colors);
      applyChartColors();
    }
    try {
      await fetch("/api/ui_settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
    } catch { /* server will resync via WS */ }
  }

  function applyChartColors() {
    const root = document.documentElement;
    root.style.setProperty("--chart-completed", chartColors.completed);
    root.style.setProperty("--chart-failed",    chartColors.failed);
  }

  /** Called from state.js when the server pushes new ui_settings. */
  function applyUiSettings(ui) {
    if (!ui) return;
    if (Array.isArray(ui.visible_tabs)) {
      viewTabs = ui.visible_tabs.slice();
      if (!viewTabs.includes("dashboard")) viewTabs.unshift("dashboard");
      applyViewTabVisibility();
    }
    if (ui.daily_chart_colors && typeof ui.daily_chart_colors === "object") {
      chartColors = Object.assign({}, chartColors, ui.daily_chart_colors);
      applyChartColors();
    }
    if (typeof ui.stats_recent_window_sec === "number") recentWindow = ui.stats_recent_window_sec;
    if (typeof ui.stats_long_window_hours === "number") longWindow   = ui.stats_long_window_hours;
    if (window.Nishro.setStatsWindows) {
      window.Nishro.setStatsWindows(recentWindow, longWindow);
    }
    if (typeof ui.auth_required === "boolean") authRequired = ui.auth_required;
    if (typeof ui.has_password === "boolean")  hasPassword  = ui.has_password;
    if (document.body.dataset.mode === "admin") renderViewTabsPanel();
  }

  /** Register a callback to run when a specific tab becomes visible. */
  function onShow(tabId, cb) {
    (onShowHooks[tabId] = onShowHooks[tabId] || []).push(cb);
  }

  function activate(tabId) {
    document.querySelectorAll("nav button").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === tabId);
    });
    document.querySelectorAll(".tab").forEach((s) => {
      s.classList.toggle("active", s.id === "tab-" + tabId);
    });
    positionNavMarker();
    for (const cb of (onShowHooks[tabId] || [])) {
      try { cb(); } catch (e) { console.error("tab onShow hook failed:", e); }
    }
  }

  /** Slide the .nav-marker accent to the active tab. Re-runs on tab
      switch, resize, and view-mode change so the indicator tracks the
      active button's current offset within .nav-track. */
  function positionNavMarker() {
    const track  = document.querySelector(".side-nav .nav-track");
    const marker = track && track.querySelector(".nav-marker");
    if (!track || !marker) return;
    const active = track.querySelector("button.active:not(.view-hidden)");
    if (!active || active.offsetParent === null) {
      marker.style.opacity = "0";
      return;
    }
    const top = active.offsetTop;
    const h   = active.offsetHeight;
    marker.style.opacity = "1";
    marker.style.setProperty("--y", top + "px");
    marker.style.setProperty("--h", h + "px");
  }

  function applyViewTabVisibility() {
    const visible = getViewTabs();
    document.querySelectorAll("nav button").forEach((b) => {
      if (b.dataset.admin === "1") return; // admin tabs handled by CSS
      const id = b.dataset.tab;
      const hidden = !visible.includes(id);
      b.classList.toggle("view-hidden", hidden);
    });
    document.querySelectorAll(".tab").forEach((s) => {
      if (s.dataset.admin === "1") return;
      const id = s.id.replace("tab-", "");
      s.classList.toggle("view-hidden", !visible.includes(id));
    });
  }

  function applyMode(m) {
    document.body.dataset.mode = m;
    localStorage.setItem(STORAGE_KEY, m);
    document.querySelectorAll(".mode-toggle button").forEach((b) => {
      b.classList.toggle("active", b.dataset.mode === m);
    });
    applyViewTabVisibility();
    if (m === "view") {
      const active = document.querySelector("nav button.active");
      if (active && (active.dataset.admin === "1" || active.classList.contains("view-hidden"))) {
        activate("dashboard");
      }
    }
    renderViewTabsPanel();
    positionNavMarker();
  }

  async function setMode(mode) {
    if (mode === "admin") {
      if (!authenticated) {
        const ok = await checkAuth();
        if (!ok) {
          // First-run setup: auth is on but no credentials exist yet.
          // Prompt the user to create the initial admin account instead
          // of asking for credentials that don't exist.
          if (authRequired && !hasPassword) {
            showSetup();
          } else {
            showLogin();
          }
          return;
        }
      }
      applyMode("admin");
    } else {
      applyMode("view");
    }
  }

  async function checkAuth() {
    try {
      const r = await fetch("/api/auth/status");
      const j = await r.json();
      authenticated = !!j.authenticated;
      if (typeof j.required === "boolean")     authRequired = j.required;
      if (typeof j.has_password === "boolean") hasPassword  = j.has_password;
    } catch {
      authenticated = false;
    }
    updateAuthUI();
    return authenticated;
  }

  function updateAuthUI() {
    const logoutBtn = $("authLogout");
    const changePwBtn = $("authChangePassword");
    if (logoutBtn)   logoutBtn.style.display = authenticated ? "" : "none";
    if (changePwBtn) changePwBtn.style.display = authenticated ? "" : "none";
  }

  // -- central UI settings panel (visible_tabs + chart colors) -------
  function renderViewTabsPanel() {
    const host = $("viewTabsPanel");
    if (!host) return;
    if (document.body.dataset.mode !== "admin") {
      host.style.display = "none";
      return;
    }
    host.style.display = "";
    const current = getViewTabs();
    const colors  = getChartColors();
    const labels = {
      dashboard: "Dashboard", sessions: "Sessions",
      files: "Files", stats: "Statistics", logs: "Logs",
    };
    const authPwNote = authRequired
      ? (hasPassword
          ? '<span class="chip ok">admin login required &middot; credentials set</span>'
          : '<span class="chip warn">admin login required &middot; credentials not yet set (a setup prompt will appear on the next admin switch)</span>')
      : '<span class="chip">admin login is OFF &middot; toggle it in the main Config form under Web UI</span>';
    host.innerHTML =
        '<h3>View-mode tab visibility &amp; chart colors (central)</h3>'
      + '<div class="section-desc">All settings here are saved to config.yaml and pushed to every connected browser instantly &mdash; no refresh required. ' + authPwNote + '</div>'
      + '<div class="body" style="grid-template-columns:repeat(auto-fill,minmax(140px,1fr));">'
      + ALL_VIEW_TABS.map((id) => {
        const checked = current.includes(id) ? "checked" : "";
        return '<label class="form-field checkbox">'
          + '<input type="checkbox" class="vtab-check" value="' + id + '" ' + checked + '>'
          + ' ' + labels[id]
          + '</label>';
      }).join("")
      + '</div>'
      + '<div class="body" style="grid-template-columns:repeat(auto-fill,minmax(220px,1fr));">'
      + '<div class="form-field">'
      +   '<label>Daily chart &mdash; successful color</label>'
      +   '<div class="color-input">'
      +     '<input type="color" class="vchart-color" data-key="completed" value="' + colors.completed + '">'
      +     '<input type="text"  class="vchart-hex"   data-key="completed" value="' + colors.completed + '" maxlength="7">'
      +   '</div>'
      + '</div>'
      + '<div class="form-field">'
      +   '<label>Daily chart &mdash; failed color</label>'
      +   '<div class="color-input">'
      +     '<input type="color" class="vchart-color" data-key="failed" value="' + colors.failed + '">'
      +     '<input type="text"  class="vchart-hex"   data-key="failed" value="' + colors.failed + '" maxlength="7">'
      +   '</div>'
      + '</div>'
      + '</div>'
      + '<h3 style="margin-top:16px;">Statistics throughput windows</h3>'
      + '<div class="section-desc">The two throughput charts on the Statistics page use these windows. Saved server-side and applied to every browser.</div>'
      + '<div class="body" style="grid-template-columns:repeat(auto-fill,minmax(240px,1fr));">'
      + '<div class="form-field">'
      +   '<label>Recent window &mdash; seconds (10&ndash;600)</label>'
      +   '<input type="number" id="vstatRecent" min="10" max="600" step="10" value="' + recentWindow + '">'
      + '</div>'
      + '<div class="form-field">'
      +   '<label>Long window &mdash; hours (1&ndash;48)</label>'
      +   '<input type="number" id="vstatLong" min="1" max="48" step="1" value="' + longWindow + '">'
      + '</div>'
      + '</div>';

    host.querySelectorAll(".vtab-check").forEach((cb) => {
      cb.addEventListener("change", () => {
        const tabs = ALL_VIEW_TABS.filter((id) => {
          const el = host.querySelector('.vtab-check[value="' + id + '"]');
          return el && el.checked;
        });
        if (!tabs.includes("dashboard")) tabs.unshift("dashboard");
        saveUiSettings({ visible_tabs: tabs });
      });
    });

    const HEX = /^#[0-9a-fA-F]{6}$/;
    const commitColors = () => {
      const next = { completed: chartColors.completed, failed: chartColors.failed };
      host.querySelectorAll(".vchart-color").forEach((el) => {
        const key = el.dataset.key;
        if (HEX.test(el.value)) next[key] = el.value;
      });
      saveUiSettings({ daily_chart_colors: next });
    };
    host.querySelectorAll(".vchart-color").forEach((el) => {
      el.addEventListener("change", () => {
        const hex = host.querySelector('.vchart-hex[data-key="' + el.dataset.key + '"]');
        if (hex) hex.value = el.value;
        commitColors();
      });
    });
    host.querySelectorAll(".vchart-hex").forEach((el) => {
      el.addEventListener("change", () => {
        const v = el.value.trim();
        if (!HEX.test(v)) { el.value = chartColors[el.dataset.key]; return; }
        const sw = host.querySelector('.vchart-color[data-key="' + el.dataset.key + '"]');
        if (sw) sw.value = v;
        commitColors();
      });
    });

    const rec = host.querySelector("#vstatRecent");
    if (rec) rec.addEventListener("change", () => {
      const v = Math.max(10, Math.min(600, parseInt(rec.value, 10) || recentWindow));
      rec.value = v;
      recentWindow = v;
      if (window.Nishro.setStatsWindows) window.Nishro.setStatsWindows(recentWindow, longWindow);
      saveUiSettings({ stats_recent_window_sec: v });
    });
    const lng = host.querySelector("#vstatLong");
    if (lng) lng.addEventListener("change", () => {
      const v = Math.max(1, Math.min(48, parseInt(lng.value, 10) || longWindow));
      lng.value = v;
      longWindow = v;
      if (window.Nishro.setStatsWindows) window.Nishro.setStatsWindows(recentWindow, longWindow);
      saveUiSettings({ stats_long_window_hours: v });
    });
  }

  // -- login overlay --------------------------------------------------
  function showLogin() {
    $("loginOverlay").classList.add("visible");
    $("loginError").textContent = "";
    $("loginUser").value = "";
    $("loginPass").value = "";
    $("loginUser").parentElement.style.display = "";
    $("loginPass").parentElement.style.display = "";
    $("loginSubmit").style.display = "";
    $("loginCancel").style.display = "";
    $("changePwSection").style.display = "none";
    const setup = $("setupSection");
    if (setup) setup.style.display = "none";
    const title = $("loginTitle");
    if (title) title.textContent = "Admin login";
    $("loginUser").focus();
  }

  /** First-run credential setup prompt (no current password exists).
      Hides the top-row loginCancel so only the Cancel inside setupSection
      (beside Create admin) is visible. */
  function showSetup() {
    $("loginOverlay").classList.add("visible");
    $("loginError").textContent = "";
    $("loginUser").parentElement.style.display = "none";
    $("loginPass").parentElement.style.display = "none";
    $("loginSubmit").style.display = "none";
    $("loginCancel").style.display = "none";
    $("changePwSection").style.display = "none";
    const setup = $("setupSection");
    if (setup) {
      setup.style.display = "block";
      $("setupUser").value = "";
      $("setupPass").value = "";
      $("setupPass2").value = "";
      $("setupUser").focus();
    }
    const title = $("loginTitle");
    if (title) title.textContent = "Set initial admin credentials";
  }

  function hideLogin() {
    $("loginOverlay").classList.remove("visible");
  }

  async function doSetup() {
    const user  = $("setupUser").value.trim();
    const pw    = $("setupPass").value;
    const pw2   = $("setupPass2").value;
    if (!user || !pw) {
      $("loginError").textContent = "username and password required";
      return;
    }
    if (pw.length < 4) {
      $("loginError").textContent = "password must be at least 4 characters";
      return;
    }
    if (pw !== pw2) {
      $("loginError").textContent = "passwords do not match";
      return;
    }
    try {
      const r = await fetch("/api/auth/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: user, password: pw }),
      });
      const j = await r.json();
      if (j.ok) {
        authenticated = true;
        hasPassword   = true;
        hideLogin();
        applyMode("admin");
        updateAuthUI();
      } else {
        $("loginError").textContent = j.error || "setup failed";
      }
    } catch (e) {
      $("loginError").textContent = "connection error";
    }
  }

  async function doLogin() {
    const user = $("loginUser").value.trim();
    const pass = $("loginPass").value;
    if (!user || !pass) {
      $("loginError").textContent = "username and password required";
      return;
    }
    try {
      const r = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: user, password: pass }),
      });
      const j = await r.json();
      if (j.ok) {
        authenticated = true;
        hideLogin();
        applyMode("admin");
        updateAuthUI();
      } else {
        $("loginError").textContent = j.error || "login failed";
      }
    } catch (e) {
      $("loginError").textContent = "connection error";
    }
  }

  async function doLogout() {
    try {
      await fetch("/api/logout", { method: "POST" });
    } catch { /* ignore */ }
    authenticated = false;
    applyMode("view");
    updateAuthUI();
  }

  // -- change password ------------------------------------------------
  /* Hides the top-row loginCancel so the only Cancel visible is the one
     beside the Change button inside changePwSection. */
  function showChangePassword() {
    $("loginOverlay").classList.add("visible");
    $("changePwSection").style.display = "block";
    $("loginError").textContent = "";
    $("loginUser").parentElement.style.display = "none";
    $("loginPass").parentElement.style.display = "none";
    $("loginSubmit").style.display = "none";
    $("loginCancel").style.display = "none";
    const setup = $("setupSection");
    if (setup) setup.style.display = "none";
    $("cpOldPass").value = "";
    $("cpNewUser").value = "";
    $("cpNewPass").value = "";
    $("cpNewPass2").value = "";
    $("cpOldPass").focus();
  }

  function hideChangePassword() {
    $("changePwSection").style.display = "none";
    $("loginUser").parentElement.style.display = "";
    $("loginPass").parentElement.style.display = "";
    $("loginSubmit").style.display = "";
    $("loginCancel").style.display = "";
    hideLogin();
  }

  async function doChangePassword() {
    const oldPw = $("cpOldPass").value;
    const newUser = $("cpNewUser").value.trim();
    const newPw = $("cpNewPass").value;
    const newPw2 = $("cpNewPass2").value;
    if (!oldPw || !newPw) {
      $("loginError").textContent = "fill in all required fields";
      return;
    }
    if (newPw !== newPw2) {
      $("loginError").textContent = "new passwords do not match";
      return;
    }
    try {
      const r = await fetch("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          old_password: oldPw,
          new_username: newUser,
          new_password: newPw,
        }),
      });
      const j = await r.json();
      if (j.ok) {
        $("loginError").textContent = "";
        hideChangePassword();
        authenticated = false;
        applyMode("view");
        updateAuthUI();
      } else {
        $("loginError").textContent = j.error || "change failed";
      }
    } catch (e) {
      $("loginError").textContent = "connection error";
    }
  }

  /** Per-browser light/dark theme toggle. Not synced across clients. */
  function initThemeToggle() {
    const btn = $("themeToggle");
    if (!btn) return;
    const syncAria = () => {
      const t = document.documentElement.getAttribute("data-theme") || "dark";
      btn.setAttribute("aria-checked", t === "light" ? "true" : "false");
    };
    syncAria();
    btn.addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme") || "dark";
      const next = cur === "light" ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem(THEME_KEY, next); } catch { /* ignore */ }
      syncAria();
    });
  }

  function init() {
    initThemeToggle();
    document.querySelectorAll("nav button[data-tab]").forEach((b) => {
      b.addEventListener("click", () => activate(b.dataset.tab));
    });
    window.addEventListener("resize", positionNavMarker, { passive: true });
    document.querySelectorAll(".mode-toggle button").forEach((b) => {
      b.addEventListener("click", () => setMode(b.dataset.mode));
    });
    $("loginSubmit").addEventListener("click", doLogin);
    $("loginCancel").addEventListener("click", () => { hideLogin(); hideChangePassword(); });
    $("loginPass").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
    $("authLogout").addEventListener("click", doLogout);
    $("authChangePassword").addEventListener("click", showChangePassword);
    $("cpSubmit").addEventListener("click", doChangePassword);
    $("cpCancel").addEventListener("click", hideChangePassword);
    const setupSubmit = $("setupSubmit");
    if (setupSubmit) setupSubmit.addEventListener("click", doSetup);
    const setupCancel = $("setupCancel");
    if (setupCancel) setupCancel.addEventListener("click", () => {
      $("setupSection").style.display = "none";
      $("loginCancel").style.display = "";
      hideLogin();
    });
    const setupPass2 = $("setupPass2");
    if (setupPass2) setupPass2.addEventListener("keydown", (e) => { if (e.key === "Enter") doSetup(); });

    // Fetch central UI settings (visible_tabs) before picking the mode,
    // so the initial render already hides tabs the admin turned off.
    fetch("/api/ui_settings")
      .then((r) => r.ok ? r.json() : null)
      .then((ui) => {
        if (!ui) return;
        if (Array.isArray(ui.visible_tabs)) {
          viewTabs = ui.visible_tabs.slice();
          if (!viewTabs.includes("dashboard")) viewTabs.unshift("dashboard");
        }
        if (ui.daily_chart_colors && typeof ui.daily_chart_colors === "object") {
          chartColors = Object.assign({}, chartColors, ui.daily_chart_colors);
        }
        if (typeof ui.stats_recent_window_sec === "number") recentWindow = ui.stats_recent_window_sec;
        if (typeof ui.stats_long_window_hours === "number") longWindow   = ui.stats_long_window_hours;
        if (typeof ui.auth_required === "boolean") authRequired = ui.auth_required;
        if (typeof ui.has_password === "boolean")  hasPassword  = ui.has_password;
        if (window.Nishro.setStatsWindows) window.Nishro.setStatsWindows(recentWindow, longWindow);
        applyChartColors();
      })
      .catch(() => { /* WS init frame will resync */ })
      .finally(() => {
        checkAuth().then(() => {
          const saved = localStorage.getItem(STORAGE_KEY) || "view";
          if (saved === "admin" && authenticated) {
            applyMode("admin");
          } else {
            applyMode("view");
          }
        });
      });
    activate("dashboard");

    // Poll auth status every 60 s. Auto-logout when the 10-min token expires.
    setInterval(async () => {
      if (!authenticated) return;
      const still = await checkAuth();
      if (!still && document.body.dataset.mode === "admin") {
        applyMode("view");
      }
    }, 60_000);
  }

  window.Nishro.tabs = { init, activate, onShow, setMode, applyUiSettings };
})();
