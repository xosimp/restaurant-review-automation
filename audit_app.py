"""
audit_app.py — Cavnar AI Digital Audit Scorecard
Run: python3 audit_app.py
Open: http://localhost:9000
Fill out during Zoom call → click Send → owner gets PDF in their inbox
"""
import os, io, base64
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify, send_file
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import Flowable
import resend

from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL       = os.getenv("FROM_EMAIL", "will@cavnar.ai")

# ── PDF Colors ────────────────────────────────────────────────────────────────
INK         = colors.HexColor("#0e0c0a")
INK2        = colors.HexColor("#3a3530")
INK3        = colors.HexColor("#7a736a")
PAPER       = colors.HexColor("#f7f4ef")
PAPER2      = colors.HexColor("#edeae3")
PAPER3      = colors.HexColor("#e0dbd0")
EMBER       = colors.HexColor("#c84b2f")
EMBER_LIGHT = colors.HexColor("#fdf0eb")
GREEN       = colors.HexColor("#2d5a3d")
GREEN_LIGHT = colors.HexColor("#eaf2ed")
BLUE        = colors.HexColor("#1a56cc")
BLUE_LIGHT  = colors.HexColor("#e8f0fe")
WHITE       = colors.white

def S(name, **kw): return ParagraphStyle(name, **kw)

# ── PDF Generation ────────────────────────────────────────────────────────────
class EmberBar(Flowable):
    def __init__(self, h=3):
        super().__init__()
        self.bh = h; self.width = 0; self.height = h + 4
    def draw(self):
        self.canv.setFillColor(EMBER)
        self.canv.rect(0, 2, self.width, self.bh, stroke=0, fill=1)
    def wrap(self, aw, ah):
        self.width = aw; return aw, self.height

class ScoreRow(Flowable):
    def __init__(self, label, sublabel="", score=0, note=""):
        super().__init__()
        self.label = label; self.sublabel = sublabel
        self.score = score; self.note = note
        self.width = 0; self.height = 46
    def draw(self):
        c = self.canv; w = self.width
        c.setFillColor(WHITE); c.setStrokeColor(PAPER3); c.setLineWidth(0.5)
        c.rect(0, 0, w, self.height, stroke=1, fill=1)
        c.setFillColor(EMBER); c.rect(0, 0, 2, self.height, stroke=0, fill=1)
        c.setFillColor(INK); c.setFont("Helvetica-Bold", 10)
        c.drawString(12, self.height - 16, self.label)
        if self.sublabel:
            c.setFillColor(INK3); c.setFont("Helvetica", 8)
            c.drawString(12, self.height - 28, self.sublabel)
        bw = 26; bh = 22; bg = 4
        sx = w - (5*bw + 4*bg) - 115; by = (self.height - bh) / 2
        c.setFillColor(INK3); c.setFont("Helvetica-Bold", 8)
        c.drawString(sx - 32, by + 7, "SCORE")
        for i in range(5):
            bx = sx + i * (bw + bg)
            filled = (i + 1 == self.score)
            c.setFillColor(EMBER if filled else WHITE)
            c.setStrokeColor(EMBER if filled else PAPER3)
            c.setLineWidth(0.5); c.rect(bx, by, bw, bh, stroke=1, fill=1)
            c.setFillColor(WHITE if filled else INK3)
            c.setFont("Helvetica-Bold", 9)
            c.drawCentredString(bx + bw/2, by + 7, str(i+1))
        nx = sx + 5*bw + 4*bg + 10; nw = w - nx - 8
        if self.note:
            c.setFillColor(INK2); c.setFont("Helvetica", 8)
            display = self.note[:60] + ("…" if len(self.note) > 60 else "")
            c.drawString(nx, by + 7, display)
        else:
            c.setStrokeColor(PAPER3); c.setLineWidth(0.5)
            c.line(nx, by + 4, nx + nw, by + 4)
            c.setFillColor(INK3); c.setFont("Helvetica", 7)
            c.drawString(nx, by + 7, "Notes")
    def wrap(self, aw, ah):
        self.width = aw; return aw, self.height

