"""
Seed a STAGING database with N synthetic restaurants for load tests (scale audit 10/19/26; release-safety
audit #25 WS10 — A01-16, A01-19).

THE WORLD (plan_world). Not N copies of one Chicago store: N stores spread over five time zones (Chicago 30%,
New York 25%, Los Angeles 20%, Denver 15%, Honolulu 10%), so the morning wave, the nightly close and every
restaurant-local "today" arrive at five different UTC hours, as they will in production. Per 100 stores:

  diner_24h            a 24-hour diner (three 8-hour shifts, overnight across midnight) in New York — the DST
                       week is where an overnight shift breaks first
  late_bar             a bar open 4pm to 2am (3am Friday and Saturday) in Los Angeles
  mass_time_off        13 of 22 people on approved time off for the week a Generate press drafts, in Denver
  multi_location_org   two locations of one owner sharing three people (the cross-location double-booking
                       case), in Chicago
  chain_<k>            one organization of five POS stores, one in each time zone
  the rest             POS stores (the simulated RPOWER store, `demo_store`: the real sync, archive, DSR,
                       Bar, Food Cost paths run on it) and labor-only stores built from the schedule
                       benchmark's archetypes (cafe, bistro, full service, teen QSR, single manager,
                       daypart-limited staff, zero history), alternating by --pos-share

Every restaurant is a paying client as the scheduler sees one (billing_status active — the old kit's
"internal" left the stores out of every served-client job: the labor, inventory and reviews passes, the
summaries, auto-publish — so a night measured almost nothing; --billing internal brings that back), marked
"LOADTEST" in its notes (what the guard reads), and its owner and logins are @loadtest.example (a reserved
name: never deliverable). The POS stores are demo rows (is_demo 1: they all read the one simulated store, and
only a demo may share a POS store); the labor-only stores are not. Staging has no provider key and WS2's send
gate, so nothing is sent. The simulated POS keeps
Halsted & Rye's Chicago clock for its records; each store's own clock — its business day, its 7am brief,
its report after close, its schedule's week — is its own time zone. Writes scripts/loadtest/users.csv
(username,password,restaurant_id,role,timezone,kind) for locustfile.py — keep it out of git.

REFUSES (check_environment / check_database), before it writes anything:
  * any deploy environment but staging — `config.deploy_env()` (CAVNAR_ENV, else RAILWAY_ENVIRONMENT_NAME,
    else "local") must say "staging", and a Railway environment named production is refused whatever
    CAVNAR_ENV says;
  * any provider key or production side channel in the environment (PROVIDER_KEYS): staging never sends,
    never calls a model, never pages, never pings production's monitors;
  * a database holding any restaurant this kit did not seed (not LOADTEST-marked and not a demo): a
    staging database is its own, empty one — never production's, never a copy of it.

  CAVNAR_ENV=staging DATABASE_URL=postgresql://...staging... \
  python scripts/loadtest/seed_staging.py --restaurants 100 --managers 3 --staff 2
  python scripts/loadtest/seed_staging.py --restaurants 500 --plan     # print the world, write nothing
"""
import argparse
import csv
import json
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Credentials and side channels that must not be in a load test's environment. Exact names; empty counts as
# unset. Staging gets its own Postgres URL and CREDENTIAL_KEY/SECRET_KEY of its own; none of these.
PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PERPLEXITY_API_KEY",
    "RESEND_API_KEY", "RESEND_WEBHOOK_SECRET",
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
    "GOOGLE_PLACES_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLIENT_SECRET", "GOOGLE_SSO_CLIENT_SECRET",
    "GA_SERVICE_ACCOUNT_JSON",
    "DOCUSIGN_INTEGRATION_KEY", "DOCUSIGN_PRIVATE_KEY", "DOCUSIGN_SECRET_KEY",
    "APNS_KEY_ID", "APNS_PRIVATE_KEY", "WEB_PUSH_VAPID_PRIVATE_KEY",
    "META_APP_SECRET",
    "BACKUP_S3_ACCESS_KEY_ID", "BACKUP_S3_SECRET_ACCESS_KEY", "BLOB_S3_ACCESS_KEY_ID", "BLOB_S3_SECRET_ACCESS_KEY",
    # Production's side channels: a staging event in production's Sentry, a page to Will, or a healthy
    # ping to production's dead-man's switch while production is down.
    "SENTRY_DSN", "OPS_PAGER_ROUTING_KEY", "OPS_PAGER_WEBHOOK_URL", "HEALTHCHECK_PING_URL",
)

