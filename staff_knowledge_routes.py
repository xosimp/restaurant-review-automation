"""
staff_knowledge_routes.py — the HTTP surface of staff_brief and
staff_knowledge (employee audit B7, 10/1/26).

Three blueprints:

  staff_knowledge_bp   /staff/api/...   the employee's own (PIN session):
                       language, docs and certifications, ask. Like
                       staff_routes, nothing here reads a restaurant or a
                       person from the request — both come from the
                       session's membership.
  knowledge_bp         /api/...         the owner / manager console (web,
                       CSRF-protected in hosted_dashboard)
  knowledge_mobile_bp  /mobile/api/...  the same bodies, bearer token —
                       the web/mobile-twin rule: ONE body per route.

Permissions: reading is any console login; approving the brief and the
focus item is SCHEDULE_PUBLISH (it publishes words to staff, as a schedule
does); the house rules and docs are the owner's (TEAM_INVITE — principals);
certifications are SCHEDULE_PUBLISH (whoever schedules people tracks who may
pour or hold the keys). Every write is audited in change_log.
"""
from datetime import timedelta

from flask import Blueprint, jsonify, request

from auth import login_required, mobile_login_required, staff_login_required

staff_knowledge_bp = Blueprint("staff_knowledge", __name__, url_prefix="/staff")
knowledge_bp = Blueprint("knowledge", __name__)
knowledge_mobile_bp = Blueprint("knowledge_mobile", __name__, url_prefix="/mobile/api")
from security import json_object_guard as _json_object_guard
_json_object_guard(staff_knowledge_bp)
_json_object_guard(knowledge_bp)
_json_object_guard(knowledge_mobile_bp)


def _body():
    return request.get_json(silent=True) or {}


# ── the employee's own ──────────────────────────────────────────────────────

def _staff(current_user):
    from staff_routes import _staff_context
    return _staff_context(current_user)


@staff_knowledge_bp.route("/api/language")
@staff_login_required
def staff_language(current_user):
    import staff_knowledge as sk
    lang = sk.language_for(current_user.get("membership_id"))
    return jsonify(ok=True, **sk.languages_payload(lang))


@staff_knowledge_bp.route("/api/language", methods=["POST"])
@staff_login_required
def staff_language_save(current_user):
    import staff_knowledge as sk
    rid, _name = _staff(current_user)
    try:
        lang = sk.set_language(current_user.get("membership_id"), rid, _body().get("language"))
    except sk.KnowledgeError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, **sk.languages_payload(lang))


@staff_knowledge_bp.route("/api/docs")
@staff_login_required
def staff_docs(current_user):
    """The docs that apply to this person (the house rules and everyone's,
    plus their job roles' own) and their OWN certifications."""
    import staff_knowledge as sk
    from auth import get_membership
    from staff_routes import _job_roles, _task_today
    rid, name = _staff(current_user)
    membership = get_membership(current_user["id"], rid) or {}
    roles = _job_roles(rid, membership, name)
    docs = sk.docs_for_staff(rid, roles)
    hr = next((d for d in docs if d["kind"] == "house_rules"), None)
    strip = lambda d: {k: d[k] for k in ("id", "kind", "kind_label", "title", "body", "updated_at")}
    return jsonify(ok=True, house_rules=strip(hr) if hr else None,
                   docs=[strip(d) for d in docs if d["kind"] != "house_rules"],
                   certifications=[{k: c[k] for k in ("cert", "expires_on", "days_left", "status")}
                                   for c in (sk.list_certs(rid, employee_name=name, today=_task_today(rid))
                                             if name else [])],
                   can_ask=bool(hr or docs))


@staff_knowledge_bp.route("/api/ask", methods=["POST"])
@staff_login_required
def staff_ask(current_user):
    """An answer from the house rules, the docs and the person's task
    sheets, citing its lines — or "Ask your manager" (suggest_message)."""
    import staff_knowledge as sk
    from ai_utils import ai_rate_limited
    from auth import get_membership
    from staff_routes import _job_roles
    rid, name = _staff(current_user)
    q = " ".join(str(_body().get("question") or "").split())
    if not q:
        return jsonify(ok=False, error="Type a question."), 400
    if len(q) > sk.ASK_QUESTION_MAX:
        return jsonify(ok=False, error=f"Keep it under {sk.ASK_QUESTION_MAX} characters."), 400
    mid = current_user.get("membership_id")
    if ai_rate_limited(f"staff_ask:{mid}", max_calls=sk.ASK_PER_MINUTE, window_secs=60) or \
            ai_rate_limited(f"staff_ask_day:{mid}", max_calls=sk.ASK_PER_DAY, window_secs=86400):
        return jsonify(ok=False, error="That's a lot of questions — try again in a little while, or ask "
                                       "your manager.", suggest_message=True), 429
    membership = get_membership(current_user["id"], rid) or {}
    roles = _job_roles(rid, membership, name)
    return jsonify(ok=True, **sk.answer(rid, mid, name, roles, q))


