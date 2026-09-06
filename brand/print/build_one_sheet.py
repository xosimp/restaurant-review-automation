"""Cavnar AI one-sheet — assembles brand/print/cavnar-one-sheet.html (fonts,
wordmark and QR embedded, so the file is self-contained) for Chrome to print
at exactly 8.5x11in:

  python3 brand/print/build_one_sheet.py
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \
    --no-pdf-header-footer --print-to-pdf=brand/print/cavnar-one-sheet.pdf \
    "file://$PWD/brand/print/cavnar-one-sheet.html"
"""
import base64, io, json, re
import qrcode, qrcode.image.svg

ROOT = "/Users/simp/review_automation"
def b64(path): return base64.b64encode(open(path, "rb").read()).decode()
def woff(name): return "data:font/woff2;base64," + b64(f"{ROOT}/static/fonts/{name}.woff2")
F = {"clashSemi": woff("ClashDisplay-Semibold"), "clashBold": woff("ClashDisplay-Bold"),
     "apfel": woff("ApfelGrotezk-Regular"), "apfelFett": woff("ApfelGrotezk-Fett")}
clash_med = b64(f"{ROOT}/static/fonts/ClashDisplay-Medium.woff2")
space = b64(f"{ROOT}/ios/CavnarAI/CavnarAI/Fonts/SpaceGrotesk.ttf")

wordmark = open(f"{ROOT}/brand/assets/wordmark-dark.svg").read()
wordmark = re.sub(r"<!--.*?-->", "", wordmark, flags=re.S)

qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=0)
qr.add_data("https://calendly.com/will-cavnar/30min")
qr.make(fit=True)
buf = io.BytesIO()
qr.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(buf)
qr_svg = buf.getvalue().decode()
qr_svg = re.sub(r'<\?xml[^>]*\?>', '', qr_svg)
qr_svg = re.sub(r'\s(width|height)="[^"]*"', '', qr_svg, count=2)
qr_svg = qr_svg.replace("<path", '<path fill="#141210"', 1)

ACCENT = "#c84b2f"

html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Cavnar AI — One-sheet</title>
<style>
@font-face{{font-family:"Clash Display";font-weight:600;src:url("{F["clashSemi"]}") format("woff2")}}
@font-face{{font-family:"Clash Display";font-weight:700;src:url("{F["clashBold"]}") format("woff2")}}
@font-face{{font-family:"Clash Display";font-weight:500;src:url("data:font/woff2;base64,{clash_med}") format("woff2")}}
@font-face{{font-family:"Apfel Grotezk";font-weight:400;src:url("{F["apfel"]}") format("woff2")}}
@font-face{{font-family:"Apfel Grotezk";font-weight:700;src:url("{F["apfelFett"]}") format("woff2")}}
@font-face{{font-family:"Space Grotesk";font-weight:300 700;src:url("data:font/ttf;base64,{space}") format("truetype")}}

