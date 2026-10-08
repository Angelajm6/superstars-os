/* Usage beacon — job page redesign step 1 ("measure first").
 * SSC_track(surface, view) tells the server a view was shown: the surface (job / company /
 * home), the view name and, on a job page, the job. The server adds the role and counts per
 * day; nothing about the person or the content is sent or kept. Repeats of the same view
 * within a few seconds count once. Never throws, never delays the page.
 */
(function () {
  'use strict';
  var last = { key: '', at: 0 }, early = window.SSC_trackQ || [];
  window.SSC_track = function (surface, view) {
    try {
      if (!surface || !view) return;
      var code = surface === 'job' ? (window.SSC_PROJECT_CODE || '') : '';
      var key = surface + '|' + view + '|' + code, now = Date.now();
      if (key === last.key && now - last.at < 5000) return;
      last = { key: key, at: now };
      var body = JSON.stringify({ surface: surface, view: view, project_code: code });
      if (navigator.sendBeacon && navigator.sendBeacon('/api/usage/view', new Blob([body], { type: 'application/json' }))) return;
      fetch('/api/usage/view', { method: 'POST', credentials: 'same-origin', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: body }).catch(function () {});
    } catch (e) { /* counting is never worth an error */ }
  };
  window.SSC_trackQ = [];
  early.forEach(function (a) { window.SSC_track(a[0], a[1]); });   // views shown before this script loaded
})();