# ── the console (web + mobile twins) ────────────────────────────────────────

def _rid(u):
    return u["restaurant_id"]


def _can(u, perm):
    import permissions
    return bool(u.get("is_admin")) or permissions.has_permission(u, getattr(permissions, perm))


def _forbidden(msg):
    return {"ok": False, "error": msg}, 403


def _day(u, raw, writable=False):
    """The business day a request names (today when blank). A write may
    name today or tomorrow only."""
    import preshift
    from datetime import date
    today = preshift.business_day(_rid(u))
    raw = str(raw or "").strip()[:10]
    if not raw:
        return today
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        return None
    if writable and d not in (today, today + timedelta(days=1)):
        return None
    return d


def _do_brief(u):
    import staff_brief as sb
    day = _day(u, request.args.get("day"))
    if day is None:
        return {"ok": False, "error": "day should look like 2026-10-01"}, 400
    out = sb.state(_rid(u), u, day=day, with_suggestions=_can(u, "SCHEDULE_PUBLISH"))
    return {"ok": True, "can_edit": _can(u, "SCHEDULE_PUBLISH"), "brief": out}, 200


def _brief_write(u, fn):
    import staff_brief as sb
    if not _can(u, "SCHEDULE_PUBLISH"):
        return _forbidden("Only someone who publishes the schedule can change what staff read.")
    b = _body()
    day = _day(u, b.get("day"), writable=True)
    if day is None:
        return {"ok": False, "error": "You can set today's or tomorrow's brief."}, 400
    try:
        return {"ok": True, "brief": fn(sb, day, b)}, 200
    except sb.BriefError as e:
        return {"ok": False, "error": str(e)}, 400


def _do_brief_draft(u):
    """Write today's draft now, when the nudge hasn't (one model call a day
    whoever asks; a second ask returns the first draft)."""
    from strategy_routes import _limited
    if _can(u, "SCHEDULE_PUBLISH") and _limited(u, "staff_brief_draft", 3, 60):
        return {"ok": False, "error": "Give it a moment."}, 429

    def go(sb, day, _b):
        # The manager asked and is reading it: no "waiting" push to them.
        sb.draft(_rid(u), day=day, announce=False)
        return sb.state(_rid(u), u, day=day, with_suggestions=True)
    return _brief_write(u, go)


def _do_brief_approve(u):
    def go(sb, day, b):
        text = b.get("text") if "text" in b and b.get("text") is not None else None
        return sb.approve(_rid(u), u, day=day, text=text)
    return _brief_write(u, go)


def _do_brief_withdraw(u):
    return _brief_write(u, lambda sb, day, _b: sb.withdraw(_rid(u), u, day=day))


def _do_brief_focus(u):
    return _brief_write(u, lambda sb, day, b: sb.set_focus(_rid(u), u, b.get("item"), b.get("line") or "", day=day))


def _do_house_rules(u):
    import staff_knowledge as sk
    return {"ok": True, "can_edit": _can(u, "TEAM_INVITE"), "house_rules": sk.house_rules(_rid(u))}, 200


def _do_house_rules_save(u):
    import staff_knowledge as sk
    if not _can(u, "TEAM_INVITE"):
        return _forbidden("Only the account owner can change the house rules.")
    b = _body()
    try:
        doc = sk.save_doc(_rid(u), u, "house_rules", b.get("title") or "House rules", b.get("body"))
    except sk.KnowledgeError as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, "house_rules": doc}, 200


def _do_docs(u):
    import staff_knowledge as sk
    return {"ok": True, "can_edit": _can(u, "TEAM_INVITE"),
            "kinds": [{"kind": k, "label": sk.DOC_KIND_LABEL[k]} for k in sk.DOC_KINDS if k != "house_rules"],
            "docs": [d for d in sk.list_docs(_rid(u)) if d["kind"] != "house_rules"]}, 200


