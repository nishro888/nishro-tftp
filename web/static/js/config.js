/* =====================================================================
   config.js - schema-driven config editor
   ---------------------------------------------------------------------
   CONFIG_SCHEMA below is the authoritative description of every field
   the UI exposes. Unknown keys on the server round-trip unchanged
   because saveConfigForm() deep-clones the loaded config and only
   overwrites paths the schema knows about.
   ===================================================================== */
(function () {
  "use strict";
  const { $, getPath, setPath } = window.Nishro;

  // ---- policy options for per-option negotiation ------------------
  const policyOptions = () => [
    ["client", "client - use client's requested value"],
    ["server", "server - always use default"],
    ["min",    "min - always use configured minimum"],
    ["max",    "max - always use configured maximum"],
  ];

  // ---- schema -----------------------------------------------------
  const CONFIG_SCHEMA = [
    { title: "Network adapter", fields: [
      { path: ["network","nic"],         label: "Ethernet adapter", type: "nic", wide: true,
        desc: "Must have Npcap installed; runs in promiscuous mode." },
      { path: ["network","virtual_ip"],  label: "Virtual IP",  type: "text",
        desc: "Must NOT be assigned to the NIC in Windows." },
      { path: ["network","virtual_mac"], label: "Virtual MAC", type: "text", nullable: true,
        desc: "Leave blank to use the NIC's real MAC." },
      { path: ["network","promiscuous"], label: "Promiscuous mode", type: "checkbox" },
    ]},
    { title: "Sessions", fields: [
      { path: ["sessions","max_concurrent"], label: "Max concurrent sessions", type: "number", min: 1, max: 10000 },
      { path: ["sessions","overflow_policy"], label: "Overflow policy", type: "select",
        options: [["reject","reject (send TFTP error)"],["queue","queue (wait for a slot)"]] },
      { path: ["sessions","queue_timeout"], label: "Queue timeout (seconds)", type: "number", min: 0, step: 0.5 },
    ]},
    { title: "TFTP protocol", fields: [
      { path: ["tftp","engine"], label: "TFTP engine", type: "select",
        options: [["python","Python (default)"],["c","C native (fastest)"]],
        desc: "Switch between the pure-Python engine and the native C engine (nishro_core.exe). Changing this restarts the server." },
      { path: ["tftp","listen_port"],   label: "Listen UDP port", type: "number", min: 1, max: 65535 },
      { path: ["tftp","enable_writes"], label: "Enable writes (WRQ)", type: "checkbox",
        desc: "When off, every WRQ is rejected with 'writes disabled'." },
      { path: ["tftp","allow_block_rollover"], label: "Allow 16-bit block counter rollover", type: "checkbox",
        desc: "When off, transfers that would exceed 65535 blocks are rejected up front." },
      { path: ["tftp","max_retries"], label: "Max retransmit attempts", type: "number", min: 0, max: 100 },
    ]},
    { title: "Option negotiation (per option)", fields: [
      { path: ["tftp","negotiation","blksize"],    label: "blksize policy",    type: "select", options: policyOptions() },
      { path: ["tftp","negotiation","windowsize"], label: "windowsize policy", type: "select", options: policyOptions() },
      { path: ["tftp","negotiation","timeout"],    label: "timeout policy",    type: "select", options: policyOptions() },
      { path: ["tftp","defaults","blksize"],       label: "Default blksize (bytes)",   type: "number", min: 8, max: 65464 },
      { path: ["tftp","defaults","windowsize"],    label: "Default windowsize",        type: "number", min: 1, max: 64 },
      { path: ["tftp","defaults","timeout"],       label: "Default timeout (seconds)", type: "number", min: 1, max: 60 },
      { path: ["tftp","limits","blksize_min"],     label: "blksize min",    type: "number", min: 8, max: 65464 },
      { path: ["tftp","limits","blksize_max"],     label: "blksize max",    type: "number", min: 8, max: 65464 },
      { path: ["tftp","limits","windowsize_min"],  label: "windowsize min", type: "number", min: 1, max: 64 },
      { path: ["tftp","limits","windowsize_max"],  label: "windowsize max", type: "number", min: 1, max: 64 },
      { path: ["tftp","limits","timeout_min"],     label: "timeout min",    type: "number", min: 1, max: 60 },
      { path: ["tftp","limits","timeout_max"],     label: "timeout max",    type: "number", min: 1, max: 60 },
    ]},
    { title: "Storage & directories",
      desc: "RRQ filenames are routed by shape: names starting with 'ftp://' or the configured short-form trigger (default 'f::') go to FTP; everything else is served from the local RRQ root. Incoming WRQ uploads land in the WRQ uploads folder.",
      fields: [
        // --- WRQ (uploads from clients) ---
        { path: ["files","write_root"], label: "WRQ uploads folder (received from clients)", type: "text", wide: true,
          desc: "Where incoming TFTP writes are stored on this machine." },
        { path: ["files","max_wrq_size"], label: "Max WRQ file size (per upload)", type: "bytes",
          desc: "Hard cap on a single incoming upload. 0 = no limit." },
        { path: ["files","allow_wrq_ftp"], label: "Allow WRQ uploads to go to FTP (when client requests 'ftp://' or 'f::' target)", type: "checkbox",
          desc: "Off by default. When on, WRQs whose filename is FTP-shaped are pushed to the target FTP server after reception instead of being rejected." },
        // --- RRQ local ---
        { path: ["files","local","recursive"], label: "Search RRQ root recursively by basename", type: "checkbox",
          desc: "If a request names just a file (no folder), search sub-folders for a matching basename." },
        { path: ["files","local","root"], label: "RRQ root folder (served to clients)", type: "text", wide: true,
          desc: "Local folder served to TFTP clients for ordinary (non-FTP) read requests." },
        { path: ["files","max_rrq_size"], label: "Max RRQ file size (local reads)", type: "bytes",
          desc: "Hard cap on files served from the RRQ root. 0 = no limit." },
        // --- Files tab copy caps (two separate limits) ---
        { path: ["files","max_copy_size_local"], label: "Max copy size: WRQ uploads -> RRQ root", type: "bytes",
          desc: "Caps a single copy from the WRQ uploads panel into the RRQ root. Default 500 MB." },
        { path: ["files","max_copy_size_ftp"], label: "Max copy size: FTP -> RRQ root", type: "bytes",
          desc: "Caps a single copy from the FTP browser panel into the RRQ root. Default 500 MB." },
    ]},
    { title: "FTP server - primary source",
      desc: "Used for RRQs whose filename is 'ftp://<this-host>/...' (credentials matched by host), for the short-form 'f::' trigger's destination, and for the Files tab FTP browser.",
      fields: [
        { path: ["files","ftp","host"],     label: "Server host / IP",     type: "text" },
        { path: ["files","ftp","port"],     label: "Port",                 type: "number", min: 1, max: 65535 },
        { path: ["files","ftp","user"],     label: "Username",             type: "text" },
        { path: ["files","ftp","password"], label: "Password",             type: "password" },
        { path: ["files","ftp","root"],     label: "Base directory on server", type: "text", wide: true,
          desc: "All requested files are resolved relative to this directory. '/' means the server's root." },
        { path: ["files","max_ftp_size"], label: "Max file size served from FTP", type: "bytes",
          desc: "Hard cap on files fetched via 'ftp://' URL or the 'f::' trigger. 0 = no limit." },
    ]},
    { title: "FTP prefix routing (BDCOM-style trigger)",
      desc: 'When a RRQ filename starts with the trigger (e.g. "f::12/file"), the digit block is zero-padded and the request is diverted to a dedicated FTP server - independent of the primary source above.',
      fields: [
        { path: ["files","ftp_prefix","enabled"], label: "Enable prefix routing", type: "checkbox" },
        { path: ["files","ftp_prefix","trigger"], label: "Trigger prefix", type: "text",
          desc: 'Literal prefix on the requested filename. Example: "f::"' },
        { path: ["files","ftp_prefix","folder_prefix"], label: "Folder name prefix", type: "text",
          desc: 'Prepended to the digit block to form the folder name. Example: "BDCOM" + 12 -> "BDCOM0012"' },
        { path: ["files","ftp_prefix","digit_pad"], label: "Digit zero-pad width", type: "number", min: 1, max: 10,
          desc: "Number of digits after zero-padding. 4 -> 0012, 5 -> 00012." },
        { path: ["files","ftp_prefix","host"],     label: "FTP host / IP", type: "text" },
        { path: ["files","ftp_prefix","port"],     label: "FTP port",      type: "number", min: 1, max: 65535 },
        { path: ["files","ftp_prefix","user"],     label: "Username",      type: "text" },
        { path: ["files","ftp_prefix","password"], label: "Password",      type: "password" },
        { path: ["files","ftp_prefix","root"],     label: "Base directory on server", type: "text", wide: true },
    ]},
    { title: "LRU file cache (deprecated - off by default)", fields: [
      { path: ["files","cache","enabled"],        label: "Enabled",               type: "checkbox",
        desc: "Cache is deprecated and disabled by default. Enable only if you have a specific need." },
      { path: ["files","cache","max_bytes"],      label: "Total cache size",      type: "bytes" },
      { path: ["files","cache","max_file_bytes"], label: "Per-file cache cap",    type: "bytes" },
    ]},
    { title: "Logging", fields: [
      { path: ["logging","level"], label: "Level", type: "select",
        options: [["DEBUG","DEBUG"],["INFO","INFO"],["WARNING","WARNING"],["ERROR","ERROR"]] },
      { path: ["logging","file"],                   label: "Log file path", type: "text", wide: true },
      { path: ["logging","rotate","enabled"],       label: "Rotate on size", type: "checkbox" },
      { path: ["logging","rotate","max_bytes"],     label: "Rotate at size", type: "bytes" },
      { path: ["logging","rotate","backup_count"],  label: "Backup count", type: "number", min: 0, max: 99 },
      { path: ["logging","memory_buffer"], label: "In-memory log buffer lines", type: "number", min: 100, max: 100000 },
    ]},
    { title: "Web UI", fields: [
      { path: ["web","host"],           label: "Bind host", type: "text" },
      { path: ["web","port"],           label: "Bind port", type: "number", min: 1, max: 65535 },
      { path: ["web","stats_interval"], label: "Stats push interval (seconds)", type: "number", min: 0.1, step: 0.1 },
      { path: ["web","require_auth"],   label: "Require admin login", type: "checkbox",
        desc: "Off by default -- anyone reaching the web UI is treated as admin. Turn on to force every browser to log in with the admin credential (no per-browser override). Set a password in Admin Config -> Auth first." },
    ]},
  ];

  // ---- state ------------------------------------------------------
  let currentConfig = null;
  let currentNics   = [];

  // ---- field renderers --------------------------------------------
  const UNITS = [["B",1],["KiB",1024],["MiB",1048576],["GiB",1073741824]];

  function renderField(field) {
    const wrap = document.createElement("div");
    wrap.className =
      "form-field" + (field.wide ? " wide" : "") +
      (field.type === "checkbox" ? " checkbox" : "");
    const id = "fld_" + field.path.join("_");
    const current = getPath(currentConfig, field.path);

    // Checkbox - label on the right
    if (field.type === "checkbox") {
      const input = document.createElement("input");
      input.type = "checkbox";
      input.id = id;
      input.checked = !!current;
      wrap.appendChild(input);
      const label = document.createElement("label");
      label.htmlFor = id;
      label.textContent = field.label;
      wrap.appendChild(label);
      if (field.desc) addDesc(wrap, field.desc);
      wrap._getValue = () => input.checked;
      return wrap;
    }

    // All other types get a top label
    const label = document.createElement("label");
    label.htmlFor = id;
    label.textContent = field.label;
    wrap.appendChild(label);

    if (field.type === "nic") {
      const sel = document.createElement("select");
      sel.id = id;
      if (!currentNics.length) {
        const opt = document.createElement("option");
        opt.value = current || "";
        opt.textContent = current ? "(current) " + current : "(no adapters enumerated)";
        sel.appendChild(opt);
      } else {
        for (const nic of currentNics) {
          if (nic.error || nic.warning) continue;
          if (!nic.npf) continue;
          const opt = document.createElement("option");
          opt.value = nic.npf;
          const ip = (nic.ips && nic.ips[0]) ? nic.ips[0] : "no ip";
          const friendly = nic.friendly_name || nic.description || nic.name || "?";
          const extras = [];
          extras.push(nic.isup ? "up" : "down");
          if (nic.speed_mbps) extras.push(nic.speed_mbps + " Mbps");
          if (nic.description && nic.description !== friendly) extras.push(nic.description);
          extras.push(nic.mac || "no mac");
          extras.push(ip);
          opt.textContent = `${friendly}  -  ${extras.join("  |  ")}`;
          if (nic.npf === current) opt.selected = true;
          sel.appendChild(opt);
        }
        if (current && !currentNics.some((n) => n.npf === current)) {
          const opt = document.createElement("option");
          opt.value = current;
          opt.textContent = "(stale) " + current;
          opt.selected = true;
          sel.appendChild(opt);
        }
      }
      wrap.appendChild(sel);
      if (field.desc) addDesc(wrap, field.desc);
      wrap._getValue = () => sel.value || null;
      return wrap;
    }

    if (field.type === "select") {
      const sel = document.createElement("select");
      sel.id = id;
      for (const [value, lbl] of field.options) {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = lbl;
        if (String(current) === String(value)) opt.selected = true;
        sel.appendChild(opt);
      }
      wrap.appendChild(sel);
      if (field.desc) addDesc(wrap, field.desc);
      wrap._getValue = () => sel.value;
      return wrap;
    }

    if (field.type === "bytes") {
      const row = document.createElement("div");
      row.className = "size-input";
      const numEl = document.createElement("input");
      numEl.type = "number";
      numEl.id = id;
      numEl.min = 0;
      numEl.step = "any";
      const unitEl = document.createElement("select");
      for (const [name, mul] of UNITS) {
        const opt = document.createElement("option");
        opt.value = String(mul);
        opt.textContent = name;
        unitEl.appendChild(opt);
      }
      // Pick the biggest unit that leaves a whole number.
      const raw = Number(current || 0);
      let mul = 1;
      for (const [, m] of UNITS) {
        if (raw % m === 0 && raw / m >= 1) mul = m;
      }
      if (raw === 0) mul = 1;
      numEl.value = String(raw / mul);
      unitEl.value = String(mul);
      row.appendChild(numEl);
      row.appendChild(unitEl);
      wrap.appendChild(row);
      if (field.desc) addDesc(wrap, field.desc);
      wrap._getValue = () =>
        Math.round(parseFloat(numEl.value || "0") * parseInt(unitEl.value, 10));
      return wrap;
    }

    if (field.type === "password") {
      const input = document.createElement("input");
      input.type = "password";
      input.id = id;
      input.value = current == null ? "" : String(current);
      wrap.appendChild(input);
      if (field.desc) addDesc(wrap, field.desc);
      wrap._getValue = () => input.value;
      return wrap;
    }

    // number or text
    const input = document.createElement("input");
    input.type = field.type === "number" ? "number" : "text";
    input.id = id;
    if (field.type === "number") {
      if (field.min  !== undefined) input.min  = field.min;
      if (field.max  !== undefined) input.max  = field.max;
      if (field.step !== undefined) input.step = field.step;
    }
    input.value = current == null ? "" : String(current);
    wrap.appendChild(input);
    if (field.desc) addDesc(wrap, field.desc);
    wrap._getValue = () => {
      const v = input.value;
      if (v === "") return field.nullable ? null : "";
      if (field.type === "number") return parseFloat(v);
      return v;
    };
    return wrap;
  }

  function addDesc(wrap, text) {
    const d = document.createElement("div");
    d.className = "desc";
    d.textContent = text;
    wrap.appendChild(d);
  }

  // ---- load / render / save ---------------------------------------
  async function load() {
    const [cfgRes, nicRes] = await Promise.all([
      fetch("/api/config"),
      fetch("/api/nics"),
    ]);
    currentConfig = await cfgRes.json();
    currentNics = await nicRes.json();
    if (currentNics.length && currentNics[0].error) {
      console.warn("nic enumeration error:", currentNics[0].error);
      currentNics = [];
    }
    render();
    $("cfgStatus").textContent = "";
    $("cfgStatus").className = "chip";
  }

  function render() {
    const host = $("cfgForm");
    host.innerHTML = "";
    for (const section of CONFIG_SCHEMA) {
      const sec = document.createElement("div");
      sec.className = "form-section";
      const h = document.createElement("h3");
      h.textContent = section.title;
      sec.appendChild(h);
      if (section.desc) {
        const sd = document.createElement("div");
        sd.className = "section-desc";
        sd.textContent = section.desc;
        sec.appendChild(sd);
      }
      const body = document.createElement("div");
      body.className = "body";
      for (const field of section.fields) {
        body.appendChild(renderField(field));
      }
      sec.appendChild(body);
      host.appendChild(sec);
    }
  }

  async function save() {
    if (!currentConfig) { await load(); }
    // Refetch the server's current config before writing. Other panels
    // (e.g. view-tab visibility, ACL editor) can save out-of-band to
    // /api/ui_settings or /api/acl; without this refetch the main form
    // would write back a stale copy of those fields.
    let fresh = currentConfig;
    try {
      const r = await fetch("/api/config");
      if (r.ok) fresh = await r.json();
    } catch { /* fall back to currentConfig */ }
    const out = JSON.parse(JSON.stringify(fresh));
    for (const section of CONFIG_SCHEMA) {
      for (const field of section.fields) {
        const el = document.querySelector('[id="fld_' + field.path.join("_") + '"]');
        const container = el ? el.closest(".form-field") : null;
        if (container && container._getValue) {
          setPath(out, field.path, container._getValue());
        }
      }
    }
    try {
      const r = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(out),
      });
      const j = await r.json();
      $("cfgStatus").textContent = j.ok ? "saved - reloading..." : ("error: " + j.error);
      $("cfgStatus").className = "chip " + (j.ok ? "ok" : "err");
      if (j.ok) currentConfig = out;
    } catch (e) {
      $("cfgStatus").textContent = "error: " + e.message;
      $("cfgStatus").className = "chip err";
    }
  }

  function init() {
    $("cfgSave").addEventListener("click", save);
    $("cfgReload").addEventListener("click", load);
    window.Nishro.tabs.onShow("config", load);
  }

  window.Nishro.configInit = init;
})();
