"""Printed forms a client hands out, drawn as PDFs in the Cavnar AI brand.

The employee availability sheet (Will, 10/6/26: "a branded employee
availability sheet I can give to each client going forward"). One letter
page: the wordmark, the person, a week of availability, shifts and
certificates, time off already planned, signatures. Its boxes are the ones
the schedule reads (staff_settings): a day is any time / mornings / nights /
not available (DAYPART_CHOICES) or only between two times (time_windows);
full or part time with at least / at most / ideally hours; a minor's age
band; whether they close; the certificates — so a manager can type a sheet
straight into Team.

Paper is light (email's palette, `emails.BRAND`), never the dark UI: it is
printed. Fonts are the brand's, from static/fonts/pdf (Apfel Grotezk there
is converted to TrueType outlines, the only kind reportlab embeds).
"""
import io
import os
from datetime import date

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(ROOT, "static", "fonts", "pdf")
WORDMARK = os.path.join(ROOT, "static", "brand", "wordmark-dark-email.png")
SEAL = os.path.join(ROOT, "static", "brand", "seal-dark-email.png")
# The form's revision, printed in its footer, so a stack of old copies can
# be told from new ones. Bump it with any change to the boxes.
REVISION = date(2026, 10, 6)


def _palette():
    from emails import BRAND
    return {"ink": HexColor(BRAND["strong"]), "body": HexColor(BRAND["body"]), "muted": HexColor(BRAND["muted"]),
            "ember": HexColor(BRAND["ember"]), "ember2": HexColor(BRAND["ember2"]),
            "rule": HexColor(BRAND["border"]), "hair": HexColor(BRAND["rule"]), "tint": HexColor("#fbf7f3")}


_FACES = (("Clash", "ClashDisplay-Medium.ttf"), ("ClashSemi", "ClashDisplay-Semibold.ttf"),
          ("Apfel", "ApfelGrotezk-Regular.ttf"), ("ApfelBold", "ApfelGrotezk-Fett.ttf"),
          ("Space", "SpaceGrotesk.ttf"))


def _fonts():
    have = set(pdfmetrics.getRegisteredFontNames())
    for name, f in _FACES:
        if name not in have:
            pdfmetrics.registerFont(TTFont(name, os.path.join(FONTS, f)))


W, H = letter
M = 38                 # page margin
COL = W - 2 * M


class _Sheet:
    def __init__(self, c, colors):
        self.c, self.k = c, colors

    # ── pieces ──────────────────────────────────────────────────────────────
    def text(self, x, y, s, font="Apfel", size=9, color="body", right=False, spacing=0):
        c = self.c
        c.setFillColor(self.k[color])
        if spacing:
            # Tc is graphics state in PDF and outlives the text object: kept
            # inside save/restore, or every later line inherits the spacing.
            c.saveState()
            t = c.beginText(x, y)
            t.setFont(font, size)
            t.setCharSpace(spacing)
            if right:
                t.setTextOrigin(x - pdfmetrics.stringWidth(s, font, size) - spacing * (len(s) - 1), y)
            t.textOut(s)
            c.drawText(t)
            c.restoreState()
            return
        c.setFont(font, size)
        (c.drawRightString if right else c.drawString)(x, y, s)

    def width(self, s, font="Apfel", size=9):
        return pdfmetrics.stringWidth(s, font, size)

    def box(self, x, y, size=8.5):
        """A tick box whose baseline sits at y."""
        c = self.c
        c.setStrokeColor(self.k["body"])
        c.setLineWidth(0.8)
        c.roundRect(x, y - 1, size, size, 1.6, stroke=1, fill=0)
        return x + size

    def choice(self, x, y, label, size=9):
        x = self.box(x, y) + 5
        self.text(x, y, label, size=size, color="ink")
        return x + self.width(label, size=size) + 14

    def line(self, x1, x2, y, color="rule", width=0.8):
        c = self.c
        c.setStrokeColor(self.k[color])
        c.setLineWidth(width)
        c.line(x1, y, x2, y)

    def field(self, x, y, w, label, value=None):
        """A small label over a writing line; the line sits at y."""
        self.text(x, y + 15, label.upper(), font="ApfelBold", size=6.6, color="muted", spacing=0.7)
        if value:
            self.text(x, y + 3, value, size=11, color="ink")
        self.line(x, x + w, y)

    def blank_date(self, x, y, size=9):
        """__/__/__ written as the house's M/D/YY."""
        seg = 15
        for i in range(3):
            self.line(x, x + seg, y - 2, color="body", width=0.6)
            x += seg
            if i < 2:
                self.text(x + 1.5, y, "/", font="Space", size=size, color="muted")
                x += 7
        return x

    def blank_time(self, x, y):
        self.line(x, x + 34, y - 2, color="body", width=0.6)
        return x + 34

    def section(self, y, n, title, note=None, x0=M, x1=W - M):
        self.text(x0, y, "%02d" % n, font="Space", size=9, color="ember")
        self.text(x0 + 20, y, title.upper(), font="ApfelBold", size=8.6, color="ink", spacing=1.1)
        x = x0 + 20 + self.width(title.upper(), "ApfelBold", 8.6) + 1.1 * len(title) + 10
        if note:
            self.text(x, y, note, size=7.8, color="muted")
            x += self.width(note, size=7.8) + 10
        self.line(x, x1, y + 3, color="hair")


