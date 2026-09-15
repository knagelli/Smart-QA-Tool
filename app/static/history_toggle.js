(function(){
  var toggles = document.querySelectorAll('[data-history-toggle]');
  if (!toggles.length) return;
  var rows = document.querySelectorAll('.history-row');
  toggles.forEach(function(btn){
    btn.addEventListener('click', function(){
      var showMineOnly = btn.getAttribute('data-target') === 'mine';
      rows.forEach(function(r){
        if (showMineOnly) { r.hidden = !r.hasAttribute('data-mine'); }
        else { r.hidden = false; }
      });
      toggles.forEach(function(b){ b.hidden = (b === btn); });
    });
  });
})();
