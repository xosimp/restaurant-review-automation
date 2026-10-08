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

  // ── a light haptic tap on phones. Android: the Vibration API. iOS
  //    (Safari 18+): toggling a native switch input taps the Taptic Engine,
  //    the only haptic a web page can reach there; on anything else it's a
  //    no-op. ──
  var tapSwitch = null;
  function haptic() {
    try {
      if (navigator.vibrate) { navigator.vibrate(8); return; }
      if (!tapSwitch) {
        var lab = document.createElement('label'), inp = document.createElement('input');
        inp.type = 'checkbox'; inp.setAttribute('switch', ''); inp.tabIndex = -1;
        lab.setAttribute('aria-hidden', 'true');
        lab.style.cssText = 'position:fixed;left:-9999px;top:0;width:1px;height:1px;overflow:hidden;opacity:0;pointer-events:none';
        lab.appendChild(inp); document.body.appendChild(lab); tapSwitch = lab;
      }
      tapSwitch.click();
    } catch (e) { /* no haptics here */ }
  }

  // ── nav: solid once the page moves; a menu on phones ──
  var nav = $('.nav');
  if (nav) {
    var solid = function () { nav.classList.toggle('solid', window.scrollY > 12); };
    solid(); window.addEventListener('scroll', solid, { passive: true });
    var menu = $('.menu', nav);
    if (menu) menu.addEventListener('click', function () {
      haptic();
      var open = nav.classList.toggle('open'); menu.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    $$('.links a', nav).forEach(function (a) { a.addEventListener('click', function () { if (nav.classList.contains('open')) haptic(); nav.classList.remove('open'); }); });
  }

  // ── staggered reveals, once ──
  $$('.rv').forEach(function (el) { onView(el, function () { el.classList.add('shown'); }, 0.12); });

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
      for (c = 0; c < 7; c++) h += plan[r][c] ? '<div class="c s" data-r="' + r + '" data-c="' + c + '" data-t="' + times[(r + c) % times.length] + '">' + times[(r + c) % times.length] + '</div>' : '<div class="c"></div>';
    }
    g.innerHTML = h;
    var steps = $$('#steps div'), lit = function (i) { if (steps[i]) steps[i].classList.add('on'); };
    // The week builds as you scroll (10/8/26): pinned on a desktop tall
    // enough to hold it, the scroll scrubs the draft — cells fill, two
    // overtime shifts flag and move, labor lands on target, the score rises —
    // and scrolling back unbuilds it. On a phone it scrubs as the board
    // crosses the screen. Reduced motion shows the finished week.
    var cells = $$('.s', g), ot = $$('.s[data-r="6"][data-c="5"], .s[data-r="6"][data-c="6"]', g);
    var sec = $('#schedule'), board = $('#board'), last = -1, clamp = function (x) { return Math.max(0, Math.min(1, x)); };
    var marks = [0.001, 0.3, 0.62, 0.78, 0.88, 0.97];
    var apply = function (p) {
      if (Math.abs(p - last) < 0.002) { lightSteps(p); return; }
      last = p;
      var n = Math.round(clamp(p / 0.55) * cells.length);
      cells.forEach(function (el, i) { el.classList.toggle('shown', i < n); });
      var flagged = p >= 0.6, moved = p >= 0.7;
      ot.forEach(function (e) { e.classList.toggle('ot', flagged && !moved); e.classList.toggle('moved', moved); e.textContent = moved ? 'moved' : e.getAttribute('data-t'); });
      $('#otv').textContent = moved ? '0' : '6.5';
      $('#ots').textContent = moved ? 'Nobody past 40h' : '2 shifts over 40h';
      var lab = clamp((p - 0.74) / 0.12), sq = clamp((p - 0.84) / 0.12);
      $('#labbar').style.transform = 'scaleX(' + (31.4 / 50 * lab) + ')';
      $('#labv').textContent = (31.4 * lab).toFixed(1);
      $('#ringfg').style.strokeDashoffset = String(151 * (1 - 0.86 * sq));
      $('#sqv').textContent = String(Math.round(86 * sq));
      lightSteps(p);
    };
    // Pinned (a desktop), the steps keep time with the build beside them.
    // Unpinned (a phone) the list sits above the board and would still be
    // waiting on it after scrolling away (10/8/26): there each step lights as
    // it reaches the lower part of the screen, in order, as it is read.
    var lightSteps = function (p) {
      var pinnedNow = sec.classList.contains('pinned'), line = window.innerHeight * 0.78;
      steps.forEach(function (st, i) {
        var on = reduce || pinnedNow ? p >= marks[i] : (i === 0 || steps[i - 1].classList.contains('on')) && st.getBoundingClientRect().top < line;
        st.classList.toggle('on', on);
      });
    };
    // pin only when the whole section fits under the nav (a short laptop
    // screen would clip its first and last lines); else scrub in place
    var wide = window.matchMedia ? window.matchMedia('(min-width:961px)') : null, inner = $('.wrap.sched', sec);
    var fit = function () {
      var on = !reduce && !!wide && wide.matches && inner.offsetHeight <= window.innerHeight - 96;
      if (on !== sec.classList.contains('pinned')) sec.classList.toggle('pinned', on);
    };
    fit();
    var progress = function () {
      var vh = window.innerHeight;
      if (sec.classList.contains('pinned')) { var r = sec.getBoundingClientRect(); return clamp(-r.top / Math.max(1, r.height - vh)); }
      var b = board.getBoundingClientRect();
      // unpinned (a phone): from the board's top at 85% of the screen until
      // it has scrolled a fifth of a screen past the top, so the meters at
      // its foot are in view when labor and the score land
      return clamp((vh * 0.85 - b.top) / (vh * 1.05));
    };
    if (reduce) apply(1);
    else {
      var queued = false;
      var tick = function () { queued = false; apply(progress()); };
      // a frame, or 120ms, whichever comes first (a throttled tab can hold frames back)
      var ask = function () { if (!queued) { queued = true; requestAnimationFrame(tick); setTimeout(function () { if (queued) tick(); }, 120); } };
      window.addEventListener('scroll', ask, { passive: true });
      window.addEventListener('resize', function () { fit(); ask(); });
      // the fit is measured again once the real fonts have set the headline
      window.addEventListener('load', function () { fit(); ask(); });
      if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { fit(); ask(); });
      tick();
    }
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

  // ── Ask Cavnar AI: a question typed, the answer streamed with its
  //    figures, then where each figure came from. Illustrative figures. ──
  var asks = $('#asks'), chatb = $('#chatbody');
  if (asks && chatb) {
    var QA = [
      { q: 'Why was labor high on Tuesday?',
        a: 'Tuesday ran [41.2%] labor against your [35%] target. Sales came in [$1,180] under a normal Tuesday while four servers were on from 4pm. Sending one home at 8pm would have landed it near [36%].',
        s: ['Labor · Tue 9/29', 'Nightly report · Tue 9/29'] },
      { q: 'What did the Bears game do to sales?',
        a: 'Sunday’s noon game brought in [$9,860], [38%] above a normal Sunday. The bar did most of it at [$4,210], double the usual. Next Bears Sunday, plan one more bartender from 11am.',
        s: ['Nightly report · Sun 10/4', 'Events · Bears game'] },
      { q: 'Which menu items are losing money?',
        a: 'Two items sit over your [30%] food cost target: the short rib at [36%] since beef rose [9%], and the chicken parm at [33%]. A [$1] price move brings the short rib back to [31%].',
        s: ['Food cost · last 4 invoices', 'Menu margins'] },
      { q: 'Did last month’s campaign work?',
        a: 'The game-day text reached [418] guests and [37] came in within three days, about [$1,260] in sales against the four Sundays before. Measured before and after, not proof it was the only cause.',
        s: ['Marketing · campaign', 'Nightly reports · 4 Sundays'] }
    ];
    asks.innerHTML = QA.map(function (x, i) { return '<button type="button" class="q" data-q="' + i + '" aria-pressed="false">' + x.q + '</button>'; }).join('');
    var chatTimers = [], chatAuto = null, chatCur = -1;
    var esc = function (t) { return t.replace(/&/g, '&amp;').replace(/</g, '&lt;'); };
    var askPlay = function (i) {
      chatTimers.forEach(clearTimeout); chatTimers = []; chatCur = i;
      $$('.q', asks).forEach(function (b) { b.setAttribute('aria-pressed', +b.getAttribute('data-q') === i ? 'true' : 'false'); });
      var x = QA[i];
      // the answer as words; [figure] is a number from the restaurant's data
      var words = x.a.split(' ').map(function (w) {
        var m = /^\[(.+?)\](.*)$/.exec(w);
        return m ? '<span class="w"><b' + (/%|\$/.test(m[1]) ? ' class="hot"' : '') + '>' + esc(m[1]) + '</b>' + esc(m[2]) + '</span>' : '<span class="w">' + esc(w) + '</span>';
      }).join(' ');
      chatb.innerHTML = '<div class="msg me">' + esc(x.q) + '</div><div class="dots" aria-hidden="true"><i></i><i></i><i></i></div>';
      var wait = reduce ? 0 : 900;
      chatTimers.push(setTimeout(function () {
        chatb.innerHTML = '<div class="msg me">' + esc(x.q) + '</div><div class="msg ai">' + words + '</div><div class="srcs">' + x.s.map(function (t) { return '<span>' + esc(t) + '</span>'; }).join('') + '</div>';
        var ws = $$('.msg.ai .w', chatb), step = reduce ? 0 : 55;
        ws.forEach(function (w, k) { chatTimers.push(setTimeout(function () { w.classList.add('on'); }, k * step)); });
        chatTimers.push(setTimeout(function () { var sr = $('.srcs', chatb); if (sr) sr.classList.add('on'); }, ws.length * step + 200));
      }, wait));
    };
    asks.addEventListener('click', function (e) {
      var b = e.target.closest('.q'); if (!b) return;
      clearInterval(chatAuto); chatAuto = null; askPlay(+b.getAttribute('data-q'));
    });
    onView(chatb, function () { askPlay(0); if (!reduce) chatAuto = setInterval(function () { askPlay((chatCur + 1) % QA.length); }, 9000); }, 0.35);
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

  // ── headlines that catch: where the browser can't tie the sweep to the
  //    scroll (no animation-timeline), it plays once as the heading arrives ──
  if (!(window.CSS && CSS.supports && CSS.supports('animation-timeline: view()'))) {
    $$('.ig').forEach(function (el) { onView(el, function () { el.classList.add('lit'); }, 0.6); });
  }

  // ── the Ember Thread (10/8/26): one ember line leaves the hero, runs down
  //    a rail in the gutter and branches into each core as it is reached —
  //    by the bottom the visitor has traced everything back to one AI.
  //    Nothing on the rail is redrawn per frame, so a fast scroll can't
  //    outrun it: the lit rail is drawn whole in the page (it scrolls
  //    natively), the head is fixed on the reading line 60% down the screen,
  //    and a fixed veil in the page's own colour hides the lit rail below the
  //    head, with the dashed track drawn over it. Line and head are both
  //    placed by the browser, so they always meet. A branch draws in as the
  //    head reaches its bend, and its core answers with one pulse. Branches
  //    come in from the side or down through the section's empty top
  //    padding, so no line crosses a word. ──
  (function () {
    var hero = $('.hero'), anchors = $$('[data-thread]');
    if (!hero || !anchors.length) return;
    var NS = 'http://www.w3.org/2000/svg';
    var mk = function (tag, attrs, parent) { var e = document.createElementNS(NS, tag); for (var a in attrs) e.setAttribute(a, attrs[a]); parent.appendChild(e); return e; };
    var layer = function (id) { var v = document.createElementNS(NS, 'svg'); v.setAttribute('id', id); v.setAttribute('class', 'thread-layer'); v.setAttribute('aria-hidden', 'true'); v.setAttribute('focusable', 'false'); document.body.appendChild(v); return v; };
    // paint order: the lit rail, the veil over it, then the track, the
    // lead-in and the branches, then the head
    var litSvg = layer('thread-lit');
    var veil = document.createElement('div'); veil.className = 'thread-veil'; veil.setAttribute('aria-hidden', 'true'); document.body.appendChild(veil);
    var svg = layer('thread');
    var gT = mk('g', {}, svg), gL = mk('g', {}, svg), gB = mk('g', {}, svg);
    var head = document.createElement('div'); head.className = 'thread-head'; head.setAttribute('aria-hidden', 'true'); document.body.appendChild(head);
    var railTop = 0, railEnd = 0, lead = null, brs = [];
    var abs = function (el) { var r = el.getBoundingClientRect(); return { x: r.left + window.pageXOffset, y: r.top + window.pageYOffset, w: r.width, h: r.height }; };
    var phone = function () { return window.innerWidth < 760; };
    var update = function () {
      var readY = reduce ? 1e9 : window.pageYOffset + window.innerHeight * 0.6;
      // the lead-in from the hero (above the rail; only near the top)
      if (lead) {
        var f = Math.max(0, Math.min(1, (readY - lead.y1) / Math.max(1, lead.y2 - lead.y1)));
        if (Math.abs(f - lead.f) > 0.0005) { lead.el.setAttribute('stroke-dashoffset', (lead.L * (1 - f)).toFixed(1)); lead.f = f; }
      }
      var on = !reduce && readY > railTop && readY < railEnd;
      if (on !== head._on) { head._on = on; head.classList.toggle('on', on); }
      brs.forEach(function (b) {
        var hit = readY >= b.y;
        if (hit === b.on) return;
        b.on = hit;
        if (b.p) b.p.style.strokeDashoffset = hit ? '0' : String(b.L);
        if (hit && !reduce && window.EmberCore) setTimeout(function () { if (b.on) window.EmberCore.pulse(b.name); }, b.p ? 650 : 0);
      });
    };
    var geometry = function () {
      var root = document.documentElement, W = root.scrollWidth, H = root.scrollHeight;
      [litSvg, svg].forEach(function (v) { v.setAttribute('width', W); v.setAttribute('height', H); });
      var w0 = $('.sec .wrap'), wa = abs(w0), pad = parseFloat(window.getComputedStyle(w0).paddingLeft) || 0;
      var rx = phone() ? 10 : Math.max(16, wa.x + pad - 34);
      var hb = abs(hero), hint = $('.scrollhint'), hr = hint ? abs(hint) : null;
      var sx = hr && hr.w ? hr.x + hr.w / 2 : hb.x + hb.w / 2, sy = hr && hr.w ? hr.y + hr.h + 8 : hb.y + hb.h - 40, y0 = hb.y + hb.h;
      var taps = anchors.map(function (el) {
        var mode = (el.getAttribute('data-thread') || 'left|left').split('|')[phone() ? 1 : 0];
        var a = abs(el), ax = a.x + a.w / 2, ay = a.y + a.h / 2, r = Math.min(a.w, a.h) / 2 + 6;
        var sc = el.closest('section'), st = sc ? abs(sc).y : ay - 120, d = null, y = ay;
        // a core within a bend's reach of the rail (a phone's demo core sits
        // ~4px off it) gets the bend alone: a straight run after it would
        // double back and leave a stub (10/8/26)
        var ex = Math.max(rx + 4, ax - r);
        if (mode === 'top') { y = st + 46; d = ax - rx < 40 ? 'M' + rx + ' ' + (y - 16) + ' V' + (ay - r) : 'M' + rx + ' ' + (y - 16) + ' Q' + rx + ' ' + y + ' ' + (rx + 16) + ' ' + y + ' H' + (ax - 16) + ' Q' + ax + ' ' + y + ' ' + ax + ' ' + (y + 16) + ' V' + (ay - r); }
        else if (mode === 'left') { d = 'M' + rx + ' ' + (ay - 16) + ' Q' + rx + ' ' + ay + ' ' + (ex < rx + 16 ? ex + ' ' + ay : (rx + 16) + ' ' + ay + ' H' + ex); }
        else y = st + 46;            // no branch (a pinned or crowded core): it still answers
        // a branch starts at its bend, 16px above the tap: that's where the
        // head leaves the rail for it
        return { y: d ? y - 16 : y, d: d, name: el.getAttribute('data-core') };
      }).sort(function (p, q) { return p.y - q.y; });
      // the rail ends where the last branch bends, so nothing pokes past it
      var last = taps[taps.length - 1];
      railTop = y0; railEnd = last ? Math.max(y0, last.y) : y0 + 400;
      var rail = 'M' + rx + ' ' + y0 + ' V' + railEnd, leadD = 'M' + sx + ' ' + sy + ' C' + sx + ' ' + (sy + 90) + ' ' + rx + ' ' + (y0 - 90) + ' ' + rx + ' ' + y0;
      litSvg.innerHTML = ''; gT.innerHTML = ''; gL.innerHTML = ''; gB.innerHTML = '';
      mk('path', { d: rail, 'class': 'lit' }, litSvg);
      mk('path', { d: leadD + ' V' + railEnd, 'class': 'track' }, gT);
      var le = mk('path', { d: leadD, 'class': 'lit' }, gL), LL = le.getTotalLength();
      le.setAttribute('stroke-dasharray', LL + ' ' + LL); le.setAttribute('stroke-dashoffset', LL);
      lead = { el: le, L: LL, y1: sy, y2: y0, f: -1 };
      var rl = (window.innerHeight * 0.6).toFixed(1);
      head.style.transform = 'translate3d(' + rx.toFixed(1) + 'px,' + rl + 'px,0)';
      veil.style.transform = 'translate3d(' + (rx - 4).toFixed(1) + 'px,' + rl + 'px,0)';
      var was = {};
      brs.forEach(function (b) { was[b.name] = b.on; });
      brs = taps.map(function (t) {
        var p = t.d ? mk('path', { d: t.d, 'class': 'br' }, gB) : null, L = p ? p.getTotalLength() : 0, on = !!was[t.name];
        if (p) { p.setAttribute('stroke-dasharray', L + ' ' + L); p.style.strokeDashoffset = on ? '0' : String(L); }
        return { p: p, L: L, y: t.y, name: t.name, on: on };
      });
      update();
    };
    var queued = false, regeo = 0;
    var tick = function () { queued = false; update(); };
    window.addEventListener('scroll', function () { if (!queued) { queued = true; requestAnimationFrame(tick); setTimeout(function () { if (queued) tick(); }, 120); } }, { passive: true });
    // the route follows the layout: on resize, as fonts land, and as sections reveal
    var later = function () { clearTimeout(regeo); regeo = setTimeout(geometry, 180); };
    window.addEventListener('resize', later);
    window.addEventListener('load', later);
    document.addEventListener('transitionend', function (e) { if (e.target && e.target.classList && e.target.classList.contains('rv')) later(); });
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(later);
    geometry();
  })();
})();
