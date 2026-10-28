"""
rpower_routes.py — Flask routes for the RPOWER POS integration
Blueprint: rpower_bp
Registered in hosted_dashboard.py alongside the other blueprints.

Admin endpoints (admin_required, restaurant_id in URL):
  POST /admin/rpower/save/<restaurant_id>        save + verify a token (or bind a held credential)
  POST /admin/rpower/bootstrap/<restaurant_id>   resolve which store this is
  POST /admin/rpower/sync/<restaurant_id>        kick a background sync
  GET  /admin/rpower/status/<restaurant_id>
  POST /admin/rpower/disconnect/<restaurant_id>
  GET  /admin/rpower/credentials                 the partner credentials (never a token)
  POST /admin/rpower/credentials/<id>/rotate     a new token for every store on it, one step
  POST /admin/rpower/credentials/<id>/revoke     retire a leaked token
  POST /admin/rpower/live-mirror/<restaurant_id> a demo's override to read a live store
  POST /admin/rpower/owner-check/<restaurant_id> release a bulk store's owner hold

Client endpoints (login_required, scoped to the session's restaurant):
  GET  /api/rpower/status
  POST /api/rpower/sync          the account card's Sync button (429 within
                                 SYNC_NOW_COOLDOWN_MINUTES of the last press)
  POST /api/rpower/disconnect    principal only, with the password step-up
  GET  /api/pos/store-check      the bulk owner check's state (app twin: mobile_api)
  POST /api/pos/store-check      {store_name, serial} — the owner confirms the store

Connecting RPOWER is deliberately admin-only and has no client-facing save.
The token is issued by RPOWER to Cavnar as an integrator, not to the
restaurant — an owner has nothing to paste, and offering them a field would
imply otherwise. Since Audit #23 (F7, 10/21/26) the token is held ONCE, as a
partner credential (credentials.pos_credentials), and a restaurant binds the
credential's id: a rotation is one step, not one paste per store.
"""
from flask import Blueprint, request, jsonify
from auth import admin_required, login_required, recent_auth_required
from models import update_restaurant

rpower_bp = Blueprint("rpower", __name__)
# "Sync now" from the account card or the app, at most once in this many
# minutes per restaurant (POS-21; do_sync) — pos.SYNC_NOW_COOLDOWN_MINUTES,
# the one cooldown every Sync now shares (scale audit 10/19/26 A3-09).
from pos import SYNC_NOW_COOLDOWN_MINUTES
# A JSON body must be an object: "x" or [1] used to 500 (SEC-32).
from security import json_object_guard as _json_object_guard
_json_object_guard(rpower_bp)
# Its /admin writes (token, bootstrap, sync, disconnect) are in the audit trail (#126).
import admin_events as _admin_events
_admin_events.register_audit(rpower_bp)

# Every RPOWER field a disconnect clears — the credential binding with them.
_DISCONNECT_FIELDS = ("rpower_token", "rpower_credential_id", "rpower_cg", "rpower_store_mid", "rpower_store_name",
                      "rpower_verified_at", "rpower_sync_error", "rpower_last_synced")


def _rpower_state(kwargs):
    """What an RPOWER admin write changes, for its typed audit row (R7-13):
    the binding and whether a token is held — never the token."""
    from models import get_restaurant
    r = get_restaurant(kwargs.get("restaurant_id"))
    if not r:
        return {}
    return {"token_set": bool((getattr(r, "rpower_token", None) or "").strip()
                              or getattr(r, "rpower_credential_id", None)),
            "credential_id": getattr(r, "rpower_credential_id", None),
            "cg": getattr(r, "rpower_cg", None), "store_mid": getattr(r, "rpower_store_mid", None),
            "store_name": getattr(r, "rpower_store_name", None),
            "live_mirror": int(getattr(r, "pos_live_mirror", 0) or 0),
            "owner_hold": bool(getattr(r, "pos_owner_hold", None))}


@rpower_bp.route("/admin/rpower/save/<int:restaurant_id>", methods=["POST"])
@admin_required
@recent_auth_required()
def save_rpower_token(restaurant_id, current_user):
    """Typed audit around the save (R7-13; the Toast, Square and Clover
    twins already had one): the binding before and after, never the token."""
    return _audited_rpower("rpower.token_saved", restaurant_id, current_user, _save_rpower_token)


