// EmberCore.metal — Cavnar AI's intelligence, drawn (10/8/26).
//
// The same "molten core" as the web's ember-core.js (public/static/ and
// static/), ported line for line: a dark glass shell holding a slow plasma —
// a hot heart that leans toward the touch, thin veins of light carried by a
// domain-warped 3D flow, smoke the heart's light scatters through — a warm
// fresnel rim, one soft reflection band, and embers in orbit (placed by
// EmberCoreView once a frame and passed in). Change the look in both.
//
// A SwiftUI colorEffect: one call per pixel of the view's square, which is
// the core's diameter times `scale` so the glow and the embers have room.

#include <metal_stdlib>
#include <SwiftUI/SwiftUI_Metal.h>
using namespace metal;

namespace ember {

float3 mod289(float3 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
float4 mod289(float4 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
float4 permute(float4 x) { return mod289(((x * 34.0) + 1.0) * x); }
float4 taylorInvSqrt(float4 r) { return 1.79284291400159 - 0.85373472095314 * r; }

float snoise(float3 v) {
    const float2 C = float2(1.0 / 6.0, 1.0 / 3.0);
    const float4 D = float4(0.0, 0.5, 1.0, 2.0);
    float3 i = floor(v + dot(v, C.yyy));
    float3 x0 = v - i + dot(i, C.xxx);
    float3 g = step(x0.yzx, x0.xyz);
    float3 l = 1.0 - g;
    float3 i1 = min(g.xyz, l.zxy);
    float3 i2 = max(g.xyz, l.zxy);
    float3 x1 = x0 - i1 + C.xxx;
    float3 x2 = x0 - i2 + C.yyy;
    float3 x3 = x0 - D.yyy;
    i = mod289(i);
    float4 p = permute(permute(permute(i.z + float4(0.0, i1.z, i2.z, 1.0)) + i.y + float4(0.0, i1.y, i2.y, 1.0)) + i.x + float4(0.0, i1.x, i2.x, 1.0));
    float n_ = 0.142857142857;
    float3 ns = n_ * D.wyz - D.xzx;
    float4 j = p - 49.0 * floor(p * ns.z * ns.z);
    float4 x_ = floor(j * ns.z);
    float4 y_ = floor(j - 7.0 * x_);
    float4 x = x_ * ns.x + ns.yyyy;
    float4 y = y_ * ns.x + ns.yyyy;
    float4 h = 1.0 - abs(x) - abs(y);
    float4 b0 = float4(x.xy, y.xy);
    float4 b1 = float4(x.zw, y.zw);
    float4 s0 = floor(b0) * 2.0 + 1.0;
    float4 s1 = floor(b1) * 2.0 + 1.0;
    float4 sh = -step(h, float4(0.0));
    float4 a0 = b0.xzyw + s0.xzyw * sh.xxyy;
    float4 a1 = b1.xzyw + s1.xzyw * sh.zzww;
    float3 p0 = float3(a0.xy, h.x);
    float3 p1 = float3(a0.zw, h.y);
    float3 p2 = float3(a1.xy, h.z);
    float3 p3 = float3(a1.zw, h.w);
    float4 norm = taylorInvSqrt(float4(dot(p0, p0), dot(p1, p1), dot(p2, p2), dot(p3, p3)));
    p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;
    float4 m = max(0.6 - float4(dot(x0, x0), dot(x1, x1), dot(x2, x2), dot(x3, x3)), 0.0);
    m = m * m;
    return 42.0 * dot(m * m, float4(dot(p0, x0), dot(p1, x1), dot(p2, x2), dot(p3, x3)));
}

float3 rotY(float3 p, float a) { float c = cos(a), s = sin(a); return float3(c * p.x + s * p.z, p.y, -s * p.x + c * p.z); }
float3 rotX(float3 p, float a) { float c = cos(a), s = sin(a); return float3(p.x, c * p.y - s * p.z, s * p.y + c * p.z); }

// smoke, deep ember, ember, hot amber, the white heart
float3 ramp(float x) {
    x = clamp(x, 0.0, 1.0);
    float3 a = float3(.07, .012, .004), b = float3(.42, .07, .02), c = float3(.80, .25, .09), d = float3(1., .52, .22), e = float3(1., .85, .62);
    if (x < .25) return mix(a, b, x / .25);
    if (x < .5) return mix(b, c, (x - .25) / .25);
    if (x < .78) return mix(c, d, (x - .5) / .28);
    return mix(d, e, (x - .78) / .22);
}

float fbm3(float3 p) { float a = .5, s = 0.; for (int i = 0; i < 3; i++) { s += a * snoise(p); p = p * 2.03 + float3(1.7, 9.2, 3.1); a *= .5; } return s; }
float ridge3(float3 p) { float a = .5, s = 0.; for (int i = 0; i < 3; i++) { float n = 1. - abs(snoise(p)); s += a * n * n; p = p * 2.1 + float3(3.1, 1.7, 5.3); a *= .5; } return s; }

}  // namespace ember

using namespace ember;

[[ stitchable ]] half4 emberCore(float2 position, half4 color,
                                 float2 size, float t, float fl, float energy, float pulse, float flare,
                                 float think, float steps, float parts, float scale, float seed, float2 gaze,
                                 float density, device const float *P, int pcount) {
    float2 uv = position / size * 2.0 - 1.0;
    uv.y = -uv.y;
    float2 ps = uv * scale;
    // breath: two incommensurate sines, never the same twice
    float br = .5 + .3 * sin(t * .53 + seed) + .2 * sin(t * .31 + 1.3 + seed * 2.);
    float R = 1. + .014 * (br - .5) + .035 * flare;
    float r = length(ps);
    float en = .5 + .5 * energy;
    float d = max(r - R, 0.);
    float edge = 1. - smoothstep(scale * .7, scale * .98, r);
    float glow = (exp(-d * 1.8) * .30 + exp(-d * 5.5) * .42 + exp(-d * 18.) * .22) * en * (.88 + .22 * br) * (1. + .5 * flare) * edge;
    float3 col = mix(float3(.70, .17, .05), float3(1., .62, .34), exp(-d * 9.)) * glow;
    // a tap: one ring of light leaves the shell
    if (pulse > 0.) {
        float pr = 1. + pulse * (scale * .85 - 1.);
        float w = .08 + .25 * pulse;
        float ring = exp(-pow((r - pr) / w, 2.)) * pow(1. - pulse, 2.) * .75 * edge;
        col += float3(1., .64, .36) * ring;
    }
    float aa = 2.4 * scale / (size.y * density);
    float inside = 1. - smoothstep(R - aa, R + aa, r);
    if (r < R + aa) {
        float rr = min(r, R * .9999);
        float3 n = float3(ps, sqrt(R * R - rr * rr)) / R;
        float2 hc = gaze * .16 + float2(.05 * sin(t * .23 + seed), .05 * cos(t * .19 + seed));
        float ry = fl * .09 + gaze.x * .5, rx = .22 * sin(t * .061 + seed) + gaze.y * .35;
        // the flow's warp, once per pixel from the front of the shell
        float3 s0 = rotX(rotY(n, ry), rx) * .95;
        float3 warp = float3(fbm3(s0 + float3(0., fl * .05, 0.)), fbm3(s0 + float3(5.2, -fl * .04, 1.3)), fbm3(s0 + float3(2., 1., fl * .045))) * .85;
        float3 acc = float3(0.);
        float tr = 1.;
        float dz = 2. * n.z / steps;
        for (int i = 0; i < 10; i++) {
            if (float(i) >= steps) break;
            float f = (float(i) + .5) / steps;
            float3 q = float3(ps / R, mix(n.z, -n.z, f));
            float3 wq = rotX(rotY(q, ry), rx) * .95 + warp;
            float vein = pow(ridge3(wq * 1.05 + float3(0., 0., fl * .03)), 4.6);
            float smoke = smoothstep(-.2, .6, fbm3(wq * .9 - fl * .02));
            float hd = length(q - float3(hc, 0.));
            float heart = exp(-hd * hd * 5.6);
            float scat = smoke * exp(-hd * 2.1);
            // thinking: sparks run the veins
            float spark = 0.;
            if (think > .01) spark = smoothstep(.5, .95, snoise(wq * 2.6 + float3(fl * 1.3, 0., -fl))) * vein * think * 3.;
            float temp = clamp(heart * .95 + vein * .8 + scat * .35 + .06 * energy + spark * .5, 0., 1.);
            float emit = heart * 3. * (.8 + .35 * energy + .8 * flare) + vein * 3. * (1. + .7 * think) + scat * 1.05 + .05 + spark * 4.;
            float3 e = ramp(.18 + .82 * temp) * emit * (.75 + .5 * energy);
            float shell = smoothstep(.35, 1., length(q));
            float absorb = .3 + (.6 + 1.15 * shell) * smoke * (1. - heart);
            acc += e * tr * dz * 1.35;
            tr *= exp(-absorb * dz);
        }
        // the glass: a warm rim where light wraps the edge, one soft band
        float fres = pow(1. - n.z, 2.6);
        float lit = .5 + .5 * dot(normalize(n.xy + 1e-5), normalize(float2(-.62, .7) + gaze * .4));
        float3 rim = mix(float3(.7, .2, .06), float3(1., .7, .45), lit) * fres * (.32 + .42 * lit);
        float band = exp(-pow((n.y - .55 - .12 * n.x + gaze.y * .05) * 9., 2.)) * smoothstep(.15, .55, n.z) * .10;
        float3 ins = acc + rim + float3(1., .9, .8) * band;
        float lum = dot(ins, float3(.3, .5, .2));
        ins = ins * (1. / (1. + lum * .42));
        col = mix(col, ins, inside);
    }
    // embers in orbit: x, y, size, light — four floats each
    int np = min(int(parts), pcount / 4);
    for (int k = 0; k < 18; k++) {
        if (k >= np) break;
        float2 c = float2(P[k * 4], P[k * 4 + 1]);
        float s = P[k * 4 + 2], li = P[k * 4 + 3];
        float2 dv = ps - c;
        float g = exp(-dot(dv, dv) / (s * s)) * li * en;
        float hal = exp(-length(dv) / (s * 4.)) * .22 * li * en;
        col += float3(1., .72, .46) * g * 1.2 + float3(.9, .33, .1) * hal;
    }
    // premultiplied, and never brighter than its own alpha
    float al = clamp(max(inside, max(col.r, max(col.g, col.b))), 0., 1.);
    return half4(half3(min(col, float3(al))), half(al));
}
