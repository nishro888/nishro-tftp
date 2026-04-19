/* =====================================================================
   files.js - three-panel file browser with CRUD, cross-panel copy,
              and open-in-file-explorer
   ---------------------------------------------------------------------
   Layout (split in two halves):
     Left  half: top    = RRQ root (local folder)
                  bottom = WRQ folder (uploads)
     Right half: FTP directory browser

   Features on every panel:
     * Breadcrumb navigation, single-click select, double-click enter
     * Refresh button (manual) + global auto-refresh on a 10 s tick
     * New folder / Rename / Delete (admin gating lives only on the
       config value; the actions themselves are always available)
     * Open current directory in the host's File Explorer
   Features on uploads / FTP panels:
     * Copy selected item into the currently-open RRQ root directory,
       size-capped per source (files.max_copy_size_local for uploads,
       files.max_copy_size_ftp for the FTP panel - both admin-editable)

   Auto-refresh never races the TFTP loop: it pauses during active
   sessions, when the tab isn't visible, or when the user opts out.
   ===================================================================== */
(function () {
  "use strict";
  const { $, fmtBytes, fmtDateTime, escapeHtml } = window.Nishro;

  const AUTO_REFRESH_MS = 10000;

  // Each panel carries its own navigation + selection state.
  const panels = {
    rrq: { path: "", sel: null, tree: "local", api: "local", containerId: "rrqExplorer", toolbarId: "rrqToolbar", canCopy: false, label: "RRQ root" },
    wrq: { path: "", sel: null, tree: "wrq",   api: "wrq",   containerId: "wrqExplorer", toolbarId: "wrqToolbar", canCopy: "wrq",   label: "Uploads" },
    ftp: { path: "", sel: null, tree: "ftp",   api: "ftp",   containerId: "ftpExplorer", toolbarId: "ftpToolbar", canCopy: "ftp",   label: "FTP" },
  };

  let autoTimer = null;
  // Auto-refresh is OFF by default. With 50 concurrent browsers, we'd
  // rather each user opt in explicitly than fire background refreshes
  // for everyone and steal cycles from the TFTP path.
  let autoPaused = true;

  // -- generic explorer renderer ------------------------------------
  //
  // Render flow is unified so the toolbar state is guaranteed to be
  // refreshed on every render (including error + empty branches). Stale
  // selections (item was deleted externally, or we navigated into a
  // folder where the same name doesn't exist) are cleared here as well.
  function renderExplorer(panel, entries) {
    const container = $(panel.containerId);
    if (!container) return;

    const isError = typeof entries === "object" && !Array.isArray(entries) && entries.error;
    const items = isError ? [] : (Array.isArray(entries) ? entries : (entries.entries || []));

    // Drop stale selection: if the selected name isn't in the current
    // listing, clear it so Rename/Delete/Copy don't stay armed against
    // something that no longer exists.
    if (panel.sel && !items.some((f) => f.name === panel.sel.name)) {
      panel.sel = null;
    }

    // Breadcrumbs
    const parts = (panel.path || "").split("/").filter(Boolean);
    let crumbs = '<span class="crumb" data-path="">root</span>';
    let accumulated = "";
    for (const p of parts) {
      accumulated += (accumulated ? "/" : "") + p;
      crumbs += ' / <span class="crumb" data-path="' + escapeHtml(accumulated) + '">' + escapeHtml(p) + '</span>';
    }

    let html = '<div class="explorer-crumbs">' + crumbs + '</div>';

    if (isError) {
      html += '<div class="file-empty">' + escapeHtml(String(entries.error)) + '</div>';
    } else if (!items.length) {
      html += '<div class="file-empty">empty</div>';
    } else {
      html += '<div class="explorer-list">';
      for (const f of items) {
        const name = escapeHtml(f.name || "");
        const full = panel.path ? panel.path + "/" + f.name : f.name;
        const selected = panel.sel && panel.sel.name === f.name ? " selected" : "";
        if (f.is_dir) {
          html += '<div class="explorer-row dir' + selected + '"'
                + ' data-name="' + escapeHtml(f.name) + '"'
                + ' data-path="' + escapeHtml(full) + '"'
                + ' data-is-dir="1">'
                + '<span class="icon">\u{1F4C1}</span>'
                + '<span class="name">' + name + '</span>'
                + '</div>';
        } else {
          const sizeStr = f.size != null ? fmtBytes(f.size) : "";
          const mtimeStr = f.mtime ? fmtDateTime(f.mtime) : "";
          const showDl = panel.api === "local";
          const dlUrl = showDl ? "/api/files/download?name=" + encodeURIComponent(full) : null;
          html += '<div class="explorer-row file' + selected + '"'
                + ' data-name="' + escapeHtml(f.name) + '"'
                + ' data-path="' + escapeHtml(full) + '"'
                + ' data-is-dir="0">'
                + '<span class="icon">\u{1F4C4}</span>'
                + '<span class="name">' + name + '</span>'
                + '<span class="size">' + sizeStr + '</span>'
                + '<span class="mtime">' + mtimeStr + '</span>'
                + (dlUrl ? '<a href="' + dlUrl + '" download="' + name + '">dl</a>' : '')
                + '</div>';
        }
      }
      html += '</div>';
    }

    container.innerHTML = html;

    // Row click = select; dblclick on dir = enter. Click on empty
    // area of the container clears selection so Rename/Delete/Copy
    // can't be fired against a stale target.
    container.querySelectorAll(".explorer-row").forEach((el) => {
      el.addEventListener("click", (ev) => {
        if (ev.target.tagName === "A") return;
        ev.stopPropagation();
        panel.sel = { name: el.dataset.name, is_dir: el.dataset.isDir === "1" };
        container.querySelectorAll(".explorer-row.selected").forEach((s) => s.classList.remove("selected"));
        el.classList.add("selected");
        refreshToolbarState(panel);
      });
      if (el.dataset.isDir === "1") {
        el.addEventListener("dblclick", () => navigate(panel, el.dataset.path));
      }
    });
    // The container DOM node is stable across re-renders, so we bind
    // the "click-empty-to-deselect" handler exactly once. Re-renders
    // just replace innerHTML; the listener on the container itself
    // persists. Guard with a flag on the node.
    if (!container._deselectBound) {
      container._deselectBound = true;
      container.addEventListener("click", (ev) => {
        if (ev.target.closest(".explorer-row")) return;
        if (ev.target.classList && ev.target.classList.contains("crumb")) return;
        if (panel.sel) {
          panel.sel = null;
          container.querySelectorAll(".explorer-row.selected").forEach((s) => s.classList.remove("selected"));
          refreshToolbarState(panel);
        }
      });
    }

    _bindCrumbs(container, panel);
    refreshToolbarState(panel);
  }

  function _bindCrumbs(container, panel) {
    container.querySelectorAll(".crumb").forEach((el) => {
      el.addEventListener("click", () => navigate(panel, el.dataset.path));
    });
  }

  // -- navigation + loaders -----------------------------------------
  function navigate(panel, newPath) {
    panel.path = newPath || "";
    panel.sel = null;
    load(panel);
  }

  async function load(panel) {
    // Show a transient "loading" marker only when the container is
    // empty-looking, so FTP round-trips don't leave the user staring
    // at a stale listing with no feedback.
    const container = $(panel.containerId);
    if (container && !container.querySelector(".explorer-row")) {
      container.innerHTML = '<div class="file-empty">loading...</div>';
    }
    const url = "/api/browse/" + panel.api + "?path=" + encodeURIComponent(panel.path);
    try {
      const r = await fetch(url);
      if (!r.ok) {
        const detail = await r.json().catch(() => ({}));
        throw new Error(detail.detail || r.statusText);
      }
      const data = await r.json();
      renderExplorer(panel, data);
    } catch (e) {
      renderExplorer(panel, { error: e.message });
    }
  }

  function refreshAll() {
    load(panels.rrq);
    load(panels.wrq);
    load(panels.ftp);
  }

  // -- auto-refresh --------------------------------------------------
  function shouldSkipAuto() {
    if (autoPaused) return true;
    if (document.hidden) return true;
    // Never step on an active transfer.
    const sess = window.Nishro.state && window.Nishro.state.sessions;
    if (Array.isArray(sess) && sess.length > 0) return true;
    const tab = document.getElementById("tab-files");
    if (!tab || !tab.classList.contains("active")) return true;
    return false;
  }

  function tickAuto() {
    if (shouldSkipAuto()) return;
    refreshAll();
  }

  function startAuto() {
    if (autoTimer) return;
    autoTimer = setInterval(tickAuto, AUTO_REFRESH_MS);
  }

  function setAutoPaused(v) {
    autoPaused = v;
    const chip = $("filesAutoChip");
    if (chip) {
      chip.textContent = "Auto-refresh: " + (v ? "OFF" : "ON");
      chip.className = "chip" + (v ? "" : " ok");
    }
    const btn = $("filesAutoToggle");
    if (btn) btn.textContent = v ? "Turn auto-refresh ON" : "Turn auto-refresh OFF";
  }

  // -- API helpers --------------------------------------------------
  async function apiPost(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) {
      const detail = await r.json().catch(() => ({}));
      throw new Error(detail.detail || r.statusText);
    }
    return r.json();
  }

  function flash(text, cls) {
    const el = $("filesStatus");
    if (!el) return;
    el.textContent = text;
    el.className = "chip" + (cls ? " " + cls : "");
    if (cls === "ok" || cls === "err") {
      setTimeout(() => { el.textContent = ""; el.className = "chip"; }, 3000);
    }
  }

  // -- actions ------------------------------------------------------
  async function actMkdir(panel) {
    if (panel.tree === "ftp") return flash("FTP is read-only here", "err");
    const name = (prompt("New folder name (in " + (panel.path || "root") + "):") || "").trim();
    if (!name) return;
    if (/[\/\\]/.test(name) || name === "." || name === "..") {
      return flash("invalid folder name", "err");
    }
    const newPath = panel.path ? panel.path + "/" + name : name;
    try {
      await apiPost("/api/fs/mkdir", { tree: panel.tree, path: newPath });
      flash("folder created", "ok");
      load(panel);
    } catch (e) { flash(e.message, "err"); }
  }

  async function actRename(panel) {
    if (panel.tree === "ftp") return flash("FTP is read-only here", "err");
    if (!panel.sel) return flash("select an item first", "err");
    const cur = panel.sel.name;
    const next = (prompt("Rename to:", cur) || "").trim();
    if (!next || next === cur) return;
    if (/[\/\\]/.test(next) || next === "." || next === "..") {
      return flash("invalid name", "err");
    }
    const full = panel.path ? panel.path + "/" + cur : cur;
    try {
      await apiPost("/api/fs/rename", { tree: panel.tree, path: full, new_name: next });
      flash("renamed", "ok");
      panel.sel = null;
      load(panel);
    } catch (e) { flash(e.message, "err"); }
  }

  async function actDelete(panel) {
    if (panel.tree === "ftp") return flash("FTP is read-only here", "err");
    if (!panel.sel) return flash("select an item first", "err");
    const cur = panel.sel.name;
    if (!confirm("Delete " + (panel.sel.is_dir ? "folder" : "file") + ' "' + cur + '"?')) return;
    const full = panel.path ? panel.path + "/" + cur : cur;
    try {
      await apiPost("/api/fs/delete", { tree: panel.tree, path: full });
      flash("deleted", "ok");
      panel.sel = null;
      load(panel);
    } catch (e) { flash(e.message, "err"); }
  }

  async function actUpload(panel, files) {
    if (panel.tree !== "local") return;
    if (!files || !files.length) return;
    if (panel._uploadInFlight) return flash("an upload is already in progress", "err");
    panel._uploadInFlight = true;
    if (panel._btnUpload) panel._btnUpload.disabled = true;
    try {
      let ok = 0, fail = 0;
      for (const f of files) {
        const fd = new FormData();
        fd.append("path", panel.path || "");
        fd.append("file", f, f.name);
        flash("uploading " + f.name + "...", "");
        try {
          const r = await fetch("/api/fs/upload", { method: "POST", body: fd });
          if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            throw new Error(d.detail || r.statusText);
          }
          ok++;
        } catch (e) {
          fail++;
          flash("upload " + f.name + " failed: " + e.message, "err");
        }
      }
      if (!fail) flash("uploaded " + ok + " file" + (ok === 1 ? "" : "s"), "ok");
      load(panel);
    } finally {
      panel._uploadInFlight = false;
      if (panel._btnUpload) panel._btnUpload.disabled = false;
    }
  }

  function actBack(panel) {
    if (!panel.path) return;  // already at root
    const slash = panel.path.lastIndexOf("/");
    navigate(panel, slash < 0 ? "" : panel.path.slice(0, slash));
  }

  async function actCopy(panel) {
    const src = panel.sel;
    if (!src) return flash("select a file/folder first", "err");
    if (panel._copyInFlight) return flash("a copy is already in progress", "err");
    const srcFull = panel.path ? panel.path + "/" + src.name : src.name;
    const dstDir  = panels.rrq.path;
    const dstShow = dstDir || "root";
    if (!confirm('Copy "' + src.name + '" from ' + panel.label + ' to RRQ ' + dstShow + "?\n(overwrites if it already exists)")) return;
    panel._copyInFlight = true;
    if (panel._btnCopy) panel._btnCopy.disabled = true;
    try {
      const url = panel.tree === "ftp" ? "/api/fs/copy_ftp" : "/api/fs/copy";
      const body = panel.tree === "ftp"
        ? { src_path: srcFull, is_dir: src.is_dir, dst_dir: dstDir }
        : { src_path: srcFull, dst_dir: dstDir };
      flash("copying...", "");
      const res = await apiPost(url, body);
      flash("copied " + fmtBytes(res.bytes || 0), "ok");
      load(panels.rrq);
    } catch (e) {
      flash(e.message, "err");
    } finally {
      panel._copyInFlight = false;
      // refreshToolbarState will re-enable based on selection state.
      refreshToolbarState(panel);
    }
  }

  // -- toolbar ------------------------------------------------------
  // One-time build. We keep references to individual buttons so we can
  // toggle their disabled state based on selection (instead of tearing
  // down + rebuilding the DOM on every navigation).
  function bindToolbar(panel) {
    const host = $(panel.toolbarId);
    if (!host) return;
    host.innerHTML = "";
    host.classList.add("explorer-toolbar");

    const group = () => {
      const g = document.createElement("div");
      g.className = "btn-group";
      host.appendChild(g);
      return g;
    };

    const txtBtn = (parent, label, title, cls, handler) => {
      const b = document.createElement("button");
      b.className = "txtbtn" + (cls ? " " + cls : "");
      b.title = title || label;
      b.textContent = label;
      b.addEventListener("click", handler);
      parent.appendChild(b);
      return b;
    };

    // Group 1: Navigation
    const g1 = group();
    panel._btnBack    = txtBtn(g1, "Back",    "Go up to the parent directory", null, () => actBack(panel));
    panel._btnRefresh = txtBtn(g1, "Refresh", "Reload the current folder",      null, () => load(panel));

    // Group 2: Mutations (local/wrq only)
    if (panel.tree !== "ftp") {
      const g2 = group();
      panel._btnNew    = txtBtn(g2, "New Folder", "Create a new folder here",                  null,     () => actMkdir(panel));
      panel._btnRename = txtBtn(g2, "Rename",     "Rename the selected file/folder",           null,     () => actRename(panel));
      panel._btnDelete = txtBtn(g2, "Delete",     "Delete the selected file/folder",           "danger", () => actDelete(panel));
    }

    // Group 3: Upload (RRQ root only)
    if (panel.tree === "local") {
      const g3 = group();
      const fileInput = document.createElement("input");
      fileInput.type = "file";
      fileInput.multiple = true;
      fileInput.style.display = "none";
      fileInput.addEventListener("change", () => {
        const files = Array.from(fileInput.files || []);
        fileInput.value = "";
        if (files.length) actUpload(panel, files);
      });
      g3.appendChild(fileInput);
      panel._btnUpload = txtBtn(
        g3,
        "Upload",
        "Upload one or more files into the current RRQ folder",
        "primary",
        () => fileInput.click(),
      );
    }

    // Group 4: Cross-panel copy (uploads/ftp only)
    if (panel.canCopy) {
      const g4 = group();
      panel._btnCopy = txtBtn(
        g4,
        "Copy to RRQ Root",
        "Copy the selected file/folder into the RRQ root panel's current directory",
        "primary",
        () => actCopy(panel),
      );
    }

    refreshToolbarState(panel);
  }

  function refreshToolbarState(panel) {
    const hasSel = !!panel.sel;
    const atRoot = !panel.path;
    const busy   = !!panel._copyInFlight;
    if (panel._btnBack)   panel._btnBack.disabled   = atRoot;
    if (panel._btnRename) panel._btnRename.disabled = !hasSel || busy;
    if (panel._btnDelete) panel._btnDelete.disabled = !hasSel || busy;
    if (panel._btnCopy)   panel._btnCopy.disabled   = !hasSel || busy;
  }

  // -- init ---------------------------------------------------------
  function init() {
    const topBar = $("filesTopBar");
    if (topBar) {
      topBar.addEventListener("click", (ev) => {
        const tgt = ev.target.closest("button");
        if (!tgt) return;
        if (tgt.id === "filesRefreshAll") refreshAll();
        else if (tgt.id === "filesAutoToggle") setAutoPaused(!autoPaused);
      });
    }

    bindToolbar(panels.rrq);
    bindToolbar(panels.wrq);
    bindToolbar(panels.ftp);

    // Starts paused; initializes chip + button labels to match.
    setAutoPaused(true);
    startAuto();

    window.Nishro.tabs.onShow("files", refreshAll);
  }

  window.Nishro.filesInit = init;
})();
