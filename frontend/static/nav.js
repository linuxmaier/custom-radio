(function () {
  var path = location.pathname;
  var links = [
    { href: '/', label: 'Submit' },
    { href: '/playing.html', label: 'Now Playing' },
    { href: '/library.html', label: 'Library' },
    { href: '/admin.html', label: 'Admin' },
  ];
  var items = links.map(function (l) {
    var active = l.href === '/' ? path === '/' : path === l.href;
    return '<a href="' + l.href + '"' + (active ? ' class="active"' : '') + '>' + l.label + '</a>';
  }).join('');

  var name = localStorage.getItem('station_name') || 'Radio';
  var script = document.currentScript || document.scripts[document.scripts.length - 1];
  script.insertAdjacentHTML('beforebegin', '<nav><a id="nav-logo" class="logo" href="/">\uD83D\uDCFB ' + name + '</a>' + items + '</nav>');

  fetch('/api/status')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var fetched = data.station_name || 'Radio';
      localStorage.setItem('station_name', fetched);
      var logo = document.getElementById('nav-logo');
      if (logo && fetched !== name) logo.textContent = '\uD83D\uDCFB ' + fetched;
      document.title = fetched + ' \u2014 ' + document.title;
    })
    .catch(function () {});
}());
