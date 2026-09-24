"""
dsr.xlsx — Erik's weekly grid as a real .xlsx, with no dependency.

openpyxl is not installed and the platform does not add packages for one
export, so this writes the Office Open XML parts directly with zipfile:
[Content_Types].xml, the package and workbook relationships, one workbook,
one worksheet (inline strings, so no shared-strings table), and a styles.xml
with the handful of formats the grid needs — bold, $#,##0, 0.0%, a signed
dollar variance and a title.

Two layers:

  workbook(sheet_name, rows, widths, freeze_rows)   the generic writer: rows
      are lists of cells; a cell is None (left empty — never a 0), a str, a
      number, or (value, style) with style one of STYLES.
  week_workbook(grid, restaurant_name)              the week in the grid's
      layout: title row, header, one row per day, the week's totals, period
      to date. The manager export has no budget columns (the grid payload
      already had them removed by access.redact_grid; this only lays out
      what it was given).

A percentage in the grid is in points (27.4); a cell holds the fraction
(0.274) so Excel's 0.0% format shows it the way the screen does.
"""
import io
import zipfile
from xml.sax.saxutils import escape

# style name → cellXfs index in _styles_xml (order matters)
STYLES = {
    None: 0,
    "bold": 1,
    "money": 2,
    "pct": 3,
    "money_bold": 4,
    "pct_bold": 5,
    "title": 6,
    "header": 7,
    "delta": 8,
    "delta_bold": 9,
    "muted": 10,
}

_MONEY = "&quot;$&quot;#,##0"
_DELTA = "+&quot;$&quot;#,##0;-&quot;$&quot;#,##0;&quot;$&quot;0"


def _styles_xml():
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<numFmts count="3"><numFmt numFmtId="164" formatCode="{_MONEY}"/>'
        '<numFmt numFmtId="165" formatCode="0.0%"/>'
        f'<numFmt numFmtId="166" formatCode="{_DELTA}"/></numFmts>'
        '<fonts count="4">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="14"/><name val="Calibri"/></font>'
        '<font><sz val="10"/><color rgb="FF7A736A"/><name val="Calibri"/></font>'
        '</fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFEDEAE3"/><bgColor indexed="64"/></patternFill></fill>'
        '</fills>'
        '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
        '<border><left/><right/><top/><bottom style="thin"><color rgb="FF7A736A"/></bottom><diagonal/></border>'
        '</borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="11">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'                                  # 0 default
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'                    # 1 bold
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'          # 2 money
        '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'          # 3 pct
        '<xf numFmtId="164" fontId="1" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>'  # 4
        '<xf numFmtId="165" fontId="1" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>'  # 5
        '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'                    # 6 title
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>'  # 7 header
        '<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'          # 8 delta
        '<xf numFmtId="166" fontId="1" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1"/>'  # 9
        '<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"/>'                    # 10 muted
        '</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )


def col_letter(i):
    """0 → A, 25 → Z, 26 → AA."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell_xml(ref, cell):
    style = None
    value = cell
    if isinstance(cell, tuple):
        value, style = cell
    if style not in STYLES:
        raise ValueError(f"unknown style {style!r}")
    s = STYLES[style]
    sattr = f' s="{s}"' if s else ""
    if value is None:
        return f'<c r="{ref}"{sattr}/>' if s else ""
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{sattr}><v>{repr(float(value)) if isinstance(value, float) else value}</v></c>'
    text = escape(str(value))
    return f'<c r="{ref}"{sattr} t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet_xml(rows, widths=None, freeze_rows=0, merges=None):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">']
    if freeze_rows:
        top = f"A{freeze_rows + 1}"
        out.append('<sheetViews><sheetView workbookViewId="0">'
                   f'<pane ySplit="{freeze_rows}" topLeftCell="{top}" activePane="bottomLeft" state="frozen"/>'
                   f'<selection pane="bottomLeft" activeCell="{top}" sqref="{top}"/></sheetView></sheetViews>')
    out.append('<sheetFormatPr defaultRowHeight="15"/>')
    if widths:
        out.append("<cols>" + "".join(
            f'<col min="{i + 1}" max="{i + 1}" width="{float(w):.1f}" customWidth="1"/>'
            for i, w in enumerate(widths)) + "</cols>")
    out.append("<sheetData>")
    for r, row in enumerate(rows, 1):
        cells = "".join(_cell_xml(f"{col_letter(c)}{r}", cell) for c, cell in enumerate(row or []))
        out.append(f'<row r="{r}">{cells}</row>')
    out.append("</sheetData>")
    if merges:
        out.append(f'<mergeCells count="{len(merges)}">' +
                   "".join(f'<mergeCell ref="{m}"/>' for m in merges) + "</mergeCells>")
    out.append('<pageMargins left="0.5" right="0.5" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
               '<pageSetup orientation="landscape"/>')
    out.append("</worksheet>")
    return "".join(out)


def workbook(sheet_name, rows, widths=None, freeze_rows=0, merges=None) -> bytes:
    """A one-sheet .xlsx as bytes."""
    name = escape("".join(ch for ch in str(sheet_name or "Sheet1") if ch not in '[]:*?/\\')[:31] or "Sheet1")
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="xl/workbook.xml"/></Relationships>'),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{name}" sheetId="1" r:id="rId1"/></sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'styles" Target="styles.xml"/></Relationships>'),
        "xl/styles.xml": _styles_xml(),
        "xl/worksheets/sheet1.xml": _sheet_xml(rows, widths, freeze_rows, merges),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, xml in parts.items():
            z.writestr(path, xml.encode("utf-8"))
    return buf.getvalue()


# ── the week, in the grid's layout ──────────────────────────────────────────

def _frac(pct):
    return None if pct is None else round(float(pct) / 100.0, 4)


def columns(grid):
    """[(header, key, kind)] for the grid, in the screen's order. `kind` is
    money | delta | pct | text. Budget columns only when the grid has them
    (the manager's grid had them removed)."""
    owner = "budget" not in (grid.get("withheld") or [])
    cols = [(c, "cat:" + c, "money") for c in grid.get("categories") or []]
    cols += [("Gross", "gross", "money"), ("Net", "net", "money")]
    if owner:
        cols += [("Budget gross", "budget_gross", "money"), ("Budget net", "budget_net", "money"),
                 ("vs Budget", "vs_budget_net", "delta"), ("vs Budget %", "vs_budget_net_pct", "pct")]
    cols += [("Last year", "last_year_net", "money"), ("vs LY", "vs_last_year_net", "delta"),
             ("vs LY %", "vs_last_year_net_pct", "pct"), ("Labor %", "labor_pct", "pct")]
    return cols


_NOTE_COLS = [("Weather", "weather"), ("Event", "event"), ("Influence", "influence")]


def _value(row, key):
    if key.startswith("cat:"):
        return (row.get("cats") or {}).get(key[4:])
    return row.get(key)


def _cell(value, kind, bold=False):
    if value is None:
        return None
    if kind == "text":
        return str(value)
    style = {"money": "money", "delta": "delta", "pct": "pct"}[kind]
    if kind == "pct":
        value = _frac(value)
    return (value, style + ("_bold" if bold else ""))


def title(grid, restaurant_name):
    """"Simple EJ's · Period 9 · Week 4 · 9/16/26 – 9/22/26"."""
    from time_utils import mdy
    parts = [restaurant_name] if restaurant_name else []
    label = grid.get("label") or ""
    if label and not label.startswith("Week of"):
        parts.append(label)
    parts.append(f"{mdy(grid.get('start'))} – {mdy(grid.get('end'))}")
    return " · ".join(parts)


def week_rows(grid, restaurant_name):
    """(rows, widths, merges) — what week_workbook writes, for tests to read."""
    from time_utils import mdy
    cols = columns(grid)
    header = ["Day"] + [h for h, _k, _t in cols] + [h for h, _k in _NOTE_COLS]
    width = len(header)
    rows = [[(title(grid, restaurant_name), "title")] + [None] * (width - 1),
            [(h, "header") for h in header]]
    provisional = 0
    for d in grid.get("days") or []:
        # A provisional night (the report finished with data still syncing)
        # is marked in the workbook as it is on screen — the weekly .xlsx
        # carried no marker, so a provisional night read as final (CA1 D1).
        label = f"{d.get('weekday') or ''} {mdy(d.get('date'))}".strip()
        if d.get("provisional"):
            label += " (provisional)"
            provisional += 1
        row = [label]
        row += [_cell(_value(d, k), t) for _h, k, t in cols]
        row += [_cell(d.get(k), "text") for _h, k in _NOTE_COLS]
        rows.append(row)

    def total_row(label, t):
        if not t:
            return None
        r = [(label, "bold")]
        for _h, k, kind in cols:
            v = (t.get("cats") or {}).get(k[4:]) if k.startswith("cat:") else t.get(k)
            r.append(_cell(v, kind, bold=True))
        return r + [None] * len(_NOTE_COLS)

    tot = total_row("Week total", grid.get("totals"))
    if tot:
        rows.append(tot)
    ptd = grid.get("period_to_date")
    if ptd:
        rows.append(total_row(f"Period to date ({mdy(ptd.get('start'))} – {mdy(ptd.get('end'))})", ptd))
    measured = (grid.get("totals") or {}).get("days_measured")
    rows.append([])
    rows.append([(f"Totals sum only the days that were measured ({measured or 0} of "
                  f"{len(grid.get('days') or [])}). An empty cell was not measured — it is not $0.", "muted")])
    if provisional:
        rows.append([(f"{provisional} night{'' if provisional == 1 else 's'} marked provisional: the report "
                      f"finished while some data was still syncing, and the figures may still change.", "muted")])
    # The gross column can cover fewer nights than net, and can mix the two
    # definitions of gross when the owner switched bases inside the range.
    t = grid.get("totals") or {}
    gd = t.get("gross_days")
    if gd is not None and measured is not None and gd < measured:
        rows.append([(f"Gross totals cover {gd} of {measured} measured nights: on the others the POS didn't "
                      f"report everything the gross includes.", "muted")])
    if t.get("gross_mixed"):
        rows.append([("Gross mixes two definitions this week (items only, and everything rung incl. tax and "
                      "voids): the gross setting changed inside it.", "muted")])
    widths = [30] + [13 if t != "text" else 14 for _h, _k, t in cols] + [24, 22, 36]
    merges = [f"A1:{col_letter(width - 1)}1"]
    return rows, widths, merges


def week_workbook(grid, restaurant_name) -> bytes:
    rows, widths, merges = week_rows(grid, restaurant_name)
    label = grid.get("label") or "Week"
    return workbook(label.replace("·", "-"), rows, widths=widths, freeze_rows=2, merges=merges)


def filename(grid, restaurant_name):
    import re
    base = re.sub(r"[^A-Za-z0-9]+", "-", f"{restaurant_name or 'dsr'} {grid.get('label') or ''}").strip("-")
    from time_utils import mdy
    return f"{base or 'dsr'}-{mdy(grid.get('start')).replace('/', '-')}.xlsx"