class OppBox(Flowable):
    def __init__(self, num, title, desc, saving, col=EMBER, bg=EMBER_LIGHT):
        super().__init__()
        self.num=num; self.title=title; self.desc=desc
        self.saving=saving; self.col=col; self.bg=bg
        self.width=0; self.height=76
    def draw(self):
        c = self.canv; w = self.width
        c.setFillColor(self.bg); c.setStrokeColor(self.col); c.setLineWidth(0.5)
        c.roundRect(0, 0, w, self.height, 4, stroke=1, fill=1)
        c.setFillColor(self.col); c.circle(20, self.height-20, 10, stroke=0, fill=1)
        c.setFillColor(WHITE); c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(20, self.height-23, str(self.num))
        c.setFillColor(INK); c.setFont("Helvetica-Bold", 11)
        c.drawString(38, self.height-17, self.title[:55])
        c.setFillColor(INK2); c.setFont("Helvetica", 9)
        desc = self.desc
        if len(desc) > 88:
            mid = desc[:88].rfind(" ")
            c.drawString(38, self.height-32, desc[:mid])
            c.drawString(38, self.height-44, desc[mid+1:112])
            sy = self.height - 60
        else:
            c.drawString(38, self.height-32, desc)
            sy = self.height - 47
        if self.saving:
            pill = self.saving[:40]
            pw = len(pill)*6 + 16
            c.setFillColor(self.col)
            c.roundRect(38, sy-2, pw, 14, 7, stroke=0, fill=1)
            c.setFillColor(WHITE); c.setFont("Helvetica-Bold", 7)
            c.drawString(46, sy+2, pill)
    def wrap(self, aw, ah):
        self.width = aw; return aw, self.height

