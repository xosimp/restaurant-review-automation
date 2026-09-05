/*
 * cavnar-orb.js — the Ask Cavnar thinking orb.
 *
 * A native port of the dotted-sphere "thinking orb" idea: a point cloud on
 * a sphere, projected in 3D and animated differently for each thing the
 * agent is doing. Nine states, one engine. Geometry is pure math over
 * (size, t, opts) so the Swift port (DesignSystem/CavnarOrb.swift) can be
 * compared against it numerically — the two must stay in step.
 *
 * Deliberately not the npm package: the web app is server-rendered ES5
 * with no bundler and no React, and the iOS app is native SwiftUI, so a
 * React component fits neither. This is written in ES5 to match the rest
 * of dashboard.html's inline JS (tests/test_frontend_rules.py enforces
 * that on the inline scripts; this file follows it by convention).
 *
 * Branding: the reference orbs are grayscale. Ours paint on an ember ramp —
 * far dots a muted warm grey, near dots ember — so the orb reads as ours
 * without changing the motion. Ember is the only accent (see the motion
 * language notes); nothing here bounces.
 *
 * Usage:
 *   var orb = CavnarOrb.mount(canvasEl, 'searching', { size: 64 });
 *   CavnarOrb.setState(orb, 'composing');
 *   CavnarOrb.stop(orb);
 *
 * States: connecting, solving, searching, working, shaping, composing,
 *         breathing, listening, weaving
 */
