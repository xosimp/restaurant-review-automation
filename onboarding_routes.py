"""onboarding_routes.py — the admin console's onboarding-at-scale actions
(scale audit 10/19/26, §9). Blueprint: onboarding_bp.

  GET  /admin/api/onboarding/backfill                 backfill slots + the room left (ONB-1)
  POST /admin/api/onboarding/provision/dry-run        {csv} -> each row: create / exists / error (ONB-2)
  POST /admin/api/onboarding/provision/apply          {csv, partner_id?, rpower_token?, send_welcome?}
                                                      step-up; on the admin job pool -> {job_id}
  GET  /admin/api/onboarding/provision/<job_id>       the apply's result when done
  GET  /admin/api/onboarding/provision-batches        the last applied batches
  GET  /admin/api/onboarding/<rid>/setup              setup confirmations, checklists, DSR presets (ONB-7/11/12)
  POST /admin/api/onboarding/<rid>/confirm            {step, value?}
  POST /admin/api/onboarding/<rid>/checklists         {doc, apply} -> the dry run, or the load (ONB-7)
  POST /admin/api/onboarding/<rid>/dsr                {preset?, fiscal_year_start?, fiscal_week_start_dow?,
                                                       settings?, apply} -> the DSR calendar (ONB-7)
  GET  /admin/api/partners                            partners (ONB-10)
  POST /admin/api/partners                            {name, billing_mode, ...} step-up
  POST /admin/api/partners/<pid>/stores               {restaurant_id} step-up

Every write is an admin action recorded through admin_events.
record_admin_action (and the blueprint's writes are in the audit trail);
support logins read, never write (auth.admin_required).
"""
import json

from flask import Blueprint, jsonify, request

from auth import admin_required, recent_auth_required

onboarding_bp = Blueprint("onboarding_admin", __name__)

from security import json_object_guard as _json_object_guard  # noqa: E402
_json_object_guard(onboarding_bp)
import admin_events as _admin_events  # noqa: E402
_admin_events.register_audit(onboarding_bp)


def _audit(current_user, action, restaurant_id=None, target=None, before=None, after=None, result="ok",
           summary=None):
    try:
        _admin_events.record_admin_action(current_user, action, restaurant_id=restaurant_id, target=target,
                                          before=before, after=after, result=result,
                                          summary=summary or f"{current_user.get('username')} {action}")
    except Exception:
        pass


def _body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _restaurant(rid):
    from models import get_restaurant
    return get_restaurant(int(rid))


# ── backfill pacing (ONB-1) ──────────────────────────────────────────────────

@onboarding_bp.route("/admin/api/onboarding/backfill")
@admin_required
def admin_onboarding_backfill(current_user):
    import onboarding
    state = onboarding.backfill_state()
    state["room"] = onboarding.storage_room(onboarding.BACKFILL_BYTES_PER_STORE_DAY * 7)
    return jsonify(ok=True, **state)


# ── bulk provisioning (ONB-2) ────────────────────────────────────────────────

@onboarding_bp.route("/admin/api/onboarding/provision/dry-run", methods=["POST"])
@admin_required
def admin_provision_dry_run(current_user):
    """Every row checked, nothing written."""
    import bulk_provisioning
    rows, err = bulk_provisioning.parse_csv(_body().get("csv"))
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(bulk_provisioning.plan(rows))


@onboarding_bp.route("/admin/api/onboarding/provision/apply", methods=["POST"])
@admin_required
@recent_auth_required()
def admin_provision_apply(current_user):
    """Create the list's new restaurants on the admin job pool (it may read
    RPOWER once). Step-up: it creates logins and keeps a POS credential."""
    import uuid
    import bulk_provisioning
    import ops
    import admin_routes
    data = _body()
    rows, err = bulk_provisioning.parse_csv(data.get("csv"))
    if err:
        return jsonify(ok=False, error=err), 400
    partner_id = data.get("partner_id")
    if partner_id not in (None, ""):
        try:
            partner_id = int(partner_id)
        except (TypeError, ValueError):
            return jsonify(ok=False, error="Pick a partner from the list."), 400
    else:
        partner_id = None
    token = (data.get("rpower_token") or "").strip() or None
    send_welcome = bool(data.get("send_welcome", True))
    actor = current_user.get("username") or "admin"
    job_id = str(uuid.uuid4())
    _audit(current_user, "bulk_provision.started", target=f"job:{job_id}",
           after={"rows": len(rows), "partner_id": partner_id, "rpower_token_given": bool(token),
                  "send_welcome": send_welcome})
    ops.start_async_job(job_id, "bulk_provision", None)

    def _run():
        out = bulk_provisioning.apply(rows, actor, partner_id=partner_id, rpower_token=token,
                                      send_welcome=send_welcome)
        _audit(current_user, "bulk_provision.applied", target=f"batch:{out.get('batch_id')}",
               after={"counts": out.get("counts"), "partner_id": partner_id, "rpower_token_given": bool(token),
                      "send_welcome": send_welcome},
               result="ok" if out.get("ok") else "failed",
               summary=f"{actor} applied a provisioning list: {json.dumps(out.get('counts') or {})}"
                       + ("" if out.get("ok") else f" — {out.get('error')}"))
        ops.finish_async_job(job_id, "done" if out.get("ok") else "error",
                             {k: v for k, v in out.items()})
    why = admin_routes._submit_admin_job(job_id, _run)
    if why:
        ops.finish_async_job(job_id, "error", {"ok": False, "error": why})
        return jsonify(ok=False, error=why), 503
    return jsonify(ok=True, job_id=job_id, rows=len(rows))


