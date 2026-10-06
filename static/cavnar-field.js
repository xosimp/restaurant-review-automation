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
   60 to look any different); the constellation at device resolution
   capped at 2x. Frozen under prefers-reduced-motion, paused while the tab
   is hidden, resized on the fly. ES5 only.

   No banding (owner, 9/30/26: "circular bands... this needs to be ULTRA
   smooth, it's a first impression"). Three causes, three fixes:
     - each bloom was five straight-line stops, and the eye reads every
       kink in a falloff as a ring (Mach bands) - FALLOFF is one smooth
       monotone curve through the same five points, 21 stops, flat at the
       rim so the bloom has no edge either;
     - 8-bit colour on a dark ground steps one level every few dozen px,
       and the quarter-resolution canvas stretched each step 4x - the
       aurora now paints at CSS resolution, on the page's own ground, with
       the vignette, and ends with a static 0-2 level dither (DITHER) that
       turns each step into grain finer than the eye resolves;
     - the CSS vignette was a second 8-bit gradient on top - it is drawn
       here, under the dither, instead. */
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

  // The bloom's falloff, centre to rim: a monotone cubic through iOS's
  // five stops (1, .55 at .22, .22 at .45, .06 at .68, 0), sampled at 21.
  var FALLOFF = [1.0, 0.8946, 0.7866, 0.6816, 0.585, 0.4995, 0.4175, 0.3413, 0.2743, 0.22, 0.1745,
                 0.1331, 0.098, 0.0712, 0.054, 0.0396, 0.0267, 0.0158, 0.0074, 0.0019, 0.0];
  // The vignette to Paper, top to bottom (was .cf-vignette in CSS).
  var VIGNETTE = [[0, 0], [0.45, 0.28], [1, 0.90]];

  // A static tile of 0-2 levels added over the aurora: dither, not a
  // texture - one level of grain is invisible, a one-level step is a ring.
  var DITHER = 128;
  function ditherTile() {
    var c = document.createElement('canvas');
    c.width = c.height = DITHER;
    var x = c.getContext('2d'), img = x.createImageData(DITHER, DITHER), d = img.data, i, v;
    for (i = 0; i < d.length; i += 4) {
      v = Math.random() + Math.random();          // triangular: 0, 1 or 2, mostly 1
      v = v < 0.5 ? 0 : (v < 1.5 ? 1 : 2);
      d[i] = d[i + 1] = d[i + 2] = v; d[i + 3] = 255;
    }
    x.putImageData(img, 0, 0);
    return c;
  }

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

  /* The aurora on the GPU (owner, 9/30/26, second report of banding).
     Canvas 2D computes and stores a gradient in 8-bit colour, so on this
     dark ground a bloom steps one level every few pixels; the dither added
     after it lived at CSS resolution, was stretched 2x on a Retina screen
     and could be re-quantised by the browser's colour conversion - and the
     card's 100px CSS shadow was a second 8-bit gradient of rings. Here the
     whole ground - blooms, vignette and the card's shadow - is computed in
     float per DEVICE pixel and dithered at the very last step with
     triangular noise of +-1 level, which is below what the eye can see and
     exactly what turns a step into a smooth ramp. Canvas 2D is the fallback. */
  var GL_VS = 'attribute vec2 p;void main(){gl_Position=vec4(p,0.0,1.0);}';
  var GL_FS = [
    '#ifdef GL_FRAGMENT_PRECISION_HIGH',
    'precision highp float;',
    '#else',
    'precision mediump float;',
    '#endif',
    'uniform vec2 uRes;uniform float uDpr;uniform vec3 uBase;',
    'uniform vec4 uBloom[3];uniform vec3 uCol[3];',
    'uniform vec4 uCard;uniform float uCardR;uniform float uShadow;uniform float uSeed;',
    'float h(vec2 q){q=fract(q*vec2(0.1031,0.1030));q+=dot(q,q.yx+33.33);return fract((q.x+q.y)*q.x);}',
    'float erfc2(float x){return 1.0/(1.0+exp(clamp(2.405*x,-60.0,60.0)));}',
    'void main(){',
    '  vec2 px=vec2(gl_FragCoord.x,uRes.y-gl_FragCoord.y);',
    '  vec3 c=uBase;',
    '  for(int i=0;i<3;i++){',
    '    float d=length(px-uBloom[i].xy)/uBloom[i].z;',
    '    float f=pow(clamp(1.0-d,0.0,1.0),2.43);',
    '    c=mix(c,uCol[i],uBloom[i].w*f);',
    '  }',
    '  float y=clamp(px.y/uRes.y,0.0,1.0);',
    '  c=mix(c,uBase,0.9*pow(y,1.6));',
    '  if(uShadow>0.0){',
    '    vec2 hc=uCard.xy+uCard.zw*0.5;',
    '    vec2 q=abs(px-hc)-uCard.zw*0.5+vec2(uCardR);',
    '    float sd=length(max(q,0.0))+min(max(q.x,q.y),0.0)-uCardR;',
    '    float s1=0.28*erfc2(sd/(14.0*1.4142*uDpr));',
    '    float s2=0.26*erfc2(sd/(35.0*1.4142*uDpr));',
    '    float s=1.0-(1.0-s1)*(1.0-s2);',
    '    c=mix(c,vec3(0.0),s*uShadow);',
    '  }',
    '  float n=h(gl_FragCoord.xy+uSeed)+h(gl_FragCoord.yx*1.37+uSeed+17.0)-1.0;',
    '  gl_FragColor=vec4(c+n/255.0,1.0);',
    '}'
  ].join('\n');

  function glAurora(canvas) {
    var gl = null;
    try {
      gl = canvas.getContext('webgl', {alpha: false, antialias: false, depth: false, stencil: false,
                                       premultipliedAlpha: false, preserveDrawingBuffer: false,
                                       powerPreference: 'low-power'});
    } catch (e) { gl = null; }
    if (!gl) return null;
    function sh(type, src) {
      var o = gl.createShader(type); gl.shaderSource(o, src); gl.compileShader(o);
      return gl.getShaderParameter(o, gl.COMPILE_STATUS) ? o : null;
    }
    var vs = sh(gl.VERTEX_SHADER, GL_VS), fs = sh(gl.FRAGMENT_SHADER, GL_FS);
    if (!vs || !fs) return null;
    var prog = gl.createProgram();
    gl.attachShader(prog, vs); gl.attachShader(prog, fs); gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return null;
    gl.useProgram(prog);
    var buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    var loc = gl.getAttribLocation(prog, 'p');
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    var U = {}, names = ['uRes', 'uDpr', 'uBase', 'uBloom', 'uCol', 'uCard', 'uCardR', 'uShadow', 'uSeed'], i;
    for (i = 0; i < names.length; i++) U[names[i]] = gl.getUniformLocation(prog, names[i]);
    return {gl: gl, U: U};
  }

  function mount(opts) {
    var aurora = opts.aurora, sky = opts.sky;
    if (!aurora || !sky || !aurora.getContext) return null;
    var GL = glAurora(aurora);
    if (!GL && aurora.parentNode && aurora.cloneNode) {
      // A canvas that once gave out a WebGL context never gives a 2D one:
      // a shader that would not compile left the page black. Fall back on
      // a fresh canvas in its place.
      var fresh = aurora.cloneNode(false);
      aurora.parentNode.replaceChild(fresh, aurora);
      aurora = fresh;
    }
    var actx = GL ? null : aurora.getContext('2d'), sctx = sky.getContext('2d');
    // The card's soft shadow is painted by the GL ground (dithered) instead
    // of CSS, which banded; its 1px edge stays in CSS. Its strength lives in
    // the shader (s1, s2) and matches login.html's fallback box-shadow:
    // softened twice on 10/6/26 (owner: "a bit too strong").
    var card = GL ? document.querySelector('.card') : null, cardR = 14;
    if (card) {
      try { cardR = parseFloat(window.getComputedStyle(card).borderTopLeftRadius) || 14; } catch (e) {}
      card.style.boxShadow = '0 0 0 1px rgba(0,0,0,.22)';
    }
    var reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    var W = 0, H = 0, dpr = 1, ascale = 1;
    var base = window.getComputedStyle ? window.getComputedStyle(document.body).backgroundColor : '';
    if (!base || base === 'transparent' || base === 'rgba(0, 0, 0, 0)') base = '#141110';
    var baseRGB = (base.match(/\d+/g) || [20, 17, 16]).slice(0, 3);
    var grain = null;
    try { grain = actx ? actx.createPattern(ditherTile(), 'repeat') : null; } catch (e) { grain = null; }
    var seed = 0;
    var t0 = Date.now() / 1000, last = 0, raf = null, frames = 0, drawMs = 0;

    function size() {
      W = window.innerWidth; H = window.innerHeight;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      var as = GL ? dpr : ascale;
      aurora.width = Math.max(1, Math.round(W * as)); aurora.height = Math.max(1, Math.round(H * as));
      sky.width = Math.round(W * dpr); sky.height = Math.round(H * dpr);
      aurora.style.width = sky.style.width = W + 'px';
      aurora.style.height = sky.style.height = H + 'px';
      if (actx) actx.setTransform(ascale, 0, 0, ascale, 0, 0);
      if (GL) GL.gl.viewport(0, 0, aurora.width, aurora.height);
      sctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function drawGL(t, frozen) {
      var gl = GL.gl, U = GL.U, i, bl = [], col = [];
      for (i = 0; i < BLOOMS.length; i++) {
        var b = BLOOMS[i], w = 2 * Math.PI / b.period;
        var dx = frozen ? 0 : Math.sin(t * w + b.phase) * W * 0.16;
        var dy = frozen ? 0 : Math.cos(t * w * 0.8 + b.phase) * H * 0.05;
        var breathe = frozen ? 1 : 0.82 + 0.18 * Math.sin(t * w * 1.3 + b.phase);
        bl.push((b.x * W + dx) * dpr, (b.y * H + dy) * dpr, b.r * W * dpr, b.a * breathe);
        col.push(b.c[0] / 255, b.c[1] / 255, b.c[2] / 255);
      }
      gl.uniform2f(U.uRes, aurora.width, aurora.height);
      gl.uniform1f(U.uDpr, dpr);
      gl.uniform3f(U.uBase, baseRGB[0] / 255, baseRGB[1] / 255, baseRGB[2] / 255);
      gl.uniform4fv(U.uBloom, new Float32Array(bl));
      gl.uniform3fv(U.uCol, new Float32Array(col));
      var r = card && card.getBoundingClientRect ? card.getBoundingClientRect() : null;
      var op = 1;
      if (r) { try { op = parseFloat(window.getComputedStyle(card).opacity); if (isNaN(op)) op = 1; } catch (e) { op = 1; } }
      gl.uniform1f(U.uShadow, r ? op : 0);
      if (r) { gl.uniform4f(U.uCard, r.left * dpr, r.top * dpr, r.width * dpr, r.height * dpr); gl.uniform1f(U.uCardR, cardR * dpr); }
      seed = (seed + 1) % 997;
      gl.uniform1f(U.uSeed, frozen ? 0 : seed * 3.1);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }

    function drawAurora(t, frozen) {
      if (GL) { drawGL(t, frozen); return; }
      actx.globalCompositeOperation = 'source-over';
      actx.fillStyle = base;
      actx.fillRect(0, 0, W, H);
      var i, k;
      for (i = 0; i < BLOOMS.length; i++) {
        var b = BLOOMS[i], w = 2 * Math.PI / b.period;
        var dx = frozen ? 0 : Math.sin(t * w + b.phase) * W * 0.16;
        var dy = frozen ? 0 : Math.cos(t * w * 0.8 + b.phase) * H * 0.05;
        var breathe = frozen ? 1 : 0.82 + 0.18 * Math.sin(t * w * 1.3 + b.phase);
        var cx = b.x * W + dx, cy = b.y * H + dy, r = b.r * W, a = b.a * breathe;
        var g = actx.createRadialGradient(cx, cy, 0, cx, cy, r);
        for (k = 0; k < FALLOFF.length; k++) g.addColorStop(k / (FALLOFF.length - 1), rgba(b.c, a * FALLOFF[k]));
        actx.fillStyle = g;
        actx.fillRect(cx - r, cy - r, r * 2, r * 2);
      }
      var v = actx.createLinearGradient(0, 0, 0, H);
      for (k = 0; k < VIGNETTE.length; k++) v.addColorStop(VIGNETTE[k][0], rgba(baseRGB, VIGNETTE[k][1]));
      actx.fillStyle = v;
      actx.fillRect(0, 0, W, H);
      if (grain) {
        actx.globalCompositeOperation = 'lighter';
        actx.fillStyle = grain;
        actx.fillRect(0, 0, W, H);
        actx.globalCompositeOperation = 'source-over';
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
    // The card fades in over ~half a second; a still frame (reduced motion,
    // a hidden tab) is repainted once it has, so its shadow is drawn.
    if (card) window.setTimeout(function () { drawAurora(reduced ? 0 : Date.now() / 1000 - t0 + 1000, reduced); }, 700);
    window.addEventListener('resize', function () { size(); if (reduced) { drawAurora(0, true); drawSky(0); } else { drawAurora(Date.now() / 1000 - t0 + 1000, false); drawSky(Date.now() / 1000 - t0 + 1000); } });
    document.addEventListener('visibilitychange', function () { if (document.hidden) stop(); else { last = 0; start(); } });

    function tick() {
      var t = Date.now() / 1000 - t0 + 1000;
      var s = performance.now();
      drawAurora(t, false); drawSky(t);
      return performance.now() - s;
    }
    return { stats: function () { return { frames: frames, lastDrawMs: drawMs, reduced: reduced, dpr: dpr, size: [W, H], gl: !!GL }; }, stop: stop, start: start, tick: tick };
  }

  window.CavnarField = { mount: mount };
}());
