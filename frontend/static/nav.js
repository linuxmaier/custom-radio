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

  // Persistent mini-player (shown on all pages except Now Playing)
  if (path !== '/playing.html') {
    var state = null;
    try { state = JSON.parse(sessionStorage.getItem('radioState')); } catch (e) {}

    if (state && state.active) {
      document.addEventListener('DOMContentLoaded', function () {
        var streamUrl = location.protocol + '//' + location.hostname + '/stream';

        document.body.insertAdjacentHTML('beforeend',
          '<div id="mini-player" class="mini-player">' +
            '<audio id="mini-audio" preload="auto"></audio>' +
            '<a href="/playing.html" class="mini-track">' +
              '<div class="mini-title" id="mini-title">Loading\u2026</div>' +
              '<div class="mini-sub" id="mini-sub"></div>' +
            '</a>' +
            '<button id="mini-btn" class="mini-btn" aria-label="Play/Pause">' +
              '<span id="mini-spinner" class="spinner" style="display:none;"></span>' +
              '<span id="mini-icon">\u25b6</span>' +
            '</button>' +
          '</div>'
        );
        document.body.classList.add('has-mini-player');

        var miniAudio = document.getElementById('mini-audio');
        var miniBtn = document.getElementById('mini-btn');
        var miniIcon = document.getElementById('mini-icon');
        var miniSpinner = document.getElementById('mini-spinner');
        var miniTitle = document.getElementById('mini-title');
        var miniSub = document.getElementById('mini-sub');
        var miniPlaying = false;
        var miniUserPaused = false;

        function setMiniState(playing, loading) {
          miniPlaying = playing;
          miniSpinner.style.display = loading ? 'inline-block' : 'none';
          miniIcon.style.display = loading ? 'none' : 'inline';
          miniIcon.textContent = playing ? '\u23f8' : '\u25b6';
        }

        miniAudio.src = streamUrl;

        miniAudio.addEventListener('playing', function () {
          setMiniState(true, false);
          sessionStorage.setItem('radioState', JSON.stringify({ active: true, paused: false }));
        });
        miniAudio.addEventListener('waiting', function () {
          miniSpinner.style.display = 'inline-block';
          miniIcon.style.display = 'none';
        });
        miniAudio.addEventListener('pause', function () {
          setMiniState(false, false);
          if (miniUserPaused) {
            sessionStorage.setItem('radioState', JSON.stringify({ active: true, paused: true }));
          }
          miniUserPaused = false;
        });
        miniAudio.addEventListener('error', function () {
          setMiniState(false, false);
        });

        miniBtn.addEventListener('click', function () {
          if (miniPlaying) {
            miniUserPaused = true;
            miniAudio.pause();
          } else {
            setMiniState(false, true);
            miniAudio.src = streamUrl + '?t=' + Date.now();
            miniAudio.play().catch(function () { setMiniState(false, false); });
          }
        });

        // Auto-resume if stream was active and not paused
        if (!state.paused) {
          setMiniState(false, true);
          miniAudio.play().catch(function () { setMiniState(false, false); });
        }

        // Fetch and poll track info
        function fetchMiniStatus() {
          fetch('/api/status')
            .then(function (r) { return r.json(); })
            .then(function (data) {
              var np = data.now_playing;
              if (np) {
                miniTitle.textContent = np.title;
                miniSub.textContent = np.artist;
              } else {
                miniTitle.textContent = 'Starting up\u2026';
                miniSub.textContent = '';
              }
            })
            .catch(function () {});
        }

        fetchMiniStatus();
        setInterval(fetchMiniStatus, 15000);
      });
    }
  }
}());
