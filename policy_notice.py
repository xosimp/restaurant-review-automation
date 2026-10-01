"""policy_notice — the dashboard notice a material policy change owes.

The Privacy Policy and Terms (public/privacy.html, public/terms.html; commit
c2e02db9) say that benchmarks use only de-identified ratios pooled across
five or more restaurants, and that those pooled statistics may remain after
an account is deleted. The policy's own rule for a material change is a
notice in the dashboard for 30 days. This module is that notice: one date,
one sentence, one link, shown on Home (web and iOS — the home brief payload's
`policy_notice`) to the account holders (principal logins), each of whom can
dismiss it for good (users.policy_notice_dismissed, per login).

POLICY_UPDATED_ON is the ONE place the date lives. It is set on the day the
change ships; while it is None nothing is shown. A later policy change sets a
new date, and every login sees that notice again (a dismissal names the date
it dismissed).
"""
from datetime import date, timedelta

# The day the updated Privacy Policy and Terms took effect — set on ship day
# (e.g. date(2026, 10, 1)). None: no notice is owed.
POLICY_UPDATED_ON = date(2026, 9, 29)
# How long the notice stays up (the policy's own rule for a material change).
NOTICE_DAYS = 30
POLICY_URL = "https://cavnar.ai/privacy"


def _updated_on():
    d = POLICY_UPDATED_ON
    if d is None:
        return None
    if isinstance(d, str):
        try:
            return date.fromisoformat(d[:10])
        except ValueError:
            return None
    return d if isinstance(d, date) else None


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def _dismissed(user_id, db_path=None):
    if not user_id:
        return None
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT policy_notice_dismissed FROM users WHERE id=?", (int(user_id),)).fetchone()
        finally:
            conn.close()
        return (row["policy_notice_dismissed"] if row is not None else None) or None
    except Exception as e:
        print(f"[policy_notice] dismissal unreadable for user {user_id}: {e}")
        return None


def notice_for(user, today=None, db_path=None):
    """The notice this login should see today, or None: only an account
    holder (permissions.answer_authority "principal" — an admin's view-as
    never sees it, so it can never dismiss it for the owner), only from the
    effective date for NOTICE_DAYS days, and only until this login dismissed
    this date's notice.

    {key, updated_on (ISO), updated_label (M/D/YY), text, link_label, url,
     shown_until (ISO, the last day), shown_until_label, dismiss {web, mobile}}.
    Never raises."""
    try:
        from permissions import answer_authority
        if not user or answer_authority(user) != "principal":
            return None
        start = _updated_on()
        if start is None:
            return None
        if today is None:
            # The owner's own date - the server's is tomorrow after 7pm
            # Central (UTC host); Home passes its local date already.
            try:
                from time_utils import restaurant_now_by_id
                today = restaurant_now_by_id(user.get("restaurant_id")).date()
            except Exception:
                today = date.today()
        if hasattr(today, "date") and callable(getattr(today, "date")):
            today = today.date()
        last = start + timedelta(days=NOTICE_DAYS - 1)
        if today < start or today > last:
            return None
        if _dismissed(user.get("id"), db_path=db_path) == start.isoformat():
            return None
        from time_utils import mdy
        label = mdy(start)
        return {"key": f"policy:{start.isoformat()}", "updated_on": start.isoformat(), "updated_label": label,
                "text": (f"We updated our Privacy Policy and Terms on {label}: how Cavnar AI’s "
                         "benchmarks use pooled, de-identified figures."),
                "link_label": "Read what changed", "url": POLICY_URL,
                "shown_until": last.isoformat(), "shown_until_label": mdy(last),
                "dismiss": {"web": "/api/account/policy-notice/dismiss",
                            "mobile": "/mobile/api/account/policy-notice/dismiss"}}
    except Exception as e:
        print(f"[policy_notice] notice unavailable: {e}")
        return None


def dismiss(user, db_path=None) -> bool:
    """This login has read the notice: it stays gone for this date's policy
    change, on every device and location. An admin (view-as) dismisses
    nothing for the owner. True when a notice was dismissed."""
    from permissions import answer_authority
    start = _updated_on()
    if start is None or not user or not user.get("id") or answer_authority(user) != "principal":
        return False
    conn = get_conn(db_path)
    try:
        n = conn.execute("UPDATE users SET policy_notice_dismissed=? WHERE id=?",
                         (start.isoformat(), int(user["id"]))).rowcount
        conn.commit()
    finally:
        conn.close()
    return bool(n)
