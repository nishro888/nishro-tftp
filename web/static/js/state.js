/* =====================================================================
   state.js - central pub/sub + websocket wiring.
   ---------------------------------------------------------------------
   Every tab module registers a render callback; state.js calls them on
   each websocket frame. Also exposes the raw log ring to the logs tab
   so we don't double-buffer.

   History rings:
     * "recent" - every tick (~1 s); window length is admin-configurable
       via web.stats_recent_window_sec (default 60 s). Used for the
       compact sparklines in the stats view.
     * "long"   - one sample per LONG_BUCKET_SEC (10 s); total span is
       admin-configurable via web.stats_long_window_hours (default 12).
       Used for the full long-view throughput graphs.

   Optimization: the server only sends session_history and users when
   they change. Ticks without those keys leave the prior values intact.
   ===================================================================== */
(function () {
  "use strict";
  const { $ } = window.Nishro;

  const LOG_RING_SIZE   = 2000;
  const LONG_BUCKET_SEC = 10;     // downsample interval for long ring
  const windows = {
    recentSec:  60,               // ~1 min at 1 s/tick
    longHours:  12,
    longPoints: 12 * 3600 / 10,   // = 4320 samples for 12 h at 10 s bucket
  };

  function setStatsWindows(recentSec, longHours) {
    const rec  = Math.max(10, Math.min(600, Number(recentSec) | 0 || 60));
    const hrs  = Math.max(1,  Math.min(48,  Number(longHours) | 0 || 12));
    windows.recentSec  = rec;
    windows.longHours  = hrs;
    windows.longPoints = Math.ceil(hrs * 3600 / LONG_BUCKET_SEC);
    // Truncate existing rings so we drop stale data when admin shrinks window.
    for (const k in state.history)  if (state.history[k].length  > rec)               state.history[k]  = state.history[k].slice(-rec);
    for (const k in state.longHist) if (state.longHist[k].length > windows.longPoints) state.longHist[k] = state.longHist[k].slice(-windows.longPoints);
  }

  function makeRing() {
    return { bytesSent: [], bytesRecv: [], sessionCount: [], cpu: [], sysCpu: [], rss: [] };
  }

  const state = {
    stats:    {},
    sessions: [],
    sessionHistory: [],       // last 100 completed/failed sessions
    users:    {},              // employee ID -> name mapping
    logs:     [],
    history:  makeRing(),     // recent (~1 min, every tick)
    longHist: makeRing(),     // 12-hour downsampled
    uiSettings: null,         // central UI settings pushed by server
  };

  let lastLongTs = 0;         // epoch ms of last long-history sample

  const subscribers = [];

  function subscribe(cb) { subscribers.push(cb); }

  /* Coalesce bursts of WS frames into one render per animation frame.
     Server ticks at ~1 s today but can be tuned lower; this keeps the
     UI at monitor refresh even if ticks arrive faster, and avoids doing
     any paint work while the tab is hidden. */
  let renderScheduled = false;
  function runRender() {
    renderScheduled = false;
    for (let i = 0; i < subscribers.length; i++) {
      try { subscribers[i](state); } catch (e) { console.error("tab render failed:", e); }
    }
  }
  function publish() {
    if (renderScheduled) return;
    renderScheduled = true;
    if (typeof requestAnimationFrame === "function" && !document.hidden) {
      requestAnimationFrame(runRender);
    } else {
      setTimeout(runRender, 16);
    }
  }

  function pushRing(ring, maxLen, s) {
    const push = (arr, v) => {
      arr.push(Number(v || 0));
      if (arr.length > maxLen) arr.shift();
    };
    push(ring.bytesSent,    s.bytes_sent);
    push(ring.bytesRecv,    s.bytes_received);
    push(ring.sessionCount, s.session_count);
    push(ring.cpu,          s.process_cpu);
    push(ring.sysCpu,       s.system_cpu);
    push(ring.rss,          s.process_rss);
  }

  function recordHistory(s) {
    pushRing(state.history, windows.recentSec, s);
    const now = Date.now();
    if (now - lastLongTs >= LONG_BUCKET_SEC * 1000) {
      pushRing(state.longHist, windows.longPoints, s);
      lastLongTs = now;
    }
  }

  let ws = null;
  function connectWS() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws");
    ws.onopen = () => {
      $("wsChip").textContent = "connected";
      $("wsChip").className = "chip ok";
    };
    ws.onclose = () => {
      $("wsChip").textContent = "disconnected";
      $("wsChip").className = "chip err";
      setTimeout(connectWS, 2000);
    };
    ws.onerror = () => {};
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); }
      catch (e) { console.error("bad ws payload:", e); return; }

      if (msg.type === "init") {
        state.logs     = msg.logs || [];
        state.stats    = msg.stats || {};
        state.sessions = msg.sessions || [];
        state.sessionHistory = msg.session_history || [];
        state.users    = msg.users || {};
        if (msg.ui_settings) {
          state.uiSettings = msg.ui_settings;
          if (window.Nishro.tabs && window.Nishro.tabs.applyUiSettings) {
            window.Nishro.tabs.applyUiSettings(msg.ui_settings);
          }
        }
      } else if (msg.type === "tick") {
        state.stats    = msg.stats || {};
        // Sessions: server omits the key when the list is unchanged
        // (same as history/users); preserve last value in that case.
        if (msg.sessions !== undefined) state.sessions = msg.sessions;
        // Only update history / users / ui_settings if the server included them
        if (msg.session_history) state.sessionHistory = msg.session_history;
        if (msg.users)           state.users = msg.users;
        if (msg.ui_settings) {
          state.uiSettings = msg.ui_settings;
          if (window.Nishro.tabs && window.Nishro.tabs.applyUiSettings) {
            window.Nishro.tabs.applyUiSettings(msg.ui_settings);
          }
        }
        if (msg.logs && msg.logs.length) {
          const batch = msg.logs;
          for (let i = 0; i < batch.length; i++) state.logs.push(batch[i]);
          const over = state.logs.length - LOG_RING_SIZE;
          if (over > 0) state.logs.splice(0, over);
        }
      }
      recordHistory(state.stats);
      publish();
    };
  }

  window.Nishro.state            = state;
  window.Nishro.subscribe        = subscribe;
  window.Nishro.connectWS        = connectWS;
  window.Nishro.statsWindows     = windows;          // live-read by stats.js
  window.Nishro.setStatsWindows  = setStatsWindows;  // called by tabs.js on ui_settings
  window.Nishro.LONG_BUCKET_SEC  = LONG_BUCKET_SEC;
})();
