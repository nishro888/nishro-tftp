/* =====================================================================
   sessions.js - detailed session table (active + last 100 history)
   ---------------------------------------------------------------------
   Shows active sessions at the top (highlighted), followed by the most
   recent completed/failed sessions. Newest first. A timestamp column
   sits at the leftmost position.
   ===================================================================== */
(function () {
  "use strict";
  const { $, fmtBytes, fmtDateTime, escapeHtml, resolveUser, subscribe } = window.Nishro;

  function fmtDuration(ms) {
    if (ms == null || isNaN(ms)) return '-';
    const s = ms / 1000;
    if (s < 1)    return (ms | 0) + ' ms';
    if (s < 60)   return s.toFixed(1) + ' s';
    if (s < 3600) return Math.floor(s / 60) + 'm ' + Math.floor(s % 60) + 's';
    return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  }

  function sessionDuration(se, isActive) {
    if (typeof se.duration_ms === 'number') return se.duration_ms;
    if (!se.started_at) return null;
    const end = isActive ? (Date.now() / 1000) : (se.ended_at || se.started_at);
    return Math.max(0, (end - se.started_at) * 1000);
  }

  function tsCell(epoch) {
    if (!epoch) return '<td data-label="Time" class="mono muted">-</td>';
    const d = new Date(epoch * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    const time = pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
    const date = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
    return '<td data-label="Time" class="mono" style="white-space:nowrap;">' + date + " " + time + '</td>';
  }

  function sessionRow(se, isActive, users) {
    const cls = isActive ? ' class="active-row"' : '';
    // Finished sessions carry the actual reason in ``se.state`` -- "done"
    // on success, the protocol-level message ("file not found", "client
    // timeout", "disk write failed", ...) otherwise. Chip colour picks
    // green for "done", red for genuine server faults, amber for anything
    // else (client dropouts / policy rejections).
    let stateChip;
    if (isActive) {
      stateChip = '<span class="chip info">' + escapeHtml(se.state || 'active') + '</span>';
    } else if (se.state === 'done') {
      stateChip = '<span class="chip ok">done</span>';
    } else {
      const cls2 = se.server_fault ? 'err' : 'warn';
      stateChip = '<span class="chip ' + cls2 + '" title="' + escapeHtml(se.state || '') + '">'
                + escapeHtml(se.state || 'ended') + '</span>';
    }
    const user = resolveUser(se.filename, users);
    return '<tr' + cls + '>'
        + tsCell(se.started_at)
        + '<td data-label="User">' + (user ? escapeHtml(user) : '<span class="muted">-</span>') + '</td>'
        + '<td data-label="Kind"><span class="chip ' + (se.kind === 'read' ? 'ok' : 'warn') + '">' + se.kind + '</span></td>'
        + '<td data-label="File" class="scell-file">' + escapeHtml(se.filename) + '</td>'
        + '<td data-label="MAC" class="mono">' + (se.client_mac || '') + '</td>'
        + '<td data-label="Endpoint">' + se.client_ip + ':' + se.client_port + '</td>'
        + '<td data-label="VLAN">' + (se.vlan_id != null ? se.vlan_id : '-') + '</td>'
        + '<td data-label="blksize">' + se.blksize + '</td>'
        + '<td data-label="wsize">' + se.windowsize + '</td>'
        + '<td data-label="Progress" class="scell-progress">' + fmtBytes(se.bytes_transferred) + '/' + fmtBytes(se.total_bytes)
          + '<div class="progress"><div style="width:' + ((se.progress || 0) * 100) + '%"></div></div></td>'
        + '<td data-label="Speed">' + (isActive
              ? (fmtBytes(se.speed) + '/s')
              : (fmtBytes(se.max_speed || se.speed) + '/s <span class="muted" style="font-size:0.85em">peak</span>'))
          + '</td>'
        + '<td data-label="Duration" class="mono">' + fmtDuration(sessionDuration(se, isActive)) + '</td>'
        + '<td data-label="State" class="scell-state">' + stateChip + '</td>'
        + '</tr>';
  }

  function render(state) {
    if (!document.querySelector("#tab-sessions.active")) return;

    const active = (state.sessions || [])
      .slice()
      .sort((a, b) => (b.started_at || 0) - (a.started_at || 0));

    const history = state.sessionHistory || [];
    const users = state.users || {};

    const rows = active.map((s) => sessionRow(s, true, users)).join("")
               + history.map((s) => sessionRow(s, false, users)).join("");

    $("sessTable").querySelector("tbody").innerHTML =
      rows || '<tr><td colspan="13" style="color:var(--muted)">no sessions yet</td></tr>';
  }

  subscribe(render);

  // Render immediately when the tab becomes visible (don't wait for next WS tick)
  window.Nishro.tabs.onShow("sessions", () => render(window.Nishro.state));
})();
