(function(){
  var lightbox = document.getElementById('proof-lightbox');
  var lightboxImg = document.getElementById('proof-lightbox-img');
  var closeBtn = document.getElementById('proof-lightbox-close');
  if (!lightbox || !lightboxImg) return;

  function open(src, alt){
    lightboxImg.src = src;
    lightboxImg.alt = alt || '';
    lightbox.hidden = false;
    document.body.style.overflow = 'hidden';
  }
  function close(){
    lightbox.hidden = true;
    lightboxImg.src = '';
    document.body.style.overflow = '';
  }

  document.querySelectorAll('.js-proof-zoom').forEach(function(btn){
    btn.addEventListener('click', function(){
      var img = btn.querySelector('img');
      open(btn.getAttribute('data-full'), img ? img.alt : '');
    });
  });
  lightbox.addEventListener('click', close);
  closeBtn.addEventListener('click', function(e){ e.stopPropagation(); close(); });
  document.addEventListener('keydown', function(e){
    if (e.key === 'Escape' && !lightbox.hidden) close();
  });
})();