def _audited_rpower(action, restaurant_id, current_user, body, extra=None):
    before = _rpower_state({"restaurant_id": restaurant_id})
    resp = body(restaurant_id, current_user)
    status = resp[1] if isinstance(resp, tuple) else getattr(resp, "status_code", 200)
    after = _rpower_state({"restaurant_id": restaurant_id})
    if extra:
        after.update(extra)
    _admin_events.record_admin_action(
        current_user, action, restaurant_id=restaurant_id, target=f"restaurant:{restaurant_id}",
        before=before, after=after,
        result="ok" if int(status or 200) < 400 else "failed",
        summary=f"{current_user.get('username') or 'admin'} {action} ({status})")
    return resp


def _save_rpower_token(restaurant_id, current_user):
    """Save a token, after checking it actually works.

    The token is verified against /store/get before anything is written, so a
    typo fails here rather than at 5am in a sync job nobody is watching. When
    the token sees exactly one store the binding is completed in the same
    call; when it sees several the stores are returned for the admin to pick
    from, because binding a restaurant to the wrong store produces a module
    full of somebody else's numbers that looks entirely normal.

    The token is held once, as a partner credential (A7-01): pasting a token
    another store already uses binds the same credential, and
    {credential_id} instead of {token} binds a credential already held
    without pasting anything. Refused, before anything is written, where it
    could not be encrypted (A7-08)."""
    import rpower
    import credentials
    from models import get_restaurant, pos_binding_conflict
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    try:
        want_cred = int(data.get("credential_id") or 0) or None
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Pick a partner credential from the list."), 400
    if want_cred and not token:
        token = credentials.credential_token(want_cred) or ""
        if not token:
            return jsonify(ok=False, error="That partner credential can't be read (revoked, or its key is "
                                           "missing)."), 400
    if not token:
        return jsonify(ok=False, error="A token is required."), 400
    r = get_restaurant(restaurant_id)
    if not r:
        return jsonify(ok=False, error="Restaurant not found"), 404
    refused = credentials.saves_refused()
    if refused and not token.startswith("demo-store:"):
        return jsonify(ok=False, error=refused), 503

    with rpower.for_store(restaurant_id):
        probe = rpower.test_token(token)
    if not probe["ok"]:
        return jsonify(ok=False, error=probe["error"]), 400
    stores = probe["stores"]

    # A demo reads a live store only with the audited override (A7-15).
    mirror = rpower.demo_live_store_refusal(r, token)
    if mirror:
        return jsonify(ok=False, error=mirror), 400
    # One store, one restaurant (fix round #143): refused before anything is
    # written. A token that sees several stores is checked when one is
    # chosen (rpower.bootstrap).
    if len(stores) == 1 and not int(getattr(r, "is_demo", 0) or 0):
        clash = pos_binding_conflict("rpower", (stores[0].get("cg"), stores[0].get("store_mid")),
                                     exclude_id=restaurant_id)
        if clash:
            return jsonify(ok=False, error=f"That RPOWER store is already connected to {clash}. One store can "
                                           f"feed only one restaurant — disconnect it there first."), 400

    if token.startswith("demo-store:"):
        # The simulated store's token is no secret and never leaves the
        # process: it stays on the row (demo_store reads it there).
        fields = {"rpower_token": token, "rpower_credential_id": None}
    else:
        try:
            cred_id = credentials.credential_for_token("rpower", token,
                                                       actor=current_user.get("username") or "admin",
                                                       label="RPOWER partner token")
        except credentials.CredentialKeyError as e:
            return jsonify(ok=False, error=str(e)), 503
        fields = {"rpower_credential_id": cred_id, "rpower_token": None}
    fields.update({"rpower_sync_error": None, "pos_system": "RPOWER"})
    current = (rpower._creds(restaurant_id).get("token") or "").strip()
    if token != current:
        # A new token is a new connection. The old store binding used to stay
        # while a choice between several stores was pending — the restaurant
        # half-connected to a store the new token may not even see (fix
        # round #143). Its sync stamp goes with it unless the new token
        # resolves to the same store.
        prev = (str(getattr(r, "rpower_cg", "") or ""), str(getattr(r, "rpower_store_mid", "") or ""))
        fields.update({"rpower_cg": None, "rpower_store_mid": None, "rpower_store_name": None,
                       "rpower_verified_at": None})
        if not (len(stores) == 1 and (str(stores[0].get("cg") or ""), str(stores[0].get("store_mid") or "")) == prev):
            fields["rpower_last_synced"] = None
    update_restaurant(restaurant_id, fields)
    if len(stores) == 1:
        result = rpower.bootstrap(restaurant_id, store_mid=stores[0]["store_mid"], probe=probe)
        if not result.get("ok"):
            return jsonify(ok=False, error=result.get("error"), stores=stores), 400
        return jsonify(ok=True, connected=True, store=result["store"],
                       applied=result.get("applied", []), credential_id=fields.get("rpower_credential_id"),
                       message=f"Connected to {stores[0].get('name') or 'RPOWER'}.")
    return jsonify(ok=True, connected=False, needs_choice=True, stores=stores,
                   credential_id=fields.get("rpower_credential_id"),
                   message=f"Token saved. It can see {len(stores)} stores — pick one.")