TIMEZONES = (("America/Chicago", 30), ("America/New_York", 25), ("America/Los_Angeles", 20),
             ("America/Denver", 15), ("Pacific/Honolulu", 10))
# The labor-only stores, cycled (schedule_benchmark.ARCHETYPES), each at a scale that keeps a seed of 500
# within an hour.
LABOR_ARCHETYPES = (("cafe_6", 1.0), ("bistro_22", 1.0), ("full_service_60", 0.5), ("qsr_teens", 0.5),
                    ("single_manager", 1.0), ("daypart_limited", 0.5), ("zero_history", 1.0))
# The special stores, each once per 100 (at least once from SPECIALS_FROM stores up), and where.
SPECIALS = (("diner_24h", "America/New_York"), ("late_bar", "America/Los_Angeles"),
            ("mass_time_off", "America/Denver"))
CHAIN_SIZE = len(TIMEZONES)
SPECIALS_FROM = 12
MARK = "LOADTEST (scripts/loadtest/seed_staging.py)"
EMAIL_DOMAIN = "loadtest.example"


class SeedRefused(SystemExit):
    """The seeder will not write: what was found, and what to change."""


# ── the guards ─────────────────────────────────────────────────────────────

def deploy_env() -> str:
    """config.deploy_env() (WS2's contract) when it exists; until then the same rule here: CAVNAR_ENV, else
    RAILWAY_ENVIRONMENT_NAME, else "local"."""
    try:
        import config
        fn = getattr(config, "deploy_env", None)
        if callable(fn):
            return str(fn() or "").strip().lower()
    except Exception as e:  # an unreadable config is a refusal below, never a pass
        print(f"[seed_staging] config.deploy_env unreadable ({e!r}); reading the environment", file=sys.stderr)
    return str(os.environ.get("CAVNAR_ENV") or os.environ.get("RAILWAY_ENVIRONMENT_NAME") or "local").strip().lower()


def check_environment() -> list:
    """Every reason this environment may not be load-tested (empty: it may)."""
    problems = []
    env = deploy_env()
    if env != "staging":
        problems.append(f"the deploy environment is {env!r}, not 'staging' — this seeds a staging database only "
                        "(CAVNAR_ENV=staging on the staging environment)")
    for name in ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT"):
        if str(os.environ.get(name) or "").strip().lower() == "production":
            problems.append(f"{name} is 'production' — never on production, whatever CAVNAR_ENV says")
    live = [k for k in PROVIDER_KEYS if str(os.environ.get(k) or "").strip()]
    if live:
        problems.append(f"provider keys are set ({', '.join(live)}) — unset them; staging never sends, calls a "
                        "model, pages or pings production's monitors")
    return problems


def _seeded(row) -> bool:
    notes = str(row["internal_notes"] or "")
    return notes.startswith("LOADTEST") or bool(row["is_demo"])


def check_database(db_path=None) -> list:
    """Every reason this database may not be seeded: it holds restaurants this kit did not make."""
    import models
    conn = models.get_conn(db_path)
    try:
        rows = conn.execute("SELECT id, is_demo, internal_notes FROM restaurants").fetchall()
    finally:
        conn.close()
    foreign = [r["id"] for r in rows if not _seeded(r)]
    if foreign:
        return [f"the database holds {len(foreign)} restaurant(s) this kit did not seed (ids {foreign[:5]}) — "
                "staging has its own empty Postgres, never production's or a copy of it"]
    return []


def _guard(db_path=None, database=True):
    """Refuse (SeedRefused, a SystemExit) unless this is staging, keyless, and its database is the kit's.
    nightly_sim.py runs the same guard."""
    problems = check_environment()
    if not problems and database:
        problems = check_database(db_path)
    if problems:
        raise SeedRefused("refusing to seed:\n  - " + "\n  - ".join(problems))


# ── the world ──────────────────────────────────────────────────────────────

def _quota(n, weights):
    """n split by integer weights, largest remainder (deterministic)."""
    total = sum(w for _k, w in weights)
    raw = [(k, n * w / total) for k, w in weights]
    out = {k: int(v) for k, v in raw}
    left = n - sum(out.values())
    for k, v in sorted(raw, key=lambda kv: (-(kv[1] - int(kv[1])), [k for k, _w in weights].index(kv[0])))[:left]:
        out[k] += 1
    return out


