"""sales_audit_routes.py — the in-person Cavnar AI sales audit.

Admin-only pages and JSON under /admin/audits and /admin/api/audits; one
public route, /audit/r/<token>, that serves the customer-facing report for
an explicitly generated, revocable share link and nothing else.
"""
from datetime import date

from flask import Blueprint, jsonify, render_template, request, abort, redirect

from auth import admin_required
import sales_audits as store
import sales_audit_engine as engine
from sales_audit_schema import public_schema, STATUSES
import sales_audit_cheatsheet as cheatsheet

audit_bp = Blueprint("sales_audit", __name__)


@audit_bp.app_template_filter("money")
def _money_filter(v):
    try:
        return "$" + "{:,.0f}".format(float(v or 0))
    except (TypeError, ValueError):
        return "$0"


@audit_bp.app_template_filter("mdy")
def _mdy_filter(v):
    """9/6/26 — the date format used everywhere on the web app."""
    if not v:
        return "—"
    s = str(v)[:10]
    try:
        y, m, d = s.split("-")
        return "%d/%d/%s" % (int(m), int(d), y[2:])
    except (ValueError, AttributeError):
        return s

MAX_BODY = 512 * 1024  # answers + notes for one audit; far above any real audit


def _json():
    if request.content_length and request.content_length > MAX_BODY:
        abort(413)
    return request.get_json(silent=True) or {}


def _clean_blob(v):
    return v if isinstance(v, dict) else None


# ── Pages ────────────────────────────────────────────────────────────────────

import sales_audit_notes_ai as notes_ai


def _results(a):
    """Engine results for an audit row, with the notes reader's last
    conclusions folded in. Marked stale (shown, not applied) when the
    notes have changed since they were read."""
    nr = a.get("notes_ai") or None
    if nr:
        nr = dict(nr, stale=(nr.get("fingerprint") != notes_ai.notes_fingerprint(a)))
    return engine.compute(a["answers"], a.get("pricing_override"), nr)


def _read_notes_into(a):
    """Run the reader and store it. Returns (record, error)."""
    try:
        rec = notes_ai.read_notes(a, engine.compute(a["answers"], a.get("pricing_override")))
    except Exception as e:  # network, key, malformed JSON — never blocks the audit
        return None, str(e)[:300]
    store.store_notes_ai(a["id"], rec)
    return rec, None


@audit_bp.route("/admin/audits")
@admin_required
def audits_page(current_user):
    return render_template("audit_list.html", current_user=current_user)


@audit_bp.route("/admin/audits/<int:audit_id>")
@admin_required
def audit_page(audit_id, current_user):
    a = store.get_audit(audit_id)
    if not a:
        abort(404)
    return render_template("audit_tool.html", current_user=current_user, audit_id=audit_id)


@audit_bp.route("/admin/audits/<int:audit_id>/report")
@admin_required
def audit_report_page(audit_id, current_user):
    a = store.get_audit(audit_id)
    if not a:
        abort(404)
    if not a.get("results"):
        res = engine.compute(a["answers"], a.get("pricing_override"))
        store.store_results(audit_id, res)
        a["results"] = res
    share = store.active_share(audit_id)
    return render_template("audit_report.html", r=store.public_view(a), is_admin=True,
                           share_url=(request.url_root.rstrip("/") + "/audit/r/" + share["token"]) if share else None)


@audit_bp.route("/audit/r/<token>")
def shared_report(token):
    a = store.resolve_share(token)
    if not a or not a.get("results"):
        return render_template("audit_report.html", r=None, is_admin=False, share_url=None), 404
    return render_template("audit_report.html", r=store.public_view(a), is_admin=False, share_url=None)


@audit_bp.route("/admin/audits/<int:audit_id>/cheatsheet")
@admin_required
def audit_cheatsheet_page(audit_id, current_user):
    a = store.get_audit(audit_id)
    if not a:
        abort(404)
    res = a.get("results") or engine.compute(a["answers"], a.get("pricing_override"))
    return render_template("audit_cheatsheet.html", audit=a, sheet=cheatsheet.build(a, res),
                           embed=request.args.get("embed") == "1")


# ── JSON ─────────────────────────────────────────────────────────────────────

