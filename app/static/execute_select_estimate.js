(function(){
  var boxes = document.querySelectorAll(".js-select-tc");
  var line = document.getElementById("estimate-line");
  if (!boxes.length || !line) return;
  function update() {
    var n = 0;
    boxes.forEach(function(b){ if (b.checked) n++; });
    if (n === 0) {
      line.textContent = "Select test cases above to see an estimated run time.";
      return;
    }
    var lowMin = n * 1, highMin = n * 2;
    line.textContent = n + " test case" + (n === 1 ? "" : "s") + " selected — roughly " + lowMin + "-" + highMin + " minutes total, run one after another.";
  }
  boxes.forEach(function(b){ b.addEventListener("change", update); });
  update();
})();
