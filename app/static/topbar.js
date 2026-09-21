(function(){
  // Mobile hamburger toggle for the shared topbar (_topbar.html), loaded on
  // every page. Kept separate from chooser.js, which only ships on the
  // homepage and shouldn't be assumed present elsewhere.
  var toggle = document.querySelector('.topbar-toggle');
  var nav = document.querySelector('.topbar-nav');
  if (!toggle || !nav) return;
  toggle.addEventListener('click', function(){
    var open = nav.classList.toggle('open');
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  // Close the menu after navigating, so it doesn't stay open on the next page.
  nav.querySelectorAll('a').forEach(function(a){
    a.addEventListener('click', function(){ nav.classList.remove('open'); });
  });

  // Grouped nav dropdowns (<details class="nav-drop">, see style.css) -
  // clicking outside an open one closes it, and opening one closes any
  // other that's already open, so only one panel is ever showing at a time.
  var drops = nav.querySelectorAll('.nav-drop');
  drops.forEach(function(d){
    d.addEventListener('toggle', function(){
      if (d.open) {
        drops.forEach(function(other){ if (other !== d) other.open = false; });
      }
    });
  });
  document.addEventListener('click', function(e){
    drops.forEach(function(d){
      if (d.open && !d.contains(e.target)) d.open = false;
    });
  });
})();
