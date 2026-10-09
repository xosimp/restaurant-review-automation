/* cavnar-grain.js — one fine grain over the whole page, so no glow or
   gradient bands (owner, 10/9/26: "all of these glows have bad banding").
   A dark gradient drawn in 8-bit colour steps a level every few dozen
   pixels and the eye reads each step as a ring; a grain of a level or two,
   different at every device pixel, scatters the steps finer than the eye
   resolves. The tile is drawn at the screen's own pixel density (so each
   grain is one device pixel), half lightening and half darkening (so the
   page's overall tone does not move), and it never takes a click. ES5. */
(function () {
  if (typeof document === 'undefined' || document.getElementById('cav-grain')) return;
  function make() {
    var dpr = Math.min(3, Math.max(1, window.devicePixelRatio || 1)), n = 128;
    var c = document.createElement('canvas'), ctx;
    c.width = n; c.height = n;
    try { ctx = c.getContext('2d'); } catch (e) { return; }
    if (!ctx) return;
    var img = ctx.createImageData(n, n), d = img.data, i, light;
    for (i = 0; i < d.length; i += 4) {
      light = Math.random() < 0.5;
      d[i] = d[i + 1] = d[i + 2] = light ? 255 : 0;
      d[i + 3] = Math.floor(Math.random() * 7);          // 0-6 of 255: about ±1-2 levels
    }
    ctx.putImageData(img, 0, 0);
    var url;
    try { url = c.toDataURL('image/png'); } catch (e2) { return; }
    var el = document.createElement('div');
    el.id = 'cav-grain';
    el.setAttribute('aria-hidden', 'true');
    el.style.cssText = 'position:fixed;left:0;top:0;right:0;bottom:0;pointer-events:none;z-index:2147483000;'
      + 'background-image:url(' + url + ');background-repeat:repeat;background-size:' + (n / dpr) + 'px ' + (n / dpr) + 'px';
    document.body.appendChild(el);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', make); else make();
})();