def plan_world(n, pos_share=0.5) -> list:
    """The stores to seed, in order: [{n, kind, timezone, name, group, location, scale, owner}]. Deterministic
    for (n, pos_share). `kind` is "pos" or a schedule_benchmark archetype key."""
    n = max(0, int(n))
    stores = []

    def add(kind, tz, group=None, location=None, scale=1.0, owner=None):
        k = len(stores) + 1
        stores.append({"n": k, "kind": kind, "timezone": tz, "name": f"Loadtest {k:05d} {kind}",
                       "group": group, "location": location, "scale": scale, "owner": owner or f"owner{k}"})

    per = max(1, n // 100) if n >= SPECIALS_FROM else 0
    for i in range(per):
        for kind, tz in SPECIALS:
            add(kind, tz)
        # Two locations of one owner, three people shared (built as one archetype: two restaurants).
        add("multi_location_org", "America/Chicago", group=f"Loadtest Shared {i + 1}", owner=f"orgshared{i + 1}")
        stores.append(dict(stores[-1], n=len(stores) + 1, name=stores[-1]["name"] + " (north)", sibling=True))
        # One organization, five POS stores, one in each zone.
        for j, (tz, _w) in enumerate(TIMEZONES):
            add("pos", tz, group=f"Loadtest Chain {i + 1}", location=tz.split("/")[-1].replace("_", " "),
                owner=f"chain{i + 1}")
    rest = n - len(stores)
    if rest > 0:
        # The rest by zone so the whole world lands on the weights.
        have = {}
        for s in stores:
            have[s["timezone"]] = have.get(s["timezone"], 0) + 1
        target = _quota(n, TIMEZONES)
        need = {tz: max(0, target[tz] - have.get(tz, 0)) for tz, _w in TIMEZONES}
        short = rest - sum(need.values())
        if short > 0:
            for tz, c in _quota(short, TIMEZONES).items():
                need[tz] += c
        elif short < 0:
            for tz, _w in sorted(TIMEZONES, key=lambda t: t[1]):
                while short < 0 and need[tz] > 0:
                    need[tz] -= 1
                    short += 1
        # Interleaved, so any prefix of the world is spread over the zones too.
        order, left = [], dict(need)
        while any(left.values()):
            for tz, _w in TIMEZONES:
                if left[tz]:
                    order.append(tz)
                    left[tz] -= 1
        pos_every = max(0.0, min(1.0, float(pos_share)))
        labor_i = 0
        for i, tz in enumerate(order):
            if pos_every and int((i + 1) * pos_every) > int(i * pos_every):
                add("pos", tz)
            else:
                kind, scale = LABOR_ARCHETYPES[labor_i % len(LABOR_ARCHETYPES)]
                labor_i += 1
                add(kind, tz, scale=scale)
    return stores[:n] if len(stores) > n else stores


def summarize(stores) -> dict:
    """Counts by zone and kind (what --plan prints, and what the tests read)."""
    by_tz, by_kind, groups = {}, {}, {}
    for s in stores:
        by_tz[s["timezone"]] = by_tz.get(s["timezone"], 0) + 1
        by_kind[s["kind"]] = by_kind.get(s["kind"], 0) + 1
        if s.get("group"):
            groups.setdefault(s["group"], set()).add(s["timezone"])
    return {"stores": len(stores), "by_timezone": by_tz, "by_kind": by_kind,
            "groups": {g: sorted(z) for g, z in groups.items()}}


# ── writing it ─────────────────────────────────────────────────────────────

def _mark(rid, tz, group=None, location=None, billing="active", is_demo=0):
    import models
    cols = {"billing_status": billing, "is_demo": int(is_demo), "internal_notes": MARK, "timezone": tz}
    if group:
        cols["location_group"] = group
    if location:
        cols["location_name"] = location
    models.update_restaurant(rid, cols)


def _seed_pos(store, history_days, billing="active"):
    """A store on the simulated RPOWER POS (demo_store), like the showcase. A demo row: every POS store here
    reads the one simulated store, and one POS store feeds one LIVE restaurant (the bind's check and the
    partial unique index both leave demos out) — the only path that lets a hundred stores share it."""
    import models
    import demo_store
    import pos
    import rpower
    rid = models.create_restaurant(models.Restaurant(
        name=store["name"], owner_email=f"{store['owner']}@{EMAIL_DOMAIN}", owner_name=f"Owner {store['n']}",
        is_demo=1, module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1, service_tier="full",
        timezone=store["timezone"], hourly_rate=17.0, labor_target_pct=28.0))
    models.update_restaurant(rid, {"pos_system": "rpower", "rpower_token": demo_store.TOKEN, "dsr_enabled": 1})
    bound = rpower.bootstrap(rid)
    if not bound.get("ok"):
        raise RuntimeError(f"store {store['n']} (restaurant {rid}): the simulated POS did not bind: {bound.get('error')}")
    # The store's zone is its own, whatever the simulator's (Chicago) says.
    _mark(rid, store["timezone"], store.get("group"), store.get("location"), billing, is_demo=1)
    csv_str = rpower.build_shifts_csv(rid, days=history_days)
    if csv_str:
        pos.save_synced_shifts(rid, csv_str, "rpower")
    return [rid]


def _seed_archetype(store, today=None, billing="active"):
    """A labor-only store from a schedule_benchmark archetype: its hours, roster, settings, history and time
    off, through the app's own writes. multi_location_org writes two restaurants (its sibling first)."""
    import schedule_benchmark as sb
    import time_utils
    from datetime import date as _date, timedelta as _td
    spec = sb.ARCHETYPES[store["kind"]](scale=store.get("scale") or 1.0)
    email = f"{store['owner']}@{EMAIL_DOMAIN}"
    rids = []
    for s in list(spec.get("siblings") or []) + [spec]:
        s = dict(s, label=f"{store['name']} {s['label']}"[:120],
                 cols=dict(s.get("cols") or {}, timezone=store["timezone"]))
        if store.get("group") and s["cols"].get("location_group"):
            s["cols"]["location_group"] = store["group"]
        rid = sb._write_restaurant(s, email)
        _mark(rid, store["timezone"], billing=billing)
        local_today = today or time_utils.restaurant_now_by_id(rid).date()
        wsd = int(s["cols"].get("week_start_day") or 0)
        week_start = sb.next_week_start(wsd, local_today)
        week_dates = [(_date.fromisoformat(week_start) + _td(days=i)).isoformat() for i in range(7)]
        sb._write_history(rid, s, local_today)
        sb._write_people(rid, s, week_dates)
        rids.append(rid)
    return rids


def _logins(rid, n, store, managers, staff):
    from auth import create_user
    rows = []
    for role, count in (("owner", 1), ("manager", managers), ("staff", staff)):
        for k in range(count):
            user = f"lt{n:05d}{rid}{role[0]}{k}"
            pw = secrets.token_urlsafe(12) + "!A1"
            create_user(rid, user, f"{user}@{EMAIL_DOMAIN}", pw)
            rows.append({"username": user, "password": pw, "restaurant_id": rid, "role": role,
                         "timezone": store["timezone"], "kind": store["kind"]})
    return rows


def seed(stores, managers=3, staff=2, history_days=60, out=None, progress=print, billing="active", today=None):
    """Write every store and its logins; the users.csv rows. Runs the guard first. `today` pins the
    archetypes' local date (tests); unset, each store's own local today."""
    _guard()
    if billing not in ("active", "internal"):
        raise SeedRefused(f"--billing must be active or internal, not {billing!r}")
    rows = []
    for i, store in enumerate(s for s in stores if not s.get("sibling")):
        rids = (_seed_pos(store, history_days, billing) if store["kind"] == "pos"
                else _seed_archetype(store, today, billing))
        for rid in rids:
            rows += _logins(rid, store["n"], store, managers, staff)
        if progress and (i + 1) % 25 == 0:
            progress(f"seeded {i + 1} stores", flush=True)
    if out:
        with open(out, "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=["username", "password", "restaurant_id", "role", "timezone", "kind"])
            wr.writeheader()
            wr.writerows(rows)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Seed a staging database with synthetic restaurants.")
    ap.add_argument("--restaurants", type=int, default=100)
    ap.add_argument("--managers", type=int, default=3, help="manager logins per restaurant")
    ap.add_argument("--staff", type=int, default=2, help="staff-app logins per restaurant")
    ap.add_argument("--history-days", type=int, default=60, help="POS stores' synced shift history")
    ap.add_argument("--pos-share", type=float, default=0.5, help="share of the ordinary stores on the POS")
    ap.add_argument("--billing", default="active", choices=("active", "internal"),
                    help="active (default): served like a paying client; internal: left out of served-client jobs")
    ap.add_argument("--out", default="scripts/loadtest/users.csv")
    ap.add_argument("--plan", action="store_true", help="print the world and write nothing")
    a = ap.parse_args(argv)
    stores = plan_world(a.restaurants, pos_share=a.pos_share)
    if a.plan:
        print(json.dumps(summarize(stores), indent=2, sort_keys=True))
        return 0
    rows = seed(stores, a.managers, a.staff, a.history_days, a.out, billing=a.billing)
    print(f"done: {len(stores)} restaurants, {len(rows)} logins -> {a.out} (keep it out of git)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