@audit_bp.route("/admin/api/audits/schema")
@admin_required
def api_schema(current_user):
    return jsonify(ok=True, schema=public_schema())


@audit_bp.route("/admin/api/audits")
@admin_required
def api_list(current_user):
    rows = store.list_audits(q=request.args.get("q"), status=request.args.get("status"),
                             include_archived=request.args.get("archived") == "1")
    return jsonify(ok=True, audits=rows, statuses=STATUSES)


@audit_bp.route("/admin/api/audits", methods=["POST"])
@admin_required
def api_create(current_user):
    d = _json()
    answers = _clean_blob(d.get("answers")) or {}
    for k in ("restaurant_name", "owner_name", "restaurant_type", "service_model", "city", "state"):
        if isinstance(d.get(k), str) and d[k].strip():
            answers[k] = d[k].strip()[:200]
    if d.get("locations"):
        answers["locations"] = d["locations"]
    audit_date = d.get("audit_date") if isinstance(d.get("audit_date"), str) and len(d.get("audit_date")) == 10 else date.today().isoformat()
    aid = store.create_audit(answers=answers, audit_date=audit_date, created_by=current_user.get("id"))
    return jsonify(ok=True, id=aid)


@audit_bp.route("/admin/api/audits/<int:audit_id>")
@admin_required
def api_get(audit_id, current_user):
    a = store.get_audit(audit_id)
    if not a:
        return jsonify(ok=False, error="Audit not found"), 404
    res = _results(a)
    share = store.active_share(audit_id)
    return jsonify(ok=True, audit=a, results=res, share=({"token": share["token"], "views": share["views"], "created_at": share["created_at"]} if share else None))


@audit_bp.route("/admin/api/audits/<int:audit_id>", methods=["PATCH", "POST"])
@admin_required
def api_save(audit_id, current_user):
    """Autosave. Body: {answers?, notes?, sales?, status?, audit_date?,
    pricing_override?, version}. Returns the saved row's version plus fresh
    results so the live preview stays in step with what is on disk."""
    d = _json()
    try:
        saved = store.save_audit(
            audit_id,
            answers=_clean_blob(d.get("answers")),
            notes=_clean_blob(d.get("notes")),
            sales=_clean_blob(d.get("sales")),
            status=d.get("status") if d.get("status") in STATUSES else None,
            audit_date=d.get("audit_date") if isinstance(d.get("audit_date"), str) and len(d.get("audit_date")) == 10 else None,
            pricing_override=d.get("pricing_override") if "pricing_override" in d else None,
            expected_version=d.get("version"),
        )
    except store.VersionConflict as vc:
        return jsonify(ok=False, conflict=True, error="This audit was changed elsewhere.", audit=vc.current,
                       results=_results(vc.current)), 409
    if not saved:
        return jsonify(ok=False, error="Audit not found"), 404
    res = _results(saved)
    return jsonify(ok=True, version=saved["version"], updated_at=saved["updated_at"], status=saved["status"], results=res)


@audit_bp.route("/admin/api/audits/<int:audit_id>/results")
@admin_required
def api_results(audit_id, current_user):
    a = store.get_audit(audit_id)
    if not a:
        return jsonify(ok=False, error="Audit not found"), 404
    return jsonify(ok=True, results=_results(a))


@audit_bp.route("/admin/api/audits/<int:audit_id>/generate", methods=["POST"])
@admin_required
def api_generate(audit_id, current_user):
    """Freeze the results into the row and mark the report generated. The
    customer report renders from this snapshot, so what Will showed in the
    room is what the share link shows later."""
    a = store.get_audit(audit_id)
    if not a:
        return jsonify(ok=False, error="Audit not found"), 404
    # Notes the reader has not seen yet (or that changed since) get read
    # now, so the frozen report reflects the whole conversation. A reader
    # failure is reported but never stops the generate.
    notes_warning = None
    nr = a.get("notes_ai") or {}
    if notes_ai.collect_notes(a) and nr.get("fingerprint") != notes_ai.notes_fingerprint(a):
        rec, err = _read_notes_into(a)
        if rec:
            a["notes_ai"] = rec
        else:
            notes_warning = "Notes were not read: " + (err or "unknown error")
    res = _results(a)
    store.store_results(audit_id, res, mark_generated=True)
    if a["status"] in ("Draft", "In Progress"):
        store.save_audit(audit_id, status="Completed")
    return jsonify(ok=True, results=res, report_url="/admin/audits/%d/report" % audit_id, notes_warning=notes_warning)


