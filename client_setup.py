"""client_setup.py — per-client setup that used to run on a production shell.

Scale audit 10/19/26 ONB-7: a client's checklists (docs/clients/<client>/
checklists.json) and its DSR fiscal calendar were loaded by an engineer over
`railway ssh` (scripts/load_checklists.py, "POST /api/dsr/settings as an
owner, or update_restaurant"). At a hundred stores that is a person with
production shell access per store. Both are admin-console actions now
(onboarding_routes.py: dry run first, then apply; audited), and the script
is a thin wrapper over the same functions.

  checklists: plan_checklists(rid, doc) -> what would be added;
              apply_checklists(rid, doc, who) -> adds it. Each sheet lands on
              the task sheet for its job and shift (made when there is none),
              each line once — a re-run adds only what is new.
  DSR:        DSR_PRESETS (4-4-5, 4-5-4, 5-4-4, 13 four-week periods);
              dsr_settings_for(preset, ...) -> the settings body;
              apply_dsr_settings(rid, body) -> validated by the owner route's
              own rules (dsr.settings.validate), then saved.
"""
import models as _models_mod


# ── checklists ───────────────────────────────────────────────────────────────

MAX_CHECKLIST_LINES = 2000


def sheet_rows(sheet) -> list:
    """The sheet's lines in order: [{label, section, critical}]."""
    prefix = (sheet.get("prefix") or "").strip()
    out = []
    for section in sheet.get("sections") or []:
        title = (section.get("title") or "").strip() or None
        for item in section.get("items") or []:
            if not isinstance(item, dict):
                item = {"label": str(item)}
            label = (item.get("label") or "").strip()
            subs = [(s.get("label") if isinstance(s, dict) else s) or "" for s in (item.get("items") or [])]
            lines = [f"{label} - {s.strip()}" for s in subs if s.strip()] if subs else [label]
            for ln in lines:
                if ln:
                    out.append({"label": f"{prefix}: {ln}" if prefix else ln, "section": title,
                                "critical": bool(item.get("critical"))})
    return out


def sheet_lines(sheet) -> list:
    """The sheet's lines in order, as labels."""
    return [r["label"] for r in sheet_rows(sheet)]


def plan_sheets(doc: dict, existing: dict) -> list:
    """[(sheet, [row])] to add: each sheet with the rows not already on that
    job's sheets (`existing`: {job lowercased: set of labels})."""
    out, seen = [], {k: set(v) for k, v in existing.items()}
    for sheet in doc.get("sheets") or []:
        job = (sheet.get("job_code") or "").strip()
        if not job:
            continue
        have = seen.setdefault(job.lower(), set())
        rows = []
        for r in sheet_rows(sheet):
            if r["label"][:200] not in have:
                rows.append(r)
                have.add(r["label"][:200])
        if rows:
            out.append((sheet, rows))
    return out


def plan(doc: dict, existing: dict) -> list:
    """[(job, line)] to add: every sheet's lines not already on that job's
    list (`existing`: {job lowercased: set of labels})."""
    return [((sheet.get("job_code") or "").strip(), r["label"]) for sheet, rows in plan_sheets(doc, existing)
            for r in rows]


def _existing(rid):
    import task_sheets as ts
    sheets = ts.list_sheets(rid)
    existing = {}
    for sh in sheets:
        existing.setdefault(sh["job_code"].strip().lower(), set()).update(l["label"] for l in sh["lines"])
    return sheets, existing


def check_doc(doc):
    """The first thing wrong with a checklists document, or None."""
    if not isinstance(doc, dict) or not isinstance(doc.get("sheets"), list):
        return "A checklists file is a JSON object with a list of sheets (docs/clients/<client>/checklists.json)."
    total = 0
    for sheet in doc["sheets"]:
        if not isinstance(sheet, dict) or not (sheet.get("job_code") or "").strip():
            return "Every sheet needs the job code it is for."
        total += len(sheet_rows(sheet))
    if total > MAX_CHECKLIST_LINES:
        return f"That file has {total} lines; one load takes up to {MAX_CHECKLIST_LINES}."
    return None