def _do_doc_save(u):
    import staff_knowledge as sk
    if not _can(u, "TEAM_INVITE"):
        return _forbidden("Only the account owner can change staff docs.")
    b = _body()
    if str(b.get("kind") or "") == "house_rules":
        return {"ok": False, "error": "The house rules have their own editor."}, 400
    try:
        doc_id = int(b["id"]) if b.get("id") not in (None, "") else None
        doc = sk.save_doc(_rid(u), u, b.get("kind"), b.get("title"), b.get("body"), roles=b.get("roles"),
                          doc_id=doc_id)
    except (TypeError, ValueError) as e:
        return {"ok": False, "error": str(e) if isinstance(e, sk.KnowledgeError) else "id should be a number"}, 400
    return {"ok": True, "doc": doc}, 200


def _do_doc_remove(u, doc_id):
    import staff_knowledge as sk
    if not _can(u, "TEAM_INVITE"):
        return _forbidden("Only the account owner can change staff docs.")
    try:
        sk.remove_doc(_rid(u), u, doc_id)
    except sk.KnowledgeError as e:
        return {"ok": False, "error": str(e)}, 404
    return {"ok": True}, 200


def _do_certs(u):
    import preshift
    import staff_brief
    import staff_knowledge as sk
    return {"ok": True, "can_edit": _can(u, "SCHEDULE_PUBLISH"), "remind_days": sk.CERT_REMIND_DAYS,
            "certs": sk.list_certs(_rid(u), today=preshift.business_day(_rid(u))),
            "roster": staff_brief.roster_names(_rid(u))}, 200


def _do_cert_save(u):
    import staff_knowledge as sk
    if not _can(u, "SCHEDULE_PUBLISH"):
        return _forbidden("Only someone who schedules the team can change certifications.")
    b = _body()
    try:
        cert = sk.save_cert(_rid(u), u, b.get("employee_name"), b.get("cert"), expires_on=b.get("expires_on"),
                            issued_on=b.get("issued_on"), note=b.get("note"))
    except sk.KnowledgeError as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, "cert": cert}, 200


def _do_cert_remove(u, cert_id):
    import staff_knowledge as sk
    if not _can(u, "SCHEDULE_PUBLISH"):
        return _forbidden("Only someone who schedules the team can change certifications.")
    try:
        sk.remove_cert(_rid(u), u, cert_id)
    except sk.KnowledgeError as e:
        return {"ok": False, "error": str(e)}, 404
    return {"ok": True}, 200


_ROUTES = [
    # (path, methods, body, endpoint)
    ("/staff-brief", ["GET"], _do_brief, "staff_brief"),
    ("/staff-brief/draft", ["POST"], _do_brief_draft, "staff_brief_draft"),
    ("/staff-brief/approve", ["POST"], _do_brief_approve, "staff_brief_approve"),
    ("/staff-brief/withdraw", ["POST"], _do_brief_withdraw, "staff_brief_withdraw"),
    ("/staff-brief/focus", ["POST"], _do_brief_focus, "staff_brief_focus"),
    ("/house-rules", ["GET"], _do_house_rules, "house_rules"),
    ("/house-rules", ["POST"], _do_house_rules_save, "house_rules_save"),
    ("/staff-docs", ["GET"], _do_docs, "staff_docs"),
    ("/staff-docs", ["POST"], _do_doc_save, "staff_doc_save"),
    ("/staff-docs/<int:doc_id>/remove", ["POST"], _do_doc_remove, "staff_doc_remove"),
    ("/staff-certs", ["GET"], _do_certs, "staff_certs"),
    ("/staff-certs", ["POST"], _do_cert_save, "staff_cert_save"),
    ("/staff-certs/<int:cert_id>/remove", ["POST"], _do_cert_remove, "staff_cert_remove"),
]


def _wrap(body, decorator):
    def view(current_user, **kw):
        payload, status = body(current_user, **kw)
        resp = jsonify(**payload)
        resp.headers["Cache-Control"] = "no-store"
        return resp, status
    view.__name__ = body.__name__
    return decorator(view)


for _path, _methods, _body_fn, _ep in _ROUTES:
    knowledge_bp.add_url_rule("/api" + _path, endpoint=_ep, methods=_methods,
                              view_func=_wrap(_body_fn, login_required))
    knowledge_mobile_bp.add_url_rule(_path, endpoint=_ep, methods=_methods,
                                     view_func=_wrap(_body_fn, mobile_login_required))