@audit_bp.route("/admin/api/audits/<int:audit_id>/read-notes", methods=["POST"])
@admin_required
def api_read_notes(audit_id, current_user):
    """Run the notes reader on demand. Returns fresh results with the
    reader's conclusions applied."""
    a = store.get_audit(audit_id)
    if not a:
        return jsonify(ok=False, error="Audit not found"), 404
    if not notes_ai.collect_notes(a):
        return jsonify(ok=False, error="No notes to read yet — type something in a section's notes first."), 400
    rec, err = _read_notes_into(a)
    if not rec:
        return jsonify(ok=False, error="The notes reader failed: " + (err or "unknown error")), 502
    a["notes_ai"] = rec
    return jsonify(ok=True, notes_read=rec, results=_results(a))


@audit_bp.route("/admin/api/audits/<int:audit_id>/duplicate", methods=["POST"])
@admin_required
def api_duplicate(audit_id, current_user):
    nid = store.duplicate_audit(audit_id, created_by=current_user.get("id"))
    if not nid:
        return jsonify(ok=False, error="Audit not found"), 404
    return jsonify(ok=True, id=nid)


@audit_bp.route("/admin/api/audits/<int:audit_id>/status", methods=["POST"])
@admin_required
def api_status(audit_id, current_user):
    d = _json()
    st = d.get("status")
    if st not in STATUSES:
        return jsonify(ok=False, error="Unknown status"), 400
    if st == "Archived":
        store.archive_audit(audit_id)
        return jsonify(ok=True, status=st)
    saved = store.save_audit(audit_id, status=st)
    if not saved:
        return jsonify(ok=False, error="Audit not found"), 404
    return jsonify(ok=True, status=saved["status"], version=saved["version"])


@audit_bp.route("/admin/api/audits/<int:audit_id>", methods=["DELETE"])
@admin_required
def api_delete(audit_id, current_user):
    a = store.get_audit(audit_id)
    if not a:
        return jsonify(ok=False, error="Audit not found"), 404
    if not a.get("archived_at") and a.get("status") != "Archived":
        return jsonify(ok=False, error="Archive the audit first, then delete it."), 400
    store.delete_audit(audit_id)
    return jsonify(ok=True)


@audit_bp.route("/admin/api/audits/<int:audit_id>/share", methods=["POST"])
@admin_required
def api_share(audit_id, current_user):
    a = store.get_audit(audit_id)
    if not a:
        return jsonify(ok=False, error="Audit not found"), 404
    if not a.get("report_generated_at"):
        return jsonify(ok=False, error="Generate the final audit first — the link shows the generated report."), 400
    token = store.create_share(audit_id)
    return jsonify(ok=True, token=token, url=request.url_root.rstrip("/") + "/audit/r/" + token)


@audit_bp.route("/admin/api/audits/<int:audit_id>/share", methods=["DELETE"])
@admin_required
def api_unshare(audit_id, current_user):
    store.revoke_shares(audit_id)
    return jsonify(ok=True)


def _latest_active_audit_id():
    rows = store.list_audits()
    for r in rows:
        if r["status"] in ("Draft", "In Progress", "Follow-Up"):
            return r["id"]
    return rows[0]["id"] if rows else None


@audit_bp.route("/admin/audits/latest")
@admin_required
def latest_audit_redirect(current_user):
    """The admin console's 'Open audit' button: straight into the audit
    being worked on, or the list when there isn't one."""
    aid = _latest_active_audit_id()
    return redirect("/admin/audits/%d" % aid if aid else "/admin/audits")


@audit_bp.route("/admin/audits/latest/cheatsheet")
@admin_required
def latest_cheatsheet_redirect(current_user):
    aid = _latest_active_audit_id()
    return redirect("/admin/audits/%d/cheatsheet" % aid if aid else "/admin/audits")


@audit_bp.route("/admin/audits/new")
@admin_required
def new_audit_redirect(current_user):
    aid = store.create_audit(created_by=current_user.get("id"))
    return redirect("/admin/audits/%d" % aid)
