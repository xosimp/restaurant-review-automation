/* cavnar.ai — the marketing site's one script (redesign 10/6/26).
   Every animation here explains something: the schedule building itself,
   modules lighting up as data moves between them, the learning curve. All
   motion is transform/opacity (or a canvas), paused off screen, and off
   entirely for reduced motion. No library. */
(function () {
  'use strict';
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  function onView(el, fn, th) {
    if (!el) return;
    if (reduce || !('IntersectionObserver' in window)) { fn(); return; }
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { fn(); io.disconnect(); } });
    }, { threshold: th || 0.3 });
    io.observe(el);
  }
  function whileVisible(el, start, stop) {
    if (!el || !('IntersectionObserver' in window)) { start(); return; }
    new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) start(); else stop(); });
    }, { threshold: 0 }).observe(el);
  }
  function countTo(el, to, dec, ms) {
    if (!el) return;
    dec = dec || 0; ms = ms || 1400;
    if (reduce) { el.textContent = to.toFixed(dec); return; }
    var t0 = performance.now();
    (function step(t) {
      var p = Math.min(1, (t - t0) / ms), e = 1 - Math.pow(1 - p, 3);
      el.textContent = (to * e).toFixed(dec);
      if (p < 1) requestAnimationFrame(step);
    })(t0);
  }

  // ── nav: solid once the page moves; a menu on phones ──
  var nav = $('.nav');
  if (nav) {
    var solid = function () { nav.classList.toggle('solid', window.scrollY > 12); };
    solid(); window.addEventListener('scroll', solid, { passive: true });
    var menu = $('.menu', nav);
    if (menu) menu.addEventListener('click', function () {
      var open = nav.classList.toggle('open'); menu.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    $$('.links a', nav).forEach(function (a) { a.addEventListener('click', function () { nav.classList.remove('open'); }); });
  }

  // ── staggered reveals, once ──
  $$('.rv').forEach(function (el) { onView(el, function () { el.classList.add('in'); }, 0.12); });

  // ── parallax: the hero's rings and glow drift slower than the page,
  //    so the core reads as depth behind the headline; the demo's glow
  //    rises into view. Only while those sections are on screen. ──
  var layers = $$('[data-par]');
  if (layers.length && !reduce) {
    var ticking = false;
    var paint = function () {
      ticking = false;
      layers.forEach(function (el) {
        var host = el.closest('section') || el, r = host.getBoundingClientRect();
        if (r.bottom < -200 || r.top > window.innerHeight + 200) return;
        var f = parseFloat(el.getAttribute('data-par')) || 0;
        el.style.transform = 'translate3d(0,' + (-r.top * f).toFixed(1) + 'px,0)';
      });
    };
    window.addEventListener('scroll', function () { if (!ticking) { ticking = true; requestAnimationFrame(paint); } }, { passive: true });
    paint();
  }

  // ── motes: data drifting into the core ──
  function motes(canvas, opts) {
    if (!canvas || reduce || !canvas.getContext) return;
    var ctx = canvas.getContext('2d'), dpr = Math.min(2, window.devicePixelRatio || 1), W = 0, H = 0, run = false, raf = 0, list = [];
    function size() {
      var r = canvas.getBoundingClientRect(); W = r.width; H = r.height;
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    function spawn(m, fresh) {
      var a = Math.random() * Math.PI * 2, c = opts.center(W, H), d = opts.radius(W, H) * (0.55 + Math.random() * 0.6);
      m.x = c[0] + Math.cos(a) * d; m.y = c[1] + Math.sin(a) * d * 0.75;
      m.v = 0.12 + Math.random() * 0.22; m.r = 0.6 + Math.random() * 1.5; m.t = fresh ? Math.random() : 0; m.swirl = (Math.random() - 0.5) * 0.6;
      return m;
    }
    for (var i = 0; i < opts.count; i++) list.push(spawn({}, true));
    function frame() {
      ctx.clearRect(0, 0, W, H);
      var c = opts.center(W, H);
      for (var i = 0; i < list.length; i++) {
        var m = list[i], dx = c[0] - m.x, dy = c[1] - m.y, d = Math.sqrt(dx * dx + dy * dy) || 1;
        m.x += (dx / d) * m.v + (-dy / d) * m.swirl * m.v; m.y += (dy / d) * m.v + (dx / d) * m.swirl * m.v;
        m.t = Math.min(1, m.t + 0.006);
        var near = Math.min(1, d / opts.absorb), a = m.t * near * opts.alpha;
        if (d < opts.absorb * 0.25) { spawn(m, false); continue; }
        ctx.beginPath(); ctx.arc(m.x, m.y, m.r, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(' + opts.rgb + ',' + a.toFixed(3) + ')'; ctx.fill();
      }
      raf = requestAnimationFrame(frame);
    }
    size(); window.addEventListener('resize', size);
    whileVisible(canvas, function () { if (!run) { run = true; raf = requestAnimationFrame(frame); } },
                         function () { run = false; cancelAnimationFrame(raf); });
    document.addEventListener('visibilitychange', function () { if (document.hidden) { run = false; cancelAnimationFrame(raf); } });
  }
  motes($('#motes'), {
    count: window.innerWidth < 700 ? 26 : 48, rgb: '232,149,106', alpha: 0.55,
    center: function (W, H) { var o = $('.orb'); if (!o) return [W / 2, H * 0.3]; var r = o.getBoundingClientRect(), h = $('#motes').getBoundingClientRect(); return [r.left - h.left + r.width / 2, r.top - h.top + r.height / 2]; },
    radius: function (W, H) { return Math.max(W, H) * 0.55; }, absorb: 260
  });

  // ── the problem: four silos join once read ──
  var silos = $('#silos');
  onView(silos, function () { setTimeout(function () { silos.classList.add('joined'); }, reduce ? 0 : 1300); }, 0.5);

  // ── the Schedule Generator builds a week ──
  var g = $('#grid7');
  if (g) {
    var people = ['Dana R.', 'Marcus L.', 'Priya S.', 'Tom W.', 'Alex K.', 'Jordan P.', 'Sam T.', 'Riley C.'];
    var days = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];
    var plan = [[1,1,1,0,1,1,1],[1,0,1,1,1,1,0],[1,1,0,1,1,1,1],[0,1,1,1,1,1,0],[1,1,0,0,1,1,1],[1,0,1,1,0,1,1],[1,1,1,1,1,1,1],[0,1,1,0,1,1,1]];
    var times = ['10a–4p', '4p–11p', '11a–5p', '5p–10p', '9a–3p', '3p–11p'];
    var h = '<div></div>', r, c;
    for (c = 0; c < 7; c++) h += '<div class="h">' + days[c] + '</div>';
    for (r = 0; r < people.length; r++) {
      h += '<div class="p">' + people[r] + '</div>';
      for (c = 0; c < 7; c++) h += plan[r][c] ? '<div class="c s" data-r="' + r + '" data-c="' + c + '">' + times[(r + c) % times.length] + '</div>' : '<div class="c"></div>';
    }
    g.innerHTML = h;
    var steps = $$('#steps div'), lit = function (i) { if (steps[i]) steps[i].classList.add('on'); };
    onView($('#board'), function () {
      var cells = $$('.s', g), gap = reduce ? 0 : 36, t = reduce ? 0 : 160 + cells.length * gap;
      lit(0);
      cells.forEach(function (el, i) { setTimeout(function () { el.classList.add('in'); }, reduce ? 0 : 160 + i * gap); });
      setTimeout(function () { lit(1); }, t * 0.5);
      // Sam works seven days: the weekend shifts break 40h, flag, and move.
      var ot = $$('.s[data-r="6"][data-c="5"], .s[data-r="6"][data-c="6"]', g);
      setTimeout(function () { ot.forEach(function (e) { e.classList.add('ot'); }); }, t + (reduce ? 0 : 200));
      setTimeout(function () {
        lit(2); ot.forEach(function (e) { e.classList.remove('ot'); e.classList.add('moved'); e.textContent = 'moved'; });
        $('#otv').textContent = '0'; $('#ots').textContent = 'Nobody past 40h';
      }, t + (reduce ? 0 : 1200));
      setTimeout(function () { lit(3); $('#labbar').style.transform = 'scaleX(' + (31.4 / 50) + ')'; countTo($('#labv'), 31.4, 1); }, t + (reduce ? 0 : 1650));
      setTimeout(function () { lit(4); $('#ringfg').style.strokeDashoffset = String(151 * (1 - 0.86)); countTo($('#sqv'), 86, 0); }, t + (reduce ? 0 : 2250));
      setTimeout(function () { lit(5); }, t + (reduce ? 0 : 3050));
    }, 0.35);
  }

  // ── everything connected ──
  var stage = $('#stage');
  if (stage) {
    var mods = ['Schedule Generator', 'Labor', 'Food Cost', 'Marketing', 'Reviews', 'Intel', 'AI CFO', 'Daily Sales Reports', 'Morning Brief', 'Ask Cavnar AI', 'Forecasting', 'Events'];
    var pos = {}, cx = 300, cy = 300, R = 236, nh = '', lh = '';
    mods.forEach(function (m, i) {
      var a = -Math.PI / 2 + i * (2 * Math.PI / mods.length);
      pos[m] = [cx + R * Math.cos(a), cy + R * Math.sin(a)];
      nh += '<div class="node" data-m="' + m + '" style="left:' + (pos[m][0] / 6) + '%;top:' + (pos[m][1] / 6) + '%"><i></i><span>' + m + '</span></div>';
    });
    $('#nodes').innerHTML = nh;
    var chains = [
      { t: 'A Bears game is on the calendar', s: ['Events', 'Forecasting', 'Schedule Generator', 'Marketing'], w: ['Bears game detected', 'Forecast adjusts', 'Schedule staffs up', 'Game-day promotion drafted'] },
      { t: 'A campaign works', s: ['Marketing', 'Daily Sales Reports', 'AI CFO', 'Morning Brief'], w: ['Campaign sent', 'Sales measured that night', 'CFO measures the impact', 'Tomorrow’s brief says so'] },
      { t: 'Labor starts drifting over target', s: ['Labor', 'Schedule Generator', 'AI CFO', 'Ask Cavnar AI'], w: ['Labor trends worse', 'Staffing change recommended', 'Cost measured', 'Ask Cavnar AI explains why'] },
      { t: 'A guest flags slow service', s: ['Reviews', 'Labor', 'Schedule Generator', 'Morning Brief'], w: ['Slow service flagged', 'That night’s staffing checked', 'Next week staffed for it', 'The brief tells the GM'] }
    ];
    var curve = function (a, b) {
      var p = pos[a], q = pos[b], mx = (p[0] + q[0]) / 2, my = (p[1] + q[1]) / 2, k = 0.38;
      return 'M' + p[0] + ' ' + p[1] + ' Q' + (mx + (cx - mx) * k) + ' ' + (my + (cy - my) * k) + ' ' + q[0] + ' ' + q[1];
    };
    lh += '<circle class="orbitring" cx="300" cy="300" r="236"/>';
    mods.forEach(function (m) { lh += '<path class="lnk spoke" d="M' + cx + ' ' + cy + ' L' + pos[m][0] + ' ' + pos[m][1] + '"/>'; });
    chains.forEach(function (ch, ci) { for (var i = 0; i < ch.s.length - 1; i++) lh += '<path class="lnk" data-ch="' + ci + '" data-i="' + i + '" d="' + curve(ch.s[i], ch.s[i + 1]) + '"/>'; });
    $('#links').innerHTML = lh;
    var box = $('#chains'), bh = '';
    chains.forEach(function (ch, ci) { bh += '<button type="button" class="chain" data-ci="' + ci + '"><span class="ttl">' + ch.t + '</span><ol>' + ch.w.map(function (w) { return '<li>' + w + '</li>'; }).join('') + '</ol></button>'; });
    box.innerHTML = bh;
    var pulse = $('#pulse'), cur = -1, timers = [], auto = null;
    var clear = function () {
      timers.forEach(clearTimeout); timers = [];
      $$('.lnk.lit, .node.on, .chain li.lit').forEach(function (e) { e.classList.remove('lit'); e.classList.remove('on'); });
      pulse.style.opacity = 0;
    };
    var travel = function (path, ms) {
      if (reduce) return;
      var L = path.getTotalLength(), t0 = performance.now();
      pulse.style.opacity = 1;
      (function step(t) {
        var p = Math.min(1, (t - t0) / ms), e = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2, pt = path.getPointAtLength(L * e);
        pulse.setAttribute('cx', pt.x); pulse.setAttribute('cy', pt.y);
        if (p < 1) requestAnimationFrame(step);
      })(t0);
    };
    var play = function (ci) {
      clear(); cur = ci;
      $$('.chain', box).forEach(function (b) { var on = +b.getAttribute('data-ci') === ci; b.classList.toggle('on', on); b.setAttribute('aria-pressed', on ? 'true' : 'false'); });
      var ch = chains[ci], lis = $$('.chain[data-ci="' + ci + '"] li', box), step = reduce ? 0 : 1100;
      ch.s.forEach(function (m, i) {
        timers.push(setTimeout(function () {
          var n = $('.node[data-m="' + m + '"]'); if (n) n.classList.add('on'); if (lis[i]) lis[i].classList.add('lit');
          if (i < ch.s.length - 1) { var p = $('.lnk[data-ch="' + ci + '"][data-i="' + i + '"]'); if (p) { p.classList.add('lit'); travel(p, 900); } }
        }, i * step));
      });
    };
    box.addEventListener('click', function (e) { var b = e.target.closest('.chain'); if (!b) return; clearInterval(auto); auto = null; play(+b.getAttribute('data-ci')); });
    onView(stage, function () { play(0); if (!reduce) auto = setInterval(function () { play((cur + 1) % chains.length); }, 6400); }, 0.3);
  }

  // ── it learns: the curve draws as the things it learns light up ──
  var chart = $('#chart');
  if (chart) {
    var q = [64, 69, 73, 77, 80, 83, 85, 87], ed = [31, 24, 19, 14, 11, 8, 6, 4];
    var X = function (i) { return 50 + i * 63; }, Y = function (v) { return 180 - (v - 60) / 30 * 140; };
    var d = q.map(function (v, i) { return (i ? 'L' : 'M') + X(i) + ' ' + Y(v); }).join(' ');
    $('#ln').setAttribute('d', d);
    $('#ar').setAttribute('d', d + ' L' + X(7) + ' 180 L' + X(0) + ' 180 Z');
    var eh = '', wk = '';
    ed.forEach(function (v, i) {
      eh += '<rect x="' + (X(i) - 9) + '" y="' + (226 - v * 1.3) + '" width="18" height="' + (v * 1.3) + '" rx="3" style="transition-delay:' + (i * 0.12) + 's"/>';
      wk += '<text x="' + (X(i) - 9) + '" y="246">W' + (i + 1) + '</text>';
    });
    $('#edits').innerHTML = eh; $('#wk').innerHTML = wk;
    $('#lastdot').setAttribute('cx', X(7)); $('#lastdot').setAttribute('cy', Y(87));
    onView(chart, function () {
      $('#ln').style.strokeDashoffset = '0'; $('#ar').style.opacity = '1'; $('#lastdot').style.opacity = '1';
      $$('#edits rect').forEach(function (r) { r.style.transform = 'scaleY(1)'; });
      $$('#learns span').forEach(function (s, i) { setTimeout(function () { s.classList.add('on'); }, reduce ? 0 : 250 + i * 260); });
    }, 0.35);
    // what it learns, flowing into the curve
    motes($('#learnmotes'), {
      count: 18, rgb: '246,195,155', alpha: 0.5,
      center: function (W, H) { return [W * 0.88, H * 0.3]; }, radius: function (W, H) { return W * 0.9; }, absorb: 120
    });
  }

  // ── results count up from their own account ──
  var kp = $('#kpis');
  onView(kp, function () { $$('b[data-to]', kp).forEach(function (b) { countTo(b, +b.getAttribute('data-to'), 0, 1300); }); }, 0.4);

  // ── the demo request (Formspree, as before) ──
  var form = $('#demoForm');
  if (form) form.addEventListener('submit', function (e) {
    e.preventDefault();
    var btn = $('button[type=submit]', form), ok = $('.ok', form), err = $('.err', form);
    btn.disabled = true; btn.textContent = 'Sending…'; err.style.display = 'none';
    fetch('https://formspree.io/f/xbdwzydw', { method: 'POST', body: new FormData(form), headers: { Accept: 'application/json' } })
      .then(function (res) {
        if (!res.ok) throw new Error('send');
        ok.style.display = 'block'; btn.style.display = 'none';
      })
      .catch(function () { btn.disabled = false; btn.textContent = 'Request a demo'; err.style.display = 'block'; });
  });
})();