@rpower_bp.route("/admin/rpower/bootstrap/<int:restaurant_id>", methods=["POST"])
@admin_required
@recent_auth_required()
def bootstrap_rpower(restaurant_id, current_user):
    """Bind this restaurant to one of the stores the token can see."""
    return _audited_rpower("rpower.bootstrapped", restaurant_id, current_user, _bootstrap_rpower)


def _bootstrap_rpower(restaurant_id, current_user):
    import rpower
    data = request.get_json(silent=True) or {}
    store_mid = (data.get("store_mid") or "").strip() or None
    result = rpower.bootstrap(restaurant_id, store_mid=store_mid)
    return jsonify(**result), (200 if result.get("ok") else 400)


@rpower_bp.route("/admin/rpower/sync/<int:restaurant_id>", methods=["POST"])
@admin_required
@_admin_events.audited("pos.rpower.sync_started")
def sync_rpower(restaurant_id, current_user):
    """Kick a sync in the background so the admin screen doesn't hang on it —
    through pos.sync_restaurant on the bounded admin pool
    (scheduler.start_manual_pos_sync, #65), so it is recorded in the Data
    Health ledger like the nightly sync. Returns a job id to poll."""
    import rpower
    if not rpower.is_connected(restaurant_id):
        return jsonify(ok=False, error="RPOWER isn't fully connected for this restaurant."), 400
    import scheduler
    job_id, _joined = scheduler.start_manual_pos_sync(restaurant_id, current_user.get("username") or "admin")
    return jsonify(ok=True, job_id=job_id, message="Sync started.")


@rpower_bp.route("/admin/rpower/status/<int:restaurant_id>")
@admin_required
def rpower_status_admin(restaurant_id, current_user):
    import rpower
    from models import get_restaurant
    out = rpower.get_connection_status(restaurant_id)
    r = get_restaurant(restaurant_id)
    if r is not None:
        out.update(live_mirror=bool(int(getattr(r, "pos_live_mirror", 0) or 0)),
                   is_demo=bool(int(getattr(r, "is_demo", 0) or 0)),
                   owner_hold=getattr(r, "pos_owner_hold", None))
    if out.get("credential_id"):
        import credentials
        out["credential"] = credentials.get_credential(out["credential_id"])
    return jsonify(**out)


def _clear_fields():
    from time_utils import utc_stamp
    fields = {f: None for f in _DISCONNECT_FIELDS}
    # The POS purge clock starts (A7-13): the archive goes
    # POS_PURGE_AFTER_DISCONNECT_DAYS later unless the store connects again.
    fields["pos_disconnected_at"] = utc_stamp()
    return fields


@rpower_bp.route("/admin/rpower/disconnect/<int:restaurant_id>", methods=["POST"])
@admin_required
@recent_auth_required()
def disconnect_rpower(restaurant_id, current_user):
    """Clear every RPOWER field.

    The store binding goes with the token deliberately: a cg and store_mid
    left behind after a token is removed is a half-connected state that
    is_connected would keep reporting as live. The sync stamp goes too: left
    behind, it decayed to 0% freshness under every labor and food card
    (re-audit B6#1). The partner credential itself is untouched — other
    stores read through it."""
    def _clear(rid, _user):
        update_restaurant(rid, _clear_fields())
        return jsonify(ok=True, message="RPOWER disconnected.")
    return _audited_rpower("rpower.disconnected", restaurant_id, current_user, _clear)


