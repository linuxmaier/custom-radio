(function () {
  var hash = location.hash;
  var links = [
    { href: '/#submit',  label: 'Submit',      icon: '+' },
    { href: '/#playing', label: 'Now Playing',  icon: '♪' },
    { href: '/#library', label: 'Library',      icon: '≡' },
    { href: '/#admin',   label: 'Admin',        icon: '⚙' },
  ];

  function linkIsActive(linkHash) {
    return linkHash === hash || (hash === '' && linkHash === '#playing');
  }

  var items = links.map(function (l) {
    var linkHash = new URL(l.href, location.origin).hash;
    var active = linkIsActive(linkHash);
    return '<a href="' + l.href + '"' + (active ? ' class="active"' : '') + '>' +
      '<span class="nav-icon">' + l.icon + '</span>' +
      '<span class="nav-label">' + l.label + '</span>' +
      '</a>';
  }).join('');

  var name = localStorage.getItem('station_name') || 'Radio';
  var script = document.currentScript || document.scripts[document.scripts.length - 1];
  script.insertAdjacentHTML('beforebegin',
    '<nav><a id="nav-logo" class="logo" href="/#playing"><img src="/static/badge-96.png" alt="" width="20" height="20" style="vertical-align:middle;margin-right:0.4em;"> ' + name + '</a>' + items + '</nav>');

  // Update active link on hash navigation (SPA — no full page reload)
  window.addEventListener('hashchange', function () {
    var currentHash = location.hash;
    document.querySelectorAll('nav a[href^="/#"]').forEach(function (a) {
      var linkHash = new URL(a.href).hash;
      a.classList.toggle('active', linkHash === currentHash || (currentHash === '' && linkHash === '#playing'));
    });
  });

  // Register service worker for PWA support
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(function () {});
  }

  fetch('/api/status')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var fetched = data.station_name || 'Radio';
      localStorage.setItem('station_name', fetched);
      var logo = document.getElementById('nav-logo');
      if (logo && fetched !== name) {
        var img = logo.querySelector('img');
        logo.textContent = ' ' + fetched;
        if (img) logo.prepend(img);
      }
      document.title = fetched + ' \u2014 ' + document.title;
      // Keep token stream URL in sync on every page load
      if (data.public_stream_url) {
        localStorage.setItem('radioPublicStreamUrl', location.protocol + '//' + location.hostname + data.public_stream_url);
      } else {
        localStorage.removeItem('radioPublicStreamUrl');
      }
    })
    .catch(function () {});
}());
