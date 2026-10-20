"""A restaurant's own guest texting, set up inside Cavnar AI (texting_setup,
owner 10/20/26). Nobody signs into Twilio: the form goes to Twilio under
Cavnar AI's account, step by step, each step's id kept so a failure resumes
without paying a fee twice; the hourly job registers the campaign once the
brand is approved and switches the restaurant's texts to its own number once
the campaign is verified."""
import pytest

import guest_hub as gh
import guest_marketing as gm
import models
import texting_setup as ts
from models import Restaurant, create_restaurant

FORM = {"legal_name": "Simple EJ's LLC", "ein": "12 3456789", "business_type": "Limited Liability Corporation",
        "website": "simpleejs.example", "street": "1 Main St", "city": "St. Charles", "state": "il",
        "zip": "60174", "first_name": "Pat", "last_name": "Lee", "email": "pat@example.com",
        "phone": "1 (630) 555-0100", "title": "Owner", "job_position": "CEO", "area_code": "630"}


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    gm.init_guest_marketing(db_path)
    gh._SENDER_CACHE["at"] = 0.0
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_ISV_PROFILE_SID", "BUprimary")
    monkeypatch.setenv("TWILIO_TRUSTHUB_EMAIL", "isv@cavnar.example")
    monkeypatch.setattr(ts, "_live_refused", lambda: None)
    monkeypatch.setattr(ts, "_tell_owner_live", lambda rid, number: None)
    yield


@pytest.fixture
def rid(db_path):
    return create_restaurant(Restaurant(name="Simple EJ's", owner_email="erik@x.test"), db_path=db_path)


class FakeTwilio:
    """Answers every call ts._tw makes, counting the ones that cost money."""

    def __init__(self, brand="PENDING", campaign="IN_PROGRESS", evaluation="compliant"):
        self.calls, self.brand, self.campaign, self.evaluation = [], brand, campaign, evaluation
        self.n = 0

    def __call__(self, method, url, data=None, params=None):
        self.calls.append((method, url, dict(data or {}), dict(params or {})))
        self.n += 1
        if url.endswith("/Evaluations"):
            return {"status": self.evaluation, "results": [
                {"fields": [{"friendly_name": "Business Registration Number", "passed": False}]}]}
        if "AvailablePhoneNumbers" in url:
            return {"available_phone_numbers": [{"phone_number": "+16305550199"}]}
        if url.endswith("IncomingPhoneNumbers.json"):
            return {"sid": "PNnum", "phone_number": "+16305550199"}
        if method == "GET" and "/BrandRegistrations/" in url:
            return {"status": self.brand, "failure_reason": "EIN does not match"}
        if method == "GET" and "/Usa2p/" in url:
            return {"campaign_status": self.campaign, "errors": [{"description": "Opt-in unclear"}]}
        if url.endswith("/Usa2p"):
            return {"sid": "QEcampaign"}
        if url.endswith("/Services"):
            return {"sid": "MG" + "a" * 32}
        if url.endswith("/BrandRegistrations"):
            return {"sid": "BNbrand"}
        return {"sid": f"XX{self.n}"}

    def posts(self, suffix):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]


def test_the_form_is_checked_before_a_fee_is_paid():
    form, probs = ts.clean_form(FORM)
    assert probs == []
    assert form["ein"] == "12-3456789" and form["state"] == "IL" and form["phone"] == "6305550100"
    assert form["website"] == "https://simpleejs.example"
    _f, probs = ts.clean_form({"legal_name": "X LLC", "ein": "123"})
    assert any("EIN" in p for p in probs) and any("address" in p for p in probs)


def test_a_local_server_never_creates_twilio_resources(monkeypatch, rid):
    monkeypatch.setattr(ts, "_live_refused", lambda: "Texting can only be set up from the live Cavnar AI site.")
    fake = FakeTwilio()
    monkeypatch.setattr(ts, "_tw", fake)
    res = ts.submit(rid, FORM)
    assert not res["ok"] and "live" in res["error"] and fake.calls == []


def test_submit_runs_every_step_that_does_not_wait_on_a_carrier(monkeypatch, rid):
    fake = FakeTwilio()
    monkeypatch.setattr(ts, "_tw", fake)
    res = ts.submit(rid, FORM, user_id=7)
    assert res["status"] == "brand_review"
    row = ts._row(rid)
    s = row["sids"]
    for k in ("profile", "business", "rep", "address", "address_doc", "a2p", "a2p_info", "brand", "service",
              "number"):
        assert s.get(k), k
    assert row["phone_number"] == "+16305550199"
    # The secondary profile hangs off Cavnar AI's primary (ISV) profile.
    assert any(c[2].get("ObjectSid") == "BUprimary" for c in fake.calls)
    # A Low-Volume Standard brand, on the restaurant's own profile.
    brand = fake.posts("/BrandRegistrations")[0][2]
    assert brand["SkipAutomaticSecVet"] == "true" and brand["A2PProfileBundleSid"] == s["a2p"]
    # The campaign waits for the brand.
    assert not fake.posts("/Usa2p")
    # The number is the area code asked for, and goes into the restaurant's service.
    assert fake.calls[[i for i, c in enumerate(fake.calls) if "AvailablePhoneNumbers" in c[1]][0]][3]["AreaCode"] == "630"
    assert fake.posts("/PhoneNumbers")


