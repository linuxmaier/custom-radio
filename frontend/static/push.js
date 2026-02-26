/* Push notification helpers — exposes window.pushHelpers */
(function () {
  function urlBase64ToUint8Array(base64String) {
    var padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    var base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    var raw = atob(base64);
    var output = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) {
      output[i] = raw.charCodeAt(i);
    }
    return output;
  }

  function isSupported() {
    return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  }

  // Returns 'ios' if the user needs to install to home screen first,
  // 'android' if a native install prompt is available, or false otherwise.
  function needsInstall() {
    var isIos = /iphone|ipad|ipod/i.test(navigator.userAgent);
    var isInStandaloneMode = window.matchMedia('(display-mode: standalone)').matches
      || navigator.standalone === true;
    if (isIos && !isInStandaloneMode) return 'ios';
    return false;
  }

  async function getRegistration() {
    return navigator.serviceWorker.ready;
  }

  async function isSubscribed() {
    if (!isSupported()) return false;
    try {
      var reg = await getRegistration();
      var sub = await reg.pushManager.getSubscription();
      return sub !== null;
    } catch (_) {
      return false;
    }
  }

  async function subscribe() {
    if (!isSupported()) throw new Error('Push not supported');
    var reg = await getRegistration();

    // Request notification permission if needed
    if (Notification.permission === 'denied') throw new Error('Permission denied');
    if (Notification.permission !== 'granted') {
      var perm = await Notification.requestPermission();
      if (perm !== 'granted') throw new Error('Permission not granted');
    }

    // Fetch VAPID public key
    var res = await fetch('/api/push/vapid-key');
    if (!res.ok) throw new Error('Push not configured on server');
    var { public_key } = await res.json();

    var sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(public_key),
    });

    var json = sub.toJSON();
    await fetch('/api/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        endpoint: json.endpoint,
        p256dh: json.keys.p256dh,
        auth: json.keys.auth,
      }),
    });

    return sub;
  }

  async function unsubscribe() {
    if (!isSupported()) return;
    var reg = await getRegistration();
    var sub = await reg.pushManager.getSubscription();
    if (!sub) return;
    var json = sub.toJSON();
    await fetch('/api/push/unsubscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        endpoint: json.endpoint,
        p256dh: json.keys.p256dh,
        auth: json.keys.auth,
      }),
    });
    await sub.unsubscribe();
  }

  window.pushHelpers = { isSupported, needsInstall, isSubscribed, subscribe, unsubscribe };
}());
