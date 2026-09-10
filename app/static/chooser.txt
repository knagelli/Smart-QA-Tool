(function(){
  var chooser = document.getElementById('chooser');
  var panels = {named: document.getElementById('tab-named'), custom: document.getElementById('tab-custom')};

  function showPanel(name){
    chooser.hidden = true;
    Object.keys(panels).forEach(function(k){ panels[k].hidden = (k !== name); });
    window.scrollTo(0, 0);
  }
  function showChooser(){
    chooser.hidden = false;
    Object.keys(panels).forEach(function(k){ panels[k].hidden = true; });
  }

  document.querySelectorAll('[data-choose]').forEach(function(btn){
    btn.addEventListener('click', function(){ showPanel(btn.dataset.choose); });
  });
  document.querySelectorAll('[data-back]').forEach(function(btn){
    btn.addEventListener('click', showChooser);
  });

  var initialTabEl = document.getElementById('initial-tab');
  var initial = initialTabEl ? initialTabEl.dataset.activeTab : '';
  if (initial === 'named' || initial === 'custom') { showPanel(initial); }
})();
