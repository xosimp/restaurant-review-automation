"""
Seed a STAGING database with N simulated RPOWER restaurants for load tests (scale audit 10/19/26).

Every restaurant is a copy of the showcase's simulated store (demo_store): its POS answers from the
simulator, so a load test drives the real sync, archive, report and Studio paths without touching RPOWER,
Google, Twilio, Resend or a model. Writes scripts/loadtest/users.csv (username,password,restaurant_id,role)
for locustfile.py.

Refuses to run unless LOADTEST_STAGING=1 is set AND no provider key is in the environment - a staging
database only, never production's.

  LOADTEST_STAGING=1 DATABASE_URL=postgresql://...staging... \
  python scripts/loadtest/seed_staging.py --restaurants 100 --managers 3 --staff 2
"""
import argparse
import csv
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "RESEND_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "STRIPE_SECRET_KEY",
                 "GOOGLE_PLACES_API_KEY", "PERPLEXITY_API_KEY", "DOCUSIGN_SECRET_KEY", "APNS_KEY_ID")


def _guard():
    if os.environ.get("LOADTEST_STAGING") != "1":
        sys.exit("set LOADTEST_STAGING=1 - this seeds a staging database only")
    live = [k for k in PROVIDER_KEYS if (os.environ.get(k) or "").strip()]
    if live:
        sys.exit(f"refusing: provider keys are set in this shell ({', '.join(live)}) - unset them; staging never sends")


def main():
    _guard()
    ap = argparse.ArgumentParser()
    ap.add_argument("--restaurants", type=int, default=100)
    ap.add_argument("--managers", type=int, default=3, help="manager logins per restaurant")
    ap.add_argument("--staff", type=int, default=2, help="staff-app logins per restaurant")
    ap.add_argument("--history-days", type=int, default=60)
    ap.add_argument("--out", default="scripts/loadtest/users.csv")
    a = ap.parse_args()

    import models
    import demo_store
    from auth import create_user
    from demo_store import world as w
    import rpower
    import pos

    rows = []
    for n in range(1, a.restaurants + 1):
        name = f"Loadtest Store {n:05d}"
        rid = models.create_restaurant(models.Restaurant(
            name=name, owner_email=f"owner{n}@loadtest.example", owner_name=f"Owner {n}", is_demo=1,
            module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1, service_tier="full",
            timezone=w.TIMEZONE, hourly_rate=17.0, labor_target_pct=28.0))
        models.update_restaurant(rid, {"billing_status": "internal", "pos_system": "rpower",
                                       "rpower_token": demo_store.TOKEN, "dsr_enabled": 1,
                                       "internal_notes": "LOADTEST (scripts/loadtest/seed_staging.py)"})
        rpower.bootstrap(rid)
        csv_str = rpower.build_shifts_csv(rid, days=a.history_days)
        if csv_str:
            pos.save_synced_shifts(rid, csv_str, "rpower")
        for role, count in (("owner", 1), ("manager", a.managers), ("staff", a.staff)):
            for k in range(count):
                user = f"lt{n:05d}{role[0]}{k}"
                pw = secrets.token_urlsafe(12) + "!A1"
                create_user(rid, user, f"{user}@loadtest.example", pw)
                rows.append({"username": user, "password": pw, "restaurant_id": rid, "role": role})
        if n % 25 == 0:
            print(f"seeded {n}/{a.restaurants}", flush=True)
    with open(a.out, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["username", "password", "restaurant_id", "role"])
        wr.writeheader()
        wr.writerows(rows)
    print(f"done: {a.restaurants} restaurants, {len(rows)} logins -> {a.out} (keep it out of git)")


if __name__ == "__main__":
    main()
