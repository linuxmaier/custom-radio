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
  document.write('<nav><a class="logo" href="/">\uD83D\uDCFB Family Radio</a>' + items + '</nav>');
}());
