/* cavnar.ai — the Ember Core: Cavnar AI's intelligence, drawn (10/7/26).
   One WebGL canvas fixed over the page draws the core wherever an element
   carries data-core (the hero, the problem's join, the schedule's drafter,
   the platform's centre, Ask Cavnar AI, the demo): the same AI, met again in
   every section. Each core is drawn only inside its own square (viewport +
   scissor), so the GPU shades a few hundred thousand pixels, not the page.

   The material is "molten core": a dark glass shell holding a slow plasma —
   a hot heart, thin veins of light carried by a domain-warped flow, smoke
   that the heart's light scatters through — ringed by orbiting embers.
   Nothing loops: the flow is noise in 3D, the breath two incommensurate
   sines. It notices you: the heart leans toward the cursor, warms as you
   come near, answers a click with one pulse, quickens while it thinks, and
   brightens as you read down the page ("it learns").

   The CSS sphere (.ember) under each anchor stays as the fallback: no
   WebGL, a lost context or a failed compile leaves it showing. Reduced
   motion draws one still frame per scroll. ES5, no library, like site.js. */
(function () {
  'use strict';
  var doc = document, root = doc.documentElement;
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var anchors = [], canvas, gl, prog, U = {}, running = false, raf = 0, lastT = 0, flowT = 0;
  var mouse = { x: -1e4, y: -1e4 }, knowledge = 0, scrollKick = 0, lastScrollY = window.pageYOffset;
  var quality = 1, slow = 0, frames = 0, dprCap = window.innerWidth < 700 ? 1.5 : 1.75;

  // ── the shader ─────────────────────────────────────────────────────────
  var NOISE = [
    'vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}',
    'vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}',
    'vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}',
    'vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}',
    'float snoise(vec3 v){const vec2 C=vec2(1.0/6.0,1.0/3.0);const vec4 D=vec4(0.0,0.5,1.0,2.0);',
    'vec3 i=floor(v+dot(v,C.yyy));vec3 x0=v-i+dot(i,C.xxx);vec3 g=step(x0.yzx,x0.xyz);vec3 l=1.0-g;',
    'vec3 i1=min(g.xyz,l.zxy);vec3 i2=max(g.xyz,l.zxy);vec3 x1=x0-i1+C.xxx;vec3 x2=x0-i2+C.yyy;vec3 x3=x0-D.yyy;',
    'i=mod289(i);vec4 p=permute(permute(permute(i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));',
    'float n_=0.142857142857;vec3 ns=n_*D.wyz-D.xzx;vec4 j=p-49.0*floor(p*ns.z*ns.z);vec4 x_=floor(j*ns.z);vec4 y_=floor(j-7.0*x_);',
    'vec4 x=x_*ns.x+ns.yyyy;vec4 y=y_*ns.x+ns.yyyy;vec4 h=1.0-abs(x)-abs(y);vec4 b0=vec4(x.xy,y.xy);vec4 b1=vec4(x.zw,y.zw);',
    'vec4 s0=floor(b0)*2.0+1.0;vec4 s1=floor(b1)*2.0+1.0;vec4 sh=-step(h,vec4(0.0));vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy;vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;',
    'vec3 p0=vec3(a0.xy,h.x);vec3 p1=vec3(a0.zw,h.y);vec3 p2=vec3(a1.xy,h.z);vec3 p3=vec3(a1.zw,h.w);',
    'vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));p0*=norm.x;p1*=norm.y;p2*=norm.z;p3*=norm.w;',
    'vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0);m=m*m;',
    'return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));}'
  ].join('\n');
  var FS = [
    'precision highp float;',
    'uniform vec2 u_res,u_off,u_gaze;',
    'uniform float u_t,u_flow,u_energy,u_pulse,u_flare,u_think,u_steps,u_parts,u_scale,u_seed,u_alpha;',
    NOISE,
    'vec3 rotY(vec3 p,float a){float c=cos(a),s=sin(a);return vec3(c*p.x+s*p.z,p.y,-s*p.x+c*p.z);}',
    'vec3 rotX(vec3 p,float a){float c=cos(a),s=sin(a);return vec3(p.x,c*p.y-s*p.z,s*p.y+c*p.z);}',
    // the ember ramp: smoke, deep ember, ember, hot amber, the white heart
    'vec3 ramp(float x){x=clamp(x,0.,1.);vec3 a=vec3(.07,.012,.004),b=vec3(.42,.07,.02),c=vec3(.80,.25,.09),d=vec3(1.,.52,.22),e=vec3(1.,.85,.62);',
    'if(x<.25)return mix(a,b,x/.25);if(x<.5)return mix(b,c,(x-.25)/.25);if(x<.78)return mix(c,d,(x-.5)/.28);return mix(d,e,(x-.78)/.22);}',
    'float fbm3(vec3 p){float a=.5,s=0.;for(int i=0;i<3;i++){s+=a*snoise(p);p=p*2.03+vec3(1.7,9.2,3.1);a*=.5;}return s;}',
    'float ridge3(vec3 p){float a=.5,s=0.;for(int i=0;i<3;i++){float n=1.-abs(snoise(p));s+=a*n*n;p=p*2.1+vec3(3.1,1.7,5.3);a*=.5;}return s;}',
    'float hash(float n){return fract(sin(n)*43758.5453);}',
    'void main(){',
    ' vec2 ps=(((gl_FragCoord.xy-u_off)/u_res)*2.-1.)*u_scale;',
    ' float t=u_t,fl=u_flow;',
    // breath: two incommensurate sines, never the same twice
    ' float br=.5+.3*sin(t*.53+u_seed)+.2*sin(t*.31+1.3+u_seed*2.);',
    ' float R=1.+.014*(br-.5)+.035*u_flare;',
    ' float r=length(ps); float en=.5+.5*u_energy;',
    ' float d=max(r-R,0.);',
    ' float edge=1.-smoothstep(u_scale*.7,u_scale*.98,r);',
    ' float glow=(exp(-d*1.8)*.30+exp(-d*5.5)*.42+exp(-d*18.)*.22)*en*(.88+.22*br)*(1.+.5*u_flare)*edge;',
    ' vec3 col=mix(vec3(.70,.17,.05),vec3(1.,.62,.34),exp(-d*9.))*glow;',
    // a click: one ring of light leaves the shell
    ' if(u_pulse>0.){float pr=1.+u_pulse*(u_scale*.85-1.);float w=.08+.25*u_pulse;float ring=exp(-pow((r-pr)/w,2.))*pow(1.-u_pulse,2.)*.75*edge;col+=vec3(1.,.64,.36)*ring;}',
    ' float aa=2.4*u_scale/u_res.y;',
    ' float inside=1.-smoothstep(R-aa,R+aa,r);',
    ' if(r<R+aa){',
    '  float rr=min(r,R*.9999); vec3 n=vec3(ps,sqrt(R*R-rr*rr))/R;',
    '  vec2 hc=u_gaze*.16+vec2(.05*sin(t*.23+u_seed),.05*cos(t*.19+u_seed));',
    '  float ry=fl*.09+u_gaze.x*.5, rx=.22*sin(t*.061+u_seed)+u_gaze.y*.35;',
    // the flow's warp, once per pixel from the front of the shell
    '  vec3 s0=rotX(rotY(n,ry),rx)*.95;',
    '  vec3 warp=vec3(fbm3(s0+vec3(0.,fl*.05,0.)),fbm3(s0+vec3(5.2,-fl*.04,1.3)),fbm3(s0+vec3(2.,1.,fl*.045)))*.85;',
    '  vec3 acc=vec3(0.); float tr=1.; float dz=2.*n.z/u_steps;',
    '  for(int i=0;i<10;i++){',
    '   if(float(i)>=u_steps)break;',
    '   float f=(float(i)+.5)/u_steps; vec3 q=vec3(ps/R,mix(n.z,-n.z,f));',
    '   vec3 wq=rotX(rotY(q,ry),rx)*.95+warp;',
    '   float vein=pow(ridge3(wq*1.05+vec3(0.,0.,fl*.03)),4.6);',
    '   float smoke=smoothstep(-.2,.6,fbm3(wq*.9-fl*.02));',
    '   float hd=length(q-vec3(hc,0.));',
    '   float heart=exp(-hd*hd*5.6);',
    '   float scat=smoke*exp(-hd*2.1);',
    // thinking: sparks run the veins (the neural concept, folded in)
    '   float spark=0.; if(u_think>.01){spark=smoothstep(.5,.95,snoise(wq*2.6+vec3(fl*1.3,0.,-fl)))*vein*u_think*3.;}',
    '   float temp=clamp(heart*.95+vein*.8+scat*.35+.06*u_energy+spark*.5,0.,1.);',
    '   float emit=heart*3.*(.8+.35*u_energy+.8*u_flare)+vein*3.*(1.+.7*u_think)+scat*1.05+.05+spark*4.;',
    '   vec3 e=ramp(.18+.82*temp)*emit*(.75+.5*u_energy);',
    '   float shell=smoothstep(.35,1.,length(q));',
    '   float absorb=.3+(.6+1.15*shell)*smoke*(1.-heart);',
    '   acc+=e*tr*dz*1.35; tr*=exp(-absorb*dz);',
    '  }',
    // the glass: a warm rim where light wraps the edge, one soft reflection band
    '  float fres=pow(1.-n.z,2.6);',
    '  float lit=.5+.5*dot(normalize(n.xy+1e-5),normalize(vec2(-.62,.7)+u_gaze*.4));',
    '  vec3 rim=mix(vec3(.7,.2,.06),vec3(1.,.7,.45),lit)*fres*(.32+.42*lit);',
    '  float band=exp(-pow((n.y-.55-.12*n.x+u_gaze.y*.05)*9.,2.))*smoothstep(.15,.55,n.z)*.10;',
    '  vec3 ins=acc+rim+vec3(1.,.9,.8)*band;',
    '  float lum=dot(ins,vec3(.3,.5,.2)); ins=ins*(1./(1.+lum*.42));',
    '  col=mix(col,ins,inside);',
    ' }',
    // embers in orbit, hidden when they pass behind the shell
    ' for(int k=0;k<18;k++){',
    '  float fk=float(k); if(fk>=u_parts)break;',
    '  float h1=hash(fk*12.9898+u_seed),h2=hash(fk*78.233+u_seed),h3=hash(fk*39.42+u_seed);',
    '  float orb=1.1+h1*.75; float sp=(.035+.09*h2)*(mod(fk,2.)<1.?1.:-1.); float a=h3*6.2831+fl*sp;',
    '  vec3 q=vec3(cos(a)*orb,sin(a)*orb*.16,sin(a)*orb); q=rotX(q,(h1-.5)*1.1+.25); q=rotY(q,h2*3.);',
    '  float occl=(q.z<0.&&length(q.xy)<R)?0.:1.;',
    '  float fli=.5+.5*sin(t*(.9+h2*1.7)+fk*2.1);',
    '  float s=(.006+.009*h3)*(1.+.35*q.z/orb);',
    '  vec2 dv=ps-q.xy; float g=exp(-dot(dv,dv)/(s*s))*occl*fli*en;',
    '  float hal=exp(-length(dv)/(s*4.))*.22*occl*fli*en;',
    '  col+=vec3(1.,.72,.46)*g*1.2+vec3(.9,.33,.1)*hal;',
    ' }',
    // premultiplied, and never brighter than its own alpha: it lights the page, never punches through it
    ' float al=max(inside,max(col.r,max(col.g,col.b)));',
    ' al=clamp(al,0.,1.);',
    ' gl_FragColor=vec4(min(col,vec3(al)),al)*u_alpha;',
    '}'
  ].join('\n');

  function compile() {
    function sh(type, src) {
      var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || 'compile');
      return s;
    }
    prog = gl.createProgram();
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, 'attribute vec2 a;void main(){gl_Position=vec4(a,0.,1.);}'));
    gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error('link');
    gl.useProgram(prog);
    var buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    var loc = gl.getAttribLocation(prog, 'a'); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    ['u_res', 'u_off', 'u_gaze', 'u_t', 'u_flow', 'u_energy', 'u_pulse', 'u_flare', 'u_think', 'u_steps', 'u_parts', 'u_scale', 'u_seed', 'u_alpha'].forEach(function (n) { U[n] = gl.getUniformLocation(prog, n); });
    gl.enable(gl.SCISSOR_TEST);
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.clearColor(0, 0, 0, 0);
  }

  // ── anchors: every element that holds the core ─────────────────────────
  function Core(el, i) {
    this.el = el; this.name = el.getAttribute('data-core');
    this.scale = parseFloat(el.getAttribute('data-core-scale')) || 2.6;
    this.seed = 1.7 + i * 3.1;
    this.energy = 0.55; this.think = 0; this.thinkTo = 0; this.flare = 0;
    this.pulse = -1; this.gx = 0; this.gy = 0; this.hot = 0; this.box = null;
    // the section's own reveal (.rv) fades the core in with it
    var f = el; while (f && f !== doc.body && !(f.classList && f.classList.contains('rv'))) f = f.parentNode;
    this.fade = f && f !== doc.body ? f : null;
  }
  Core.prototype.measure = function () {
    var r = this.el.getBoundingClientRect(), s = Math.min(r.width, r.height);
    this.box = { cx: r.left + r.width / 2, cy: r.top + r.height / 2, s: s };
    var q = s * this.scale / 2, vw = canvas.clientWidth || window.innerWidth, vh = canvas.clientHeight || window.innerHeight;
    this.visible = s > 3 && r.width > 0 && this.box.cx + q > 0 && this.box.cx - q < vw && this.box.cy + q > 0 && this.box.cy - q < vh;
    return this.visible;
  };
  function find(name) { for (var i = 0; i < anchors.length; i++) if (anchors[i].name === name) return anchors[i]; return null; }

  // ── the loop ───────────────────────────────────────────────────────────
  function resize() {
    if (!canvas) return;
    var d = Math.min(window.devicePixelRatio || 1, dprCap) * quality;
    var w = Math.round((canvas.clientWidth || window.innerWidth) * d), h = Math.round((canvas.clientHeight || window.innerHeight) * d);
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  }
  function frame(now) {
    raf = 0;
    var t = now / 1000, dt = lastT ? Math.min(0.1, t - lastT) : 0.016; lastT = t;
    if (!reduce) {
      // quality steps down once if the device can't hold the frame rate
      frames++; if (dt > 0.026) slow++;
      if (frames === 120) { if (slow > 50 && quality > 0.7) { quality = 0.7; resize(); } frames = 0; slow = 0; }
    }
    var y = window.pageYOffset, dy = Math.abs(y - lastScrollY); lastScrollY = y;
    scrollKick = Math.min(1, scrollKick * 0.92 + dy * 0.004);
    var max = Math.max(1, root.scrollHeight - window.innerHeight);
    knowledge = Math.max(knowledge, Math.min(1, y / max));
    var anyThink = 0;
    anchors.forEach(function (c) { anyThink = Math.max(anyThink, c.think); });
    flowT += dt * (1 + 1.4 * anyThink + 0.8 * scrollKick);

    var k = canvas.width / (canvas.clientWidth || window.innerWidth), H = canvas.height, visible = 0;
    gl.disable(gl.SCISSOR_TEST); gl.viewport(0, 0, canvas.width, H); gl.clear(gl.COLOR_BUFFER_BIT); gl.enable(gl.SCISSOR_TEST);
    anchors.forEach(function (c) {
      if (!c.measure()) return;
      visible++;
      var b = c.box, dx = mouse.x - b.cx, dyy = mouse.y - b.cy, dist = Math.sqrt(dx * dx + dyy * dyy);
      var reach = Math.max(420, b.s * 3);
      var near = Math.max(0, 1 - dist / reach), over = dist < b.s * 0.6 ? 1 : 0;
      var gx = Math.max(-1, Math.min(1, dx / reach)), gy = Math.max(-1, Math.min(1, -dyy / reach));
      var e = 0.55 + 0.4 * knowledge + 0.16 * near + 0.18 * over + 0.12 * scrollKick + 0.2 * c.think;
      var ease = Math.min(1, dt * 2.2);
      c.energy += (Math.min(1.2, e) - c.energy) * ease;
      c.gx += (gx * near - c.gx) * Math.min(1, dt * 1.6); c.gy += (gy * near - c.gy) * Math.min(1, dt * 1.6);
      c.think += (c.thinkTo - c.think) * Math.min(1, dt * 2.5);
      c.flare = Math.max(0, c.flare - dt * 0.9);
      if (c.pulse >= 0) { c.pulse += dt / 1.6; if (c.pulse >= 1) c.pulse = -1; }
      var px = b.s * k, q = px * c.scale;
      var x0 = Math.round(b.cx * k - q / 2), y0 = Math.round(H - (b.cy * k + q / 2)), qs = Math.round(q);
      gl.viewport(x0, y0, qs, qs); gl.scissor(x0, y0, qs, qs);
      var steps = px >= 150 ? 10 : px >= 70 ? 7 : px >= 36 ? 5 : 3;
      if (quality < 1) steps = Math.max(3, steps - 2);
      gl.uniform2f(U.u_res, qs, qs); gl.uniform2f(U.u_off, x0, y0); gl.uniform2f(U.u_gaze, c.gx, c.gy);
      gl.uniform1f(U.u_t, reduce ? 12 : t); gl.uniform1f(U.u_flow, reduce ? 12 : flowT);
      gl.uniform1f(U.u_energy, c.energy); gl.uniform1f(U.u_pulse, c.pulse); gl.uniform1f(U.u_flare, c.flare);
      gl.uniform1f(U.u_think, c.think); gl.uniform1f(U.u_steps, steps);
      gl.uniform1f(U.u_parts, px >= 110 ? 18 : px >= 60 ? 9 : 0);
      gl.uniform1f(U.u_scale, c.scale); gl.uniform1f(U.u_seed, c.seed);
      gl.uniform1f(U.u_alpha, c.fade ? +window.getComputedStyle(c.fade).opacity : 1);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    });
    var live = Streams.step(now);
    running = !reduce && !doc.hidden && (visible > 0 || live);
    if (running) raf = requestAnimationFrame(frame);
  }
  function kick() {
    if (!gl || raf) return;
    if (reduce || doc.hidden) { raf = requestAnimationFrame(frame); return; }
    lastT = 0; raf = requestAnimationFrame(frame);
  }

  // ── streams: data moving into the core, or out of it ───────────────────
  var SVGNS = 'http://www.w3.org/2000/svg';
  var Streams = (function () {
    var svg, list = [], uid = 0;
    function ease(x) { return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2; }
    function mk(tag, attrs, parent) { var e = doc.createElementNS(SVGNS, tag); for (var a in attrs) e.setAttribute(a, attrs[a]); if (parent) parent.appendChild(e); return e; }
    function init() {
      svg = mk('svg', { id: 'core-streams', 'aria-hidden': 'true', focusable: 'false' });
      var defs = mk('defs', {}, svg);
      var rg = mk('radialGradient', { id: 'cs-dot' }, defs);
      mk('stop', { offset: '0', 'stop-color': '#fff4e8', 'stop-opacity': '1' }, rg);
      mk('stop', { offset: '.28', 'stop-color': '#f2b183', 'stop-opacity': '.9' }, rg);
      mk('stop', { offset: '1', 'stop-color': '#c84b2f', 'stop-opacity': '0' }, rg);
      doc.body.appendChild(svg);
    }
    function center(el) { var r = el.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2, Math.min(r.width, r.height), r.width / 2, r.height / 2]; }
    // how far from an end's centre the line starts: the core's shell, or the
    // edge of a box (so a line never runs across a card's words)
    function inset(el, P, ux, uy) {
      if (el.hasAttribute('data-core')) return P[2] * 0.5;
      var tx = Math.abs(ux) > 1e-4 ? P[3] / Math.abs(ux) : 1e9, ty = Math.abs(uy) > 1e-4 ? P[4] / Math.abs(uy) : 1e9;
      return Math.min(tx, ty) + 2;
    }
    // from: an element or the core; to: the same. dir only changes the curve's lean.
    function add(fromEl, toEl, opts) {
      if (!svg || reduce || list.length > 12) return;
      opts = opts || {};
      var id = 'cs' + (uid++);
      var g = mk('linearGradient', { id: id, gradientUnits: 'userSpaceOnUse' }, svg.firstChild);
      mk('stop', { offset: '0', 'stop-color': '#e8956a', 'stop-opacity': '0' }, g);
      mk('stop', { offset: '.55', 'stop-color': '#e8956a', 'stop-opacity': '.55' }, g);
      mk('stop', { offset: '1', 'stop-color': '#f6c39b', 'stop-opacity': '.9' }, g);
      var path = mk('path', { fill: 'none', stroke: 'url(#' + id + ')', 'stroke-width': opts.w || 1.4, 'stroke-linecap': 'round' }, svg);
      var dot = mk('circle', { r: opts.r || 7, fill: 'url(#cs-dot)' }, svg);
      list.push({ a: fromEl, b: toEl, g: g, path: path, dot: dot, born: performance.now(), dur: opts.dur || 1500, side: opts.side || (Math.random() < 0.5 ? -1 : 1), bend: opts.bend == null ? 0.22 : opts.bend, done: opts.done, inset: opts.inset || 0 });
      kick();
    }
    function step(now) {
      if (!svg) return false;
      for (var i = list.length - 1; i >= 0; i--) {
        var s = list[i], age = (now - s.born) / s.dur;
        if (age >= 1) { s.path.remove(); s.dot.remove(); s.g.remove(); list.splice(i, 1); if (s.done) s.done(); continue; }
        var A = center(s.a), B = center(s.b), dx = B[0] - A[0], dy = B[1] - A[1], L = Math.sqrt(dx * dx + dy * dy) || 1;
        // end on the core's shell, not its centre
        var ia = inset(s.a, A, dx / L, dy / L), ib = inset(s.b, B, -dx / L, -dy / L);
        var ax = A[0] + dx / L * ia, ay = A[1] + dy / L * ia, bx = B[0] - dx / L * ib, by = B[1] - dy / L * ib;
        var nx = -dy / L * L * s.bend * s.side, ny = dx / L * L * s.bend * s.side;
        var d = 'M' + ax.toFixed(1) + ' ' + ay.toFixed(1) + ' C' + (ax + (bx - ax) * 0.3 + nx).toFixed(1) + ' ' + (ay + (by - ay) * 0.3 + ny).toFixed(1) + ' ' + (ax + (bx - ax) * 0.72 + nx * 0.55).toFixed(1) + ' ' + (ay + (by - ay) * 0.72 + ny * 0.55).toFixed(1) + ' ' + bx.toFixed(1) + ' ' + by.toFixed(1);
        s.path.setAttribute('d', d);
        s.g.setAttribute('x1', ax); s.g.setAttribute('y1', ay); s.g.setAttribute('x2', bx); s.g.setAttribute('y2', by);
        var len = s.path.getTotalLength(), reveal = ease(Math.min(1, age / 0.6));
        s.path.setAttribute('stroke-dasharray', len + ' ' + len);
        s.path.setAttribute('stroke-dashoffset', (len * (1 - reveal)).toFixed(1));
        s.path.setAttribute('opacity', (age < 0.7 ? 1 : 1 - (age - 0.7) / 0.3).toFixed(3));
        var pt = s.path.getPointAtLength(len * ease(Math.min(1, age / 0.62)));
        s.dot.setAttribute('cx', pt.x.toFixed(1)); s.dot.setAttribute('cy', pt.y.toFixed(1));
        s.dot.setAttribute('opacity', (age < 0.62 ? 1 : Math.max(0, 1 - (age - 0.62) / 0.15)).toFixed(3));
        if (!s.arrived && age >= 0.62) { s.arrived = true; if (s.b.hasAttribute('data-core')) { var c = find(s.b.getAttribute('data-core')); if (c) c.flare = Math.min(1, c.flare + 0.35); } }
      }
      return list.length > 0;
    }
    return { init: init, add: add, step: step };
  })();

  // ── the story: what each section asks of the core ──────────────────────
  function onView(el, enter, leave, th) {
    if (!el || !('IntersectionObserver' in window)) { if (el && enter) enter(); return; }
    new IntersectionObserver(function (es) { es.forEach(function (e) { if (e.isIntersecting) { if (enter) enter(); } else if (leave) leave(); }); }, { threshold: th || 0.25 }).observe(el);
  }
  function story() {
    // 2 · the problem: what each system knows flows into the core
    var silos = doc.getElementById('silos'), pc = doc.querySelector('[data-core="prob"]');
    if (silos && pc) {
      var timer = 0, n = 0;
      var feed = function () {
        var s = silos.querySelectorAll('.silo'); if (!s.length) return;
        Streams.add(s[n % s.length], pc, { dur: 1700, side: n % 2 ? 1 : -1, bend: 0.12 }); n++;
        timer = setTimeout(feed, silos.classList.contains('joined') ? 650 : 1300);
      };
      onView(silos, function () { if (!timer) timer = setTimeout(feed, 900); }, function () { clearTimeout(timer); timer = 0; }, 0.4);
    }
    // 3 · the Schedule Generator: the core drafts the week, cell by cell
    var grid = doc.getElementById('grid7'), sc = doc.querySelector('[data-core="sched"]');
    if (grid && sc && 'MutationObserver' in window) {
      var fed = 0;
      new MutationObserver(function (ms) {
        ms.forEach(function (m) {
          var el = m.target;
          if (m.attributeName !== 'class' || !el.classList) return;
          if (el.classList.contains('shown') && !el._fed) { el._fed = 1; if ((fed++) % 2 === 0) Streams.add(sc, el, { dur: 1000, bend: 0.1, w: 1.1, r: 5 }); }
          if (el.classList.contains('moved') && !el._moved) { el._moved = 1; var c = find('sched'); if (c) c.flare = 1; }
        });
      }).observe(grid, { attributes: true, subtree: true, attributeFilter: ['class'] });
    }
    // 4 · the platform: the core feeds each module as it lights
    var nodes = doc.getElementById('nodes'), links = doc.getElementById('links');
    if (nodes && links && 'MutationObserver' in window) {
      new MutationObserver(function (ms) {
        ms.forEach(function (m) {
          var el = m.target;
          if (!el.classList || !el.classList.contains('node') || !el.classList.contains('on') || el._lit) return;
          el._lit = 1; setTimeout(function () { el._lit = 0; }, 900);
          var all = nodes.querySelectorAll('.node'), i = Array.prototype.indexOf.call(all, el);
          var spoke = links.querySelectorAll('.spoke')[i], c = find('platform');
          if (c) c.flare = Math.min(1, c.flare + 0.45);
          if (!spoke || reduce) return;
          spoke.classList.add('feed'); setTimeout(function () { spoke.classList.remove('feed'); }, 1500);
          var dot = doc.createElementNS(SVGNS, 'circle'); dot.setAttribute('r', 5); dot.setAttribute('class', 'feeddot'); links.appendChild(dot);
          var L = spoke.getTotalLength(), t0 = performance.now();
          (function go(now) { var p = Math.min(1, (now - t0) / 700), e = 1 - Math.pow(1 - p, 3), pt = spoke.getPointAtLength(L * e); dot.setAttribute('cx', pt.x); dot.setAttribute('cy', pt.y); dot.style.opacity = p < 0.85 ? 1 : (1 - p) / 0.15; if (p < 1) requestAnimationFrame(go); else dot.remove(); })(t0);
        });
      }).observe(nodes, { attributes: true, subtree: true, attributeFilter: ['class'] });
    }
    // Ask Cavnar AI: it thinks while it answers
    var chat = doc.getElementById('chatbody');
    if (chat && 'MutationObserver' in window) {
      var settle = 0;
      new MutationObserver(function () {
        var c = find('ask'); if (!c) return;
        clearTimeout(settle);
        if (chat.querySelector('.dots')) c.thinkTo = 1;
        else { c.thinkTo = 0.7; settle = setTimeout(function () { c.thinkTo = 0; }, 3200); }
        kick();
      }).observe(chat, { childList: true, subtree: true });
    }
    // the demo: a request answered with one pulse
    var form = doc.getElementById('demoForm');
    if (form) form.addEventListener('submit', function () { var c = find('demo'); if (c) { c.pulse = 0; c.flare = 1; kick(); } });
  }

  // ── start ──────────────────────────────────────────────────────────────
  function start() {
    var els = doc.querySelectorAll('[data-core]'); if (!els.length) return;
    canvas = doc.createElement('canvas'); canvas.id = 'core-gl'; canvas.setAttribute('aria-hidden', 'true');
    var opts = { alpha: true, premultipliedAlpha: true, antialias: false, depth: false, stencil: false, powerPreference: 'high-performance' };
    try { gl = canvas.getContext('webgl', opts) || canvas.getContext('experimental-webgl', opts); } catch (e) { gl = null; }
    if (!gl) return;
    try { compile(); } catch (e) { gl = null; return; }
    doc.body.appendChild(canvas);
    for (var i = 0; i < els.length; i++) anchors.push(new Core(els[i], i));
    root.classList.add('core-live');
    Streams.init(); story(); resize();
    canvas.addEventListener('webglcontextlost', function (e) { e.preventDefault(); root.classList.remove('core-live'); gl = null; if (raf) cancelAnimationFrame(raf); raf = 0; });
    window.addEventListener('resize', function () { dprCap = window.innerWidth < 700 ? 1.5 : 1.75; resize(); kick(); });
    window.addEventListener('scroll', kick, { passive: true });
    window.addEventListener('pointermove', function (e) { if (e.pointerType === 'mouse') { mouse.x = e.clientX; mouse.y = e.clientY; } kick(); }, { passive: true });
    window.addEventListener('pointerdown', function (e) {
      anchors.forEach(function (c) { if (!c.box) return; var dx = e.clientX - c.box.cx, dy = e.clientY - c.box.cy; if (Math.sqrt(dx * dx + dy * dy) < c.box.s * 0.62) { c.pulse = 0; c.flare = 1; } });
      kick();
    }, { passive: true });
    doc.addEventListener('visibilitychange', kick);
    kick();
  }
  window.EmberCore = { pulse: function (n) { var c = find(n); if (c) { c.pulse = 0; c.flare = 1; kick(); } }, think: function (n, on) { var c = find(n); if (c) { c.thinkTo = on ? 1 : 0; kick(); } } };
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', start); else start();
})();