def plan_checklists(rid, doc) -> dict:
    """The dry run: {ok, sheets: [{job, shift_kind, title, target_sheet_id,
    lines: [{label, section, critical}]}], lines} — nothing written."""
    err = check_doc(doc)
    if err:
        return {"ok": False, "error": err}
    sheets, existing = _existing(rid)
    out = []
    for sheet, rows in plan_sheets(doc, existing):
        job, kind = sheet["job_code"].strip(), sheet.get("shift_kind") or "any"
        target = next((sh for sh in sheets if sh["job_code"].strip().lower() == job.lower()
                       and sh["shift_kind"] == kind), None)
        out.append({"job": job, "shift_kind": kind, "title": sheet.get("title"),
                    "target_sheet_id": target["id"] if target else None, "lines": rows})
    return {"ok": True, "sheets": out, "lines": sum(len(s["lines"]) for s in out)}


def apply_checklists(rid, doc, who="loaded from file") -> dict:
    """Write what plan_checklists found: new sheets created, lines added.
    {ok, sheets_created, lines_added, error?}."""
    import task_sheets as ts
    err = check_doc(doc)
    if err:
        return {"ok": False, "error": err}
    sheets, existing = _existing(rid)
    created = added = 0
    for sheet, rows in plan_sheets(doc, existing):
        job, kind = sheet["job_code"].strip(), sheet.get("shift_kind") or "any"
        target = next((sh for sh in sheets if sh["job_code"].strip().lower() == job.lower()
                       and sh["shift_kind"] == kind), None)
        try:
            if target is None:
                target = ts.create_sheet(rid, job, kind, title=sheet.get("title"),
                                         requires_signoff=bool(sheet.get("requires_signoff")), who=who)
                sheets.append(target)
                created += 1
            for row in rows:
                ts.add_line(rid, target["id"], row, who=who)
                added += 1
        except ts.TaskSheetError as e:
            return {"ok": False, "error": str(e), "sheets_created": created, "lines_added": added}
    return {"ok": True, "sheets_created": created, "lines_added": added}


# ── the DSR fiscal calendar ──────────────────────────────────────────────────

DSR_PRESETS = {
    "445": "4-4-5 (each quarter: two 4-week periods, then a 5-week one)",
    "454": "4-5-4",
    "544": "5-4-4",
    "4x13": "13 four-week periods",
}


def dsr_settings_for(preset, fiscal_year_start=None, fiscal_week_start_dow=None, **extra) -> dict:
    """The DSR settings body for a preset, with the year's first day and the
    week's first day (Python weekday, 0 = Monday) — the same body the
    owner's DSR settings take. `extra` passes the rest (dsr_gross_basis,
    dsr_deadline_hour, dsr_late_night_hour, dsr_enabled, dsr_notify,
    dsr_budget_mode)."""
    body = dict(extra)
    if preset:
        body["fiscal_period_scheme"] = preset
    if fiscal_year_start is not None:
        body["fiscal_year_start"] = fiscal_year_start
    if fiscal_week_start_dow is not None:
        body["fiscal_week_start_dow"] = fiscal_week_start_dow
    return body


def apply_dsr_settings(rid, body, dry_run=False) -> dict:
    """Validate `body` with the owner route's own rules and save it (unless
    a dry run). A client's docs/clients/<client>/dsr_config.json `settings`
    object is such a body. {ok, fields | error}."""
    from dsr import settings as dsr_settings
    r = _models_mod.get_restaurant(int(rid))
    if r is None:
        return {"ok": False, "error": "Restaurant not found."}
    if isinstance(body, dict) and isinstance(body.get("settings"), dict):
        body = body["settings"]                         # a whole dsr_config.json
    fields, error = dsr_settings.validate(body, r)
    if error:
        return {"ok": False, "error": error}
    if not dry_run:
        _models_mod.update_restaurant(int(rid), fields)
    return {"ok": True, "fields": fields, "dry_run": bool(dry_run)}
