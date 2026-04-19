/* =====================================================================
   stats.js -- statistics dashboard
   ---------------------------------------------------------------------
   Layout:
     1. Traffic     -- five compact cards (totals + active + completed)
     2. Throughput  -- last ~1 min, area chart + side stat panel
     3. Throughput  -- 12-hour view, area chart + side stat panel
     4. TFTP protocol / Cache / Security / System  -- compact card grids

   Charts are inline SVG -- one polyline per series + a gradient fill
   beneath the "sent" series. The y-axis auto-scales against the larger
   of the two series; the x-axis label set adapts to the sample window.
   ===================================================================== */
(function () {
  "use strict";
  const { $, fmtBytes, fmtPct, fmtDuration, subscribe } = window.Nishro;

  /* -------------------------------------------------------------------
     Helpers: derive per-tick rate, compute peak, average, format bytes/s
     ------------------------------------------------------------------- */
  function toRate(series) {
    if (!series || series.length < 2) return [];
    const r = new Array(series.length - 1);
    for (let i = 1; i < series.length; i++) {
      r[i - 1] = Math.max(0, series[i] - series[i - 1]);
    }
    return r;
  }
  function arrMax(a)  { let m = 0; for (let i = 0; i < a.length; i++) if (a[i] > m) m = a[i]; return m; }
  function arrAvg(a)  { if (!a.length) return 0; let s = 0; for (let i = 0; i < a.length; i++) s += a[i]; return s / a.length; }
  function fmtRate(bytesPerSec) { return fmtBytes(bytesPerSec) + "/s"; }

  function niceCeil(n) {
    if (n <= 0) return 1;
    const mag = Math.pow(10, Math.floor(Math.log10(n)));
    const norm = n / mag;
    let nice;
    if      (norm <= 1) nice = 1;
    else if (norm <= 2) nice = 2;
    else if (norm <= 5) nice = 5;
    else                nice = 10;
    return nice * mag;
  }

  function fmtAxisRate(bytes, perSec) {
    /* compact axis label: "1.8 MiB/s" or "220 KiB" */
    const u = fmtBytes(bytes);
    return perSec ? u + "/s" : u;
  }

  /* -------------------------------------------------------------------
     Inline area-chart renderer.
       host        -- target div
       sent / recv -- numeric arrays at the same resolution
       perSec      -- whether y units are bytes/sec (true) or bytes/sample
       xLabels     -- array of {pos: 0..1, text: "-1m"} markers
       height      -- pixel height of the plot
     ------------------------------------------------------------------- */
  function renderAreaChart(host, opts) {
    if (!host) return;
    const sent = opts.sent || [];
    const recv = opts.recv || [];
    const H    = opts.height || 200;
    const padL = 46, padR = 12, padT = 18, padB = 24;
    const W    = Math.max(320, host.clientWidth || 480);
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    /* Y-scale against larger of the two series; show 4 grid lines. */
    const peak = Math.max(arrMax(sent), arrMax(recv), 1);
    const niceMax = niceCeil(peak);
    const grid = 4;

    /* No samples: render an empty axis frame instead of nothing. */
    const haveData = sent.length > 1 || recv.length > 1;

    const ax = (vals, lineCls, fillId) => {
      if (vals.length < 2) return "";
      const step = plotW / (vals.length - 1);
      const pts = new Array(vals.length);
      for (let i = 0; i < vals.length; i++) {
        const x = padL + i * step;
        const y = padT + plotH - (vals[i] / niceMax) * plotH;
        pts[i] = x.toFixed(1) + "," + y.toFixed(1);
      }
      let out = "";
      if (fillId) {
        const baseY = padT + plotH;
        const first = padL.toFixed(1) + "," + baseY.toFixed(1);
        const last  = (padL + (vals.length - 1) * step).toFixed(1) + "," + baseY.toFixed(1);
        out += '<polygon class="stchart-fill" fill="url(#' + fillId + ')" '
             + 'points="' + first + " " + pts.join(" ") + " " + last + '"/>';
      }
      out += '<polyline class="' + lineCls + '" fill="none" stroke-width="1.6" '
           + 'stroke-linejoin="round" stroke-linecap="round" points="' + pts.join(" ") + '"/>';
      return out;
    };

    /* Y grid + labels */
    let gridLines = "";
    let yLabels   = "";
    for (let i = 0; i <= grid; i++) {
      const y = padT + plotH * (1 - i / grid);
      const v = (niceMax * i) / grid;
      gridLines += '<line class="stchart-grid" x1="' + padL + '" x2="' + (W - padR)
                + '" y1="' + y + '" y2="' + y + '"/>';
      yLabels   += '<text class="stchart-axis" x="' + (padL - 6) + '" y="' + (y + 3)
                + '" text-anchor="end">' + fmtAxisRate(v, opts.perSec) + '</text>';
    }

    /* X labels */
    let xLabelsSvg = "";
    (opts.xLabels || []).forEach((l) => {
      const x = padL + plotW * l.pos;
      xLabelsSvg += '<text class="stchart-axis" x="' + x + '" y="' + (H - 6)
                 + '" text-anchor="middle">' + l.text + '</text>';
    });

    const fillId = "stfill-" + Math.random().toString(36).slice(2, 8);

    const svg =
        '<svg width="' + W + '" height="' + H + '" xmlns="http://www.w3.org/2000/svg">'
      +   '<defs>'
      +     '<linearGradient id="' + fillId + '" x1="0" x2="0" y1="0" y2="1">'
      +       '<stop class="stchart-fill-stop-top"    offset="0%"/>'
      +       '<stop class="stchart-fill-stop-bottom" offset="100%"/>'
      +     '</linearGradient>'
      +   '</defs>'
      +   gridLines
      +   '<line class="stchart-axis-line" x1="' + padL + '" x2="' + (W - padR)
      +       '" y1="' + (padT + plotH) + '" y2="' + (padT + plotH) + '"/>'
      +   yLabels
      +   xLabelsSvg
      +   (haveData
            ? (ax(sent, "stchart-line stchart-line-sent", fillId)
             + ax(recv, "stchart-line stchart-line-recv", null))
            : '<text class="stchart-empty" x="' + (padL + plotW / 2) + '" y="' + (padT + plotH / 2)
              + '" text-anchor="middle">collecting samples...</text>')
      + '</svg>';
    host.innerHTML = svg;
  }

  /* -------------------------------------------------------------------
     X-axis label generators.
       short window: ~60 ticks at 1s spacing -> "-60s ... 0s"
       long  window: up to 4320 ticks at 10s -> "-12h ... 0h" by hour
     ------------------------------------------------------------------- */
  function xLabelsShort(n) {
    if (n < 2) return [];
    const totalSec = n - 1;
    const steps = 5;
    const out = [];
    for (let i = 0; i <= steps; i++) {
      const t = (i / steps);
      const sec = Math.round(totalSec * (1 - t));
      out.push({ pos: t, text: sec === 0 ? "0s" : "-" + sec + "s" });
    }
    return out;
  }
  function xLabelsLong(n) {
    if (n < 2) return [];
    const totalSec = (n - 1) * 10;
    const steps = 6;
    const out = [];
    for (let i = 0; i <= steps; i++) {
      const t = (i / steps);
      const sec = totalSec * (1 - t);
      let text;
      if (sec === 0)            text = "0";
      else if (sec >= 3600)     text = "-" + (sec / 3600).toFixed(sec >= 36000 ? 0 : 1) + "h";
      else if (sec >= 60)       text = "-" + Math.round(sec / 60) + "m";
      else                       text = "-" + Math.round(sec) + "s";
      out.push({ pos: t, text: text });
    }
    return out;
  }

  /* -------------------------------------------------------------------
     Markup helpers
     ------------------------------------------------------------------- */
  function statCard(label, value, sub, cls) {
    return ''
      + '<div class="stat-card ' + (cls || '') + '">'
      +   '<div class="stat-card-label">' + label + '</div>'
      +   '<div class="stat-card-value">' + value + '</div>'
      +   '<div class="stat-card-sub">'   + (sub || '&nbsp;') + '</div>'
      + '</div>';
  }
  function statBlock(title, eyebrow, body, opts) {
    const flat = opts && opts.flat ? ' flat' : '';
    return ''
      + '<div class="stat-block' + flat + '">'
      +   '<div class="stat-block-head">'
      +     '<span class="stat-block-title">' + title + '</span>'
      +     (eyebrow ? '<span class="stat-block-eyebrow">' + eyebrow + '</span>' : '')
      +   '</div>'
      +   body
      + '</div>';
  }
  function chartPanel(title, eyebrow, canvasId, sideHtml) {
    return statBlock(title, eyebrow,
        '<div class="stchart-body">'
      +   '<div class="stchart-canvas" id="' + canvasId + '"></div>'
      +   '<div class="stchart-side">' + sideHtml + '</div>'
      + '</div>');
  }
  function sideStat(label, color, value, sub) {
    return ''
      + '<div class="stchart-stat">'
      +   '<div class="stchart-stat-label" style="--dot:' + color + '">' + label + '</div>'
      +   '<div class="stchart-stat-value">' + value + '</div>'
      +   '<div class="stchart-stat-sub">'   + (sub || '&nbsp;') + '</div>'
      + '</div>';
  }
  function compactCard(label, value, sub, cls) {
    return ''
      + '<div class="stat-mini ' + (cls || '') + '">'
      +   '<div class="stat-mini-label">' + label + '</div>'
      +   '<div class="stat-mini-value">' + value + '</div>'
      +   (sub ? '<div class="stat-mini-sub">' + sub + '</div>' : '')
      + '</div>';
  }

  /* -------------------------------------------------------------------
     Cache clear handler (admin-only button rendered conditionally)
     ------------------------------------------------------------------- */
  async function clearCache() {
    const status = $("cacheClearStatus");
    try {
      const r = await fetch("/api/cache/invalidate", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      const j = await r.json();
      if (j.ok && status) { status.textContent = "cleared"; status.className = "chip ok"; }
    } catch (e) {
      if (status) { status.textContent = "error"; status.className = "chip err"; }
    }
    setTimeout(() => {
      if (status) { status.textContent = ""; status.className = "chip"; }
    }, 2000);
  }

  /* -------------------------------------------------------------------
     Render
     ------------------------------------------------------------------- */
  let rafId = 0;
  let resizeObs = null;

  function render(state) {
    if (!document.querySelector("#tab-stats.active")) return;
    const s = state.stats || {};
    const c = s.cache || {};
    const H = state.history || {};
    const L = state.longHist || {};

    /* ----- Traffic header eyebrow: "since boot · X ago" ----- */
    const uptimeStr = "since boot \u00b7 " + fmtDuration(s.uptime || 0) + " ago";

    /* ----- Top traffic cards ----- */
    const totalSess  = Number(s.tftp_sessions_total      || 0);
    const completed  = Number(s.tftp_sessions_completed  || 0);
    const failed     = Number(s.tftp_sessions_failed     || 0);
    const active     = Number(s.session_count            || 0);
    const sentBytes  = Number(s.bytes_sent               || 0);
    const recvBytes  = Number(s.bytes_received           || 0);

    const sentRateShort = toRate(H.bytesSent || []);
    const recvRateShort = toRate(H.bytesRecv || []);
    const sentRateLong  = toRate(L.bytesSent || []);
    const recvRateLong  = toRate(L.bytesRecv || []);

    const peakShortSent = arrMax(sentRateShort);
    const peakLongSent  = arrMax(sentRateLong);
    const peakLongRecv  = arrMax(recvRateLong);

    const sentSubText = peakShortSent > 0 ? "peak " + fmtRate(peakShortSent) : "idle";
    const recvSubText = recvBytes > 0      ? "in flow"                       : "idle";
    const totalSub    = active + " active \u00b7 " + (totalSess - active) + " done";
    const activeSub   = active ? "in flight" : "idle";
    const completedSub= completed
                          ? "ok" + (failed ? " \u00b7 " + failed + " err" : "")
                          : (failed ? failed + " err" : "none yet");

    const trafficCards =
        statCard("Total bytes sent",     fmtBytes(sentBytes), sentSubText, "accent")
      + statCard("Total bytes received", fmtBytes(recvBytes), recvSubText)
      + statCard("Total sessions",       totalSess,           totalSub)
      + statCard("Active",               active,              activeSub, active ? "accent" : "")
      + statCard("Completed",            completed,           completedSub, completed ? "ok" : "");

    const trafficBlock = statBlock("Traffic", uptimeStr,
      '<div class="stat-card-row stat-card-row-5">' + trafficCards + '</div>');

    /* ----- Throughput LAST 1 MINUTE ----- */
    const W = (window.Nishro && window.Nishro.statsWindows) || { recentSec: 60, longHours: 12 };
    const recentLabel = W.recentSec >= 60
        ? (W.recentSec % 60 === 0 ? (W.recentSec / 60) + " min" : (W.recentSec / 60).toFixed(1) + " min")
        : W.recentSec + " s";
    const longLabel   = W.longHours === 1 ? "1-hour view" : W.longHours + "-hour view";

    const shortAvg = arrAvg(sentRateShort);
    const shortAvgRecv = arrAvg(recvRateShort);
    const shortPeakRecv = arrMax(recvRateShort);
    const shortPanel = chartPanel(
      "Throughput \u00b7 Last " + recentLabel,
      "1 s resolution",
      "stchart-short",
        sideStat("BYTES SENT / TICK", "var(--stchart-sent)",
                 fmtRate(shortAvg),
                 "avg \u00b7 peak " + fmtRate(peakShortSent))
      + sideStat("BYTES RECEIVED / TICK", "var(--stchart-recv)",
                 fmtRate(shortAvgRecv),
                 shortPeakRecv ? "peak " + fmtRate(shortPeakRecv) : "no WRQ")
    );

    /* ----- Throughput 12-HOUR VIEW ----- */
    const longN = sentRateLong.length;
    const longRecorded = longN < 6  ? "collecting..."
                       : longN < 360 ? Math.round(longN * 10 / 60) + " min recorded"
                       : (longN * 10 / 3600).toFixed(1) + " h recorded";
    const longSentTotal = sentRateLong.reduce((a, b) => a + b, 0);
    const longRecvTotal = recvRateLong.reduce((a, b) => a + b, 0);
    const longTotalCap  = W.longHours + " h total";
    const longPanel = chartPanel(
      "Throughput \u00b7 " + longLabel,
      longRecorded + " \u00b7 10 s resolution",
      "stchart-long",
        sideStat("BYTES SENT / 10 s", "var(--stchart-sent)",
                 fmtBytes(longSentTotal),
                 longTotalCap + " \u00b7 peak " + fmtRate(peakLongSent / 10))
      + sideStat("BYTES RECEIVED / 10 s", "var(--stchart-recv)",
                 fmtBytes(longRecvTotal),
                 peakLongRecv
                    ? longTotalCap + " \u00b7 peak " + fmtRate(peakLongRecv / 10)
                    : "no WRQ in window")
    );

    /* ----- TFTP protocol ----- */
    const protocolCards =
        compactCard("RRQ (read)",     s.tftp_rrq    || 0)
      + compactCard("WRQ (write)",    s.tftp_wrq    || 0)
      + compactCard("Errors sent",    s.tftp_errors || 0, null, (s.tftp_errors || 0) ? "err" : "")
      + compactCard("ARP req / rep",  (s.arp_requests  || 0) + " / " + (s.arp_replies  || 0))
      + compactCard("ICMP req / rep", (s.icmp_requests || 0) + " / " + (s.icmp_replies || 0));
    const protocolBlock = statBlock("TFTP protocol", null,
      '<div class="stat-mini-row">' + protocolCards + '</div>', { flat: true });

    /* ----- Cache (deprecated) ----- */
    const cacheEnabled = !!c.enabled;
    const cacheCards =
        compactCard("Status",   cacheEnabled ? "ON" : "OFF", "deprecated feature", cacheEnabled ? "warn" : "")
      + compactCard("Hit rate", fmtPct(s.cache_hit_rate),
                    (c.hits || 0) + " hits \u00b7 " + (c.misses || 0) + " misses")
      + compactCard("Used",     fmtBytes(c.used_bytes), "cap " + fmtBytes(c.max_bytes))
      + compactCard("Entries",  c.entries || 0);
    const cacheButton = (document.body.dataset.mode === "admin" && cacheEnabled)
      ? '<div class="row" style="padding-top:8px;">'
        + '<button class="btn sec" id="cacheClear">Clear cache</button> '
        + '<span class="chip" id="cacheClearStatus"></span></div>'
      : '';
    const cacheBlock = statBlock("Cache",
      cacheEnabled ? "deprecated \u00b7 on" : "deprecated \u00b7 off",
      '<div class="stat-mini-row">' + cacheCards + '</div>' + cacheButton,
      { flat: true });

    /* ----- Security ----- */
    const totalDenied = Number(s.acl_denied || 0);
    const secEyebrow  = totalDenied ? totalDenied + " denials" : "clean";
    const securityCards =
        compactCard("ACL denied (total)", s.acl_denied  || 0, null, totalDenied            ? "err"  : "")
      + compactCard("VLAN denied",        s.vlan_denied || 0, null, (s.vlan_denied || 0)   ? "warn" : "")
      + compactCard("IP denied",          s.ip_denied   || 0, null, (s.ip_denied   || 0)   ? "warn" : "");
    const securityBlock = statBlock("Security", secEyebrow,
      '<div class="stat-mini-row">' + securityCards + '</div>', { flat: true });

    /* ----- System ----- */
    const coresSuffix = s.cpu_cores ? " / " + s.cpu_cores + " cores" : "";
    const systemCards =
        compactCard("Uptime",        fmtDuration(s.uptime || 0))
      + compactCard("CPU (process)", (s.process_cpu || 0).toFixed(1) + "% of 1 core")
      + compactCard("CPU (machine)", (s.system_cpu  || 0).toFixed(1) + "%" + coresSuffix)
      + compactCard("Memory (RSS)",  fmtBytes(s.process_rss));
    const systemBlock = statBlock("System", null,
      '<div class="stat-mini-row">' + systemCards + '</div>', { flat: true });

    /* ----- Stitch ----- */
    $("statBody").innerHTML =
        trafficBlock
      + shortPanel
      + longPanel
      + protocolBlock
      + cacheBlock
      + securityBlock
      + systemBlock;

    /* ----- Draw the two charts after innerHTML so the canvases exist. */
    drawCharts(state);
    bindResize();
  }

  function drawCharts(state) {
    const H = state.history || {};
    const L = state.longHist || {};
    const sentShort = toRate(H.bytesSent || []);
    const recvShort = toRate(H.bytesRecv || []);
    const sentLong  = toRate(L.bytesSent || []);
    const recvLong  = toRate(L.bytesRecv || []);

    renderAreaChart($("stchart-short"), {
      sent: sentShort, recv: recvShort,
      perSec: true,           /* 1-tick = ~1 s, so values ARE bytes/sec */
      height: 200,
      xLabels: xLabelsShort(sentShort.length + 1),
    });
    renderAreaChart($("stchart-long"), {
      sent: sentLong.map((v) => v / 10),
      recv: recvLong.map((v) => v / 10),
      perSec: true,           /* 10-s buckets divided to per-second rate */
      height: 240,
      xLabels: xLabelsLong(sentLong.length + 1),
    });
  }

  function bindResize() {
    if (resizeObs) return;
    if (!("ResizeObserver" in window)) return;
    resizeObs = new ResizeObserver(() => {
      if (rafId) return;
      rafId = requestAnimationFrame(() => {
        rafId = 0;
        if (window.Nishro && window.Nishro.state) drawCharts(window.Nishro.state);
      });
    });
    const a = $("stchart-short");
    const b = $("stchart-long");
    if (a) resizeObs.observe(a);
    if (b) resizeObs.observe(b);
  }

  /* -- delegation: cacheClear is re-rendered each tick -- */
  document.addEventListener("click", (e) => {
    if (e.target && e.target.id === "cacheClear") clearCache();
  });

  subscribe(render);
  window.Nishro.tabs.onShow("stats", () => render(window.Nishro.state));
})();
