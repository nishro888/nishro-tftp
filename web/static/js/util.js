/* =====================================================================
   util.js - shared helpers used by every tab module.
   ===================================================================== */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function fmtBytes(n) {
    if (n === null || n === undefined) return "-";
    n = Number(n);
    if (!isFinite(n)) return "-";
    if (n < 1024)       return n.toFixed(0) + " B";
    if (n < 1048576)    return (n / 1024).toFixed(1) + " KiB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MiB";
    return (n / 1073741824).toFixed(2) + " GiB";
  }

  function fmtPct(n) { return (Number(n || 0) * 100).toFixed(1) + "%"; }

  function fmtTime(t) {
    const d = new Date(Number(t) * 1000);
    if (isNaN(d.getTime())) return "-";
    const pad = (x) => String(x).padStart(2, "0");
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function fmtDateTime(t) {
    if (!t) return "-";
    const d = new Date(Number(t) * 1000);
    if (isNaN(d.getTime())) return "-";
    return d.toLocaleString();
  }

  function fmtDuration(sec) {
    sec = Math.max(0, Number(sec || 0));
    if (sec < 60)    return sec.toFixed(0) + "s";
    if (sec < 3600)  return Math.floor(sec / 60) + "m " + Math.floor(sec % 60) + "s";
    if (sec < 86400) return Math.floor(sec / 3600) + "h " + Math.floor((sec % 3600) / 60) + "m";
    return Math.floor(sec / 86400) + "d " + Math.floor((sec % 86400) / 3600) + "h";
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function debounce(fn, ms) {
    let t = null;
    return function (...args) {
      clearTimeout(t);
      t = setTimeout(() => fn.apply(this, args), ms);
    };
  }

  /** Tiny path-based deep-get / deep-set on plain objects. */
  function getPath(obj, path) {
    let node = obj;
    for (const key of path) {
      if (node == null || typeof node !== "object") return undefined;
      node = node[key];
    }
    return node;
  }
  function setPath(obj, path, value) {
    let node = obj;
    for (let i = 0; i < path.length - 1; i++) {
      if (node[path[i]] == null || typeof node[path[i]] !== "object") {
        node[path[i]] = {};
      }
      node = node[path[i]];
    }
    node[path[path.length - 1]] = value;
  }

  /**
   * Extract employee name from an ``f::NNN/...`` filename using the
   * users map. Returns the name string, or ``null`` if not an FTP
   * transfer or no mapping exists.
   */
  function resolveUser(filename, usersMap) {
    if (!filename || !usersMap) return null;
    const m = filename.match(/^f::(\d{2,3})\//);
    if (!m) return null;
    const eid = String(parseInt(m[1], 10));
    return usersMap[eid] || null;
  }

  // Publish under a single namespace so every module can share.
  window.Nishro = window.Nishro || {};
  Object.assign(window.Nishro, {
    $,
    fmtBytes,
    fmtPct,
    fmtTime,
    fmtDateTime,
    fmtDuration,
    escapeHtml,
    debounce,
    getPath,
    setPath,
    resolveUser,
  });
})();