# ── the partner credential (Audit #23 F7, A7-01) ────────────────────────────

@rpower_bp.route("/admin/rpower/credentials")
@admin_required
def rpower_credentials(current_user):
    """Every RPOWER partner credential: id, label, version, a short digest,
    when it was made and rotated, how many live stores read through it —
    never the token."""
    import credentials
    import bulk_provisioning
    return jsonify(ok=True, credentials=credentials.list_credentials("rpower"),
                   max_stores=bulk_provisioning.max_stores_per_credential(), key=credentials.key_state())


def _audit_credential(current_user, action, cred_id, before, after, result="ok", summary=None):
    _admin_events.record_admin_action(current_user, action, target=f"pos_credential:{cred_id}",
                                      before=before, after=after, result=result,
                                      summary=summary or f"{current_user.get('username') or 'admin'} {action}")


@rpower_bp.route("/admin/rpower/credentials/<int:cred_id>/rotate", methods=["POST"])
@admin_required
@recent_auth_required()
def rotate_rpower_credential(cred_id, current_user):
    """One step for every store on this credential: the new token is checked
    against RPOWER first, and refused when it cannot see a store bound to
    the credential (unless {force: true} — the stores it cannot see stop
    syncing, named in the answer). The old token's per-row copies are
    cleared. Typed audit (version before and after, never a token)."""
    import credentials
    import rpower
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    before = credentials.get_credential(cred_id)
    if not before:
        return jsonify(ok=False, error="No such credential."), 404
    if not token:
        return jsonify(ok=False, error="Paste the new token."), 400
    refused = credentials.saves_refused()
    if refused:
        return jsonify(ok=False, error=refused), 503
    probe = rpower.test_token(token)
    if not probe.get("ok"):
        _audit_credential(current_user, "rpower.credential_rotated", cred_id, before,
                          {"refused": probe.get("error")}, result="refused")
        return jsonify(ok=False, error=probe.get("error")), 400
    sees = {(str(s.get("cg")), str(s.get("store_mid"))) for s in probe.get("stores") or []}
    from models import get_conn
    conn = get_conn()
    try:
        bound = [dict(x) for x in conn.execute(
            "SELECT id, name, rpower_cg, rpower_store_mid FROM restaurants WHERE rpower_credential_id=? "
            "AND COALESCE(rpower_store_mid,'') <> ''", (cred_id,)).fetchall()]
    finally:
        conn.close()
    unseen = [{"restaurant_id": b["id"], "name": b["name"]} for b in bound
              if (str(b["rpower_cg"]), str(b["rpower_store_mid"])) not in sees]
    if unseen and not data.get("force"):
        return jsonify(ok=False, unseen=unseen,
                       error=f"The new token can't see {len(unseen)} of the stores on this credential. Send "
                             f"force to rotate anyway (those stores stop syncing until they are re-bound)."), 409
    try:
        out = credentials.rotate(cred_id, token, actor=current_user.get("username") or "admin")
    except credentials.CredentialKeyError as e:
        return jsonify(ok=False, error=str(e)), 503
    if not out.get("ok"):
        return jsonify(**out), 400
    # A refused token's short-circuit is lifted for the stores it serves.
    for b in bound:
        try:
            update_restaurant(b["id"], {"rpower_auth_failed_at": None})
        except Exception:
            pass
    after = credentials.get_credential(cred_id)
    after.update(unseen=unseen, legacy_cleared=out.get("legacy_cleared"))
    _audit_credential(current_user, "rpower.credential_rotated", cred_id, before, after,
                      summary=f"{current_user.get('username') or 'admin'} rotated RPOWER credential #{cred_id} "
                              f"to v{out['version']} ({out['stores']} stores)")
    return jsonify(ok=True, version=out["version"], stores=out["stores"], unseen=unseen,
                   message=f"Rotated. {out['stores']} store(s) read with the new token from their next request.")