def generate_pdf(data):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
        leftMargin=0.65*inch, rightMargin=0.65*inch,
        topMargin=0.6*inch, bottomMargin=0.7*inch)
    story = []
    fw = letter[0] - 1.3*inch

    sCellBody  = S("cb", fontName="Helvetica", fontSize=9, textColor=INK2, leading=13)
    sCellBold  = S("cbd", fontName="Helvetica-Bold", fontSize=9, textColor=INK, leading=13)
    sCellBoldW = S("cbdw", fontName="Helvetica-Bold", fontSize=9, textColor=WHITE, leading=13)
    sCellBodyW = S("cbw", fontName="Helvetica", fontSize=9, textColor=WHITE, leading=13)
    sCellSmall = S("cs", fontName="Helvetica", fontSize=8, textColor=INK3, leading=12)
    sEyebrow   = S("ey", fontName="Helvetica-Bold", fontSize=9, textColor=EMBER,
                   leading=12, spaceAfter=6, letterSpacing=2)
    sH2        = S("h2", fontName="Times-Roman", fontSize=17, textColor=INK,
                   leading=22, spaceBefore=14, spaceAfter=6)
    sFooter    = S("ft", fontName="Helvetica", fontSize=8, textColor=INK3,
                   leading=11, alignment=TA_CENTER)

    # Header
    story.append(EmberBar(h=3))
    story.append(Spacer(1, 10))
    hdr = [[
        Paragraph("<b><font color='#0e0c0a' size='22'>Cavnar</font>"
                  "<font color='#c84b2f' size='22'> AI</font></b>",
                  S("lg", fontName="Times-Roman", fontSize=22, textColor=INK, leading=26)),
        Paragraph(
            f"RESTAURANT AI AUDIT SCORECARD<br/>"
            f"<font color='#7a736a' size='8'>will@cavnar.ai  ·  cavnar.ai  ·  Chicago, IL  ·  "
            f"{data.get('date', datetime.now().strftime('%B %d, %Y'))}</font>",
            S("hr", fontName="Helvetica-Bold", fontSize=10, textColor=INK,
              leading=14, alignment=TA_RIGHT))
    ]]
    ht = Table(hdr, colWidths=[fw*0.5, fw*0.5])
    ht.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
    story.append(ht)
    story.append(HRFlowable(width="100%", thickness=0.5, color=PAPER3, spaceAfter=12))

    # Restaurant info
    info = [[
        Paragraph("Restaurant", sCellBoldW),
        Paragraph("Owner / contact", sCellBoldW),
        Paragraph("Audit type", sCellBoldW),
    ],[
        Paragraph(data.get("restaurant","—"), sCellBody),
        Paragraph(data.get("owner","—"), sCellBody),
        Paragraph(data.get("audit_type","Zoom"), sCellBody),
    ]]
    it = Table(info, colWidths=[fw*0.38, fw*0.38, fw*0.24])
    it.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),INK),("TEXTCOLOR",(0,0),(-1,0),WHITE),
        ("BACKGROUND",(0,1),(-1,1),PAPER2),
        ("BOX",(0,0),(-1,-1),0.5,PAPER3),("INNERGRID",(0,0),(-1,-1),0.5,PAPER3),
        ("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7),
        ("LEFTPADDING",(0,0),(-1,-1),10),
    ]))
    story.append(it)
    story.append(Spacer(1, 16))

    # Score guide
    story.append(Paragraph("HOW TO READ THIS REPORT", sEyebrow))
    sg = [[
        Paragraph("<b>1</b> — Critical, fix immediately", sCellSmall),
        Paragraph("<b>2</b> — Significant gap", sCellSmall),
        Paragraph("<b>3</b> — Moderate issue", sCellSmall),
        Paragraph("<b>4</b> — Minor gap", sCellSmall),
        Paragraph("<b>5</b> — Working well", sCellSmall),
    ]]
    sgt = Table(sg, colWidths=[fw/5]*5)
    sgt.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(0,0),EMBER_LIGHT),("BACKGROUND",(1,0),(1,0),colors.HexColor("#fff3ee")),
        ("BACKGROUND",(2,0),(2,0),colors.HexColor("#fef9ec")),
        ("BACKGROUND",(3,0),(3,0),GREEN_LIGHT),("BACKGROUND",(4,0),(4,0),colors.HexColor("#f0f7f3")),
        ("BOX",(0,0),(-1,-1),0.5,PAPER3),("INNERGRID",(0,0),(-1,-1),0.5,PAPER3),
        ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
        ("LEFTPADDING",(0,0),(-1,-1),8),
    ]))
    story.append(sgt)
    story.append(Spacer(1, 16))

    # Sections
    sections = [
        ("01  REVIEW & REPUTATION", "reviews"),
        ("02  LABOR & SCHEDULING",  "labor"),
        ("03  INVENTORY & FOOD WASTE", "inventory"),
        ("04  MARKETING & RETENTION", "marketing"),
        ("05  TECH STACK & OPERATIONS", "tech"),
    ]
    questions = {
        "reviews":   [("Google Business Profile","Claimed, complete, photos?"),
                      ("Yelp profile","Claimed, hours correct, responding?"),
                      ("Review response rate","% of reviews receiving a response"),
                      ("Response time","How quickly are reviews being answered?"),
                      ("Review sentiment","Overall positive/negative ratio")],
        "labor":     [("Scheduling method","Excel, paper, app, or gut feel?"),
                      ("Labor % of revenue","Target: 28-32%"),
                      ("Overtime frequency","How often are staff hitting 40+ hrs?"),
                      ("Overstaffed/understaffed","Does staffing match sales volume?"),
                      ("POS data utilization","Is sales data used to inform scheduling?")],
        "inventory": [("Inventory tracking","Manual count, spreadsheet, or system?"),
                      ("Food cost %","Target: 28-35%"),
                      ("Ordering process","Based on par levels or gut feel?"),
                      ("Waste tracking","Is waste being logged and reviewed?"),
                      ("86'd items frequency","How often do items run out mid-service?")],
        "marketing": [("Social media consistency","Posting frequency and quality"),
                      ("Email/SMS list","Do they have a customer list? Using it?"),
                      ("Loyalty/repeat visits","Any system to encourage return visits?"),
                      ("Google Posts","Using Google Business posts actively?"),
                      ("Content creation time","Hours/week spent on marketing tasks")],
        "tech":      [("POS system","Which POS? Data export available?"),
                      ("Reservation system","OpenTable, Resy, Yelp, or manual?"),
                      ("Online ordering","In-house or third party? Margins?"),
                      ("Team communication","How are staff updates shared?"),
                      ("Owner admin time","Hours/week on non-food tasks")],
    }
    for title, key in sections:
        qs = questions[key]
        rows = [Paragraph(title, sH2),
                HRFlowable(width="100%", thickness=0.5, color=PAPER3, spaceAfter=8)]
        for i, (lbl, sub) in enumerate(qs):
            fkey = f"{key}_{i+1}"
            score = int(data.get(f"{fkey}_score", 0) or 0)
            note  = data.get(f"{fkey}_note", "")
            rows.append(ScoreRow(lbl, sub, score, note))
        rows.append(Spacer(1, 6))
        story.append(KeepTogether(rows))

    # Summary table
    from reportlab.platypus import PageBreak
    story.append(PageBreak())
    story.append(Paragraph("OVERALL SCORE SUMMARY", sEyebrow))
    module_map = {
        "reviews":"Review Intelligence","labor":"Labor Optimizer",
        "inventory":"Inventory Control","marketing":"Marketing Autopilot","tech":"—"
    }
    sum_data = [[
        Paragraph("Area", sCellBoldW),
        Paragraph("Avg score", sCellBoldW),
        Paragraph("Module", sCellBoldW),
        Paragraph("Priority", sCellBoldW),
    ]]
    for _, key in sections:
        scores = [int(data.get(f"{key}_{i+1}_score",0) or 0) for i in range(5)]
        filled = [s for s in scores if s > 0]
        avg = round(sum(filled)/len(filled), 1) if filled else "—"
        priority = ""
        if isinstance(avg, float):
            priority = "HIGH" if avg <= 2 else ("MEDIUM" if avg <= 3.5 else "LOW")
        sum_data.append([
            Paragraph({"reviews":"Review & Reputation","labor":"Labor & Scheduling",
                       "inventory":"Inventory & Waste","marketing":"Marketing & Retention",
                       "tech":"Tech Stack & Ops"}[key], sCellBody),
            Paragraph(str(avg), sCellBold),
            Paragraph(module_map[key], sCellSmall),
            Paragraph(priority, sCellBold),
        ])
    st = Table(sum_data, colWidths=[fw*0.33, fw*0.15, fw*0.30, fw*0.22])
    st.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),INK),("TEXTCOLOR",(0,0),(-1,0),WHITE),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[WHITE, PAPER2]),
        ("BOX",(0,0),(-1,-1),0.5,PAPER3),("INNERGRID",(0,0),(-1,-1),0.5,PAPER3),
        ("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7),
        ("LEFTPADDING",(0,0),(-1,-1),10),
    ]))
    story.append(st)
    story.append(Spacer(1, 18))

    # Opportunities
    story.append(Paragraph("TOP OPPORTUNITIES IDENTIFIED", sEyebrow))
    story.append(Spacer(1, 6))
    opp_colors = [(EMBER, EMBER_LIGHT),(BLUE, BLUE_LIGHT),(GREEN, GREEN_LIGHT)]
    for i in range(1, 4):
        title = data.get(f"opp{i}_title","")
        desc  = data.get(f"opp{i}_desc","")
        saving= data.get(f"opp{i}_saving","")
        if title:
            col, bg = opp_colors[i-1]
            story.append(OppBox(i, title, desc, saving, col, bg))
            story.append(Spacer(1, 8))
    story.append(Spacer(1, 10))

    # Recommendation
    story.append(Paragraph("RECOMMENDED NEXT STEP", sEyebrow))
    rec = data.get("recommendation","")
    if rec:
        story.append(Paragraph(rec, S("rec", fontName="Helvetica", fontSize=10,
            textColor=INK2, leading=15, spaceAfter=8)))
    rec_data = [[
        Paragraph("", sCellBold),
        Paragraph("Est. monthly savings", sCellBold),
        Paragraph("Setup", sCellBold),
        Paragraph("Monthly", sCellBold),
    ],[
        Paragraph("Starter Module (1 system)", sCellBold),
        Paragraph(data.get("saving_starter","$_____"), sCellBody),
        Paragraph("$500", sCellBody),
        Paragraph("$300/mo", sCellBody),
    ],[
        Paragraph("Full System (all 4 modules)", sCellBoldW),
        Paragraph(data.get("saving_full","$_____"), sCellBodyW),
        Paragraph("$2,000", sCellBodyW),
        Paragraph("$1,500/mo", sCellBodyW),
    ]]
    rt = Table(rec_data, colWidths=[fw*0.36, fw*0.22, fw*0.20, fw*0.22])
    rt.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),PAPER2),
        ("BACKGROUND",(0,1),(-1,1),WHITE),
        ("BACKGROUND",(0,2),(-1,2),INK),("TEXTCOLOR",(0,2),(-1,2),WHITE),
        ("BOX",(0,0),(-1,-1),0.5,PAPER3),("INNERGRID",(0,0),(-1,-1),0.5,PAPER3),
        ("TOPPADDING",(0,0),(-1,-1),8),("BOTTOMPADDING",(0,0),(-1,-1),8),
        ("LEFTPADDING",(0,0),(-1,-1),10),
    ]))
    story.append(rt)
    story.append(Spacer(1, 18))

    # Footer
    story.append(HRFlowable(width="100%", thickness=0.5, color=PAPER3, spaceAfter=8))
    story.append(Paragraph(
        "Cavnar AI  ·  will@cavnar.ai  ·  cavnar.ai  ·  Chicago, IL  ·  "
        "This report is confidential and prepared exclusively for the restaurant named above.",
        sFooter))

    doc.build(story)
    buf.seek(0)
    return buf

