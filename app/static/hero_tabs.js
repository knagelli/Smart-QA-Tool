(function () {
  // Hero mockup tab toggle (Test Report / Traceability / Gaps). Pure
  // display toggle, no data - each panel's content is static markup
  // already in the page. Uses [hidden] rather than inline display so the
  // browser's own default (all panels visible) degrades sanely if this
  // script fails to load, rather than showing nothing.
  var tabs = document.querySelectorAll(".frame-tabs .frame-tab");
  if (!tabs.length) return;

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      var targetId = tab.getAttribute("data-panel");
      var target = document.getElementById(targetId);
      if (!target) return;

      tabs.forEach(function (t) { t.classList.remove("active"); });
      tab.classList.add("active");

      var panels = document.querySelectorAll(".hero-visual .browser-frame-body");
      panels.forEach(function (p) { p.hidden = true; });
      target.hidden = false;
    });
  });
})();
