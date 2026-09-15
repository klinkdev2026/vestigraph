// Runs before anything else (classic script, no module): move the one-time
// sign-in token out of the address bar so it is never kept, shared or logged.
(function () {
  var match = /(?:^|[#&])bootstrap=([^&]+)/.exec(location.hash || "");
  if (match) {
    window.__vestigraphBootstrap = decodeURIComponent(match[1]);
    history.replaceState(null, "", location.pathname + location.search);
  }
})();