# ── Email via Resend ─────────────────────────────────────────────────────────
def send_email(to_email, restaurant, pdf_buf):
    pdf_data = base64.b64encode(pdf_buf.read()).decode()
    date_str = datetime.now().strftime("%B %d, %Y")
    resend.api_key = RESEND_API_KEY
    params = {
        "from": f"Will Cavnar <{FROM_EMAIL}>",
        "to": [to_email],
        "subject": f"Your AI Audit Report — {restaurant}",
        "html": f"""
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#1a1714">
  <div style="border-top:3px solid #c84b2f;padding-top:24px;margin-bottom:24px">
    <h2 style="font-family:Georgia,serif;font-size:22px;font-weight:400;margin:0 0 4px">
      Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
    </h2>
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">
      Restaurant AI Consulting · Chicago, IL
    </p>
  </div>
  <p style="font-size:15px;line-height:1.6;margin-bottom:16px">
    Hi — thanks for taking the time today. Attached is your AI audit report for
    <strong>{restaurant}</strong>, completed on {date_str}.
  </p>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:16px">
    The report outlines your scores across all five operational areas,
    the top opportunities we identified, and estimated monthly savings
    from each. Take a look and let me know if you have any questions.
  </p>
  <p style="font-size:14px;color:#3a3530;line-height:1.7;margin-bottom:24px">
    If you would like to move forward, just reply to this email or visit
    <a href="https://cavnar.ai" style="color:#c84b2f">cavnar.ai</a>.
  </p>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    Will Cavnar · Cavnar AI<br/>
    <a href="mailto:will@cavnar.ai" style="color:#c84b2f;text-decoration:none">will@cavnar.ai</a>
    &nbsp;·&nbsp;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
  </p>
</div>""",
        "attachments": [{
            "filename": f"Cavnar_AI_Audit_{restaurant.replace(chr(32), chr(95))}.pdf",
            "content": pdf_data,
            "content_type": "application/pdf",
        }],
    }
    response = resend.Emails.send(params)
    return response.get("id")

