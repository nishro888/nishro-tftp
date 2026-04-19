/* =====================================================================
   app.js - bootstrap
   ---------------------------------------------------------------------
   Loads last, after every tab module has published its init function
   on window.Nishro. Order matters only insofar as tabs.init() must run
   before per-tab init()s so that onShow() callbacks can register.
   ===================================================================== */
(function () {
  "use strict";
  document.addEventListener("DOMContentLoaded", () => {
    const N = window.Nishro;
    N.tabs.init();
    N.configInit();
    N.aclInit();
    N.usersInit();
    N.filesInit();
    N.logsInit();
    N.connectWS();
  });
})();
