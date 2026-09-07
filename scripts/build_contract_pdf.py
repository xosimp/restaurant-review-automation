"""Build the Cavnar AI Service Agreement PDF that the DocuSign template uses.

    python3 scripts/build_contract_pdf.py            → docs/contracts/Cavnar-AI-Service-Agreement.pdf

Brand: Clash Display headlines, Apfel Grotezk body, Space Grotesk numbers,
the wordmark and seal from static/brand. Prices and billing terms come from
pricing.py so the paper can never disagree with Stripe.

DocuSign fields are placed with ANCHOR TOKENS: invisible (white, 4pt) text
such as {{sig_client}} printed exactly where a field belongs. The template
builder (scripts/docusign_create_template.py) attaches signHere, dateSigned
and text tabs to those anchors, so nobody has to drag fields onto the page
in DocuSign, and re-running this script after a wording change keeps every
field in the right place.
"""
import os
import sys
from datetime import date

from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import HexColor, white
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import pricing  # noqa: E402

OUT = os.path.join(ROOT, "docs", "contracts", "Cavnar-AI-Service-Agreement.pdf")
FONTS = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Fonts")
WORDMARK = os.path.join(ROOT, "static", "brand", "wordmark-dark-email.png")
SEAL = os.path.join(ROOT, "static", "brand", "seal-dark-email.png")

INK = HexColor("#0e0c0a"); INK2 = HexColor("#3a3530"); MUTED = HexColor("#7a736a")
EMBER = HexColor("#c84b2f"); RULE = HexColor("#e0dbd0"); TINT = HexColor("#fbf7f3")

W, H = letter
M = 60           # margin
COL = W - 2 * M  # text column

ANCHORS = {  # token → what the template builder attaches
    "{{restaurant_name}}": "text", "{{owner_name}}": "text", "{{owner_email}}": "text", "{{modules}}": "text",
    "{{setup_fee}}": "text", "{{monthly_fee}}": "text", "{{annual_fee}}": "text",
    "{{sig_client}}": "signHere", "{{date_client}}": "dateSigned", "{{sig_admin}}": "signHere", "{{date_admin}}": "dateSigned",
}


def _truetype(path):
    """reportlab only embeds TrueType (glyf) outlines. Apfel Grotezk ships
    with PostScript (CFF) outlines inside a .ttf wrapper, so convert those
    with fontTools into a temp copy; every other face passes through."""
    with open(path, "rb") as fh:
        if fh.read(4) != b"OTTO":
            return path
    import tempfile
    from fontTools.ttLib import TTFont as FTFont, newTable
    from fontTools.pens.cu2quPen import Cu2QuPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    out = os.path.join(tempfile.gettempdir(), "cavnar-tt-" + os.path.basename(path))
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(path):
        return out
    font = FTFont(path)
    order = font.getGlyphOrder(); gs = font.getGlyphSet()
    glyf = newTable("glyf"); glyf.glyphOrder = order; glyf.glyphs = {}
    for name in order:
        pen = TTGlyphPen(gs)
        gs[name].draw(Cu2QuPen(pen, 1.0, reverse_direction=True))
        glyf.glyphs[name] = pen.glyph()
    font["glyf"] = glyf
    font["loca"] = newTable("loca")
    maxp = newTable("maxp"); maxp.tableVersion = 0x00010000
    for k in ("maxZones", "maxTwilightPoints", "maxStorage", "maxFunctionDefs", "maxInstructionDefs", "maxStackElements",
              "maxSizeOfInstructions", "maxComponentElements", "maxComponentDepth", "maxPoints", "maxContours",
              "maxCompositePoints", "maxCompositeContours"):
        setattr(maxp, k, 1 if k == "maxZones" else 0)
    maxp.numGlyphs = len(order); font["maxp"] = maxp
    post = font["post"]; post.formatType = 2.0; post.extraNames = []; post.mapping = {}; post.glyphOrder = order
    del font["CFF "]
    font.sfntVersion = "\x00\x01\x00\x00"
    font.save(out)
    return out