(function (global) {
  'use strict';

  // ── Small math ──────────────────────────────────────────────────────────

  function lerp(a, b, t) { return a + (b - a) * t; }
  function fract(x) { return x - Math.floor(x); }
  function hash(n, s) {
    var t = Math.sin(n * 12.9898 + s * 78.233) * 43758.5453;
    return t - Math.floor(t);
  }
  // 2D value noise with smoothstep blending.
  function noise(x, y) {
    var xi = Math.floor(x), yi = Math.floor(y);
    var fx = x - xi, fy = y - yi;
    fx = fx * fx * (3 - 2 * fx);
    fy = fy * fy * (3 - 2 * fy);
    var a = hash(xi, yi), b = hash(xi + 1, yi), c = hash(xi, yi + 1), d = hash(xi + 1, yi + 1);
    return a + (b - a) * fx + (c - a) * fy + (a - b - c + d) * fx * fy;
  }
  // Point i of n on a fibonacci sphere — even coverage, no clumping at poles.
  function fib(i, n) {
    var golden = Math.PI * (3 - Math.sqrt(5));
    var y = 1 - 2 * (i + 0.5) / n;
    var r = Math.sqrt(1 - y * y);
    var th = i * golden;
    return [r * Math.cos(th), y, r * Math.sin(th)];
  }
  function angDiff(a, b) { return Math.atan2(Math.sin(a - b), Math.cos(a - b)); }
  function smooth(x) { return x * x * (3 - 2 * x); }
  function hypot2(dx, dy) { return Math.sqrt(dx * dx + dy * dy); }

  // Yaw around Y then tilt around X, then place at (cx, cy) with 'scale'.
  // Returns a function (x, y, z) -> [X, Y, Z]; Z keeps depth for sorting.
  function makeProj(yaw, tilt, cx, cy, scale) {
    var st = Math.sin(tilt), ct = Math.cos(tilt);
    var sy = Math.sin(yaw), cy_ = Math.cos(yaw);
    return function (x, y, z) {
      var x1 = x * cy_ + z * sy;
      var z1 = -x * sy + z * cy_;
      var y2 = y * ct - z1 * st;
      var z2 = y * st + z1 * ct;
      return [cx + x1 * scale, cy - y2 * scale, z2];
    };
  }

  function radiusScale(size, pow) { return Math.pow(size / 300, pow); }

  // Drop invisible dots, clamp radius, sort back-to-front.
  function finalize(dots, lines, rMin) {
    var min = (rMin == null) ? 0.3 : rMin;
    var kept = [];
    var i;
    for (i = 0; i < dots.length; i++) {
      var d = dots[i];
      var a = (d.a == null) ? 1 : d.a;
      if (a < 0.02) continue;
      d.r = Math.max(min, d.r);
      kept.push(d);
    }
    kept.sort(function (p, q) { return p.z - q.z; });
    var keptLines = [];
    for (i = 0; i < lines.length; i++) {
      var l = lines[i];
      if (((l.a == null) ? 1 : l.a) >= 0.02) keptLines.push(l);
    }
    return { dots: kept, lines: keptLines };
  }

  // ── Modes ───────────────────────────────────────────────────────────────
  // Each: (size, t, opts) -> { dots, lines }. 'white' is a 0..1 "faintness"
  // (the reference's ink value); we map it onto the ember ramp at paint.

  // Braid (weaving): three helical strands over a ghost sphere.
  function modeBraid(n, s, o) {
    var cx = n / 2, cy = n / 2, R = n / 2 * 0.76;
    var proj = makeProj(s * 0.4, 0.3, cx, cy, 1);
    var rs = radiusScale(n, o.rsPow == null ? 0.6 : o.rsPow);
    var dots = [];
    var ghostN = o.ghostN == null ? 150 : o.ghostN;
    var i, k;
    for (i = 0; i < ghostN; i++) {
      var g = fib(i, ghostN);
      var p = proj(g[0] * R, g[1] * R, g[2] * R);
      var depth = (p[2] / R + 1) / 2;
      dots.push({ x: p[0], y: p[1], z: p[2], r: 0.8 * rs, white: 0.78, a: 0.1 + 0.22 * depth });
    }
    var strandN = o.strandN == null ? 52 : o.strandN;
    var turns = o.turns == null ? 3 : o.turns;
    for (k = 0; k < 3; k++) {
      var phase = k / 3 * 2 * Math.PI;
      for (i = 0; i < strandN; i++) {
        var w = (fract(i / strandN + s * 0.045) * 2 - 1) * 0.96;
        var ring = Math.sqrt(Math.max(0, 1 - w * w));
        var fade = Math.min(1, (1 - Math.abs(w)) / 0.1);
        var ang = w * Math.PI * turns + phase;
        var bulge = 1 + 0.075 * Math.sin(w * Math.PI * turns * 2 + phase * 2 + s * 0.8);
        var rr = ring * R * bulge;
        var q = proj(Math.cos(ang) * rr, w * R * bulge, Math.sin(ang) * rr);
        var dd = (q[2] / R + 1) / 2;
        dots.push({
          x: q[0], y: q[1], z: q[2],
          r: ((o.rBase == null ? 1.2 : o.rBase) + (o.rDepth == null ? 1.8 : o.rDepth) * dd) * rs,
          white: 0.55 - 0.45 * dd,
          a: fade * (0.45 + 0.55 * dd)
        });
      }
    }
    return finalize(dots, [], o.rMin);
  }

  // Rubik (solving): a lattice sphere whose slabs twist a quarter turn,
  // one after another, then untwist.
  function rubikSchedule(t, count, dur, pause) {
    var period = 2 * count * dur + pause;
    var u = t % period;
    var amount = [];
    var i;
    for (i = 0; i < count; i++) amount.push(0);
    var active = -1;
    if (u < 2 * count * dur) {
      var idx = Math.floor(u / dur);
      var frac = (u - idx * dur) / dur;
      var eased = 1 - Math.pow(1 - Math.min(1, frac / 0.7), 3);
      if (idx < count) {
        for (i = 0; i < idx; i++) amount[i] = 1;
        amount[idx] = eased;
        active = idx;
      } else {
        var back = 2 * count - 1 - idx;
        for (i = 0; i < back; i++) amount[i] = 1;
        amount[back] = 1 - eased;
        active = back;
      }
    }
    return { amount: amount, active: active };
  }
  function rubikMoves(count) {
    var moves = [];
    var i;
    for (i = 0; i < count; i++) {
      var axis = Math.min(2, Math.floor(hash(i, 2.3) * 3));
      var lo = -1 + 0.5 * Math.min(3, Math.floor(hash(i, 5.9) * 4));
      var dir = hash(i, 7.7) < 0.5 ? 1 : -1;
      moves.push({ axis: axis, lo: lo, hi: lo + 0.5, ang: dir * Math.PI / 2 });
    }
    return moves;
  }
  function rubikApply(p, moves, sched) {
    var x = p[0], y = p[1], z = p[2], hit = false;
    var i;
    for (i = 0; i < moves.length; i++) {
      if (sched.amount[i] <= 0) continue;
      var m = moves[i];
      var coord = m.axis === 0 ? x : (m.axis === 1 ? y : z);
      if (coord < m.lo || coord >= m.hi) continue;
      if (i === sched.active) hit = true;
      var a = m.ang * sched.amount[i], c = Math.cos(a), sn = Math.sin(a), tmp;
      if (m.axis === 0) { tmp = y * c - z * sn; z = y * sn + z * c; y = tmp; }
      else if (m.axis === 1) { tmp = x * c + z * sn; z = -x * sn + z * c; x = tmp; }
      else { tmp = x * c - y * sn; y = x * sn + y * c; x = tmp; }
    }
    return [x, y, z, hit];
  }
  function modeRubik(n, s, o) {
    var cx = n / 2, cy = n / 2, R = n / 2 * 0.82;
    var proj = makeProj(s * 0.55, 0.35 + 0.1 * Math.sin(s * 0.9), cx, cy, R);
    var rs = radiusScale(n, o.rsPow == null ? 0.6 : o.rsPow);
    var moveCount = o.moveCount == null ? 14 : o.moveCount;
    var moves = rubikMoves(moveCount);
    var sched = rubikSchedule(s, moveCount, 0.42, 1.2);
    var dots = [];
    var latRings = o.latRings == null ? 15 : o.latRings;
    var lonDensity = o.lonDensity == null ? 40 : o.lonDensity;
    var i, j;
    for (i = 0; i <= latRings; i++) {
      var lat = -Math.PI / 2 + i / latRings * Math.PI;
      var cl = Math.cos(lat), sl = Math.sin(lat);
      var count = Math.max(1, Math.round(Math.abs(cl) * lonDensity));
      for (j = 0; j < count; j++) {
        var lon = j / count * 2 * Math.PI;
        var r3 = rubikApply([cl * Math.cos(lon), sl, cl * Math.sin(lon)], moves, sched);
        var p = proj(r3[0], r3[1], r3[2]);
        var depth = (p[2] + 1) / 2;
        dots.push({
          x: p[0], y: p[1], z: p[2],
          r: ((o.rBase == null ? 0.6 : o.rBase) + (o.rDepth == null ? 1.7 : o.rDepth) * depth
              + (r3[3] ? (o.rActive == null ? 0.3 : o.rActive) : 0)) * rs,
          white: (o.inkFar == null ? 0.62 : o.inkFar) - (o.inkSpan == null ? 0.54 : o.inkSpan) * depth - (r3[3] ? 0.14 : 0)
        });
      }
    }
    return finalize(dots, [], o.rMin);
  }

  // Globe (searching): a lattice sphere with a bright scanning meridian.
  function modeGlobe(n, s, o) {
    var cx = n / 2, cy = n / 2, R = n / 2 * 0.82;
    var tilt = 0.4 + 0.06 * Math.sin(s * 0.35);
    var proj = makeProj(s * 0.5, tilt, cx, cy, R);
    var scan = s * (0.5 + (1.7 - 0.5) * (o.scanMul == null ? 1 : o.scanMul));
    var rs = radiusScale(n, o.rsPow == null ? 0.6 : o.rsPow);
    var dimBase = o.dimBase == null ? 1 : o.dimBase;
    var dots = [];
    var latRings = o.latRings == null ? 17 : o.latRings;
    var lonDensity = o.lonDensity == null ? 44 : o.lonDensity;
    var i, j;
    for (i = 0; i <= latRings; i++) {
      var lat = -Math.PI / 2 + i / latRings * Math.PI;
      var cl = Math.cos(lat), sl = Math.sin(lat);
      var count = Math.max(1, Math.round(Math.abs(cl) * lonDensity));
      for (j = 0; j < count; j++) {
        var lon = j / count * 2 * Math.PI;
        var p = proj(cl * Math.cos(lon), sl, cl * Math.sin(lon));
        var depth = (p[2] + 1) / 2;
        var k = angDiff(lon + s * 0.5, scan);
        var glow = Math.exp(-(k * k) / 0.18) * Math.max(0, p[2]);
        dots.push({
          x: p[0], y: p[1], z: p[2],
          r: ((o.rBase == null ? 0.6 : o.rBase) + (o.rDepth == null ? 1.7 : o.rDepth) * depth
              + (o.rBoost == null ? 1 : o.rBoost) * glow) * rs,
          white: (o.inkFar == null ? 0.62 : o.inkFar) - (o.inkSpan == null ? 0.54 : o.inkSpan) * depth,
          a: dimBase + (1 - dimBase) * Math.min(1, glow)
        });
      }
    }
    return finalize(dots, [], o.rMin);
  }

  // Wave (listening): rings that swell in a travelling wave.
  function modeWave(n, s, o) {
    var cx = n / 2, cy = n / 2, R = n / 2 * 0.874;
    var proj = makeProj(s * 0.18, 0.38, cx, cy, 1);
    var rs = radiusScale(n, o.rsPow == null ? 0.6 : o.rsPow);
    var dots = [];
    var rings = o.rings == null ? 15 : o.rings;
    var lonDensity = o.lonDensity == null ? 40 : o.lonDensity;
    var i, j;
    for (i = 0; i <= rings; i++) {
      var lat = -Math.PI / 2 + i / rings * Math.PI;
      var cl = Math.cos(lat), sl = Math.sin(lat);
      var w = 0.62 * Math.sin(s * 2.1 - i * 0.52) + 0.38 * Math.sin(s * 1.27 + i * 0.83);
      var rr = R * (0.88 + 0.105 * w);
      var count = Math.max(1, Math.round(Math.abs(cl) * lonDensity));
      for (j = 0; j < count; j++) {
        var lon = j / count * 2 * Math.PI;
        var p = proj(cl * Math.cos(lon) * rr, sl * rr, cl * Math.sin(lon) * rr);
        var depth = (p[2] / R + 1) / 2;
        var lift = Math.max(0, w);
        dots.push({
          x: p[0], y: p[1], z: p[2],
          r: ((o.rBase == null ? 0.6 : o.rBase) + (o.rDepth == null ? 1.7 : o.rDepth) * depth) * (1 + 0.4 * lift) * rs,
          white: 0.66 - 0.56 * depth - 0.1 * lift
        });
      }
    }
    return finalize(dots, [], o.rMin);
  }

  // Morph (shaping): a dotted outline morphing circle -> triangle -> square.
  function polyline(pts) {
    var n = pts.length, lens = [], total = 0, i;
    for (i = 0; i < n; i++) {
      var a = pts[i], b = pts[(i + 1) % n];
      var L = hypot2(b[0] - a[0], b[1] - a[1]);
      lens.push(L); total += L;
    }
    return function (u) {
      var d = u * total, k = 0;
      while (d > lens[k] && k < n - 1) { d -= lens[k]; k++; }
      var p = pts[k], q = pts[(k + 1) % n];
      var f = lens[k] ? Math.min(1, d / lens[k]) : 0;
      return [p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f];
    };
  }
  var shapeCircle = function (u) {
    var a = -Math.PI / 2 + u * 2 * Math.PI;
    return [Math.cos(a) * 0.24, Math.sin(a) * 0.24];
  };
  var shapeTri = polyline([[0, -0.26], [0.24, 0.16], [-0.24, 0.16]]);
  var shapeSquare = polyline([[0, -0.2], [0.2, -0.2], [0.2, 0.2], [-0.2, 0.2], [-0.2, -0.2]]);
  var SHAPES = [shapeCircle, shapeTri, shapeSquare];
  var HOLD = 1.4, MORPH = 0.9, CYCLE = HOLD + MORPH;
  function modeMorph(n, s, o) {
    var cnt = SHAPES.length;
    var u = s % (CYCLE * cnt);
    var idx = Math.floor(u / CYCLE);
    var local = u - idx * CYCLE;
    var mix = local > HOLD ? smooth((local - HOLD) / MORPH) : 0;
    var spread = o.spread == null ? 1 : o.spread;
    var from = SHAPES[idx], to = SHAPES[(idx + 1) % cnt];
    var samples = 160, path = [], i;
    for (i = 0; i < samples; i++) {
      var g = i / samples, a = from(g), b = to(g);
      path.push([(a[0] + (b[0] - a[0]) * mix) * spread, (a[1] + (b[1] - a[1]) * mix) * spread]);
    }
    var lens = [], total = 0;
    for (i = 0; i < samples; i++) {
      var p = path[i], q = path[(i + 1) % samples];
      var L = hypot2(q[0] - p[0], q[1] - p[1]);
      lens.push(L); total += L;
    }
    var dotN = Math.max(6, Math.round(34 * (o.iconD == null ? 1 : o.iconD)));
    var rDot = (o.rDot == null ? 0.021 : o.rDot) * 1.35 * spread;
    var pulse = 1 + 0.02 * Math.sin(local * 3.1);
    var dots = [], half = n / 2, seg = 0, acc = 0;
    for (i = 0; i < dotN; i++) {
      var target = i / dotN * total;
      while (acc + lens[seg] < target && seg < samples - 1) { acc += lens[seg]; seg++; }
      var pa = path[seg], pb = path[(seg + 1) % samples];
      var f = lens[seg] ? Math.min(1, (target - acc) / lens[seg]) : 0;
      var X = (pa[0] + (pb[0] - pa[0]) * f) * pulse;
      var Y = (pa[1] + (pb[1] - pa[1]) * f) * pulse;
      dots.push({ x: half + X * n, y: half + Y * n, z: 0, r: Math.max(0.35, rDot * n), white: 0.1 });
    }
    return finalize(dots, [], o.rMin);
  }

  // Orbits (working): particles racing around tilted rings.
  function modeOrbits(n, s, o) {
    var cx = n / 2, cy = n / 2, R = n / 2 * 0.82;
    var proj = makeProj(s * 0.12, 0.3, cx, cy, 1);
    var rs = radiusScale(n, o.rsPow == null ? 0.6 : o.rsPow);
    var dots = [];
    var orbitN = o.orbitN == null ? 12 : o.orbitN;
    var ghostN = o.ghostN == null ? 40 : o.ghostN;
    var particles = o.particles == null ? 3 : o.particles;
    var i, j;
    for (i = 0; i < orbitN; i++) {
      var h1 = hash(i, 1.7), h2 = hash(i, 5.2), h3 = hash(i, 8.9);
      var rad = R * (0.45 + 0.52 * h1);
      var phi = h1 * 2 * Math.PI, theta = Math.acos(2 * h2 - 1);
      var nx = Math.sin(theta) * Math.cos(phi), ny = Math.cos(theta), nz = Math.sin(theta) * Math.sin(phi);
      // Two basis vectors in the orbit plane.
      var ux = -ny, uy = nx, uz = 0;
      var ul = Math.max(1e-6, Math.sqrt(ux * ux + uy * uy));
      ux /= ul; uy /= ul;
      var vx = ny * uz - nz * uy, vy = nz * ux - nx * uz, vz = nx * uy - ny * ux;
      var speed = (0.25 + 0.55 * h3) * (h3 > 0.5 ? 1 : -1);
      for (j = 0; j < ghostN; j++) {
        var a = j / ghostN * 2 * Math.PI;
        var p = proj((ux * Math.cos(a) + vx * Math.sin(a)) * rad,
                     (uy * Math.cos(a) + vy * Math.sin(a)) * rad,
                     (uz * Math.cos(a) + vz * Math.sin(a)) * rad);
        var d = (p[2] / rad + 1) / 2;
        dots.push({ x: p[0], y: p[1], z: p[2], r: (o.ghostR == null ? 0.9 : o.ghostR) * rs,
                    white: 0.72, a: (o.ghostA == null ? 0.5 : o.ghostA) * (0.4 + 0.6 * d) });
      }
      for (j = 0; j < particles; j++) {
        var b = s * speed + j / particles * 2 * Math.PI + h2 * 6;
        var q = proj((ux * Math.cos(b) + vx * Math.sin(b)) * rad,
                     (uy * Math.cos(b) + vy * Math.sin(b)) * rad,
                     (uz * Math.cos(b) + vz * Math.sin(b)) * rad);
        var dd = (q[2] / rad + 1) / 2;
        dots.push({ x: q[0], y: q[1], z: q[2],
                    r: ((o.partR == null ? 1.2 : o.partR) + (o.partRDepth == null ? 1.6 : o.partRDepth) * dd) * rs,
                    white: 0.3 - 0.22 * dd });
      }
    }
    return finalize(dots, [], o.rMin);
  }

  // Ribbon (composing) / Ring (breathing): undulating bands. 'faceOn' is
  // the ring variant — no camera tilt, undulation on the radius instead.
  function modeRibbon(n, s, o) {
    var cx = n / 2, cy = n / 2, R = n / 2 * 0.78;
    var spin = o.spin == null ? 1 : o.spin;
    var tilt = 0.3;
    var proj = makeProj(s * 0.1 * spin, tilt, cx, cy, 1);
    var rs = radiusScale(n, o.rsPow == null ? 0.6 : o.rsPow);
    var dots = [];
    var ghostN = o.ghostN == null ? 150 : o.ghostN;
    var i, j;
    for (i = 0; i < ghostN; i++) {
      var g = fib(i, ghostN);
      var p = proj(g[0] * R, g[1] * R, g[2] * R);
      var d = (p[2] / R + 1) / 2;
      dots.push({ x: p[0], y: p[1], z: p[2], r: 0.8 * rs, white: 0.78, a: 0.1 + 0.22 * d });
    }
    var yaw = s * 0.24 * spin;
    var lean = o.faceOn ? -tilt : 0.55 + 0.3 * Math.sin(s * 0.18) * spin;
    var cyw = Math.cos(yaw), syw = Math.sin(yaw);
    // Band frame: e1 = (cyw, 0, syw), e2 = (-syw*sin(lean), cos(lean), cyw*sin(lean)), e3 = e1 x e2
    var e1x = cyw, e1y = 0, e1z = syw;
    var e2x = -syw * Math.sin(lean), e2y = Math.cos(lean), e2z = cyw * Math.sin(lean);
    var e3x = e1y * e2z - e1z * e2y, e3y = e1z * e2x - e1x * e2z, e3z = e1x * e2y - e1y * e2x;
    var wob = 0.23 * (o.wobMul == null ? 1 : o.wobMul);
    var baseR = o.faceOn ? R / (1 + 0.85 * wob) : R;
    var lanes = o.lanes == null ? 5 : o.lanes;
    var segs = o.segs == null ? 88 : o.segs;
    var bands = Math.max(1, Math.round(lanes * (o.bandMul == null ? 1 : o.bandMul)));
    for (i = 0; i < bands; i++) {
      var off = (i - (bands - 1) / 2) * 0.075;
      var edge = Math.abs(i - (bands - 1) / 2) / Math.max(1, (bands - 1) / 2);
      for (j = 0; j < segs; j++) {
        var a = j / segs * 2 * Math.PI;
        var und = (0.16 * Math.sin(a * 3 - s * 1.7 + i * 0.22) + 0.07 * Math.sin(a * 5 + s * 1.1))
                  * (o.wobMul == null ? 1 : o.wobMul);
        var radMul = o.faceOn ? 1 + und : 1;
        var lift = o.faceOn ? off : off + und;
        var px = e1x * Math.cos(a) + e2x * Math.sin(a) + e3x * lift;
        var py = e1y * Math.cos(a) + e2y * Math.sin(a) + e3y * lift;
        var pz = e1z * Math.cos(a) + e2z * Math.sin(a) + e3z * lift;
        var len = Math.sqrt(px * px + py * py + pz * pz);
        var rr = baseR * radMul;
        var q = proj(px / len * rr, py / len * rr, pz / len * rr);
        var dd = (q[2] / R + 1) / 2;
        dots.push({
          x: q[0], y: q[1], z: q[2],
          r: ((o.rBase == null ? 1.1 : o.rBase) + (o.rDepth == null ? 1.7 : o.rDepth) * dd) * (1 - 0.25 * edge) * rs,
          white: 0.52 - 0.44 * dd + 0.18 * edge,
          a: 0.4 + 0.6 * dd
        });
      }
    }
    return finalize(dots, [], o.rMin);
  }

  // Web (connecting): drifting nodes joined by proximity, with signals
  // travelling along the links.
  function modeWeb(n, s, o) {
    var cx = n / 2, cy = n / 2, R = n / 2 * 0.8 * (o.spread == null ? 1 : o.spread);
    var proj = makeProj(s * 0.12, 0.32, cx, cy, R);
    var rs = radiusScale(n, o.rsPow == null ? 0.6 : o.rsPow);
    var nodeN = o.nodeN == null ? 30 : o.nodeN;
    var thr = o.thr == null ? 0.72 : o.thr;
    var nodeR = o.nodeR == null ? 1.4 : o.nodeR;
    var nodeRDepth = o.nodeRDepth == null ? 1.8 : o.nodeRDepth;
    var nodes = [], i, j;
    for (i = 0; i < nodeN; i++) {
      var f = fib(i, nodeN);
      var x = f[0] + 0.3 * (noise(i * 0.31 + 9, s * 0.24) - 0.5) * 2;
      var y = f[1] + 0.3 * (noise(i * 0.53 + 27, s * 0.21) - 0.5) * 2;
      var z = f[2] + 0.3 * (noise(i * 0.77 + 55, s * 0.27) - 0.5) * 2;
      var L = Math.sqrt(x * x + y * y + z * z);
      nodes.push([x / L, y / L, z / L]);
    }
    var lines = [], dots = [];
    for (i = 0; i < nodeN; i++) {
      for (j = i + 1; j < nodeN; j++) {
        var dx = nodes[i][0] - nodes[j][0], dy = nodes[i][1] - nodes[j][1], dz = nodes[i][2] - nodes[j][2];
        var dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
        if (dist >= thr) continue;
        var a = proj(nodes[i][0], nodes[i][1], nodes[i][2]);
        var b = proj(nodes[j][0], nodes[j][1], nodes[j][2]);
        var zm = ((a[2] + b[2]) / 2 + 1) / 2;
        lines.push({ x1: a[0], y1: a[1], x2: b[0], y2: b[1], white: 0.42,
                     a: (1 - dist / thr) * (0.3 + 0.55 * zm),
                     w: Math.max(0.6, (o.lineW == null ? 0.8 : o.lineW) * rs) });
      }
    }
    for (i = 0; i < nodeN; i++) {
      var p = proj(nodes[i][0], nodes[i][1], nodes[i][2]);
      var d = (p[2] + 1) / 2;
      var pulse = 1 + 0.25 * Math.sin(s * 1.4 + i * 2.7);
      dots.push({ x: p[0], y: p[1], z: p[2], r: (nodeR + nodeRDepth * d) * pulse * rs, white: 0.55 - 0.45 * d });
    }
    var signals = o.signals == null ? 5 : o.signals;
    for (i = 0; i < signals; i++) {
      var tick = Math.floor(s * 0.55 + i * 7.31);
      var from = Math.floor(hash(tick, i * 3.1 + 1.7) * nodeN);
      var to = Math.floor(hash(tick, i * 5.7 + 4.2) * nodeN);
      if (from === to) continue;
      var u = fract(s * 0.55 + i * 7.31);
      var sx = lerp(nodes[from][0], nodes[to][0], u);
      var sy = lerp(nodes[from][1], nodes[to][1], u);
      var sz = lerp(nodes[from][2], nodes[to][2], u);
      var sl = Math.max(1e-6, Math.sqrt(sx * sx + sy * sy + sz * sz));
      var q = proj(sx / sl, sy / sl, sz / sl);
      var qd = (q[2] + 1) / 2;
      dots.push({ x: q[0], y: q[1], z: q[2], r: (nodeR * 1.5 + nodeRDepth * qd) * rs, white: 0.05, a: 0.5 + 0.5 * qd });
    }
    return finalize(dots, lines, o.rMin);
  }

  var MODE_FRAMES = {
    orbits: modeOrbits, globe: modeGlobe, rubik: modeRubik, wave: modeWave,
    web: modeWeb, braid: modeBraid, ribbon: modeRibbon, ring: modeRibbon, morph: modeMorph
  };

  // ── Presets ─────────────────────────────────────────────────────────────

  var BASE = {
    globe:  { latRings: 17, lonDensity: 44, rBase: 0.6, rDepth: 1.7, rBoost: 1, inkFar: 0.62, inkSpan: 0.54, rsPow: 0.6, rMin: 0.3 },
    orbits: { orbitN: 12, ghostN: 40, ghostR: 0.9, ghostA: 0.5, particles: 3, partR: 1.2, partRDepth: 1.6, rsPow: 0.6, rMin: 0.3 },
    rubik:  { latRings: 15, lonDensity: 40, moveCount: 14, rBase: 0.6, rDepth: 1.7, rActive: 0.3, inkFar: 0.62, inkSpan: 0.54, rsPow: 0.6, rMin: 0.3 },
    wave:   { rings: 15, lonDensity: 40, rBase: 0.6, rDepth: 1.7, rsPow: 0.6, rMin: 0.3 },
    web:    { nodeN: 30, thr: 0.72, signals: 5, nodeR: 1.4, nodeRDepth: 1.8, lineW: 0.8, rsPow: 0.6, rMin: 0.3 },
    braid:  { strandN: 52, turns: 3, ghostN: 150, rBase: 1.2, rDepth: 1.8, rsPow: 0.6, rMin: 0.3 },
    ribbon: { lanes: 5, segs: 88, ghostN: 150, rBase: 1.1, rDepth: 1.7, rsPow: 0.6, rMin: 0.3 },
    ring:   { lanes: 5, segs: 88, ghostN: 0, faceOn: 1, rBase: 1.1, rDepth: 1.7, rsPow: 0.6, rMin: 0.3 },
    morph:  { rDot: 0.021, iconD: 1, rMin: 0.25 }
  };

  var STATE_TO_MODE = {
    working: 'orbits', searching: 'globe', solving: 'rubik', listening: 'wave',
    connecting: 'web', weaving: 'braid', composing: 'ribbon', breathing: 'ring', shaping: 'morph'
  };

  // Two tunings: "64" for chat-avatar scale, "20" for inline-text scale.
  var PRESETS = {
    orbits: { 64: { speed: 1.885, count: 1, size: 1 },
              20: { speed: 3.9, count: 0.238, size: 2.4 } },
    globe:  { 64: { speed: 2.015, count: 0.42, size: 1.15, extra: { scanMul: 4.08, dimBase: 0.45 } },
              20: { speed: 2.665, count: 0.105, size: 1.75, extra: { scanMul: 4.335, dimBase: 0.45 } } },
    rubik:  { 64: { speed: 1.82, count: 0.35, size: 1.05 },
              20: { speed: 1.95, count: 0.088, size: 1.9 } },
    wave:   { 64: { speed: 4.388, count: 0.341, size: 1 },
              20: { speed: 3.998, count: 0.105, size: 1.6 } },
    web:    { 64: { speed: 3.315, count: 1.35, size: 0.95 },
              20: { speed: 6.63, count: 0.25, size: 1.52 } },
    braid:  { 64: { speed: 1.625, count: 0.5, size: 1 },
              20: { speed: 2.75, count: 0.1125, size: 1.36 } },
    ribbon: { 64: { speed: 2.34, count: 0.25, size: 0.85, extra: { spin: 0, bandMul: 3.9, wobMul: 1 } },
              20: { speed: 3.12, count: 0.051, size: 1.073, extra: { spin: 0, bandMul: 4.94, wobMul: 1 } } },
    ring:   { 64: { speed: 3.24, count: 0.25, size: 0.956, extra: { spin: 0, bandMul: 3.627, wobMul: 0.368 } },
              20: { speed: 3.78, count: 0.028, size: 1.622, extra: { spin: 0, bandMul: 3.968, wobMul: 0.565 } } },
    morph:  { 64: { speed: 2.405, count: 0.702, size: 0.395, extra: { spread: 1.45 } },
              20: { speed: 2.08, count: 0.53, size: 1.011, extra: { spread: 1.45 } } }
  };

  var PAIRED = [['latRings', 'lonDensity'], ['rings', 'lonDensity'], ['lanes', 'segs']];
  var COUNTS = ['orbitN', 'ghostN', 'nodeN', 'strandN', 'signals'];
  var ICONS = ['iconD'];
  var RADII = ['rBase', 'rDepth', 'rActive', 'rDot', 'ghostR', 'partR', 'partRDepth', 'nodeR', 'nodeRDepth'];

  function copy(o) { var r = {}, k; for (k in o) if (o.hasOwnProperty(k)) r[k] = o[k]; return r; }

  function scaleCounts(opts, scale) {
    var t = copy(opts), done = {}, sq = Math.sqrt(scale), i, k;
    for (i = 0; i < PAIRED.length; i++) {
      var a = PAIRED[i][0], b = PAIRED[i][1];
      if (t[a] != null && t[b] != null && !done[a] && !done[b]) {
        t[a] = Math.max(2, Math.round(t[a] * sq));
        t[b] = Math.max(2, Math.round(t[b] * sq));
        done[a] = true; done[b] = true;
      }
    }
    for (i = 0; i < COUNTS.length; i++) {
      k = COUNTS[i];
      if (t[k] != null && t[k] !== 0 && !done[k]) t[k] = Math.max(1, Math.round(t[k] * scale));
    }
    for (i = 0; i < ICONS.length; i++) {
      k = ICONS[i];
      if (t[k] != null) t[k] = Math.max(0.02, t[k] * scale);
    }
    return t;
  }
  function scaleRadii(opts, scale) {
    var t = copy(opts), i;
    for (i = 0; i < RADII.length; i++) if (t[RADII[i]] != null) t[RADII[i]] = t[RADII[i]] * scale;
    t.rSizeMul = (t.rSizeMul == null ? 1 : t.rSizeMul) * scale;
    return t;
  }

  var presetCache = {};
  function resolvePreset(state, sizeKey) {
    var key = state + '-' + sizeKey;
    if (presetCache[key]) return presetCache[key];
    var mode = STATE_TO_MODE[state] || 'orbits';
    var p = PRESETS[mode][sizeKey];
    var opts = copy(BASE[mode]);
    if (p.count !== 1) opts = scaleCounts(opts, p.count);
    if (p.size !== 1) opts = scaleRadii(opts, p.size);
    if (p.extra) { var k; for (k in p.extra) if (p.extra.hasOwnProperty(k)) opts[k] = p.extra[k]; }
    var resolved = { mode: mode, speed: p.speed, opts: opts };
    presetCache[key] = resolved;
    return resolved;
  }

  // ── Painting ────────────────────────────────────────────────────────────
  // The reference paints grayscale. We paint an ember ramp: 'white' (0..1,
  // higher = fainter) becomes a blend from a muted warm grey out to ember.

  var RAMP = {
    dark:  { near: [232, 149, 106], far: [107, 90, 82] },   // ember2 -> warm grey
    light: { near: [200, 75, 47],   far: [184, 173, 164] }  // ember  -> warm light grey
  };
  function inkFor(white, dark) {
    var w = Math.min(1, Math.max(0, white));
    var strength = 1 - w;
    var ramp = dark ? RAMP.dark : RAMP.light;
    var r = Math.round(ramp.far[0] + (ramp.near[0] - ramp.far[0]) * strength);
    var g = Math.round(ramp.far[1] + (ramp.near[1] - ramp.far[1]) * strength);
    var b = Math.round(ramp.far[2] + (ramp.near[2] - ramp.far[2]) * strength);
    return [r, g, b];
  }
  function paint(ctx, frame, dark) {
    var i, c;
    for (i = 0; i < frame.lines.length; i++) {
      var l = frame.lines[i];
      c = inkFor(l.white, dark);
      ctx.strokeStyle = 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + ((l.a == null) ? 1 : l.a) + ')';
      ctx.lineWidth = l.w;
      ctx.beginPath(); ctx.moveTo(l.x1, l.y1); ctx.lineTo(l.x2, l.y2); ctx.stroke();
    }
    for (i = 0; i < frame.dots.length; i++) {
      var d = frame.dots[i];
      c = inkFor(d.white, dark);
      ctx.fillStyle = 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + ((d.a == null) ? 1 : d.a) + ')';
      ctx.beginPath(); ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2); ctx.fill();
    }
  }

  // ── Theme + motion preferences ──────────────────────────────────────────

  function resolveDark(el) {
    var e = el;
    while (e && e.getAttribute) {
      var t = e.getAttribute('data-theme');
      if (t === 'dark') return true;
      if (t === 'light') return false;
      e = e.parentNode;
    }
    if (typeof matchMedia !== 'undefined') return matchMedia('(prefers-color-scheme: dark)').matches;
    return true;
  }
  function reducedMotion() {
    return typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  // ── Public API ──────────────────────────────────────────────────────────

  function mount(canvas, state, options) {
    var opts = options || {};
    var size = opts.size || 64;
    var speedMul = opts.speed || 1;
    var dpr = Math.min(2, (typeof devicePixelRatio !== 'undefined' && devicePixelRatio) || 1);
    canvas.width = Math.round(size * dpr);
    canvas.height = Math.round(size * dpr);
    canvas.style.width = size + 'px';
    canvas.style.height = size + 'px';
    canvas.style.display = 'block';
    var ctx = canvas.getContext('2d');
    var handle = {
      canvas: canvas, ctx: ctx, size: size, dpr: dpr, speedMul: speedMul,
      state: null, resolved: null, raf: 0, running: false, paused: !!opts.paused,
      dark: (opts.dark == null) ? resolveDark(canvas) : !!opts.dark,
      reduced: reducedMotion()
    };
    setState(handle, state || 'breathing');
    if (!handle.reduced && !handle.paused) start(handle);
    handle._vis = function () {
      if (document.visibilityState === 'hidden') stop(handle); else if (!handle.paused) start(handle);
    };
    document.addEventListener('visibilitychange', handle._vis);
    return handle;
  }

  function draw(handle, t) {
    var ctx = handle.ctx, size = handle.size;
    ctx.setTransform(handle.dpr, 0, 0, handle.dpr, 0, 0);
    ctx.clearRect(0, 0, size, size);
    var frame = MODE_FRAMES[handle.resolved.mode](size, t, handle.resolved.opts);
    paint(ctx, frame, handle.dark);
  }

  function start(handle) {
    if (handle.running || handle.reduced) return;
    handle.running = true;
    var tick = function () {
      if (!handle.running) return;
      draw(handle, performance.now() / 1000 * handle.resolved.speed * handle.speedMul);
      handle.raf = requestAnimationFrame(tick);
    };
    handle.raf = requestAnimationFrame(tick);
  }

  function stop(handle) {
    handle.running = false;
    if (handle.raf) cancelAnimationFrame(handle.raf);
    handle.raf = 0;
  }

  function setState(handle, state) {
    if (!STATE_TO_MODE[state]) state = 'breathing';
    if (handle.state === state) return;
    handle.state = state;
    handle.resolved = resolvePreset(state, handle.size >= 40 ? 64 : 20);
    // Reduced motion gets one still frame of the new state rather than
    // nothing — the state change is still information.
    if (handle.reduced) draw(handle, 0.6);
    else if (!handle.running && !handle.paused) start(handle);
  }

  function setTheme(handle, dark) {
    handle.dark = !!dark;
    if (handle.reduced) draw(handle, 0.6);
  }

  function destroy(handle) {
    stop(handle);
    if (handle._vis) document.removeEventListener('visibilitychange', handle._vis);
  }

  global.CavnarOrb = {
    mount: mount, setState: setState, setTheme: setTheme, stop: stop, start: start,
    destroy: destroy, resolvePreset: resolvePreset, MODE_FRAMES: MODE_FRAMES,
    STATE_TO_MODE: STATE_TO_MODE, STATES: ['connecting', 'solving', 'searching', 'working',
                                           'shaping', 'composing', 'breathing', 'listening', 'weaving']
  };
})(typeof window !== 'undefined' ? window : this);
