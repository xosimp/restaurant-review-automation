"""Owner-trust backends for the schedule: matching ratings to roster names,
the experienced flag, and the auto-publish offer."""
import models
from models import Restaurant, create_restaurant, get_operational_scores, set_capability, rename_capability_holder


def test_a_rating_under_a_long_name_is_matched_to_the_roster_short_name():
    import strategy_routes as sr
    out = sr.rating_name_suggestions({"Kim Tran": 4, "Piper A.": 3, "Zed": 2},
                                     ["Kim T.", "Piper A.", "Kim R.", "Alex R."])
    by = {o["rated"]: o for o in out}
    assert "Piper A." not in by                       # already on the roster
    assert by["Kim Tran"]["suggestion"] == "Kim T."   # first name + last initial, one fit
    assert by["Zed"]["suggestion"] is None and by["Zed"]["candidates"] == []


def test_an_ambiguous_name_is_not_guessed():
    import strategy_routes as sr
    out = sr.rating_name_suggestions({"Kim": 4}, ["Kim T.", "Kim R."])
    assert out[0]["suggestion"] is None and set(out[0]["candidates"]) == {"Kim T.", "Kim R."}


def test_matching_moves_the_rating_and_never_overwrites_one(db_path):
    rid = create_restaurant(Restaurant(name="R", owner_email="o@x.test"), db_path=db_path)
    set_capability(rid, "Kim Tran", "overall", score=4, db_path=db_path)
    set_capability(rid, "Lee B.", "overall", score=2, db_path=db_path)
    set_capability(rid, "Lee Bond", "overall", score=5, db_path=db_path)
    assert rename_capability_holder(rid, "Kim Tran", "Kim T.", db_path=db_path) == 1
    scores = get_operational_scores(rid, db_path=db_path)
    assert scores.get("Kim T.") == 4 and "Kim Tran" not in scores
    assert rename_capability_holder(rid, "Lee Bond", "Lee B.", db_path=db_path) is None
    assert get_operational_scores(rid, db_path=db_path)["Lee B."] == 2


def test_the_experienced_flag_is_stored_and_read(db_path):
    import staff_settings as ss
    rid = create_restaurant(Restaurant(name="R", owner_email="o@x.test"), db_path=db_path)
    ss.upsert(rid, "Ana", experienced=True, db_path=db_path)
    ss.upsert(rid, "Bo", max_hours=30, db_path=db_path)
    assert ss.experienced_names(rid, db_path=db_path) == {"Ana"}
    ss.upsert(rid, "Ana", max_hours=20, db_path=db_path)          # other fields leave it alone
    assert ss.experienced_names(rid, db_path=db_path) == {"Ana"}
    ss.upsert(rid, "Ana", experienced=False, db_path=db_path)
    assert ss.experienced_names(rid, db_path=db_path) == set()


def test_auto_publish_is_offered_only_after_clean_weeks(monkeypatch):
    import strategy_routes as sr
    import schedule_versions as sv
    monkeypatch.setattr(models, "get_restaurant", lambda rid: Restaurant(name="R", owner_email="o@x.test", auto_publish_schedule=0))

    class _C:
        def execute(self, *a):
            class R:
                def fetchone(self_inner):
                    return {"quality_json": '{"score": 91}'}
            return R()

        def close(self):
            pass
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: _C())
    clean = {"weeks": [{"changes": 1, "unchanged_share": 0.98}] * 3}
    monkeypatch.setattr(sv, "acceptance", lambda rid, weeks=3: clean)
    assert sr._auto_publish_offer(1)["eligible"] is True
    monkeypatch.setattr(sv, "acceptance", lambda rid, weeks=3: {"weeks": [{"changes": 9, "unchanged_share": 0.7}] * 3})
    assert sr._auto_publish_offer(1)["eligible"] is False
    monkeypatch.setattr(sv, "acceptance", lambda rid, weeks=3: {"weeks": clean["weeks"][:2]})
    assert sr._auto_publish_offer(1)["eligible"] is False