@onboarding_bp.route("/admin/api/onboarding/provision/<job_id>")
@admin_required
def admin_provision_status(job_id, current_user):
    import admin_routes
    return admin_routes._admin_job_status(job_id)


@onboarding_bp.route("/admin/api/onboarding/provision-batches")
@admin_required
def admin_provision_batches(current_user):
    import bulk_provisioning
    return jsonify(ok=True, batches=bulk_provisioning.batches())


# ── one store's setup (ONB-7, ONB-11, ONB-12) ────────────────────────────────

@onboarding_bp.route("/admin/api/onboarding/<int:restaurant_id>/setup")
@admin_required
def admin_onboarding_setup(restaurant_id, current_user):
    import onboarding
    import client_setup
    from models import get_conn
    r = _restaurant(restaurant_id)
    if r is None:
        return jsonify(ok=False, error="Restaurant not found"), 404
    conf = onboarding.confirmations(restaurant_id)
    row = {"module_labor": getattr(r, "module_labor", 0), "dsr_enabled": getattr(r, "dsr_enabled", 0),
           "created_at": getattr(r, "created_at", None)}
    conn = get_conn()
    try:
        n_sheets = conn.execute("SELECT COUNT(*) FROM task_sheets WHERE restaurant_id=? AND active=1",
                                (restaurant_id,)).fetchone()[0]
    finally:
        conn.close()
    items = onboarding.setup_items(row, {"confirmations": conf})
    return jsonify(ok=True, restaurant_id=restaurant_id, name=r.name, items=items,
                   started=bool(conf), checklists=int(n_sheets or 0),
                   week_start_day=getattr(r, "week_start_day", 0),
                   dsr={"enabled": bool(getattr(r, "dsr_enabled", 0)),
                        "scheme": getattr(r, "fiscal_period_scheme", None),
                        "year_start": getattr(r, "fiscal_year_start", None),
                        "week_start_dow": getattr(r, "fiscal_week_start_dow", None),
                        "presets": client_setup.DSR_PRESETS},
                   partner=onboarding.partner_for(restaurant_id),
                   partner_billing=onboarding.partner_billing_enabled())


@onboarding_bp.route("/admin/api/onboarding/<int:restaurant_id>/confirm", methods=["POST"])
@admin_required
def admin_onboarding_confirm(restaurant_id, current_user):
    import onboarding
    data = _body()
    step = data.get("step")
    if _restaurant(restaurant_id) is None:
        return jsonify(ok=False, error="Restaurant not found"), 404
    before = (onboarding.confirmations(restaurant_id).get(step) or {}) if isinstance(step, str) else {}
    out = onboarding.confirm(restaurant_id, step, value=data.get("value"),
                             who=current_user.get("username") or "admin")
    if not out.get("ok"):
        return jsonify(out), 400
    _audit(current_user, "onboarding.confirmed", restaurant_id=restaurant_id, target=f"setup:{step}",
           before={"status": before.get("status"), "suggested": before.get("suggested")},
           after={"status": "confirmed", "value": out.get("value"), "applied": out.get("applied")})
    return jsonify(out)


@onboarding_bp.route("/admin/api/onboarding/<int:restaurant_id>/checklists", methods=["POST"])
@admin_required
def admin_onboarding_checklists(restaurant_id, current_user):
    """{doc: the checklists.json object (or its text), apply: false} is the
    dry run; apply: true loads it (idempotent)."""
    import client_setup
    if _restaurant(restaurant_id) is None:
        return jsonify(ok=False, error="Restaurant not found"), 404
    data = _body()
    doc = data.get("doc")
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except ValueError:
            return jsonify(ok=False, error="That isn't valid JSON."), 400
    if not data.get("apply"):
        out = client_setup.plan_checklists(restaurant_id, doc)
        return jsonify(out), (200 if out.get("ok") else 400)
    out = client_setup.apply_checklists(restaurant_id, doc, who=f"loaded by {current_user.get('username') or 'admin'}")
    _audit(current_user, "checklists.loaded", restaurant_id=restaurant_id,
           after={"sheets_created": out.get("sheets_created"), "lines_added": out.get("lines_added")},
           result="ok" if out.get("ok") else "failed")
    return jsonify(out), (200 if out.get("ok") else 400)


