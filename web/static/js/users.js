/* =====================================================================
   users.js - admin tab for maintaining employee ID/Name mapping
   ---------------------------------------------------------------------
   Provides a simple table editor for the {ID, Name} pairs used to
   resolve the "User" column in the sessions view.

   Data is loaded from /api/users on tab show and kept in sync via the
   WS-pushed users map. Adds/deletes are optimistic: the table updates
   immediately, and the save is sent to the server in the background.
   ===================================================================== */
(function () {
  "use strict";
  const { $, escapeHtml } = window.Nishro;

  let localUsers = {};  // working copy

  // -- API ------------------------------------------------------------
  function load() {
    fetch("/api/users")
      .then((r) => {
        if (!r.ok) throw new Error(r.status + " " + r.statusText);
        return r.json();
      })
      .then((data) => {
        localUsers = data && typeof data === "object" ? data : {};
        renderTable();
      })
      .catch((e) => {
        console.error("users load failed:", e);
        flash("load failed", "err");
      });
  }

  function save() {
    flash("saving...", "");
    fetch("/api/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(localUsers),
    })
      .then((r) => {
        if (!r.ok) throw new Error(r.status + " " + r.statusText);
        return r.json();
      })
      .then((j) => {
        flash(j.ok ? "saved" : (j.error || "error"), j.ok ? "ok" : "err");
      })
      .catch((e) => {
        flash("save failed: " + e.message, "err");
      });
  }

  function flash(text, cls) {
    const el = $("usersStatus");
    if (!el) return;
    el.textContent = text;
    el.className = "chip" + (cls ? " " + cls : "");
    if (cls === "ok" || cls === "err") {
      setTimeout(() => { el.textContent = ""; el.className = "chip"; }, 3000);
    }
  }

  // -- render ---------------------------------------------------------
  function renderTable() {
    const host = $("usersTable");
    if (!host) return;

    const entries = Object.entries(localUsers).sort(
      (a, b) => parseInt(a[0], 10) - parseInt(b[0], 10)
    );

    if (!entries.length) {
      host.innerHTML =
        '<table class="sess-table"><tbody>' +
        '<tr><td colspan="4" style="color:var(--muted);padding:20px;text-align:center;">no users defined - add one above</td></tr>' +
        '</tbody></table>';
      return;
    }

    const rows = entries.map(([id, name]) =>
      '<tr>' +
        '<td class="mono">BDCOM' + String(id).padStart(4, "0") + '</td>' +
        '<td class="mono" style="color:var(--muted);">' + escapeHtml(id) + '</td>' +
        '<td>' + escapeHtml(name) + '</td>' +
        '<td><button class="btn sec btn-sm" data-del="' + escapeHtml(id) + '">Del</button></td>' +
      '</tr>'
    ).join("");

    host.innerHTML =
      '<table class="sess-table"><thead><tr>' +
        '<th style="width:140px;">Device</th>' +
        '<th style="width:60px;">ID</th>' +
        '<th>Name</th>' +
        '<th style="width:60px;"></th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table>' +
      '<div style="color:var(--subtle);font-size:11px;padding:8px 0;">' +
        entries.length + ' user(s) registered' +
      '</div>';
  }

  // -- actions --------------------------------------------------------
  function addUser() {
    const idInput = $("userNewId");
    const nameInput = $("userNewName");
    const rawId = (idInput.value || "").trim();
    const name = (nameInput.value || "").trim();

    if (!rawId || !name) {
      flash("both ID and name are required", "err");
      return;
    }

    // Accept plain digits ("45") or full BDCOM format ("BDCOM0045")
    let eid = rawId;
    const bdMatch = rawId.match(/^BDCOM0*(\d+)$/i);
    if (bdMatch) eid = bdMatch[1];

    // Validate: must be numeric
    if (!/^\d{1,4}$/.test(eid)) {
      flash("ID must be 1-4 digits (e.g. 45 or BDCOM0045)", "err");
      return;
    }

    // Normalize: strip leading zeros
    eid = String(parseInt(eid, 10));
    localUsers[eid] = name;
    renderTable();
    idInput.value = "";
    nameInput.value = "";
    idInput.focus();
    save();
  }

  function deleteUser(id) {
    delete localUsers[id];
    renderTable();
    save();
  }

  // -- init -----------------------------------------------------------
  function init() {
    // Event delegation for delete buttons (table re-renders on each change)
    $("usersTable").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-del]");
      if (btn) deleteUser(btn.dataset.del);
    });

    $("userAdd").addEventListener("click", addUser);
    $("userNewId").addEventListener("keydown", (e) => {
      if (e.key === "Enter") $("userNewName").focus();
    });
    $("userNewName").addEventListener("keydown", (e) => {
      if (e.key === "Enter") addUser();
    });
    $("usersReload").addEventListener("click", load);

    // Load fresh data whenever the tab becomes visible
    window.Nishro.tabs.onShow("users", load);
  }

  window.Nishro.usersInit = init;
})();
