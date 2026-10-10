"""Schedule fix round 10/10/26, Z2: live labor reads its target through
thresholds.target_for (the owner's goal first, its label, and whether it may
raise anything). On Cavnar AI's starting target nothing is "over" enough to
suggest sending someone home; a slow pace still can, and a target the owner
set does."""
import labor_live
import models
from tests.test_sf10_j2_floor import db  # noqa: F401 — db is the fixture
from tests.test_sf10_j2_live import SIX, SLOW, _live


def test_an_over_target_night_suggests_a_cut_only_against_a_target_the_owner_set(db, monkeypatch):
    # On pace (400 by 6pm of a 1000 night, the 40% a Friday has done), but
    # labor far over 30%.
    rid, r = _live(db, monkeypatch, SLOW, sales_so_far=400.0, typical=1000.0,
                   labor_target_pct=30.0, labor_target_source="default")
    asked = []
    monkeypatch.setattr(labor_live, "_cuts", lambda *a, **k: asked.append(a[1]) or [])
    rd = labor_live.read(r, now_local=SIX)
    assert rd["sales"]["pace_pct"] == 0.0 and rd["pct"]["over_by"] > 0
    assert rd["pct"]["target_alerts"] is False
    assert labor_live.moves(r, now_local=SIX) == [] and asked == []
    models.update_restaurant(rid, {"labor_target_source": "set"})
    r = models.get_restaurant(rid)
    rd = labor_live.read(r, now_local=SIX)
    assert rd["pct"]["target_alerts"] is True and rd["pct"]["target"] == 30.0
    labor_live.moves(r, now_local=SIX)
    assert len(asked) == 1                     # the cut search runs against the owner's own target