@onboarding_bp.route("/admin/api/onboarding/<int:restaurant_id>/dsr", methods=["POST"])
@admin_required
def admin_onboarding_dsr(restaurant_id, current_user):
    """The DSR fiscal calendar from a preset (or a client's dsr_config.json
    `settings`), checked by the owner route's own rules. apply: false is
    the dry run. Applying confirms the setup step."""
    import client_setup
    import onboarding
    r = _restaurant(restaurant_id)
    if r is None:
        return jsonify(ok=False, error="Restaurant not found"), 404
    data = _body()
    if isinstance(data.get("settings"), dict):
        body = data["settings"]
    else:
        preset = data.get("preset")
        if preset and preset not in client_setup.DSR_PRESETS:
            return jsonify(ok=False, error="Pick 4-4-5, 4-5-4, 5-4-4 or 13 four-week periods."), 400
        body = client_setup.dsr_settings_for(preset, fiscal_year_start=data.get("fiscal_year_start"),
                                             fiscal_week_start_dow=data.get("fiscal_week_start_dow"))
    apply = bool(data.get("apply"))
    before = {"scheme": getattr(r, "fiscal_period_scheme", None), "year_start": getattr(r, "fiscal_year_start", None),
              "week_start_dow": getattr(r, "fiscal_week_start_dow", None)}
    out = client_setup.apply_dsr_settings(restaurant_id, body, dry_run=not apply)
    if not out.get("ok"):
        return jsonify(out), 400
    if apply:
        onboarding.confirm(restaurant_id, "dsr_calendar", value=out.get("fields"),
                           who=current_user.get("username") or "admin")
        _audit(current_user, "dsr_calendar.set", restaurant_id=restaurant_id, before=before, after=out.get("fields"))
    return jsonify(out)


# ── partners (ONB-10) ────────────────────────────────────────────────────────

@onboarding_bp.route("/admin/api/partners")
@admin_required
def admin_partners(current_user):
    import onboarding
    return jsonify(ok=True, partners=onboarding.partners(), partner_billing=onboarding.partner_billing_enabled())


@onboarding_bp.route("/admin/api/partners", methods=["POST"])
@admin_required
@recent_auth_required()
def admin_partner_save(current_user):
    """Create or update a partner. Step-up: with PARTNER_BILLING on, a
    partner billing its stores under a signed master agreement takes their
    contract and checkout off them."""
    import onboarding
    data = _body()
    try:
        pid = onboarding.create_partner(data.get("name"), billing_mode=data.get("billing_mode") or "direct",
                                        contact_email=(data.get("contact_email") or "").strip() or None,
                                        master_agreement_ref=(data.get("master_agreement_ref") or "").strip() or None,
                                        master_agreement_signed_at=(data.get("master_agreement_signed_at") or "").strip()
                                        or None)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    _audit(current_user, "partner.saved", target=f"partner:{pid}",
           after={k: data.get(k) for k in ("name", "billing_mode", "master_agreement_ref",
                                           "master_agreement_signed_at", "contact_email")})
    return jsonify(ok=True, partner_id=pid)


@onboarding_bp.route("/admin/api/partners/<int:partner_id>/stores", methods=["POST"])
@admin_required
@recent_auth_required()
def admin_partner_attach(partner_id, current_user):
    """Attach a store to a partner. Step-up (scale re-audit 10/20/26 R8-22):
    with PARTNER_BILLING on it takes the store's own contract and checkout
    off it — the effect saving a partner already stepped up for."""
    import onboarding
    data = _body()
    try:
        rid = int(data.get("restaurant_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Pick a restaurant."), 400
    if _restaurant(rid) is None:
        return jsonify(ok=False, error="Restaurant not found"), 404
    if not any(p["id"] == partner_id for p in onboarding.partners()):
        return jsonify(ok=False, error="Partner not found"), 404
    before = onboarding.partner_for(rid)
    onboarding.attach_store(partner_id, rid)
    _audit(current_user, "partner.store_attached", restaurant_id=rid, target=f"partner:{partner_id}",
           before={"partner_id": (before or {}).get("id")}, after={"partner_id": partner_id})
    return jsonify(ok=True)