# ── Web UI ────────────────────────────────────────────────────────────────────
TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cavnar AI — Audit Scorecard</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --ink:#0e0c0a;--ink2:#3a3530;--ink3:#7a736a;
  --paper:#f7f4ef;--paper2:#edeae3;--paper3:#e0dbd0;
  --ember:#c84b2f;--ember2:#e8956a;--ember-bg:#fdf0eb;
  --green:#2d5a3d;--green-bg:#eaf2ed;
  --blue:#1a56cc;--blue-bg:#e8f0fe;
  --r:8px;
}
body{font-family:'DM Sans',sans-serif;background:var(--paper);color:var(--ink);font-size:14px;line-height:1.6}
.hdr{background:var(--ink);padding:0 32px;height:56px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.hdr-logo{font-family:'DM Serif Display',serif;font-size:18px;color:var(--paper)}
.hdr-logo em{color:var(--ember2);font-style:italic}
.hdr-sub{font-size:11px;color:var(--ink3)}
.container{max-width:860px;margin:0 auto;padding:32px 24px}

.info-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:28px}
.form-group{display:flex;flex-direction:column;gap:5px}
.form-group.full{grid-column:1/-1}
.form-group.half{grid-column:span 2}
label{font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3)}
input,select,textarea{padding:10px 12px;border:1px solid var(--paper3);border-radius:var(--r);font-family:'DM Sans',sans-serif;font-size:13px;color:var(--ink);background:white;outline:none;transition:border .15s;width:100%}
input:focus,select:focus,textarea:focus{border-color:var(--ember)}
textarea{resize:vertical;min-height:70px}

.section{background:white;border:1px solid var(--paper3);border-radius:var(--r);margin-bottom:16px;overflow:hidden}
.section-hdr{background:var(--ink);padding:12px 18px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;user-select:none}
.section-hdr-left{display:flex;align-items:center;gap:10px}
.section-num{font-family:'DM Serif Display',serif;font-size:18px;color:var(--ember2)}
.section-title{font-size:14px;font-weight:500;color:var(--paper)}
.section-avg{font-size:12px;color:var(--ink3)}
.section-body{padding:0}
.question-row{border-bottom:1px solid var(--paper3);padding:14px 18px}
.question-row:last-child{border-bottom:none}
.q-top{display:flex;align-items:center;gap:12px;margin-bottom:8px}
.q-label{font-weight:500;font-size:13px;flex:1}
.q-sub{font-size:11px;color:var(--ink3);margin-top:1px}
.score-btns{display:flex;gap:5px;flex-shrink:0}
.score-btn{width:32px;height:32px;border-radius:6px;border:1px solid var(--paper3);background:white;font-family:'DM Sans',sans-serif;font-size:12px;font-weight:600;color:var(--ink3);cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:center}
.score-btn:hover{border-color:var(--ember);color:var(--ember)}
.score-btn.active{background:var(--ember);border-color:var(--ember);color:white}
.note-input{width:100%;padding:8px 10px;border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px;color:var(--ink);background:var(--paper);outline:none;transition:border .15s}
.note-input:focus{border-color:var(--ember);background:white}

.opp-section{background:white;border:1px solid var(--paper3);border-radius:var(--r);margin-bottom:16px;padding:18px}
.opp-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-top:12px}
.opp-card{background:var(--paper);border:1px solid var(--paper3);border-radius:var(--r);padding:14px}
.opp-num{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;border-radius:50%;font-size:11px;font-weight:600;color:white;margin-bottom:8px}
.opp1 .opp-num{background:var(--ember)}
.opp2 .opp-num{background:var(--blue)}
.opp3 .opp-num{background:var(--green)}

.rec-section{background:white;border:1px solid var(--paper3);border-radius:var(--r);margin-bottom:16px;padding:18px}
.saving-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}

