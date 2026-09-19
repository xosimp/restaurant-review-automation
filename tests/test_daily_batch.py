"""The morning batch: eight alert types about one week of trading arrive as
one notification, not eight."""
import datetime
import pytest
import models, notify, ops


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, notify, ops):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(notify, "DB_PATH", db_path)
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    yield
    notify._batch = None


@pytest.fixture
def rid(db_path):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO restaurants (id, name, owner_email) VALUES (1,'Simple EJs','o@x.test')")
    conn.commit(); conn.close()
    return 1


def _capture(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "deliver_alert",
                        lambda rid_, t, sms, subj, html, **kw: sent.append(
                            {"type": t, "sms": sms, "subject": subj, "html": html}))
    return sent


def test_several_conditions_become_one_notification(db_path, rid, monkeypatch):
    sent = _capture(monkeypatch)
    notify.begin_daily_batch()
    notify.raise_alert(rid, "labor_over", "labor sms", "Labor over target — Simple EJs",
                       lines=["Labor ran 34.1%, 4 points over."], db_path=db_path)
    notify.raise_alert(rid, "food_waste", "waste sms", "Food waste flagged — Simple EJs",
                       lines=["$420 of waste across 5 items."], db_path=db_path, value=420.0)
    notify.raise_alert(rid, "price_spike", "price sms", "Ingredient price climbing — Simple EJs",
                       lines=["Mozzarella moved $4.10 to $4.50."], db_path=db_path)
    assert sent == []           # nothing has gone out yet

    out = notify.flush_daily_batch(db_path)

    assert out["combined"] == 1 and out["single"] == 0
    assert len(sent) == 1
    assert sent[0]["type"] == "daily_briefing"
    assert "3 things to look at this morning" in sent[0]["subject"]
    for phrase in ("Labor over target", "Food waste flagged", "Ingredient price climbing"):
        assert phrase in sent[0]["html"]


def test_the_worst_thing_leads(db_path, rid, monkeypatch):
    """An owner who reads only the banner must still come away with the most
    expensive item, not whichever check happened to run first."""
    sent = _capture(monkeypatch)
    notify.begin_daily_batch()
    notify.raise_alert(rid, "ai_visibility_drop", "viz", "AI visibility dropped — Simple EJs",
                       lines=["Down from 60% to 40%."], db_path=db_path)      # P5
    notify.raise_alert(rid, "critical_low", "stock", "Running out before delivery — Simple EJs",
                       lines=["3 items won't last."], db_path=db_path)        # P1
    notify.flush_daily_batch(db_path)
    assert "3 items won't last." in sent[0]["sms"]


def test_one_condition_is_still_its_own_alert(db_path, rid, monkeypatch):
    """Folding is for a morning with several things in it. One thing should
    arrive as itself, with its own subject and its own type."""
    sent = _capture(monkeypatch)
    notify.begin_daily_batch()
    notify.raise_alert(rid, "labor_over", "labor sms", "Labor over target — Simple EJs",
                       lines=["Labor ran 34.1%."], db_path=db_path)
    out = notify.flush_daily_batch(db_path)
    assert out["single"] == 1 and out["combined"] == 0
    assert len(sent) == 1 and sent[0]["type"] == "labor_over"


def test_folded_types_still_record_themselves(db_path, rid, monkeypatch):
    """The 7-day repeat windows read alert_log. If folding stopped writing
    those rows, every folded alert would re-fire the next morning, forever."""
    _capture(monkeypatch)
    notify.begin_daily_batch()
    notify.raise_alert(rid, "labor_over", "a", "A", lines=["x"], db_path=db_path)
    notify.raise_alert(rid, "food_waste", "b", "B", lines=["y"], db_path=db_path, value=420.0)
    notify.flush_daily_batch(db_path)

    conn = models.get_conn(db_path)
    rows = {r["alert_type"]: r["value"] for r in
            conn.execute("SELECT alert_type, value FROM alert_log WHERE restaurant_id=?", (rid,))}
    conn.close()
    assert rows["labor_over"] is None
    assert rows["food_waste"] == 420.0


def test_the_briefing_does_not_spend_the_owners_daily_cap(db_path, rid, monkeypatch):
    """The folded types count, exactly as they did when they were separate
    alerts. The wrapper around them must not count a second time."""
    assert "daily_briefing" in models.NON_ALERT_TYPES


def test_a_review_alert_is_never_batched(db_path, rid, monkeypatch):
    """A 1-star review is not part of the morning summary — it is an event,
    and it has its own rush-holding path."""
    sent = _capture(monkeypatch)
    notify.begin_daily_batch()
    notify.raise_alert(rid, "1star", "sms", "1star", lines=["x"], db_path=db_path)
    assert len(sent) == 1 and sent[0]["type"] == "1star"
