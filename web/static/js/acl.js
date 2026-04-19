/* =====================================================================
   acl.js - VLAN + IP access control list editor
   ---------------------------------------------------------------------
   Admin-only tab. GETs /api/acl, populates two cards (VLAN / IP), and
   POSTs the combined payload back on save.
   ===================================================================== */
(function () {
  "use strict";
  const { $ } = window.Nishro;

  function applyChecked(selector, values) {
    document.querySelectorAll(selector).forEach((c) => {
      c.checked = (values || []).includes(c.value);
    });
  }

  function collectChecked(selector) {
    return Array.from(document.querySelectorAll(selector))
      .filter((c) => c.checked)
      .map((c) => c.value);
  }

  async function load() {
    try {
      const r = await fetch("/api/acl");
      const sec = await r.json();
      const v  = sec.vlan_acl || {};
      const ip = sec.ip_acl   || {};
      $("vlanMode").value = v.mode || "disabled";
      $("vlanList").value = (v.list || []).join(",");
      applyChecked(".vlanApply", v.apply_to);
      $("ipMode").value = ip.mode || "disabled";
      $("ipList").value = (ip.list || []).join(",");
      applyChecked(".ipApply", ip.apply_to);
      $("aclStatus").textContent = "";
      $("aclStatus").className = "chip";
    } catch (e) {
      $("aclStatus").textContent = "load error: " + e.message;
      $("aclStatus").className = "chip err";
    }
  }

  async function save() {
    const payload = {
      vlan_acl: {
        mode: $("vlanMode").value,
        list: $("vlanList").value
          .split(",").map((s) => s.trim()).filter(Boolean).map(Number),
        apply_to: collectChecked(".vlanApply"),
      },
      ip_acl: {
        mode: $("ipMode").value,
        list: $("ipList").value
          .split(",").map((s) => s.trim()).filter(Boolean),
        apply_to: collectChecked(".ipApply"),
      },
    };
    try {
      const r = await fetch("/api/acl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const j = await r.json();
      $("aclStatus").textContent = j.ok ? "saved" : ("error: " + j.error);
      $("aclStatus").className = "chip " + (j.ok ? "ok" : "err");
    } catch (e) {
      $("aclStatus").textContent = "error: " + e.message;
      $("aclStatus").className = "chip err";
    }
  }

  function init() {
    $("aclSave").addEventListener("click", save);
    $("aclReload").addEventListener("click", load);
    window.Nishro.tabs.onShow("acl", load);
  }

  window.Nishro.aclInit = init;
})();
