"""How long a draft takes, on the Building screen (owner, 10/6/26: "put
somewhere how often these generations usually take ... it's been over 5-6
minutes"). Measured from saved drafts of the generator in force, never a
guess; the steps centre under the heading and stretch to the measured time."""
from pathlib import Path

import models
import schedule_engine as se

DASH = Path(__file__).resolve().parents[1].joinpath("templates", "dashboard.html").read_text(encoding="utf-8")


def _hist(db_path, rid, at, secs, total=None):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO schedule_history (restaurant_id, generated_at, week_start, generation_seconds, "
                 "total_seconds, schedule_csv) VALUES (?, ?, '2026-10-12', ?, ?, '')", (rid, at, secs, total))
    conn.commit()
    conn.close()


def test_the_typical_time_is_the_median_of_this_generators_drafts(db_path):
    rid = models.create_restaurant(models.Restaurant(name="EJ", owner_email="e@x.test"), db_path=db_path)
    other = models.create_restaurant(models.Restaurant(name="GM", owner_email="g@x.test"), db_path=db_path)
    assert se.typical_generation_seconds(rid, db_path=db_path) is None, "nothing measured, nothing promised"
    _hist(db_path, rid, "2026-10-02 21:47:57", 135.7)                 # the old generator: never counted
    _hist(db_path, other, "2026-10-06 10:00:00", 400.0)
    _hist(db_path, other, "2026-10-06 11:00:00", 500.0)
    assert se.typical_generation_seconds(rid, db_path=db_path) == {"seconds": 450, "n": 2, "basis": "all"}
    _hist(db_path, rid, "2026-10-06 12:00:00", 300.0, total=330.0)    # total_seconds wins when kept
    _hist(db_path, rid, "2026-10-07 12:00:00", 600.0)
    assert se.typical_generation_seconds(rid, db_path=db_path) == {"seconds": 465, "n": 2, "basis": "yours"}


def test_the_start_answer_carries_it_on_every_path():
    import inspect
    import mobile_api
    src = inspect.getsource(mobile_api.mobile_generate_schedule)
    # Four since the parity round (#15): the 409 "busy" answer carries it too,
    # so the phone can say how long the running week usually takes.
    assert src.count("typical=_se.typical_generation_seconds(rid)") == 4


def test_the_building_screen_centres_the_steps_and_says_the_time():
    assert '<div id="sched-steps" class="sched-steps"></div><div id="sched-eta" class="sched-eta" aria-live="polite"></div>' in DASH
    assert ".sched-steps{width:max-content;max-width:100%;margin:20px auto 0}" in DASH
    assert ".ss-build .cm-wk-cap{font-size:13px;margin-top:44px;text-align:center}" in DASH
    assert "scheduleTiming(_schedStart, startData.typical, startData.wait_seconds);" in DASH
    eta = DASH[DASH.index("function renderScheduleEta(startedAt) {"):]
    eta = eta[:eta.index("\n}\n")]
    assert "A full week usually takes about" in eta and "Taking longer than usual" in eta
    assert "t.typical.seconds * 1.5" in eta and "_schedClock(t.until)" in eta
    steps = DASH[DASH.index("function renderScheduleSteps(startedAt) {"):]
    steps = steps[:steps.index("\n}\n")]
    assert "_SCHED_STEPS[i][0] * _schedStepScale" in steps and "font-size:14.5px" in steps
    assert 'width:17px;height:17px' in steps
