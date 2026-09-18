(function(){
  var PAGE_SIZE = 25;

  var list = document.getElementById("tc-list");
  var searchInput = document.getElementById("tc-search");
  var countLine = document.getElementById("tc-count-line");
  var pager = document.getElementById("tc-pagination");
  if (!list) return;

  var cards = Array.prototype.slice.call(list.querySelectorAll(".js-tc-card"));
  var currentPage = 1;

  // Per-card expand/collapse - independent of search/pagination, and never
  // resets a card's Keep checkbox since the card itself is never removed
  // from the DOM, only its details block is shown/hidden.
  cards.forEach(function(card){
    var btn = card.querySelector(".js-tc-expand");
    var details = card.querySelector(".js-tc-details");
    if (!btn || !details) return;
    btn.addEventListener("click", function(){
      var expanded = btn.getAttribute("aria-expanded") === "true";
      details.hidden = expanded;
      btn.setAttribute("aria-expanded", String(!expanded));
      btn.innerHTML = expanded ? "&#9656;" : "&#9662;";
    });
  });

  function matches(card, query) {
    if (!query) return true;
    var haystack = card.getAttribute("data-search") || "";
    return haystack.indexOf(query) !== -1;
  }

  function render() {
    var query = (searchInput && searchInput.value || "").trim().toLowerCase();
    var matching = cards.filter(function(c){ return matches(c, query); });
    var totalPages = Math.max(1, Math.ceil(matching.length / PAGE_SIZE));
    if (currentPage > totalPages) currentPage = totalPages;
    if (currentPage < 1) currentPage = 1;

    var start = (currentPage - 1) * PAGE_SIZE;
    var end = start + PAGE_SIZE;

    // Non-matching cards are always hidden regardless of page. Matching
    // cards are shown only if they fall within the current page's slice -
    // every card stays in the DOM either way, so checkbox state (which
    // cases are kept) is never lost as you search or page through.
    var shownCount = 0;
    cards.forEach(function(c){
      var isMatch = matches(c, query);
      c.style.display = "none";
      if (isMatch) {
        var idx = matching.indexOf(c);
        if (idx >= start && idx < end) {
          c.style.display = "";
          shownCount++;
        }
      }
    });

    if (countLine) {
      if (query) {
        countLine.textContent = matching.length + " matching “" + query + "” of " + cards.length + " total";
      } else {
        countLine.textContent = cards.length + " test case" + (cards.length === 1 ? "" : "s") + " generated";
      }
    }

    renderPager(totalPages, matching.length);
  }

  function renderPager(totalPages, matchingCount) {
    if (!pager) return;
    pager.innerHTML = "";
    if (totalPages <= 1) return;

    var prev = document.createElement("button");
    prev.type = "button";
    prev.textContent = "← Previous";
    prev.disabled = currentPage <= 1;
    prev.addEventListener("click", function(){ currentPage--; render(); });

    var label = document.createElement("span");
    label.className = "hint";
    label.style.margin = "0 12px";
    label.textContent = "Page " + currentPage + " of " + totalPages + " (" + matchingCount + " test cases, 25 per page)";

    var next = document.createElement("button");
    next.type = "button";
    next.textContent = "Next →";
    next.disabled = currentPage >= totalPages;
    next.addEventListener("click", function(){ currentPage++; render(); });

    pager.appendChild(prev);
    pager.appendChild(label);
    pager.appendChild(next);
  }

  if (searchInput) {
    searchInput.addEventListener("input", function(){
      currentPage = 1;
      render();
    });
  }

  render();
})();
