/* =====================================================================
   dashboard.js - top-level status view
   ---------------------------------------------------------------------
   Daily-sessions chart design notes:
     * Fixed-pixel SVG (width = container.clientWidth, height = 280px).
       No `preserveAspectRatio="none"` so labels never stretch / squish.
     * Per-day slot holds two side-by-side bars: Successful and Failed,
       each rising from the baseline. Reads cleanly as two independent
       series so the eye never has to decompose a stacked height.
     * Today's slot gets a soft pulsing glow behind the pair.
     * Date labels rotate -45 deg when there are >14 days so they
       never overlap, and label-every-N is computed against pixel
       budget rather than count.
     * Hover floats an absolute-positioned tooltip that follows the
       mouse and reads the bar group's data attributes.
     * Re-render is RAF-coalesced and skipped when the dashboard tab
       is not active -- saves work that the user cannot see anyway.
   ===================================================================== */
(function () {
  "use strict";
  const { $, fmtBytes, fmtPct, fmtDuration, escapeHtml, resolveUser, subscribe } = window.Nishro;

  // --- Daily sessions chart state / helpers --------------------------
  let dailyData = null;          // { days: [...], today: {...} }
  let dailyRangeDays = 30;
  let dailyFetching = false;
  let dailyLastSessionTotal = -1;
  let renderRafId = 0;
  let resizeObs = null;
  let tooltipEl = null;

  function isDashActive() {
    return !!document.querySelector("#tab-dashboard.active");
  }

  async function fetchDaily(days) {
    if (dailyFetching) return;
    dailyFetching = true;
    try {
      const r = await fetch("/api/sessions/daily?days=" + (days || 30));
      if (r.ok) {
        dailyData = await r.json();
        scheduleRender();
        // Re-render so the right-rail "today" values reflect immediately
        // on every tab (render() is a no-op past the guard when off-tab).
        if (window.Nishro && window.Nishro.state) {
          render(window.Nishro.state);
        }
      }
    } catch { /* ignore */ }
    finally { dailyFetching = false; }
  }

  function scheduleRender() {
    if (renderRafId) return;
    renderRafId = requestAnimationFrame(() => {
      renderRafId = 0;
      renderDailyChart();
    });
  }

  // ----- helpers --------------------------------------------------
  function ensureTooltip() {
    if (tooltipEl) return tooltipEl;
    tooltipEl = document.createElement("div");
    tooltipEl.className = "daily-tooltip";
    document.body.appendChild(tooltipEl);
    return tooltipEl;
  }

  function showTip(target, evt) {
    const t = ensureTooltip();
    t.innerHTML = target.dataset.tip || "";
    t.style.display = "block";
    moveTip(evt);
  }

  function moveTip(evt) {
    if (!tooltipEl) return;
    const pad = 14;
    const w = tooltipEl.offsetWidth;
    const h = tooltipEl.offsetHeight;
    let x = evt.clientX + pad;
    let y = evt.clientY + pad;
    if (x + w > window.innerWidth - 4)  x = evt.clientX - w - pad;
    if (y + h > window.innerHeight - 4) y = evt.clientY - h - pad;
    tooltipEl.style.left = x + "px";
    tooltipEl.style.top  = y + "px";
  }

  function hideTip() { if (tooltipEl) tooltipEl.style.display = "none"; }

  function attachTipHandlers(host) {
    host.querySelectorAll(".bar-group").forEach((g) => {
      g.addEventListener("mouseenter", (e) => showTip(g, e));
      g.addEventListener("mousemove",  moveTip);
      g.addEventListener("mouseleave", hideTip);
    });
  }

  // ----- render ---------------------------------------------------
  function renderDailyChart() {
    const host = $("dailyChart");
    if (!host || !dailyData) return;
    if (!isDashActive()) return;          // skip work when off-tab
    const days = (dailyData.days || []).slice();
    if (!days.length) {
      host.innerHTML = '<div class="daily-empty">'
        + '<svg width="48" height="48" viewBox="0 0 24 24" fill="none">'
        + '<path d="M3 13h4v8H3zM10 9h4v12h-4zM17 5h4v16h-4z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>'
        + '</svg>'
        + '<div>No data yet &mdash; finish a TFTP transfer to see it charted.</div>'
        + '</div>';
      renderDailyTotals();
      return;
    }

    // Geometry from real container width so fonts stay at native size.
    const W = Math.max(360, host.clientWidth || 600);
    const H = 280;
    const padL = 42, padR = 14, padT = 16, padB = 44;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;
    const slot  = plotW / days.length;
    // Two bars per day slot: Successful + Failed side-by-side, each
    // rising from the baseline. `barW` is per-bar width; `innerGap`
    // is the gap between the two bars within a slot.
    const innerGap = Math.max(1, Math.min(3, slot * 0.06));
    const barW = Math.max(2, Math.min(14, (slot * 0.82 - innerGap) / 2));

    // Y-axis scaled against the larger of the two per-day series so
    // neither bar gets visually crushed when the other dominates.
    let maxSeries = 1;
    for (let i = 0; i < days.length; i++) {
      const c = days[i].completed || 0;
      const f = days[i].failed || 0;
      if (c > maxSeries) maxSeries = c;
      if (f > maxSeries) maxSeries = f;
    }
    const niceMax = niceCeil(maxSeries);
    const gridSteps = 4;
    let grid = '';
    let yAxisLabels = '';
    for (let i = 0; i <= gridSteps; i++) {
      const y = padT + plotH * (1 - i / gridSteps);
      const val = Math.round((niceMax * i) / gridSteps);
      grid += '<line class="grid-line" x1="' + padL + '" x2="' + (W - padR) + '" y1="' + y + '" y2="' + y + '"/>';
      yAxisLabels += '<text class="axis-label" x="' + (padL - 6) + '" y="' + (y + 3) + '" text-anchor="end">' + val + '</text>';
    }

    let bars = '';
    const todayIdx = days.length - 1;
    const scale = plotH / niceMax;
    const yBase = padT + plotH;
    days.forEach((d, idx) => {
      const xCenter = padL + slot * (idx + 0.5);
      const xSucc   = xCenter - innerGap / 2 - barW;
      const xFail   = xCenter + innerGap / 2;
      const completed = d.completed || 0;
      const failed    = d.failed    || 0;
      const total     = d.total     || 0;
      const hSucc = completed * scale;
      const hFail = failed    * scale;
      const ySucc = yBase - hSucc;
      const yFail = yBase - hFail;

      const tip =
          '<div class="dt-date">' + escapeHtml(d.date) + (idx === todayIdx ? ' &middot; today' : '') + '</div>'
        + '<div class="dt-row"><span class="dt-sw ok"></span>Successful<span class="dt-val">' + completed + '</span></div>'
        + '<div class="dt-row"><span class="dt-sw fail"></span>Failed<span class="dt-val">' + failed + '</span></div>'
        + '<div class="dt-row dt-tot">Total<span class="dt-val">' + total + '</span></div>'
        + '<div class="dt-bytes">&uarr; ' + escapeHtml(fmtBytes(d.bytes_sent || 0))
        + '   &darr; ' + escapeHtml(fmtBytes(d.bytes_received || 0)) + '</div>';

      const cls = "bar-group" + (idx === todayIdx ? " is-today" : "");
      let group = '<g class="' + cls + '" data-tip=\'' + tip.replace(/'/g, "&#39;") + '\'>';
      group += '<rect class="bar-hit" x="' + (padL + slot * idx) + '" y="' + padT
            + '" width="' + slot + '" height="' + plotH + '"/>';
      // Today glow: a soft halo spanning both bars in this slot, drawn
      // before the bars so they sit on top.
      if (idx === todayIdx && total > 0) {
        const glowX = xSucc - 3;
        const glowW = (xFail + barW) - xSucc + 6;
        const glowTop = Math.min(ySucc, yFail) - 3;
        const glowH   = yBase - glowTop + 3;
        group += '<rect class="bar-glow" x="' + glowX + '" y="' + glowTop
              + '" width="' + glowW + '" height="' + glowH + '" rx="6"/>';
      }
      if (hSucc > 0) {
        group += '<rect class="bar-completed" x="' + xSucc + '" y="' + ySucc
              + '" width="' + barW + '" height="' + hSucc + '" rx="2"/>';
      }
      if (hFail > 0) {
        group += '<rect class="bar-failed" x="' + xFail + '" y="' + yFail
              + '" width="' + barW + '" height="' + hFail + '" rx="2"/>';
      }
      group += '</g>';
      bars += group;
    });

    // X-axis labels: pixel-budget aware. Aim for >= 56px between labels.
    // Rotate -45 deg when slot is narrow (>= 14 days roughly).
    const minLabelPx = 56;
    const labelEvery = Math.max(1, Math.ceil(minLabelPx / Math.max(slot, 1)));
    const rotate = days.length > 14;
    let xLabels = '';
    days.forEach((d, idx) => {
      const isLast = idx === days.length - 1;
      if (idx % labelEvery !== 0 && !isLast) return;
      const xCenter = padL + slot * (idx + 0.5);
      const yLab = H - padB + 16;
      const txt = d.date.slice(5);  // MM-DD
      if (rotate) {
        xLabels += '<text class="axis-label" x="' + xCenter + '" y="' + yLab
                + '" text-anchor="end" transform="rotate(-45 ' + xCenter + ' ' + yLab + ')">'
                + txt + '</text>';
      } else {
        xLabels += '<text class="axis-label" x="' + xCenter + '" y="' + yLab
                + '" text-anchor="middle">' + txt + '</text>';
      }
    });

    // Build SVG. Note: NO viewBox / preserveAspectRatio so fonts and
    // bars render at native pixel size regardless of container width.
    const svg =
        '<svg width="' + W + '" height="' + H + '" xmlns="http://www.w3.org/2000/svg">'
      + grid
      + '<line class="axis-line" x1="' + padL + '" y1="' + (padT + plotH)
      +   '" x2="' + (W - padR) + '" y2="' + (padT + plotH) + '"/>'
      + yAxisLabels
      + bars
      + xLabels
      + '</svg>'
      + '<div class="daily-chart-legend">'
      +   '<span><span class="swatch sw-ok"></span>Successful</span>'
      +   '<span><span class="swatch sw-fail"></span>Failed</span>'
      + '</div>';

    host.innerHTML = svg;
    attachTipHandlers(host);
    renderDailyTotals();
  }

  function niceCeil(n) {
    if (n <= 5) return 5;
    if (n <= 10) return 10;
    const mag = Math.pow(10, Math.floor(Math.log10(n)));
    const norm = n / mag;
    let nice;
    if (norm <= 1) nice = 1;
    else if (norm <= 2) nice = 2;
    else if (norm <= 5) nice = 5;
    else nice = 10;
    return nice * mag;
  }

  /* Summary row below the chart legend: window totals for Successful,
     Failed, and Total sessions. Sits inline with the legend colour
     keys above it so the viewer reads colour -> meaning -> value in a
     single vertical flow. */
  function renderDailyTotals() {
    const host = $("dailyTotals");
    if (!host || !dailyData) return;
    const days = dailyData.days || [];
    let sumT = 0, sumC = 0, sumF = 0;
    for (let i = 0; i < days.length; i++) {
      const d = days[i];
      sumT += d.total || 0;
      sumC += d.completed || 0;
      sumF += d.failed || 0;
    }
    host.innerHTML =
        '<div class="dt-sum is-ok"><span class="dt-sum-lab">Successful</span>'
      +   '<span class="dt-sum-val">' + sumC + '</span></div>'
      + '<div class="dt-sum is-fail"><span class="dt-sum-lab">Failed</span>'
      +   '<span class="dt-sum-val">' + sumF + '</span></div>'
      + '<div class="dt-sum is-total"><span class="dt-sum-lab">Total</span>'
      +   '<span class="dt-sum-val">' + sumT + '</span></div>';
  }

  function maybeRefreshDaily(state) {
    const s = state && state.stats || {};
    const done = Number(s.tftp_sessions_completed || 0)
               + Number(s.tftp_sessions_failed    || 0);
    if (dailyData === null) {
      fetchDaily(dailyRangeDays);
      dailyLastSessionTotal = done;
      return;
    }
    if (done !== dailyLastSessionTotal) {
      dailyLastSessionTotal = done;
      fetchDaily(dailyRangeDays);
    }
  }

  function renderBanner(s) {
    const host = $("nicBanner");
    if (!host) return;
    if (s && s.nic_error) {
      host.innerHTML =
        '<div class="nic-banner">'
        + '<strong>TFTP engine not running.</strong> '
        + escapeHtml(s.nic_error) + ' '
        + '<a href="#" id="nicBannerGoto">Open Admin Config &rarr; Network adapter</a>'
        + '</div>';
      host.style.display = "";
      const link = document.getElementById("nicBannerGoto");
      if (link) {
        link.addEventListener("click", (ev) => {
          ev.preventDefault();
          if (window.Nishro.tabs && window.Nishro.tabs.activate) {
            window.Nishro.tabs.activate("config");
          } else {
            const btn = document.querySelector('[data-tab="config"]');
            if (btn) btn.click();
          }
        });
      }
    } else {
      host.innerHTML = "";
      host.style.display = "none";
    }
  }

  function renderRailCards(hostId, rows) {
    const host = $(hostId);
    if (!host) return;
    host.innerHTML = rows.map(([label, value, cls, icon]) =>
      '<div class="rail-card ' + (cls || '') + '">'
      + '<div class="rail-ico" aria-hidden="true">' + (icon || '') + '</div>'
      + '<div class="rail-body">'
      + '<div class="rail-label">' + label + '</div>'
      + '<div class="rail-value">' + value + '</div>'
      + '</div>'
      + '</div>'
    ).join("");
  }

  /* Inline SVG icons kept tiny and stroke-based so they adopt the
     card text color in both themes. */
  const ICO = {
    chip:    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 3v3M12 3v3M15 3v3M9 18v3M12 18v3M15 18v3M3 9h3M3 12h3M3 15h3M18 9h3M18 12h3M18 15h3"/></svg>',
    clock:   '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    pulse:   '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l2-6 4 12 2-6h6"/></svg>',
    cpu:     '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="8" width="8" height="8" rx="1"/><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M2 10h2M2 14h2M20 10h2M20 14h2M10 2v2M14 2v2M10 20v2M14 20v2"/></svg>',
    server:  '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/></svg>',
    up:      '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
    down:    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12l7 7 7-7"/></svg>',
    cache:   '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>',
    err:     '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.5v.01"/></svg>',
    shield:  '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l8 3v6c0 4.5-3.5 8-8 9-4.5-1-8-4.5-8-9V6z"/><path d="M9.5 11.5l2 2 3.5-4"/></svg>',
    user1:   '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="3.5"/><path d="M5 20c1.2-3.2 4-5 7-5s5.8 1.8 7 5"/></svg>',
    users2:  '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="9" r="3"/><path d="M2 19c.9-2.6 3.5-4 7-4s6.1 1.4 7 4"/><circle cx="17" cy="7.5" r="2.5"/><path d="M15.5 13.3c1.3-.2 2.6-.2 3.9.2 1.6.5 2.7 1.5 3.1 2.8"/></svg>',
  };

  /* Two rail groups pinned in the right margin across every tab.
     - System status: uptime + CPU proc/host + cache hit (compact health strip)
     - Sessions:      live active + successful counts (mirrors the hero so
                      these totals stay visible even when off-tab). */
  function renderRails(state) {
    const s = state.stats || {};
    const procCpu = Number(s.process_cpu || 0);
    const sysCpu  = Number(s.system_cpu  || 0);
    const cores   = Number(s.cpu_cores   || 0);
    const active  = Number(s.session_count || 0);
    const succ    = Number(s.tftp_sessions_completed || 0);

    renderRailCards("dashRailCore", [
      ["Uptime",    fmtDuration(s.uptime),                                             "", ICO.clock],
      ["CPU proc",  procCpu.toFixed(1) + "%",                                          "", ICO.cpu],
      ["CPU host",  sysCpu.toFixed(1) + "%" + (cores ? " / " + cores + "c" : ""),      "", ICO.chip],
      ["Cache hit", fmtPct(s.cache_hit_rate),                                          "", ICO.cache],
    ]);
    renderRailCards("dashRailReach", [
      ["Active sessions",     active, active ? "accent" : "", ICO.pulse],
      ["Successful sessions", succ,   succ   ? "ok"     : "", ICO.shield],
    ]);
  }

  /* Hero stats row at the top of the dashboard. Five big cards using the
     shared .stat-block / .stat-card-row primitives from the stats page,
     so dashboard + stats stay visually coherent. */
  function renderHeroStats(state) {
    const host = $("dashHero");
    if (!host) return;
    const s  = state.stats || {};
    const rc = (dailyData && dailyData.reach_counts) || {};
    const visitors   = Number(rc.visitors_today || 0);
    const devices    = Number(rc.devices_today  || 0);
    const sent       = Number(s.bytes_sent      || 0);
    const recv       = Number(s.bytes_received  || 0);
    const engine     = (s.engine || "python").toLowerCase();
    const down       = !!s.nic_error;
    const engineVal  = down ? "offline"
                              : (engine === "c" ? "C native" : "Python");
    const engineSub  = down ? "not running"
                              : (engine === "c" ? "compiled engine" : "scapy engine");
    const engineCls  = down ? "err" : (engine === "c" ? "ok" : "accent");

    const card = (label, value, sub, cls) =>
        '<div class="stat-card ' + (cls || '') + '">'
      +   '<div class="stat-card-label">' + label + '</div>'
      +   '<div class="stat-card-value">' + value + '</div>'
      +   '<div class="stat-card-sub">'   + (sub || '&nbsp;') + '</div>'
      + '</div>';

    host.innerHTML =
        '<div class="stat-block dash-hero-block">'
      +   '<div class="stat-block-head">'
      +     '<span class="stat-block-title">Today at a glance</span>'
      +   '</div>'
      +   '<div class="stat-card-row stat-card-row-5">'
      +     card("TFTP engine",        escapeHtml(engineVal), engineSub, engineCls)
      +     card("Web users today",    visitors, visitors ? "browser sessions"      : "none yet",
                   visitors ? "ok"     : "")
      +     card("TFTP devices today", devices,  devices  ? "unique client MACs"    : "none yet",
                   devices  ? "ok"     : "")
      +     card("Bytes sent",         fmtBytes(sent), sent ? "RRQ traffic" : "no RRQ yet",
                   sent ? "ok" : "")
      +     card("Bytes received",     fmtBytes(recv), recv ? "WRQ traffic" : "no WRQ yet",
                   recv ? "ok" : "")
      +   '</div>'
      + '</div>';
  }

  function render(state) {
    const s = state.stats || {};
    renderBanner(s);            /* always refresh banner, even off-tab */
    renderRails(state);         /* rails pinned in right margin on every tab */
    maybeRefreshDaily(state);   /* keep "today" values fresh off-tab too */
    if (!isDashActive()) return;
    renderHeroStats(state);     /* hero cards only needed when tab is visible */

    const users = state.users || {};
    const sessions = state.sessions || [];
    if (!sessions.length) {
      $("dashSessions").querySelector("tbody").innerHTML =
        '<tr class="sess-empty"><td colspan="7" style="color:var(--muted)">no active sessions</td></tr>';
    } else {
      const rows = sessions.map((se) => {
        const user = resolveUser(se.filename, users);
        return '<tr class="active-row">'
          + '<td data-label="User">' + (user ? escapeHtml(user) : '<span class="muted">-</span>') + '</td>'
          + '<td data-label="Kind"><span class="chip ' + (se.kind === 'read' ? 'ok' : 'warn') + '">' + se.kind + '</span></td>'
          + '<td data-label="File" class="scell-file">' + escapeHtml(se.filename) + '</td>'
          + '<td data-label="Client IP">' + se.client_ip + '</td>'
          + '<td data-label="VLAN">' + (se.vlan_id != null ? se.vlan_id : '-') + '</td>'
          + '<td data-label="Progress" class="scell-progress">' + fmtBytes(se.bytes_transferred) + '/' + fmtBytes(se.total_bytes)
            + '<div class="progress"><div style="width:' + ((se.progress || 0) * 100) + '%"></div></div></td>'
          + '<td data-label="Speed">' + fmtBytes(se.speed) + '/s</td>'
          + '</tr>';
      }).join("");
      $("dashSessions").querySelector("tbody").innerHTML = rows;
    }
  }

  subscribe(render);
  // Render immediately on tab show
  window.Nishro.tabs.onShow("dashboard", () => {
    render(window.Nishro.state);
    if (dailyData) scheduleRender();
    else fetchDaily(dailyRangeDays);
  });

  document.addEventListener("DOMContentLoaded", () => {
    // Re-render on container width changes (window resize, sidebar toggle).
    const host = $("dailyChart");
    if (host && "ResizeObserver" in window) {
      resizeObs = new ResizeObserver(() => scheduleRender());
      resizeObs.observe(host);
    }
    fetchDaily(dailyRangeDays);
  });
})();