@rpower_bp.route("/admin/rpower/credentials/<int:cred_id>/revoke", methods=["POST"])
@admin_required
@recent_auth_required()
def revoke_rpower_credential(cred_id, current_user):
    """Retire a credential (a leaked token): every store on it reads as not
    connected until it is bound to another. Typed audit."""
    import credentials
    before = credentials.get_credential(cred_id)
    if not before:
        return jsonify(ok=False, error="No such credential."), 404
    if before.get("revoked_at"):
        # Already retired (admin audit 10/27/26 C8): it answered 200 "Revoked."
        # with ok false, and audited a failure that was a repeat.
        _audit_credential(current_user, "rpower.credential_revoked", cred_id, before, before, result="refused",
                          summary=f"{current_user.get('username') or 'admin'}: RPOWER credential #{cred_id} "
                                  "was already revoked")
        return jsonify(ok=False, error="That credential is already revoked.", stores=before.get("stores")), 409
    out = credentials.revoke(cred_id, actor=current_user.get("username") or "admin")
    _audit_credential(current_user, "rpower.credential_revoked", cred_id, before,
                      credentials.get_credential(cred_id), result="ok" if out.get("ok") else "failed")
    if not out.get("ok"):
        return jsonify(ok=False, error="The credential could not be revoked — read it again and retry.",
                       stores=out.get("stores")), 409
    return jsonify(ok=True, stores=out.get("stores"),
                   message=f"Revoked. {out.get('stores')} store(s) stop syncing until re-bound.")


@rpower_bp.route("/admin/rpower/live-mirror/<int:restaurant_id>", methods=["POST"])
@admin_required
@recent_auth_required()
def rpower_live_mirror(restaurant_id, current_user):
    """A demo restaurant's override to read a LIVE RPOWER store (A7-15) —
    every login on the demo then sees that store's sales, wages and staff.
    {on: bool, reason}. A reason is required to turn it on. Typed audit."""
    from models import get_restaurant
    data = request.get_json(silent=True) or {}
    on = bool(data.get("on"))
    reason = (data.get("reason") or "").strip()[:300]
    r = get_restaurant(restaurant_id)
    if not r:
        return jsonify(ok=False, error="Restaurant not found"), 404
    if on and not int(getattr(r, "is_demo", 0) or 0):
        return jsonify(ok=False, error="Only a demo restaurant needs this; a real one reads its own store."), 400
    if on and not reason:
        return jsonify(ok=False, error="Say why this demo should read a live store."), 400

    def _set(rid, _user):
        update_restaurant(rid, {"pos_live_mirror": 1 if on else 0})
        return jsonify(ok=True, live_mirror=on)
    return _audited_rpower("rpower.live_mirror_set", restaurant_id, current_user, _set,
                           extra={"reason": reason or None})


@rpower_bp.route("/admin/rpower/owner-check/<int:restaurant_id>", methods=["POST"])
@admin_required
@recent_auth_required()
def rpower_owner_check_release(restaurant_id, current_user):
    """Release a bulk-onboarded store's owner hold after confirming the
    owner another way (a call to the store, RPOWER's word) — {how}, which is
    required. Typed audit (A7-02)."""
    import bulk_provisioning
    data = request.get_json(silent=True) or {}
    how = (data.get("how") or "").strip()[:300]
    if not how:
        return jsonify(ok=False, error="Say how the owner was confirmed."), 400

    def _release(rid, user):
        bulk_provisioning.release_hold(rid, user.get("username") or "admin", f"admin: {how}")
        return jsonify(ok=True, released=True)
    return _audited_rpower("rpower.owner_check_released", restaurant_id, current_user, _release,
                           extra={"how": how})


# ── the owner's side ────────────────────────────────────────────────────────

@rpower_bp.route("/api/rpower/sync", methods=["POST"])
@login_required
def rpower_sync_client(current_user):
    """The account card's Sync button, like every other POS card's. Kicks a
    background sync of the owner's own connected store."""
    payload, status = do_sync(current_user)
    return jsonify(**payload), status


def do_sync(current_user):
    """Sync now — one body for the web card and the app's twin
    (POST /mobile/api/connections/rpower/sync). Any login, as on the web."""
    import rpower
    rid = current_user["restaurant_id"]
    if not rpower.is_connected(rid):
        return {"ok": False, "error": "RPower isn't connected yet."}, 400
    # Once per SYNC_NOW_COOLDOWN_MINUTES per restaurant, whoever presses it
    # and from wherever (food & bar audit 10/16/26 POS-21; scale audit
    # 10/19/26 A3-09: the one cooldown pos.sync_now shares with Data
    # Health's Sync now, so the two buttons cannot start two pulls). The
    # console's own button (admin) is not held to it; it joins a running one.
    import pos
    started, _job = pos.sync_now(rid, current_user.get("username") or "owner")
    if not started:
        return {"ok": False, "cooldown": True,
                "error": f"RPower synced in the last {SYNC_NOW_COOLDOWN_MINUTES} minutes. "
                         f"Try again in a few minutes."}, 429
    return {"ok": True, "message": "RPower sync started."}, 200


