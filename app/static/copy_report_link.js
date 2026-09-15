(function(){
  var btn = document.querySelector('[data-copy-target="report-link"]');
  if (!btn) return;
  btn.addEventListener('click', function(){
    var url = document.getElementById('report-link').href;
    var status = document.querySelector('[data-copy-status]');
    navigator.clipboard.writeText(url).then(function(){
      if (status) { status.textContent = 'Copied!'; setTimeout(function(){ status.textContent=''; }, 2500); }
    }).catch(function(){
      if (status) { status.textContent = 'Could not copy — select and copy the link manually.'; }
    });
  });
})();
