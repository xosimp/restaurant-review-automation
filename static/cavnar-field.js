/* Cavnar living background — a port of the iOS sign-in screen's
   LoginBackground (ios/…/Features/Auth/LoginBackground.swift), constant
   for constant:
     1. Aurora — three soft ember blooms drifting on 14/18/9s cycles, each
        a five-stop radial gradient (no blur filter — the falloff does the
        softening, which is what keeps this cheap).
     2. Constellation — 36 points on their own slow headings across the
        top 72% of the screen, hairlines between any two within 110px,
        fading with distance; the same LCG seed as iOS so it's the same
        sky every load.
     3. Vignette — handled in CSS (a fixed gradient div to Paper), so the
        canvases only ever paint what moves.
   Frame budget: capped at 30fps like iOS (the drift is far too slow for
   60 to look any different), the aurora is painted at quarter resolution
   and scaled up by the compositor (its blooms are hundreds of px soft, so
   the quantisation is invisible), the constellation at device resolution
   capped at 2x. Frozen under prefers-reduced-motion, paused while the tab
   is hidden, resized on the fly. ES5 only. */
(function () {
  'use strict';

  var EMBER = [212, 88, 58];    // Ember, dark appearance (#D4583A)
  var EMBER2 = [232, 149, 106]; // Ember2 (#E8956A)
  var FRAME = 1000 / 30;

  function rgba(c, a) { return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + a + ')'; }

  // The same 64-bit LCG as the Swift side, on BigInt-free arithmetic: we
  // only need the top bits, and JS doubles can't hold the full product, so
  // this walks the identical sequence with 32-bit halves.
  function makeRng(hi, lo) {
    var A_HI = 0x5851F42D, A_LO = 0x4C957F2D, C_HI = 0x14057B7E, C_LO = 0xF767814F;
    function mul32(a, b) {
      var ah = a >>> 16, al = a & 0xffff, bh = b >>> 16, bl = b & 0xffff;
      return ((al * bl) + (((ah * bl + al * bh) & 0xffff) << 16)) >>> 0;
    }
    function mulhi(a, b) {
      // high 32 bits of the 64-bit product a*b
      var ah = a >>> 16, al = a & 0xffff, bh = b >>> 16, bl = b & 0xffff;
      var ll = al * bl, lh = al * bh, hl = ah * bl, hh = ah * bh;
      var mid = (ll >>> 16) + (lh & 0xffff) + (hl & 0xffff);
      return (hh + (lh >>> 16) + (hl >>> 16) + (mid >>> 16)) >>> 0;
    }
    return function () {
      // seed = seed * A + C  (mod 2^64)
      var nlo = mul32(lo, A_LO);
      var carry = mulhi(lo, A_LO);
      var nhi = (mul32(hi, A_LO) + mul32(lo, A_HI) + carry) >>> 0;
      var sum = nlo + C_LO;
      lo = sum >>> 0;
      hi = (nhi + C_HI + (sum > 0xffffffff ? 1 : 0)) >>> 0;
      // (seed >> 33) % 10000 / 10000
      var top = Math.floor(hi / 2); // hi >>> 1, as a number (33-bit shift of the 64-bit value)
      return (top % 10000) / 10000;
    };
  }

  var BLOOMS = [
    { x: 0.22, y: 0.05, r: 0.80, c: EMBER,  a: 0.66, period: 14, phase: 0 },
    { x: 0.95, y: 0.28, r: 0.64, c: EMBER2, a: 0.42, period: 18, phase: 2.1 },
    { x: 0.50, y: 1.05, r: 0.75, c: EMBER,  a: 0.34, period: 9,  phase: 4.2 }
  ];

  var COUNT = 36, LINK = 110, FIELD_H = 0.72;
  var points = (function () {
    var next = makeRng(0x9E3779B9, 0x7F4A7C15);
    var out = [];
    for (var i = 0; i < COUNT; i++) {
      var angle = next() * 2 * Math.PI;
      var speed = 0.009 + next() * 0.013;
      out.push({ x0: next(), y0: next(), vx: Math.cos(angle) * speed, vy: Math.sin(angle) * speed,
                 r: 1 + next() * 1.4, phase: next() * 2 * Math.PI });
    }
    return out;
  }());

  function mount(opts) {
    var aurora = opts.aurora, sky = opts.sky;
    if (!aurora || !sky || !aurora.getContext) return null;
    var actx = aurora.getContext('2d'), sctx = sky.getContext('2d');
    var reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    var W = 0, H = 0, dpr = 1, ascale = 0.25;
    var t0 = Date.now() / 1000, last = 0, raf = null, frames = 0, drawMs = 0;

    function size() {
      W = window.innerWidth; H = window.innerHeight;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      aurora.width = Math.max(1, Math.round(W * ascale)); aurora.height = Math.max(1, Math.round(H * ascale));
      sky.width = Math.round(W * dpr); sky.height = Math.round(H * dpr);
      aurora.style.width = sky.style.width = W + 'px';
      aurora.style.height = sky.style.height = H + 'px';
      actx.setTransform(ascale, 0, 0, ascale, 0, 0);
      sctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function drawAurora(t, frozen) {
      actx.clearRect(0, 0, W, H);
      for (var i = 0; i < BLOOMS.length; i++) {
        var b = BLOOMS[i], w = 2 * Math.PI / b.period;
        var dx = frozen ? 0 : Math.sin(t * w + b.phase) * W * 0.16;
        var dy = frozen ? 0 : Math.cos(t * w * 0.8 + b.phase) * H * 0.05;
        var breathe = frozen ? 1 : 0.82 + 0.18 * Math.sin(t * w * 1.3 + b.phase);
        var cx = b.x * W + dx, cy = b.y * H + dy, r = b.r * W, a = b.a * breathe;
        var g = actx.createRadialGradient(cx, cy, 0, cx, cy, r);
        g.addColorStop(0, rgba(b.c, a));
        g.addColorStop(0.22, rgba(b.c, a * 0.55));
        g.addColorStop(0.45, rgba(b.c, a * 0.22));
        g.addColorStop(0.68, rgba(b.c, a * 0.06));
        g.addColorStop(1, rgba(b.c, 0));
        actx.fillStyle = g;
        actx.fillRect(cx - r, cy - r, r * 2, r * 2);
      }
    }

    var pos = [];
    function drawSky(t) {
      sctx.clearRect(0, 0, W, H);
      var h = H * FIELD_H, i, j;
      for (i = 0; i < COUNT; i++) {
        var p = points[i];
        var nx = (p.x0 + p.vx * t) % 1, ny = (p.y0 + p.vy * t) % 1;
        if (nx < 0) nx += 1; if (ny < 0) ny += 1;
        pos[i] = [nx * W, ny * h];
      }
      // Hairlines first, so the points sit on top of them. Batched by a
      // handful of alpha bands rather than one stroke per pair.
      sctx.lineWidth = 0.8;
      var bands = 6, band, path;
      for (band = 0; band < bands; band++) {
        var lo = band / bands, hi = (band + 1) / bands, any = false;
        sctx.beginPath();
        for (i = 0; i < COUNT; i++) {
          for (j = i + 1; j < COUNT; j++) {
            var ddx = pos[i][0] - pos[j][0], ddy = pos[i][1] - pos[j][1];
            var d = Math.sqrt(ddx * ddx + ddy * ddy);
            if (d >= LINK) continue;
            var k = 1 - d / LINK;
            if (k < lo || k >= hi) continue;
            sctx.moveTo(pos[i][0], pos[i][1]); sctx.lineTo(pos[j][0], pos[j][1]); any = true;
          }
        }
        if (any) { sctx.strokeStyle = rgba(EMBER2, 0.85 * 0.24 * ((lo + hi) / 2)); sctx.stroke(); }
      }
      for (i = 0; i < COUNT; i++) {
        var q = points[i], glow = 0.55 + 0.45 * Math.sin(t / 0.9 + q.phase);
        sctx.fillStyle = rgba(EMBER2, 0.85 * 0.55 * glow);
        sctx.beginPath(); sctx.arc(pos[i][0], pos[i][1], q.r, 0, 2 * Math.PI); sctx.fill();
      }
    }

    function frame(now) {
      raf = null;
      if (now - last < FRAME - 1) { raf = window.requestAnimationFrame(frame); return; }
      last = now;
      var t = Date.now() / 1000 - t0 + 1000; // offset so the sky isn't at its seed frame on load
      var s = (window.performance && performance.now) ? performance.now() : 0;
      drawAurora(t, false); drawSky(t);
      if (s) drawMs = (window.performance.now() - s);
      frames++;
      raf = window.requestAnimationFrame(frame);
    }

    function start() { if (!raf && !reduced && !document.hidden) raf = window.requestAnimationFrame(frame); }
    function stop() { if (raf) { window.cancelAnimationFrame(raf); raf = null; } }

    // Always paint one frame synchronously — a tab that loads in the
    // background (or is pre-rendered) must not sit on bare Paper until its
    // first visibilitychange.
    size();
    if (reduced) { drawAurora(0, true); drawSky(0); }
    else { drawAurora(1000, false); drawSky(1000); start(); }
    window.addEventListener('resize', function () { size(); if (reduced) { drawAurora(0, true); drawSky(0); } else { drawAurora(Date.now() / 1000 - t0 + 1000, false); drawSky(Date.now() / 1000 - t0 + 1000); } });
    document.addEventListener('visibilitychange', function () { if (document.hidden) stop(); else { last = 0; start(); } });

    function tick() {
      var t = Date.now() / 1000 - t0 + 1000;
      var s = performance.now();
      drawAurora(t, false); drawSky(t);
      return performance.now() - s;
    }
    return { stats: function () { return { frames: frames, lastDrawMs: drawMs, reduced: reduced, dpr: dpr, size: [W, H] }; }, stop: stop, start: start, tick: tick };
  }

  window.CavnarField = { mount: mount };
}());