.summary-bar{background:var(--ink);border-radius:var(--r);padding:18px 20px;margin-bottom:24px;display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
.sum-item{text-align:center}
.sum-key{font-size:9px;font-weight:500;color:var(--ink3);letter-spacing:.06em;text-transform:uppercase;margin-bottom:4px}
.sum-val{font-family:'DM Serif Display',serif;font-size:24px;color:var(--paper);line-height:1}
.sum-val.hi{color:#ef9f27}
.sum-val.crit{color:var(--ember2)}
.sum-val.ok{color:#6fcf97}

.action-bar{display:flex;gap:12px;align-items:center;padding:20px 0}
.btn-send{background:var(--ember);color:white;padding:13px 28px;border-radius:var(--r);font-family:'DM Sans',sans-serif;font-size:13px;font-weight:600;border:none;cursor:pointer;transition:background .15s}
.btn-send:hover{background:#a83d25}
.btn-send:disabled{background:var(--ink3);cursor:default}
.btn-dl{background:white;color:var(--ink2);padding:13px 28px;border-radius:var(--r);font-family:'DM Sans',sans-serif;font-size:13px;font-weight:500;border:1px solid var(--paper3);cursor:pointer;transition:all .15s}
.btn-dl:hover{background:var(--paper2)}
.send-status{font-size:13px;padding:10px 16px;border-radius:var(--r);display:none}
.send-status.ok{background:var(--green-bg);color:var(--green);border:1px solid #b7dfca}
.send-status.err{background:#fdf0ef;color:var(--ember);border:1px solid #f5c6c2}

.slabel{font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);margin-bottom:10px}
</style>
</head>
<body>
<header class="hdr">
  <div class="hdr-logo">Cavnar <em>AI</em></div>
  <div class="hdr-sub">Restaurant AI Audit Scorecard</div>
</header>

<div class="container">

  <!-- Restaurant info -->
  <div class="slabel" style="margin-bottom:10px">Audit details</div>
  <div class="info-grid" style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:18px;margin-bottom:24px">
    <div class="form-group">
      <label>Restaurant name</label>
      <input type="text" id="restaurant" placeholder="Maplewood Kitchen">
    </div>
    <div class="form-group">
      <label>Owner / contact name</label>
      <input type="text" id="owner" placeholder="Sarah Johnson">
    </div>
    <div class="form-group">
      <label>Owner email (for report)</label>
      <input type="email" id="owner_email" placeholder="sarah@restaurant.com">
    </div>
    <div class="form-group">
      <label>Date</label>
      <input type="text" id="date" placeholder="May 8, 2026">
    </div>
    <div class="form-group">
      <label>Audit type</label>
      <select id="audit_type">
        <option>Zoom</option>
        <option>In-person</option>
        <option>Phone</option>
      </select>
    </div>
    <div class="form-group">
      <label>Location / neighborhood</label>
      <input type="text" id="location" placeholder="Lincoln Park, Chicago">
    </div>
  </div>

  <!-- Score summary bar -->
  <div class="summary-bar" id="summaryBar">
    <div class="sum-item"><div class="sum-key">Reviews</div><div class="sum-val" id="avg-reviews">—</div></div>
    <div class="sum-item"><div class="sum-key">Labor</div><div class="sum-val" id="avg-labor">—</div></div>
    <div class="sum-item"><div class="sum-key">Inventory</div><div class="sum-val" id="avg-inventory">—</div></div>
    <div class="sum-item"><div class="sum-key">Marketing</div><div class="sum-val" id="avg-marketing">—</div></div>
    <div class="sum-item"><div class="sum-key">Tech</div><div class="sum-val" id="avg-tech">—</div></div>
  </div>

  <!-- Scored sections -->
  <div id="sections"></div>

  <!-- Opportunities -->
  <div class="opp-section">
    <div class="slabel">Top opportunities identified</div>
    <div class="opp-grid">
      <div class="opp-card opp1">
        <div class="opp-num">1</div>
        <div class="form-group" style="margin-bottom:8px"><label>Title</label><input type="text" id="opp1_title" placeholder="Review automation"></div>
        <div class="form-group" style="margin-bottom:8px"><label>Description</label><textarea id="opp1_desc" placeholder="Spending 4hrs/week on reviews manually…" style="min-height:55px"></textarea></div>
        <div class="form-group"><label>Est. saving</label><input type="text" id="opp1_saving" placeholder="$300/mo"></div>
      </div>
      <div class="opp-card opp2">
        <div class="opp-num">2</div>
        <div class="form-group" style="margin-bottom:8px"><label>Title</label><input type="text" id="opp2_title" placeholder="Labor optimizer"></div>
        <div class="form-group" style="margin-bottom:8px"><label>Description</label><textarea id="opp2_desc" placeholder="Scheduling on gut feel, labor at 38%…" style="min-height:55px"></textarea></div>
        <div class="form-group"><label>Est. saving</label><input type="text" id="opp2_saving" placeholder="$1,200/mo"></div>
      </div>
      <div class="opp-card opp3">
        <div class="opp-num">3</div>
        <div class="form-group" style="margin-bottom:8px"><label>Title</label><input type="text" id="opp3_title" placeholder="Food waste control"></div>
        <div class="form-group" style="margin-bottom:8px"><label>Description</label><textarea id="opp3_desc" placeholder="No inventory system, ordering by feel…" style="min-height:55px"></textarea></div>
        <div class="form-group"><label>Est. saving</label><input type="text" id="opp3_saving" placeholder="$800/mo"></div>
      </div>
    </div>
  </div>

  <!-- Recommendation -->
  <div class="rec-section">
    <div class="slabel">Recommendation & next step</div>
    <div class="form-group" style="margin-bottom:12px">
      <label>Your recommendation</label>
      <textarea id="recommendation" placeholder="Based on today's audit, I'd recommend starting with the Review Intelligence module…" style="min-height:80px"></textarea>
    </div>
    <div class="saving-row">
      <div class="form-group">
        <label>Starter module est. savings</label>
        <input type="text" id="saving_starter" placeholder="$300/mo">
      </div>
      <div class="form-group">
        <label>Full system est. savings</label>
        <input type="text" id="saving_full" placeholder="$2,300/mo">
      </div>
    </div>
  </div>

  <!-- Action bar -->
  <div class="action-bar">
    <button class="btn-send" id="sendBtn" onclick="submitAudit('send')">Send report to owner ↗</button>
    <button class="btn-dl" onclick="submitAudit('download')">Download PDF</button>
    <div class="send-status" id="sendStatus"></div>
  </div>

</div>

<script>
const SECTIONS = [
  {key:'reviews', num:'01', title:'Review & Reputation', questions:[
    {lbl:'Google Business Profile',sub:'Claimed, complete, photos uploaded?'},
    {lbl:'Yelp profile',sub:'Claimed, hours correct, responding to reviews?'},
    {lbl:'Review response rate',sub:'% of reviews receiving a response'},
    {lbl:'Response time',sub:'How quickly are reviews being answered?'},
    {lbl:'Review sentiment',sub:'Overall positive/negative ratio'},
  ]},
  {key:'labor', num:'02', title:'Labor & Scheduling', questions:[
    {lbl:'Scheduling method',sub:'Excel, paper, app, or gut feel?'},
    {lbl:'Labor % of revenue',sub:'Target: 28-32%. Write actual % in notes.'},
    {lbl:'Overtime frequency',sub:'How often are staff hitting 40+ hours?'},
    {lbl:'Overstaffed / understaffed',sub:'Does staffing match actual sales volume?'},
    {lbl:'POS data utilization',sub:'Is sales data being used to inform scheduling?'},
  ]},
  {key:'inventory', num:'03', title:'Inventory & Food Waste', questions:[
    {lbl:'Inventory tracking',sub:'Manual count, spreadsheet, or system?'},
    {lbl:'Food cost %',sub:'Target: 28-35%. Write actual % in notes.'},
    {lbl:'Ordering process',sub:'Based on par levels or gut feel?'},
    {lbl:'Waste tracking',sub:'Is waste being logged and reviewed?'},
    {lbl:"86'd items frequency",sub:'How often do items run out mid-service?'},
  ]},
  {key:'marketing', num:'04', title:'Marketing & Retention', questions:[
    {lbl:'Social media consistency',sub:'Posting frequency and quality'},
    {lbl:'Email / SMS list',sub:'Do they have a customer list? Are they using it?'},
    {lbl:'Loyalty / repeat visits',sub:'Any system to encourage return visits?'},
    {lbl:'Google Posts',sub:'Using Google Business posts actively?'},
    {lbl:'Content creation time',sub:'Hours/week spent on marketing tasks'},
  ]},
  {key:'tech', num:'05', title:'Tech Stack & Operations', questions:[
    {lbl:'POS system',sub:'Which POS? Data export available?'},
    {lbl:'Reservation system',sub:'OpenTable, Resy, Yelp, or manual?'},
    {lbl:'Online ordering',sub:'In-house or third party? What are the margins?'},
    {lbl:'Team communication',sub:'How are staff updates shared?'},
    {lbl:'Owner admin time',sub:'Hours/week on non-food operational tasks'},
  ]},
];

const scores = {};
const SCORE_COLORS = {1:'var(--ember)',2:'var(--ember)',3:'#b7791f',4:'var(--green)',5:'var(--green)'};

function buildSections() {
  const container = document.getElementById('sections');
  SECTIONS.forEach(sec => {
    const div = document.createElement('div');
    div.className = 'section';
    div.innerHTML = `
      <div class="section-hdr" onclick="toggleSection('${sec.key}')">
        <div class="section-hdr-left">
          <span class="section-num">${sec.num}</span>
          <span class="section-title">${sec.title}</span>
        </div>
        <span class="section-avg" id="avg-badge-${sec.key}">not scored</span>
      </div>
      <div class="section-body" id="body-${sec.key}">
        ${sec.questions.map((q,i) => `
          <div class="question-row">
            <div class="q-top">
              <div style="flex:1">
                <div class="q-label">${q.lbl}</div>
                <div class="q-sub">${q.sub}</div>
              </div>
              <div class="score-btns">
                ${[1,2,3,4,5].map(n => `<button class="score-btn" id="btn-${sec.key}-${i+1}-${n}" onclick="setScore('${sec.key}',${i+1},${n})">${n}</button>`).join('')}
              </div>
            </div>
            <input class="note-input" type="text" id="note-${sec.key}-${i+1}" placeholder="Add a note…">
          </div>
        `).join('')}
      </div>`;
    container.appendChild(div);
  });
}

function toggleSection(key) {
  const body = document.getElementById('body-'+key);
  body.style.display = body.style.display === 'none' ? '' : 'none';
}

function setScore(sec, q, val) {
  const fkey = `${sec}_${q}`;
  scores[fkey] = val;
  for(let n=1;n<=5;n++) {
    const btn = document.getElementById(`btn-${sec}-${q}-${n}`);
    btn.classList.toggle('active', n === val);
  }
  updateAvg(sec);
}

function updateAvg(sec) {
  const vals = [];
  for(let i=1;i<=5;i++) {
    const s = scores[`${sec}_${i}`];
    if(s) vals.push(s);
  }
  const avg = vals.length ? (vals.reduce((a,b)=>a+b,0)/vals.length).toFixed(1) : null;
  const badge = document.getElementById(`avg-badge-${sec}`);
  const sumVal = document.getElementById(`avg-${sec}`);
  if(avg) {
    const col = parseFloat(avg) <= 2 ? 'crit' : parseFloat(avg) <= 3.5 ? 'hi' : 'ok';
    badge.textContent = `avg ${avg}/5`;
    badge.style.color = parseFloat(avg) <= 2 ? 'var(--ember2)' : parseFloat(avg) <= 3.5 ? '#ef9f27' : '#6fcf97';
    sumVal.textContent = avg;
    sumVal.className = `sum-val ${col}`;
  }
}

function collectData() {
  const data = {};
  ['restaurant','owner','owner_email','date','audit_type','location',
   'opp1_title','opp1_desc','opp1_saving',
   'opp2_title','opp2_desc','opp2_saving',
   'opp3_title','opp3_desc','opp3_saving',
   'recommendation','saving_starter','saving_full'
  ].forEach(id => {
    const el = document.getElementById(id);
    if(el) data[id] = el.value;
  });
  SECTIONS.forEach(sec => {
    sec.questions.forEach((_,i) => {
      const fkey = `${sec.key}_${i+1}`;
      data[`${fkey}_score`] = scores[fkey] || 0;
      const noteEl = document.getElementById(`note-${sec.key}-${i+1}`);
      data[`${fkey}_note`] = noteEl ? noteEl.value : '';
    });
  });
  return data;
}

async function submitAudit(action) {
  const data = collectData();
  const btn = document.getElementById('sendBtn');
  const status = document.getElementById('sendStatus');
  btn.disabled = true;
  btn.textContent = action === 'send' ? 'Sending…' : 'Generating…';
  status.style.display = 'none';

  try {
    const res = await fetch(`/${action}`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(data)
    });

    if(action === 'download') {
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `Cavnar_AI_Audit_${(data.restaurant||'Report').replace(/\s+/g,'_')}.pdf`;
      a.click();
      status.textContent = '✓ PDF downloaded';
      status.className = 'send-status ok';
      status.style.display = 'block';
    } else {
      const result = await res.json();
      if(result.ok) {
        status.textContent = `✓ Report sent to ${data.owner_email}`;
        status.className = 'send-status ok';
      } else {
        status.textContent = `Error: ${result.error}`;
        status.className = 'send-status err';
      }
      status.style.display = 'block';
    }
  } catch(e) {
    status.textContent = 'Something went wrong — check the terminal.';
    status.className = 'send-status err';
    status.style.display = 'block';
  }
  btn.disabled = false;
  btn.textContent = 'Send report to owner ↗';
}

// Set today's date
document.getElementById('date').value = new Date().toLocaleDateString('en-US',{month:'long',day:'numeric',year:'numeric'});

buildSections();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(TEMPLATE)

@app.route("/send", methods=["POST"])
def send():
    data = request.get_json()
    if not data.get("owner_email"):
        return jsonify(ok=False, error="No owner email provided")
    if not RESEND_API_KEY:
        return jsonify(ok=False, error="RESEND_API_KEY not set in .env")
    try:
        pdf_buf = generate_pdf(data)
        result  = send_email(data["owner_email"], data.get("restaurant","Restaurant"), pdf_buf)
        if result:
            return jsonify(ok=True)
        return jsonify(ok=False, error="Email failed — check your RESEND_API_KEY")
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route("/download", methods=["POST"])
def download():
    data    = request.get_json()
    pdf_buf = generate_pdf(data)
    name    = data.get("restaurant","Report").replace(" ","_")
    return send_file(pdf_buf, mimetype="application/pdf",
                     as_attachment=True,
                     download_name=f"Cavnar_AI_Audit_{name}.pdf")  # noqa

if __name__ == "__main__":
    print("\n  Cavnar AI Audit Tool → http://localhost:9000\n")
    app.run(host="0.0.0.0", port=9000, debug=False)
