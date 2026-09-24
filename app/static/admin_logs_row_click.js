(function () {
  // Row-click navigation for the admin Troubleshooting Logs table, moved out
  // of an inline onclick attribute (2026-09-25). The CSP's script-src has no
  // 'unsafe-inline' (deliberately, to block inline <script> injection), and
  // that also silently blocks inline onclick="..." attributes - so the old
  // markup's row click did nothing, with no visible error. This external,
  // same-origin script is allowed under script-src 'self' and restores the
  // same behavior: clicking anywhere in a row (except a live text selection)
  // navigates to that row's log detail page.
  var rows = document.querySelectorAll("tr[data-log-href]");
  rows.forEach(function (row) {
    row.addEventListener("click", function () {
      var selection = window.getSelection ? window.getSelection().toString() : "";
      if (selection) return; // don't hijack a text-selection drag/click
      var href = row.getAttribute("data-log-href");
      if (href) window.location = href;
    });
  });
})();
