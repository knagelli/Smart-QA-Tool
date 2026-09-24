// Select All / Deselect All for the live-execution page (2026-09-24).
// Respects the server's per-run limit (data-max on #tc-bulk): Select All ticks
// at most that many, and once the limit is reached the remaining boxes are
// disabled so the form can never be submitted over the limit (which the
// server rejects). Dispatches "change" on every box it touches so the
// run-time estimate (execute_select_estimate.js) stays in sync.
(function(){
  var bar = document.getElementById("tc-bulk");
  var boxes = Array.prototype.slice.call(document.querySelectorAll(".js-select-tc"));
  if (!bar || !boxes.length) return;
  var max = parseInt(bar.getAttribute("data-max"), 10) || boxes.length;
  var countLine = document.getElementById("tc-bulk-count");
  var note = document.getElementById("tc-bulk-note");
  bar.style.display = "";  // buttons only appear when this script runs (inline style: .tc-toolbar{display:flex} would override the hidden attribute)

  function checkedCount(){ return boxes.filter(function(b){ return b.checked; }).length; }

  function refresh(){
    var n = checkedCount();
    boxes.forEach(function(b){
      b.disabled = !b.checked && n >= max;
      var row = b.closest(".js-tc-check");
      if (row) row.style.opacity = b.disabled ? "0.5" : "";
    });
    countLine.textContent = n + " of " + boxes.length + " selected" + (boxes.length > max ? " (max " + max + " per run)" : "");
    if (n >= max && boxes.length > max) {
      note.style.display = "";
      note.textContent = max + " selected — the per-run limit. Run these, then come back to run the rest.";
    } else {
      note.style.display = "none";
    }
  }

  function setAll(on){
    var n = 0;
    boxes.forEach(function(b){
      var want = on && n < max;
      if (want) n++;
      if (b.checked !== want) {
        b.checked = want;
        b.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    refresh();
  }

  document.getElementById("tc-select-all").addEventListener("click", function(){ setAll(true); });
  document.getElementById("tc-deselect-all").addEventListener("click", function(){ setAll(false); });
  boxes.forEach(function(b){ b.addEventListener("change", refresh); });
  refresh();
})();
