/* =====================================================================
   logs.js - live log viewer
   ---------------------------------------------------------------------
   Features:
     * level + text filter
     * pause (stop re-rendering but still buffer)
     * auto-scroll lock (paused automatically when user scrolls up)
     * download filtered logs as plain text
     * clear
   ===================================================================== */
(function () {
  "use strict";
  const { $, escapeHtml, fmtTime, subscribe, debounce } = window.Nishro;

  let paused    = false;
  let autoScroll = true;

  function applyFilters(logs) {
    const level  = $("logLevel").value;
    const filter = $("logFilter").value.trim().toLowerCase();
    return logs.filter((l) => {
      if (level !== "ALL" && l.level !== level) return false;
      if (!filter) return true;
      return (l.message || "").toLowerCase().includes(filter)
          || (l.logger  || "").toLowerCase().includes(filter);
    });
  }

  function render(state) {
    const box = $("logBox");
    if (!document.querySelector("#tab-logs.active")) return;
    if (paused) return;

    const items = applyFilters(state.logs || []).slice(-1000);
    box.innerHTML = items.map((l) => {
      const lvl = (l.level || "INFO");
      return `<div class="line ${lvl}">` +
             `<span class="time">${fmtTime(l.time)}</span> ` +
             `<span class="level">${lvl}</span> ` +
             `<span class="logger">${escapeHtml(l.logger || '')}</span>  ` +
             `${escapeHtml(l.message || '')}</div>`;
    }).join("");

    if (autoScroll) box.scrollTop = box.scrollHeight;
  }

  function handleScroll() {
    const box = $("logBox");
    // If user scrolled up more than 30px from the bottom, stop auto-scrolling.
    const atBottom = (box.scrollHeight - box.clientHeight - box.scrollTop) < 30;
    if (autoScroll !== atBottom) {
      autoScroll = atBottom;
      $("logAutoScroll").textContent = "auto-scroll: " + (autoScroll ? "on" : "off");
      $("logAutoScroll").className = "chip " + (autoScroll ? "ok" : "warn");
    }
  }

  function togglePause() {
    paused = !paused;
    $("logPause").textContent = paused ? "resume" : "pause";
    $("logPause").classList.toggle("sec", !paused);
    if (!paused) render(window.Nishro.state);
  }

  function clearLogs() {
    window.Nishro.state.logs = [];
    render(window.Nishro.state);
  }

  function download() {
    const items = applyFilters(window.Nishro.state.logs || []);
    const text = items.map((l) =>
      `${fmtTime(l.time)} ${l.level} ${l.logger}  ${l.message}`
    ).join("\n");
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url  = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "nishro_logs.txt";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function init() {
    const reRender = () => render(window.Nishro.state);
    $("logLevel").addEventListener("change", reRender);
    $("logFilter").addEventListener("input", debounce(reRender, 100));
    $("logPause").addEventListener("click", togglePause);
    $("logClear").addEventListener("click", clearLogs);
    $("logDownload").addEventListener("click", download);
    $("logBox").addEventListener("scroll", handleScroll);
    window.Nishro.tabs.onShow("logs", () => {
      autoScroll = true;
      reRender();
      $("logBox").scrollTop = $("logBox").scrollHeight;
    });
  }

  subscribe(render);
  window.Nishro.logsInit = init;
})();
