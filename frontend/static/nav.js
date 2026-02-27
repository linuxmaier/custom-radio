(function () {
  var path = location.pathname;
  var links = [
    { href: '/', label: 'Submit', icon: '+' },
    { href: '/playing.html', label: 'Now Playing', icon: '♪' },
    { href: '/library.html', label: 'Library', icon: '≡' },
    { href: '/admin.html', label: 'Admin', icon: '⚙' },
  ];
  var items = links.map(function (l) {
    var active = l.href === '/' ? path === '/' : path === l.href;
    return '<a href="' + l.href + '"' + (active ? ' class="active"' : '') + '>' +
      '<span class="nav-icon">' + l.icon + '</span>' +
      '<span class="nav-label">' + l.label + '</span>' +
      '</a>';
  }).join('');

  var name = localStorage.getItem('station_name') || 'Radio';
  var script = document.currentScript || document.scripts[document.scripts.length - 1];
  script.insertAdjacentHTML('beforebegin', '<nav><a id="nav-logo" class="logo" href="/"><img src="/static/badge-96.png" alt="" width="20" height="20" style="vertical-align:middle;margin-right:0.4em;"> ' + name + '</a>' + items + '</nav>');

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
    })
    .catch(function () {});
}());