def _fonts():
    for name, f in (("Clash", "ClashDisplay-Medium.ttf"), ("ClashSemi", "ClashDisplay-Semibold.ttf"),
                    ("Apfel", "ApfelGrotezk-Regular.ttf"), ("ApfelBold", "ApfelGrotezk-Fett.ttf"),
                    ("Space", "SpaceGrotesk.ttf")):
        pdfmetrics.registerFont(TTFont(name, _truetype(os.path.join(FONTS, f))))


class Page:
    def __init__(self, c):
        self.c = c; self.n = 0; self.y = 0; self.new()

    def new(self):
        if self.n:
            self._footer(); self.c.showPage()
        self.n += 1
        self.y = H - M
        if self.n == 1:
            self._masthead()
        else:
            self.c.drawImage(WORDMARK, M, H - M - 4, width=96, height=17, mask="auto")
            self.c.setFillColor(MUTED); self.c.setFont("Apfel", 8.5)
            self.c.drawRightString(W - M, H - M + 2, "SERVICE AGREEMENT  ·  CONTINUED")
            self.y = H - M - 26

    def _masthead(self):
        c = self.c
        c.drawImage(WORDMARK, M, H - M - 12, width=142, height=25, mask="auto")
        c.setFillColor(MUTED); c.setFont("Apfel", 8.5)
        c.drawRightString(W - M, H - M - 2, "RESTAURANT INTELLIGENCE  ·  CAVNAR AI LLC")
        c.setFillColor(MUTED); c.setFont("Space", 8.5)
        c.drawRightString(W - M, H - M - 14, "cavnar.ai  ·  will@cavnar.ai")
        self.y = H - M - 44
        c.setFillColor(INK); c.setFont("Clash", 30)
        c.drawString(M, self.y - 24, "Service Agreement")
        c.setStrokeColor(EMBER); c.setLineWidth(2)
        c.line(M, self.y - 36, M + 44, self.y - 36)
        self.y -= 56

    def _footer(self):
        c = self.c
        c.setStrokeColor(RULE); c.setLineWidth(0.6); c.line(M, 46, W - M, 46)
        c.drawImage(SEAL, M, 30, width=11, height=11, mask="auto")
        c.setFillColor(MUTED); c.setFont("Apfel", 8)
        c.drawString(M + 16, 33, "Cavnar AI LLC  ·  will@cavnar.ai  ·  cavnar.ai")
        c.setFont("Space", 8); c.drawRightString(W - M, 33, "v%s  ·  Page %d" % (date.today().strftime("%-m/%-d/%y"), self.n))

    def need(self, h):
        if self.y - h < 70:
            self.new()

    def anchor(self, token, x, y):
        """Invisible anchor text for a DocuSign tab. White and tiny, but
        real text — DocuSign finds it, a reader never sees it."""
        c = self.c; c.saveState(); c.setFillColor(white); c.setFont("Apfel", 4); c.drawString(x, y, token); c.restoreState()

    def h(self, num, text):
        self.need(40); self.y -= 6
        c = self.c
        c.setFillColor(EMBER); c.setFont("Space", 9.5); c.drawString(M, self.y - 10, num)
        c.setFillColor(INK); c.setFont("ClashSemi", 14); c.drawString(M + 22, self.y - 10, text)
        self.y -= 26

    def para(self, text, size=10, color=INK2, font="Apfel", indent=0, width=None, lead=None):
        width = width or COL - indent; lead = lead or size * 1.34
        words = text.split(" "); lines, cur = [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if pdfmetrics.stringWidth(t, font, size) <= width:
                cur = t
            else:
                lines.append(cur); cur = w
        if cur:
            lines.append(cur)
        self.need(lead * len(lines) + 6)
        self.c.setFillColor(color); self.c.setFont(font, size)
        for ln in lines:
            self.c.drawString(M + indent, self.y - size, ln); self.y -= lead
        self.y -= 4

    def bullet(self, title, text):
        self.need(30)
        c = self.c
        c.setFillColor(EMBER); c.circle(M + 10, self.y - 6.5, 1.6, stroke=0, fill=1)
        c.setFillColor(INK); c.setFont("ApfelBold", 10); c.drawString(M + 20, self.y - 10, title)
        tw = pdfmetrics.stringWidth(title, "ApfelBold", 10) + 4
        c.setFillColor(INK2); c.setFont("Apfel", 10)
        rest = "— " + text
        words = rest.split(" "); cur = ""; x = M + 20 + tw; first = True
        for w in words:
            t = (cur + " " + w).strip() if cur else w
            avail = COL - 20 - (tw if first else 0)
            if pdfmetrics.stringWidth(t, "Apfel", 10) <= avail:
                cur = t
            else:
                c.drawString(x, self.y - 10, cur); self.y -= 14; x = M + 20; first = False; cur = w
        c.drawString(x, self.y - 10, cur); self.y -= 16

    def kv_row(self, label, token, value_hint, note=None, x_label=None):
        """A fee-table row: label · value cell with an anchor · note."""
        self.need(30)
        c = self.c
        c.setFillColor(INK); c.setFont("ApfelBold", 10); c.drawString(M + 12, self.y - 12, label)
        vx = M + 190
        c.setFillColor(HexColor("#fdfcfa")); c.setStrokeColor(RULE); c.setLineWidth(0.6)
        c.roundRect(vx, self.y - 18, 150, 20, 3, stroke=1, fill=1)
        self.anchor(token, vx + 6, self.y - 12)
        c.setFillColor(MUTED); c.setFont("Space", 8.5); c.drawString(vx + 156, self.y - 12, value_hint)
        if note:
            c.setFillColor(MUTED); c.setFont("Apfel", 9); c.drawString(M + 12, self.y - 25, note); self.y -= 12
        self.y -= 24


def build(out=OUT):
    _fonts()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    c = canvas.Canvas(out, pagesize=letter)
    c.setTitle("Cavnar AI Service Agreement"); c.setAuthor("Cavnar AI LLC")
    p = Page(c)
    full = pricing.plan_for(4); starter = pricing.plan_for(1)
    days = pricing.RETAINER_START_DAYS; notice = pricing.NOTICE_DAYS

    # ── parties ──
    p.c.setFillColor(MUTED); p.c.setFont("Apfel", 8.5)
    p.c.drawString(M, p.y - 8, "PARTIES"); p.y -= 18
    colw = COL / 2 - 10
    y0 = p.y
    c.setFillColor(TINT); c.roundRect(M, y0 - 104, colw, 104, 6, stroke=0, fill=1)
    c.roundRect(M + colw + 20, y0 - 104, colw, 104, 6, stroke=0, fill=1)
    c.setFillColor(EMBER); c.setFont("Apfel", 8); c.drawString(M + 14, y0 - 16, "SERVICE PROVIDER")
    c.drawString(M + colw + 34, y0 - 16, "CLIENT")
    c.setFillColor(INK); c.setFont("ClashSemi", 12.5); c.drawString(M + 14, y0 - 34, "Cavnar AI LLC")
    c.setFillColor(INK2); c.setFont("Apfel", 9.5)
    c.drawString(M + 14, y0 - 50, "Will Cavnar, Owner")
    c.drawString(M + 14, y0 - 64, "will@cavnar.ai  ·  cavnar.ai")
    cx = M + colw + 34
    # label on one line, the DocuSign value on the line beneath it
    for lab, tok, yy in (("Restaurant", "{{restaurant_name}}", y0 - 30), ("Owner / authorized signer", "{{owner_name}}", y0 - 58), ("Email", "{{owner_email}}", y0 - 86)):
        c.setFillColor(MUTED); c.setFont("Apfel", 7.5); c.drawString(cx, yy, lab)
        p.anchor(tok, cx, yy - 13)
    p.y = y0 - 116

    p.para("This Service Agreement (the “Agreement”) is between Cavnar AI LLC (“Cavnar AI”) and the Client named above, and takes effect on the date the setup fee is paid. It must be signed before setup begins.", size=9.5, color=MUTED)

    # ── 1 services ──
    p.h("1", "Services")
    p.para("Cavnar AI provides its restaurant intelligence platform, delivered through dashboard.cavnar.ai and the Cavnar AI iOS app, made up of the modules selected below. Every module includes the daily Home brief, the weekly digest email, alerts, and Ask Cavnar, which answers questions from the Client’s own data.")
    p.need(28)
    c.setFillColor(INK); c.setFont("ApfelBold", 10); c.drawString(M + 12, p.y - 12, "Modules included")
    c.setFillColor(HexColor("#fdfcfa")); c.setStrokeColor(RULE); c.roundRect(M + 130, p.y - 18, COL - 130, 20, 3, stroke=1, fill=1)
    p.anchor("{{modules}}", M + 136, p.y - 12); p.y -= 30
    p.bullet("Review Intelligence", "Google review monitoring, replies drafted in the restaurant’s voice for one-click approval, sentiment and topic trends, urgent-review alerts, competitor rating tracking.")
    p.bullet("Labor Optimizer", "labor cost against the Client’s own target by day, shift and role; overtime and overstaffing alerts; schedules built from sales patterns and staff availability.")
    p.bullet("Food Cost Control", "POS-synced inventory and waste tracking, supplier price-creep alerts, suggested order quantities, menu margin reads.")
    p.bullet("Marketing Autopilot", "social, SMS and email content written in the restaurant’s voice and scheduled from one place, a guest text club, and performance tracking.")
    p.para("Modules can be added later by written agreement (email is sufficient) at the then-current published price. Cavnar AI connects to the Client’s systems — Google Business Profile, point of sale, social accounts — only with the Client’s authorization and only to provide these services. Nothing Cavnar AI drafts is published, sent or scheduled without the Client’s approval.", size=9.5)

    # ── 2 fees ── (the whole section stays on one page)
    p.need(250)
    p.h("2", "Fees and billing")
    p.para("Setup fee, one-time. Charged at checkout through the Stripe payment link, before setup begins. Not refundable once setup has started.", size=9.5)
    p.kv_row("Setup fee (one-time)", "{{setup_fee}}", "due at checkout")
    p.need(112)  # keep the retainer paragraph and both fee rows together
    p.para("Retainer. The Client chooses one of the two options at checkout. Either way the retainer begins %d days after the setup fee is paid and is billed automatically through Stripe on that date and on each renewal after it." % days, size=9.5)
    p.kv_row("Monthly retainer", "{{monthly_fee}}", "from day %d, then monthly" % (days + 1))
    p.kv_row("Annual retainer", "{{annual_fee}}", "from day %d, then yearly" % (days + 1), note="Two months free compared with paying monthly.")
    p.para("Published pricing for reference: one module %s setup and %s a month or %s a year; all four %s setup and %s a month or %s a year." % (
        pricing.money(starter["setup"]), pricing.money(starter["monthly"]), pricing.money(starter["annual"]),
        pricing.money(full["setup"]), pricing.money(full["monthly"]), pricing.money(full["annual"])), size=8.5, color=MUTED)

    # ── 3 term ──
    p.h("3", "Term and cancellation")
    p.para("This Agreement begins on the date the setup fee is paid.", size=9.5)
    p.bullet("Monthly", "continues month to month. Either party may cancel with %d days’ written notice by email. Access continues through the end of the period already paid for. No cancellation fee." % notice)
    p.bullet("Annual", "runs for twelve months from the first retainer charge and renews for a further twelve months unless either party gives %d days’ written notice before the renewal date. A prepaid annual period is not refundable." % notice)
    p.para("If a payment fails and is not resolved within ten days of notice, Cavnar AI may suspend the service until it is. Setup fees are not refundable.", size=9.5)

    # ── 4 responsibilities ──
    p.h("4", "Responsibilities")
    p.need(110)
    top = p.y; half = COL / 2 - 10
    c.setFillColor(EMBER); c.setFont("Apfel", 8); c.drawString(M, top - 8, "THE CLIENT AGREES TO"); c.drawString(M + half + 20, top - 8, "CAVNAR AI AGREES TO")
    left = ["Provide accurate business information and keep it current.", "Review AI-drafted content before it is published.", "Keep a valid payment method on file.", "Keep dashboard and app credentials private."]
    right = ["Build and configure the selected modules within 24 hours of setup payment clearing.", "Keep the service available to the best of its ability and say so when it is not.", "Respond to support requests within one business day.", "Keep all Client data strictly confidential."]
    def col(items, x):
        y = top - 24
        for it in items:
            c.setFillColor(EMBER); c.circle(x + 3, y - 3.5, 1.5, stroke=0, fill=1)
            words = it.split(" "); cur = ""; lines = []
            for w in words:
                t = (cur + " " + w).strip()
                if pdfmetrics.stringWidth(t, "Apfel", 9.5) <= half - 14: cur = t
                else: lines.append(cur); cur = w
            lines.append(cur)
            c.setFillColor(INK2); c.setFont("Apfel", 9.5)
            for ln in lines:
                c.drawString(x + 12, y - 7, ln); y -= 13
            y -= 5
        return y
    ya = col(left, M); yb = col(right, M + half + 20)
    p.y = min(ya, yb) - 6

    # ── 5 data ── (kept on one page together with the signatures)
    p.need(300)
    p.h("5", "Data, confidentiality and liability")
    p.para("All Client data remains the property of the Client. Cavnar AI will not sell, share or use Client data for any purpose other than providing the services in this Agreement, and retains it for the life of the account plus thirty days, after which it is deleted on request. Cavnar AI uses third-party AI models to draft content and analysis; all AI-generated content and recommendations should be reviewed before use, and Cavnar AI is not liable for decisions made on the basis of them. Cavnar AI’s total liability under this Agreement is limited to the fees paid by the Client in the thirty days before the claim.", size=9.5)

    # ── signatures ──
    p.need(180); p.y -= 6
    c.setFillColor(MUTED); c.setFont("Apfel", 8.5); c.drawString(M, p.y - 8, "SIGNATURES"); p.y -= 14
    p.para("By signing below, both parties agree to the terms of this Service Agreement.", size=9.5)
    top = p.y; half = COL / 2 - 10
    for i, (kicker, who, sig, dt) in enumerate((("CLIENT", "Authorized signer", "{{sig_client}}", "{{date_client}}"),
                                                ("CAVNAR AI LLC", "Will Cavnar, Owner", "{{sig_admin}}", "{{date_admin}}"))):
        x = M + i * (half + 20)
        c.setFillColor(TINT); c.roundRect(x, top - 130, half, 130, 6, stroke=0, fill=1)
        c.setFillColor(EMBER); c.setFont("Apfel", 8); c.drawString(x + 14, top - 15, kicker)
        c.setStrokeColor(RULE); c.setLineWidth(0.7); c.line(x + 14, top - 82, x + half - 14, top - 82)
        p.anchor(sig, x + 16, top - 76)
        c.setFillColor(MUTED); c.setFont("Apfel", 8); c.drawString(x + 14, top - 93, "Signature")
        c.setFillColor(INK); c.setFont("Apfel", 9.5); c.drawString(x + 14, top - 107, who)
        c.setFillColor(MUTED); c.setFont("Apfel", 8); c.drawString(x + 14, top - 121, "Date")
        p.anchor(dt, x + 48, top - 121)
    p.y = top - 140

    p._footer(); c.save()
    return out


if __name__ == "__main__":
    print(build())