def availability_sheet(restaurant_name=None) -> bytes:
    """The employee availability sheet as PDF bytes; `restaurant_name`
    printed on it when given, a line to write one on when not."""
    _fonts()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle("Employee availability" + (" · " + restaurant_name if restaurant_name else ""))
    c.setAuthor("Cavnar AI")
    c.setSubject("Employee availability sheet")
    s = _Sheet(c, _palette())
    k = s.k
    c.setFillColor(HexColor("#ffffff"))
    c.rect(0, 0, W, H, stroke=0, fill=1)

    # ── masthead ────────────────────────────────────────────────────────────
    top = H - M
    c.drawImage(WORDMARK, M, top - 21, width=120, height=21.1, mask="auto")
    s.text(W - M, top - 8, "STAFF FORM", font="ApfelBold", size=7.6, color="ember", right=True, spacing=1.4)
    s.text(W - M, top - 20, "Fill in, sign, hand to your manager", size=8, color="muted", right=True)
    y = top - 56
    s.text(M, y, "Employee availability", font="ClashSemi", size=25, color="ink")
    c.setStrokeColor(k["ember"])
    c.setLineWidth(2.2)
    c.line(M, y - 10, M + 38, y - 10)
    y -= 26
    s.text(M, y, "When you can work, so the schedule fits your life. It tells your manager when to plan you in;",
           size=8.8, color="body")
    s.text(M, y - 11.5, "it is not a promise of hours. Tell them as soon as anything here changes.", size=8.8,
           color="body")
    y -= 42
    s.field(M, y, COL * 0.62, "Restaurant", restaurant_name)
    fx = M + COL * 0.62 + 18
    s.text(fx, y + 15, "AVAILABLE STARTING", font="ApfelBold", size=6.6, color="muted", spacing=0.7)
    s.blank_date(fx, y + 2)

    # ── 01 about you ────────────────────────────────────────────────────────
    y -= 30
    s.section(y, 1, "About you")
    y -= 28
    third = (COL - 36) / 3
    for row in (("Full name", "Phone", "Email"), ("Position(s)", "Goes by", "Hire date")):
        for i, lab in enumerate(row):
            s.field(M + i * (third + 18), y, third, lab)
        y -= 30
    y += 8
    s.text(M, y, "Employment", font="ApfelBold", size=8.4, color="ink")
    x = M + 62
    x = s.choice(x, y, "Full time")
    x = s.choice(x, y, "Part time")
    x += 8
    s.text(x, y, "Hours a week", font="ApfelBold", size=8.4, color="ink")
    x += s.width("Hours a week", "ApfelBold", 8.4) + 9
    for lab in ("at least", "at most", "ideally"):
        s.text(x, y, lab, size=8.6, color="muted")
        x += s.width(lab, size=8.6) + 4
        s.line(x, x + 26, y - 2, color="body", width=0.6)
        x += 38
    y -= 19
    s.text(M, y, "Under 18?", font="ApfelBold", size=8.4, color="ink")
    x = M + 62
    x = s.choice(x, y, "No")
    x = s.choice(x, y, "16–17")
    x = s.choice(x, y, "14–15")
    s.text(x + 2, y, "Minors have legal limits on hours and late shifts; the schedule follows them.", size=7.8,
           color="muted")

    # ── 02 the week ─────────────────────────────────────────────────────────
    y -= 26
    s.section(y, 2, "Your week", "One box a day, or a window you are free")
    y -= 12
    cols = [("Day", 74), ("Any time", 60), ("Mornings", 60), ("Nights", 60), ("Not available", 72),
            ("Only between", 132), ("Note", 0)]
    cols[-1] = ("Note", COL - sum(w for _l, w in cols[:-1]))
    head_h, row_h = 18, 21
    c.setFillColor(k["ink"])
    c.roundRect(M, y - head_h, COL, head_h, 4, stroke=0, fill=1)
    x = M
    for i, (lab, w) in enumerate(cols):
        tx = x + 9 if i in (0, 5, 6) else x + w / 2 - s.width(lab.upper(), "ApfelBold", 6.8) / 2 - 2.4
        c.saveState()
        c.setFillColor(HexColor("#ffffff"))
        t = c.beginText(tx, y - head_h + 6.5)
        t.setFont("ApfelBold", 6.8)
        t.setCharSpace(0.8)
        t.textOut(lab.upper())
        c.drawText(t)
        c.restoreState()
        x += w
    y -= head_h
    days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    for r, day in enumerate(days):
        if r % 2:
            c.setFillColor(k["tint"])
            c.rect(M, y - row_h, COL, row_h, stroke=0, fill=1)
        base = y - row_h / 2 - 3
        x = M
        for i, (lab, w) in enumerate(cols):
            if i == 0:
                s.text(x + 9, base, day, font="ApfelBold", size=9, color="ink")
            elif i in (1, 2, 3, 4):
                s.box(x + w / 2 - 4.5, base, 9)
            elif i == 5:
                tx = s.blank_time(x + 9, base)
                s.text(tx + 6, base, "to", size=8.2, color="muted")
                s.blank_time(tx + 20, base)
            x += w
        y -= row_h
        s.line(M, W - M, y, color="hair", width=0.6)
    y -= 12
    s.text(M, y, "Write times like 11am or 4:30pm. A window on a school day, a second job, a ride you depend on: "
           "say so in the note.", size=7.8, color="muted")

    # ── 03 shifts and certificates ──────────────────────────────────────────
    y -= 24
    s.section(y, 3, "Shifts and certificates")
    y -= 20
    s.text(M, y, "I prefer", font="ApfelBold", size=8.4, color="ink")
    x = M + 62
    for lab in ("Mornings", "Nights", "No preference"):
        x = s.choice(x, y, lab)
    x += 10
    s.text(x, y, "I can close", font="ApfelBold", size=8.4, color="ink")
    x += s.width("I can close", "ApfelBold", 8.4) + 10
    x = s.choice(x, y, "Yes")
    s.choice(x, y, "No")
    y -= 20
    s.text(M, y, "I hold", font="ApfelBold", size=8.4, color="ink")
    certs = (("Alcohol server (e.g. BASSET)", True), ("Food handler", True), ("Food protection manager", True),
             ("Allergen training", True), ("First aid / CPR", False), ("Keys (can open)", False))
    cw = (COL - 62) / 2
    for i, (lab, exp) in enumerate(certs):
        cx = M + 62 + (i % 2) * cw
        cy = y - (i // 2) * 18
        x = s.choice(cx, cy, lab, size=8.8)
        if exp:
            s.text(cx + cw - 104, cy, "expires", size=7.6, color="muted")
            s.blank_date(cx + cw - 76, cy, size=8)
    y -= 18 * 3

    # ── 04 time off · 05 anything else, side by side ────────────────────────
    y -= 12
    half = (COL - 24) / 2
    rx = M + half + 24
    s.section(y, 4, "Time off planned", x1=M + half)
    s.section(y, 5, "Anything else", x0=rx)
    y -= 22
    for row in range(2):
        cy = y - row * 20
        s.text(M, cy, "From", size=8.4, color="muted")
        x = s.blank_date(M + 26, cy)
        s.text(x + 8, cy, "to", size=8.4, color="muted")
        x = s.blank_date(x + 20, cy)
        s.text(x + 8, cy, "for", size=8.4, color="muted")
        s.line(x + 24, M + half, cy - 2, color="body", width=0.6)
        s.line(rx, W - M, cy - 2, color="body", width=0.6)
    y -= 20

    # ── signatures ──────────────────────────────────────────────────────────
    y -= 40
    sw = (COL - 18) / 2
    for i, who in enumerate(("Employee signature", "Manager signature")):
        x0 = M + i * (sw + 18)
        s.field(x0, y, sw - 86, who)
        s.text(x0 + sw - 74, y + 15, "DATE", font="ApfelBold", size=6.6, color="muted", spacing=0.7)
        s.blank_date(x0 + sw - 74, y + 2)
    y -= 20
    x = s.box(M + sw + 18, y, 8) + 5
    s.text(x, y, "Entered in Cavnar AI on", size=7.8, color="muted")
    s.blank_date(x + s.width("Entered in Cavnar AI on", size=7.8) + 6, y, size=7.8)
    assert y > 52, "the sheet ran into its footer (y=%.0f)" % y

    # ── footer ──────────────────────────────────────────────────────────────
    s.line(M, W - M, 40, color="rule", width=0.6)
    c.drawImage(SEAL, M, 24, width=11, height=11, mask="auto")
    s.text(M + 16, 27, "Employee availability  ·  keep a copy for yourself", size=7.6, color="muted")
    rev = "Rev %d/%d/%s" % (REVISION.month, REVISION.day, REVISION.strftime("%y"))
    s.text(W - M, 27, rev, font="Space", size=7.6, color="muted", right=True)
    c.showPage()
    c.save()
    return buf.getvalue()