def test_a_rerun_never_buys_or_registers_twice(monkeypatch, rid):
    fake = FakeTwilio()
    monkeypatch.setattr(ts, "_tw", fake)
    ts.submit(rid, FORM)
    before = len([c for c in fake.calls if c[0] == "POST"])
    ts.advance(rid)
    ts.advance(rid)
    assert len([c for c in fake.calls if c[0] == "POST"]) == before
    assert len(fake.posts("IncomingPhoneNumbers.json")) == 1 and len(fake.posts("/BrandRegistrations")) == 1


def test_brand_approved_registers_the_campaign_then_verified_goes_live(monkeypatch, rid):
    fake = FakeTwilio()
    monkeypatch.setattr(ts, "_tw", fake)
    ts.submit(rid, FORM)
    fake.brand = "APPROVED"
    assert ts.advance(rid)["status"] == "campaign_review"
    camp = fake.posts("/Usa2p")[0][2]
    assert camp["UsAppToPersonUsecase"] == "LOW_VOLUME" and camp["BrandRegistrationSid"] == "BNbrand"
    assert "/join/" in camp["MessageFlow"] and "reply Y" in camp["MessageFlow"]
    assert 2 <= len(camp["MessageSamples"]) <= 5 and all(20 <= len(m) <= 1024 for m in camp["MessageSamples"])
    assert gh.sender_for(rid) is None  # not live until verified
    fake.campaign = "VERIFIED"
    assert ts.advance(rid)["status"] == "live"
    gh._SENDER_CACHE["at"] = 0.0
    assert gh.sender_for(rid)["service_sid"] == "MG" + "a" * 32
    st = ts.status(rid)
    assert st["status"] == "live" and st["number"] == "(630) 555-0199" and st["form"]["ein"].endswith("6789")
    assert "•" in st["form"]["ein"]


def test_twilio_s_own_check_comes_back_as_a_fix_not_a_fee(monkeypatch, rid):
    fake = FakeTwilio(evaluation="noncompliant")
    monkeypatch.setattr(ts, "_tw", fake)
    res = ts.submit(rid, FORM)
    assert not res["ok"] and res["status"] == "needs_fix"
    assert "Business Registration Number" in res["error"]
    assert not fake.posts("/BrandRegistrations")  # no brand fee paid


def test_a_turned_down_campaign_says_why_and_can_be_sent_again(monkeypatch, rid):
    fake = FakeTwilio(brand="APPROVED", campaign="FAILED")
    monkeypatch.setattr(ts, "_tw", fake)
    ts.submit(rid, FORM)
    st = ts.status(rid)
    assert st["status"] == "failed" and "Opt-in unclear" in st["error"] and st["editable"]
    fake.campaign = "IN_PROGRESS"
    ts.submit(rid, FORM)
    # The brand is kept; the turned-down campaign is removed and registered again.
    assert len(fake.posts("/BrandRegistrations")) == 1 and len(fake.posts("/Usa2p")) == 2
    assert [c for c in fake.calls if c[0] == "DELETE" and c[1].endswith("/Usa2p/QEcampaign")]


def test_the_hourly_job_is_bounded_and_counts_outcomes(monkeypatch, rid):
    fake = FakeTwilio()
    monkeypatch.setattr(ts, "_tw", fake)
    ts.submit(rid, FORM)
    out = ts.run_advance()
    assert out["attempted"] == 1 and out["ok"] == 1 and out["hit_bound"] is False
    import jobs_registry
    assert jobs_registry.JOBS["texting_setup_advance"]["target"] == ("texting_setup", "run_advance")


def test_usage_counts_this_month_s_guest_texts_against_the_allowance(rid):
    import pricing
    conn = models.get_conn()
    for st in ("accepted", "delivered", "failed"):
        conn.execute("INSERT INTO sms_log (restaurant_id, use_case, status, cost_usd) VALUES (?, 'guest', ?, 0.0083)",
                     (rid, st))
    conn.execute("INSERT INTO sms_log (restaurant_id, use_case, status) VALUES (?, 'alert', 'delivered')", (rid,))
    conn.commit()
    conn.close()
    u = ts.usage(rid)
    assert u["texts"] == 2 and u["included"] == pricing.TEXTS_INCLUDED_MONTHLY and u["over"] == 0


def test_only_the_owner_or_a_marketing_manager_sets_it_up():
    import pathlib
    src = pathlib.Path(ts.__file__).resolve().parent.joinpath("mobile_api.py").read_text()
    body = src[src.index("def mobile_guests_texting_setup"):src.index("@mobile_bp.route(\"/guests/inbox\"")]
    assert "may_connect_social(current_user)" in body and "!= \"ein\"" in body