@rpower_bp.route("/api/rpower/disconnect", methods=["POST"])
@login_required
def rpower_disconnect_client(current_user):
    """The owner can take their store off, as with every POS (SEC-24:
    principal only). Connecting again goes back through Cavnar, because the
    token is Cavnar's."""
    payload, status = disconnect_refusal(current_user) or do_disconnect(current_user)
    return jsonify(**payload), status


def disconnect_refusal(current_user):
    """(payload, status) refusing an owner's disconnect, or None: principal
    only (SEC-24), and the password step-up (A7-12) — a stolen principal
    session took the store off with no password and no trace. Shared by the
    web route and the app's twin."""
    from permissions import is_principal
    if not is_principal(current_user):
        return {"ok": False, "owner_only": True, "error": "Only the account owner can change the RPower "
                                                          "connection."}, 403
    from auth import account_step_up_refusal
    return account_step_up_refusal(current_user)


def do_disconnect(current_user):
    """The store's RPOWER link taken off — one body for the web card and the
    app's twin (DELETE /mobile/api/connections/rpower). The caller has
    already checked disconnect_refusal. Recorded in the account's activity
    here, for both doors (A7-12: only the app's twin wrote it)."""
    update_restaurant(current_user["restaurant_id"], _clear_fields())
    from client_api import log_account_event
    log_account_event(current_user["restaurant_id"], "pos_disconnected", current_user=current_user,
                      detail="RPOWER")
    return {"ok": True, "message": "RPower disconnected."}, 200


@rpower_bp.route("/api/rpower/status")
@login_required
def rpower_status_client(current_user):
    """Read-only for the owner's own account screen — no credential fields."""
    import rpower
    status = rpower.get_connection_status(current_user["restaurant_id"])
    # The owner sees whether it is working and when it last ran, never the
    # token or the internal identifiers — and a fixed sentence for a failed
    # sync, never the raw text (A7-14: it carried the vendor's body and a URL
    # with the cg and store id).
    return jsonify(connected=status.get("connected", False),
                   state=status.get("state"),
                   store_name=status.get("store_name"),
                   last_synced=status.get("last_synced"),
                   error=status.get("owner_error"))


# ── the bulk owner check (A7-02) — exempt from the hold it lifts ────────────

def store_check_get(current_user):
    import bulk_provisioning
    st = bulk_provisioning.owner_check_state(current_user["restaurant_id"])
    return {"ok": True, **st}, 200


def store_check_post(current_user):
    """The account holder confirms the store: its name as RPOWER has it and
    its serial number. Principal only; five misses an hour."""
    from permissions import is_principal
    if not is_principal(current_user):
        return {"ok": False, "owner_only": True,
                "error": "Only the account owner can confirm the store. Ask them to sign in."}, 403
    import bulk_provisioning
    data = request.get_json(silent=True) or {}
    rid = current_user["restaurant_id"]
    out = bulk_provisioning.verify_owner(rid, data.get("store_name"), data.get("serial"),
                                         current_user.get("username") or current_user.get("email") or "owner")
    try:
        from client_api import log_account_event
        log_account_event(rid, "pos_store_check", current_user=current_user,
                          detail="confirmed" if out.get("released") else ("ok" if out.get("ok") else "no match"))
    except Exception:
        pass
    return out, (200 if out.get("ok") else (429 if out.get("locked") else 400))


@rpower_bp.route("/api/pos/store-check", methods=["GET"])
@login_required
def store_check_get_web(current_user):
    payload, status = store_check_get(current_user)
    return jsonify(**payload), status


@rpower_bp.route("/api/pos/store-check", methods=["POST"])
@login_required
def store_check_post_web(current_user):
    payload, status = store_check_post(current_user)
    return jsonify(**payload), status
# The app's twins are mobile_api.mobile_pos_store_check (on mobile_bp: bearer
# auth, no CSRF cookie), over these same two bodies.
