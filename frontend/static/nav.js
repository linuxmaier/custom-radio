(function () {
  var path = location.pathname;
  var links = [
    { href: '/', label: 'Submit' },
    { href: '/playing.html', label: 'Now Playing' },
    { href: '/admin.html', label: 'Admin' },
  ];
  var items = links.map(function (l) {
    var active = l.href === '/' ? path === '/' : path === l.href;
    return '<a href="' + l.href + '"' + (active ? ' class="active"' : '') + '>' + l.label + '</a>';
  }).join('');
  document.write('<nav><a id="nav-logo" class="logo" href="/">\uD83D\uDCFB Radio</a>' + items + '</nav>');

  fetch('/api/status')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var name = data.station_name || 'Radio';
      var logo = document.getElementById('nav-logo');
      if (logo) logo.textContent = '\uD83D\uDCFB ' + name;
      document.title = name + ' \u2014 ' + document.title;
    })
    .catch(function () {});
}());