@page{{size:8.5in 11in;margin:0}}
:root{{
  --accent:{ACCENT};
  --ink:#141210; --ink2:#3d3833; --ink3:#7a736b;
  --line:#e6e0d8; --cream:#faf7f2; --cream2:#f3eee6;
  --disp:"Clash Display","Helvetica Neue",Arial,sans-serif;
  --sans:"Apfel Grotezk","Helvetica Neue",Arial,sans-serif;
  --num:"Space Grotesk","Helvetica Neue",Arial,sans-serif;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{width:8.5in;height:11in;background:#fff;color:var(--ink);font-family:var(--sans);
  -webkit-print-color-adjust:exact;print-color-adjust:exact;font-size:9pt;line-height:1.4}}
.page{{position:relative;width:8.5in;height:11in;padding:0.42in 0.55in 0.42in;display:flex;flex-direction:column}}
.num{{font-family:var(--num);font-variant-numeric:tabular-nums}}
.k{{font-family:var(--sans);font-weight:700;font-size:7.5pt;letter-spacing:.14em;text-transform:uppercase;color:var(--accent)}}

/* header */
.hd{{display:flex;justify-content:space-between;align-items:flex-end;padding-bottom:8pt;border-bottom:1.25pt solid var(--ink)}}
.hd svg{{height:0.33in;width:auto;display:block}}
.hd .r{{text-align:right;font-size:8.5pt;color:var(--ink3);line-height:1.35}}
.hd .r b{{display:block;color:var(--ink);font-weight:700;letter-spacing:.1em;text-transform:uppercase;font-size:7.5pt}}

/* hero */
.hero{{display:grid;grid-template-columns:1.25fr .9fr;gap:.4in;align-items:end;padding:10pt 0 8pt}}
h1{{font-family:var(--disp);font-weight:600;font-size:23pt;line-height:1.06;letter-spacing:-.012em;text-wrap:balance}}
h1 em{{font-style:normal;color:var(--accent)}}
.lede{{font-size:9.75pt;line-height:1.48;color:var(--ink2);max-width:3.1in}}
.lede b{{color:var(--ink);font-weight:700}}

/* outcomes strip */
.strip{{display:grid;grid-template-columns:repeat(3,1fr);border-top:.75pt solid var(--line);border-bottom:.75pt solid var(--line)}}
.strip div{{padding:5pt 12pt 5pt 0;font-size:8.75pt;line-height:1.4;color:var(--ink2)}}
.strip div+div{{border-left:.75pt solid var(--line);padding-left:12pt}}
.strip b{{display:block;font-family:var(--disp);font-weight:600;font-size:11pt;color:var(--ink);margin-bottom:2pt;letter-spacing:-.005em}}

/* modules */
.sec{{padding-top:8pt}}
.sec-h{{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:6pt}}
.sec-h h2{{font-family:var(--disp);font-weight:600;font-size:14pt;letter-spacing:-.01em}}
.sec-h span{{font-size:8.25pt;color:var(--ink3)}}
.sec-h span b{{color:var(--ink);font-weight:700;letter-spacing:.08em;text-transform:uppercase;font-size:7.25pt;margin:0 4pt 0 10pt}}
.mods{{display:grid;grid-template-columns:1fr 1fr;gap:7pt 22pt}}
.mod{{border-top:1.25pt solid var(--ink);padding-top:5pt;display:grid;grid-template-columns:22pt 1fr;gap:0 6pt}}
.mod i{{font-family:var(--num);font-style:normal;font-weight:700;font-size:8.5pt;color:var(--accent);padding-top:3pt}}
.mod b{{font-family:var(--disp);font-weight:600;font-size:12pt;display:block;margin-bottom:1pt;letter-spacing:-.005em}}
.mod p{{font-size:8.75pt;line-height:1.4;color:var(--ink2)}}
.alerts{{margin-top:7pt;display:grid;grid-template-columns:auto 1fr;gap:12pt;align-items:center;background:var(--cream);border-left:3pt solid var(--accent);padding:6pt 12pt}}
.alerts b{{font-family:var(--disp);font-weight:600;font-size:12pt;white-space:nowrap}}
.alerts p{{font-size:8.75pt;color:var(--ink2);line-height:1.42}}
.soon{{margin-top:5pt;font-size:8.25pt;color:var(--ink3)}}
.soon b{{color:var(--ink);font-weight:700;letter-spacing:.08em;text-transform:uppercase;font-size:7.5pt;margin-right:6pt}}

/* different */
.diff{{margin-top:8pt;display:grid;grid-template-columns:2.1in 1fr;gap:.35in 0.35in;align-items:center;padding:7pt 0 6pt;border-top:.75pt solid var(--line);border-bottom:.75pt solid var(--line)}}
.diff h2{{font-family:var(--disp);font-weight:600;font-size:15pt;line-height:1.12;letter-spacing:-.01em;margin:3pt 0 5pt}}
.diff p{{font-size:8.75pt;color:var(--ink2);line-height:1.42}}
.chain{{display:grid;grid-template-columns:repeat(4,1fr);gap:0;align-items:stretch}}
.step{{position:relative;padding:6pt 8pt 6pt 9pt;border:.75pt solid var(--line);border-right:0;font-size:8.5pt;line-height:1.3;color:var(--ink2)}}
.step:first-child{{border-radius:5pt 0 0 5pt}}
.step:last-child{{border-radius:0 5pt 5pt 0;border-right:.75pt solid var(--accent);background:var(--accent);color:#fff}}
.step:last-child b,.step:last-child i{{color:#fff}}
.step i{{display:block;font-family:var(--num);font-style:normal;font-weight:700;font-size:7.5pt;color:var(--accent);letter-spacing:.06em;margin-bottom:2pt}}
.step b{{display:block;font-family:var(--disp);font-weight:600;font-size:10pt;color:var(--ink);margin-bottom:1pt}}
.step:not(:last-child)::after{{content:"";position:absolute;right:-5pt;top:50%;width:9pt;height:9pt;background:#fff;border-top:.75pt solid var(--line);border-right:.75pt solid var(--line);transform:translateY(-50%) rotate(45deg);z-index:2}}
.step:nth-child(3)::after{{background:#fff}}
.multi{{grid-column:1/-1;font-size:8.5pt;color:var(--ink3);margin-top:-16pt}}
.multi b{{color:var(--ink);font-weight:700}}

/* pricing */
.price{{margin-top:7pt;display:grid;grid-template-columns:2.1in 1fr;gap:0 .35in;align-items:start}}
.price h2{{font-family:var(--disp);font-weight:600;font-size:14pt;letter-spacing:-.01em;margin-bottom:5pt}}
.price .why{{font-size:8.75pt;color:var(--ink2);line-height:1.42}}
.price .fine{{grid-column:1/-1;margin-top:6pt;font-size:8pt;color:var(--ink3);line-height:1.45}}
table{{width:100%;border-collapse:collapse}}
th{{font-size:7.5pt;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--ink3);text-align:right;padding:0 0 5pt;border-bottom:1.25pt solid var(--ink)}}
th:first-child{{text-align:left}}
td{{padding:3.5pt 0;border-bottom:.75pt solid var(--line);text-align:right;font-size:9.5pt;white-space:nowrap}}
td:first-child{{text-align:left;font-weight:700;color:var(--ink)}}
td .num{{font-weight:600}}
td small{{font-family:var(--sans);font-weight:400;font-size:7.75pt;color:var(--ink3);margin-left:3pt}}
tr.full td{{background:var(--cream);border-bottom:0}}
tr.full td:first-child{{padding-left:6pt;border-left:3pt solid var(--accent)}}
tr.full td:last-child{{padding-right:6pt}}
tr.full td .num{{color:var(--accent);font-weight:700}}

/* cta */
.cta{{margin-top:8pt;background:var(--accent);color:#fff;border-radius:6pt;padding:10pt 15pt;display:grid;grid-template-columns:1fr auto;gap:.3in;align-items:center}}
.cta h2{{font-family:var(--disp);font-weight:600;font-size:16pt;line-height:1.1;letter-spacing:-.01em;text-wrap:balance;max-width:4.4in}}
.cta p{{margin-top:5pt;font-size:9pt;line-height:1.45;color:rgba(255,255,255,.88);max-width:4.3in}}
.contact{{margin-top:8pt;display:flex;flex-wrap:wrap;gap:4pt 14pt;font-size:9pt;font-weight:700}}
.contact span{{opacity:.75;font-weight:400;margin-right:4pt}}
.qr{{background:#fff;border-radius:5pt;padding:7pt;width:1.0in;height:1.0in;display:flex;align-items:center;justify-content:center}}
.qr svg{{width:100%;height:100%;display:block}}
.qr-cap{{text-align:center;font-size:7pt;font-weight:700;letter-spacing:.1em;text-transform:uppercase;margin-top:4pt;color:#fff}}
</style></head><body><div class="page">

<div class="hd">
  {wordmark}
  <div class="r"><b>Restaurant intelligence</b>Built for owners, operators and restaurant groups</div>
</div>

<div class="hero">
  <h1>Know what's happening in your restaurant <em>before</em> it costs you money.</h1>
  <p class="lede">Cavnar AI reads your sales, labor, reviews and marketing every day and tells you <b>what changed, why it matters, and what to do next</b> — no reports to dig through.</p>
</div>

<div class="strip">
  <div><b>Catch it early</b>Problems surface as they start, not when the P&amp;L lands.</div>
  <div><b>Know the why</b>Every number comes with what's driving it, in plain English.</div>
  <div><b>Skip the digging</b>A weekly digest plus instant alerts for what needs you now.</div>
</div>

<div class="sec">
  <div class="sec-h"><h2>What it looks at</h2><span>Start with one module, add the rest when you're ready.<b>Coming soon</b>Bar &amp; Alcohol · Waitlist &amp; Reservations</span></div>
  <div class="mods">
    <div class="mod"><i>01</i><div><b>Labor</b><p>Labor cost against sales by shift, overtime creeping in — and next week's full-staff schedule generated in one click.</p></div></div>
    <div class="mod"><i>02</i><div><b>Food Cost</b><p>Cost pressure by item, menu margins slipping, waste and over-ordering caught before they compound.</p></div></div>
    <div class="mod"><i>03</i><div><b>Reviews</b><p>Guest sentiment across Google and Yelp, recurring complaints, what guests praise, and AI-drafted replies.</p></div></div>
    <div class="mod"><i>04</i><div><b>Marketing</b><p>Posts, texts and emails written from what's actually selling — scheduled, sent, and measured against sales.</p></div></div>
  </div>
  <div class="alerts"><b>AI alerts &amp; recommendations</b><p>Customizable alerts straight to your phone — an urgent one-star review, a labor spike, a cost creeping up — with what to do about it. Nothing waits for you to log in.</p></div>
</div>

<div class="diff">
  <div>
    <div class="k">Why it's different</div>
    <h2>Not another dashboard.</h2>
    <p>Traditional restaurant software tells you what happened. Cavnar AI carries it the rest of the way.</p>
  </div>
  <div class="chain">
    <div class="step"><i>1</i><b>What happened</b>The number, and how far it moved.</div>
    <div class="step"><i>2</i><b>Why it matters</b>What's driving it, in your own data.</div>
    <div class="step"><i>3</i><b>What needs attention</b>Ranked, so you know where to look first.</div>
    <div class="step"><i>4</i><b>What to do next</b>One you can act on today.</div>
  </div>
  <div class="multi"><b>Running more than one location?</b> Each location gets its own intelligence, with the group rolled up in one view.</div>
</div>

<div class="price">
  <div>
    <div class="k">Pricing</div>
    <h2>Simple, per module.</h2>
    <p class="why">Setup covers the full build — connecting your POS and review platforms, configuring your dashboard, and onboarding your team. Then it runs.</p>
  </div>
  <table>
    <tr><th>Modules</th><th>Setup, one-time</th><th>Monthly</th><th>Annual · 2 months free</th></tr>
    <tr><td>1 module</td><td><span class="num">$750</span></td><td><span class="num">$349</span><small>/mo</small></td><td><span class="num">$3,490</span><small>/yr</small></td></tr>
    <tr><td>2 modules</td><td><span class="num">$1,500</span></td><td><span class="num">$649</span><small>/mo</small></td><td><span class="num">$6,490</span><small>/yr</small></td></tr>
    <tr><td>3 modules</td><td><span class="num">$2,250</span></td><td><span class="num">$899</span><small>/mo</small></td><td><span class="num">$8,990</span><small>/yr</small></td></tr>
    <tr class="full"><td>Full platform · all 4</td><td><span class="num">$3,000</span></td><td><span class="num">$1,199</span><small>/mo</small></td><td><span class="num">$11,990</span><small>/yr</small></td></tr>
  </table>
  <p class="fine">Setup is paid up front · Your first 30 days of the retainer are free · Annual billing saves two months · Multiple locations? Ask about group pricing.</p>
</div>

<div class="cta">
  <div>
    <h2>See what Cavnar AI can uncover in your restaurant.</h2>
    <p>Scan to book a free 30-minute walkthrough. I'll look at your numbers with you and show you exactly what's fixable — no commitment.</p>
    <div class="contact"><div><span>Will Cavnar</span>will@cavnar.ai</div><div><span>Call or text</span><span class="num" style="opacity:1;font-weight:700">334-568-9292</span></div><div><span>Web</span>cavnar.ai</div></div>
  </div>
  <div><div class="qr">{qr_svg}</div><div class="qr-cap">Scan to book</div></div>
</div>

</div></body></html>'''
open(f"{ROOT}/brand/print/cavnar-one-sheet.html", "w").write(html)
print("html written", len(html)//1024, "KB")
