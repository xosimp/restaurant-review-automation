"""schedule_time — the one notion of a schedule row's time (schedule audit
10/10/26 ARCH-12, BUGSGEN-6/7/8/19/20/22/25)."""
import schedule_time as st
import schedule_rules as sr
import shift_quality as sq

TZ = "America/Chicago"


def _row(date, start, end, **kw):
    return dict({"date": date, "employee": "Sam", "role": "Server", "shift_start": start, "shift_end": end}, **kw)


# ── BUGSGEN-20: every stored spelling reads ─────────────────────────────────

def test_parse_minutes_reads_noon_midnight_am_pm_and_24h():
    cases = {"noon": 720, "Noon": 720, "midnight": 0, "12:00 a.m.": 0, "12:00 p.m.": 720, "9:30p": 1290,
             "9:30 p.m.": 1290, "9pm": 1260, "9 PM": 1260, "9:30": 570, "21:30": 1290, "21:30:00": 1290,
             "13": 780, "0": 0, "24:00": 0, "12am": 0, "12pm": 720, "4:00 PM": 960}
    for text, want in cases.items():
        assert st.parse_minutes(text) == want, text
        assert sr.parse_minutes(text) == want, text      # the rules read the same
        assert sq._slot_minutes(text) == want, text      # and so does the scorer


def test_a_bare_twelve_hour_number_is_not_guessed():
    # "9" is 9am or 9pm: unreadable, said by the caller, never guessed
    for text in ("9", "11", "", None, "x", "25", "9:61pm", "13pm", "0:30am"):
        assert st.parse_minutes(text) is None, text


def test_normalise_clock_is_the_one_spelling():
    assert st.normalise_clock("noon") == "12:00pm"
    assert st.normalise_clock("21:30") == "9:30pm"
    assert st.normalise_clock("12:00 a.m.") == "12:00am"
    assert st.normalise_clock("9") is None


# ── BUGSGEN-8: the start alone places a shift ───────────────────────────────

def test_the_start_alone_places_a_shift_on_its_night():
    d = "2026-10-09"
    # an hour's difference in the end never moves a shift a day
    assert st.span(_row(d, "1:00am", "5:30am")) == (1500, 1770)
    assert st.span(_row(d, "1:00am", "6:30am")) == (1500, 1830)
    # a small-hours start and its daypart agree
    assert st.span(_row(d, "3:00am", "11:00am")) == (1620, 2100)
    assert st.start_daypart("3:00am") == "night"
    assert st.span(_row(d, "4:30am", "5:30am")) == (270, 330)
    assert st.start_daypart("4:30am") == "morning"
    # the porter and the baker (E-32) as before
    assert st.span(_row(d, "12:30am", "4:00am")) == (1470, 1680)
    assert st.span(_row(d, "4:30am", "12:30pm")) == (270, 750)
    # an overnight close
    assert st.span(_row(d, "5:00pm", "2:00am")) == (1020, 1560)


def test_every_reader_places_a_row_the_same_way():
    for start, end in (("12:30am", "4:00am"), ("1:00am", "6:30am"), ("3:00am", "11:00am"), ("4:30am", "12:30pm"),
                       ("5:00pm", "2:00am"), ("10:00am", "3:15pm")):
        row = _row("2026-10-09", start, end)
        want = st.span(row)
        assert (sr.start_minutes(row), sr.end_minutes(row)) == want
        assert sq._row_span(row) == want
        assert sr.shift_span(row) == sq._span(row) == st.shift_span(row)
        import schedule_optimizer as so
        assert so._span(row) == want, (start, end)
        import schedule_skeleton as sk
        assert sk._span(row) == want, (start, end)


# ── DST: real hours (E-23, BUGSGEN-6) ───────────────────────────────────────

def test_real_hours_on_the_night_the_clocks_go_back():
    # Halloween 10/31/26: Simple EJ's 2am close; 2:00am CDT -> 1:00am CST
    close = _row("2026-10-31", "5:00pm", "2:00am")
    assert st.real_hours(close, TZ) == 10.0 and st.real_hours(close) == 9.0
    # a porter dated 10/31 working the small hours of 11/1
    porter = _row("2026-10-31", "12:30am", "4:00am")
    assert st.real_hours(porter, TZ) == 4.5 and st.real_hours(porter) == 3.5
    # the day after is ordinary
    assert st.real_hours(_row("2026-11-01", "5:00pm", "11:00pm"), TZ) == 6.0


def test_real_hours_on_the_night_the_clocks_go_forward():
    # 3/14/27: 2:00am CST -> 3:00am CDT, the night of Saturday 3/13/27
    assert st.real_hours(_row("2027-03-13", "5:00pm", "3:00am"), TZ) == 9.0
    assert st.real_hours(_row("2027-03-13", "5:00pm", "1:00am"), TZ) == 8.0
    assert st.real_hours(_row("2027-03-14", "5:00pm", "11:00pm"), TZ) == 6.0


def test_hours_text_keeps_two_decimals_never_bankers_tenths():
    # BUGSGEN-7: round(6.25, 1) was 6.2 and round(6.75, 1) 6.8
    assert st.hours_text(6.25) == "6.25" and st.hours_text(6.75) == "6.75"
    assert st.hours_text(9.0) == "9" and st.hours_text(7.5) == "7.5" and st.hours_text(0) == "0"
    assert st.row_hours_text(_row("2026-10-09", "4:45pm", "11:00pm")) == "6.25"
    r = st.retime(_row("2026-10-31", "5:00pm", "11:00pm"), end_m=26 * 60, tz=TZ)
    assert r["shift_end"] == "2:00am" and r["scheduled_hours"] == "10"


# ── BUGSGEN-22: a close is read against the open ────────────────────────────

def test_a_close_at_or_after_six_is_the_next_morning_when_it_is_not_after_the_open():
    assert st.close_on_timeline(6 * 60, 6 * 60) == 30 * 60          # a 24-hour diner
    assert st.close_on_timeline(6 * 60, 16 * 60) == 30 * 60         # a bar closing at 6am
    assert st.close_on_timeline(2 * 60, 16 * 60) == 26 * 60
    assert st.close_on_timeline(23 * 60, 11 * 60) == 23 * 60
    assert st.close_on_timeline(2 * 60) == 26 * 60                  # no open on file
    assert st.close_on_timeline(None, 11 * 60) is None
    week = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    c = sr.Constraints(restaurant_id=1, week_dates=week, week_days=days)
    c.compliance = dict(sr.DEFAULTS)
    c.open_times, c.close_times = {"Monday": "6:00am"}, {"Monday": "6:00am"}
    assert sr.close_minutes(c, "Monday") == 30 * 60
    assert sr.open_hours(c, "Monday") == (6 * 60, 30 * 60)
    # nobody on until a 6am close is now said (it read 6am of the same
    # date, before opening, and never fired)
    rows = [{"date": week[0], "day": "Monday", "employee": "Ann", "role": "Server", "shift_start": "6:00am",
             "shift_end": "2:00pm", "scheduled_hours": "8", "notes": ""}]
    c.roster_names, c.active = ["Ann"], {"ann"}
    assert "nobody_at_close" in [v["kind"] for v in sr.violations(rows, c)]


# ── BUGSGEN-19: a curve's small hours are the night's ───────────────────────

def test_business_minutes_put_the_small_hours_after_the_night():
    assert st.business_minute(60) == 25 * 60 and st.business_minute(23 * 60) == 23 * 60
    assert st.business_hour(1) == 25 and st.business_hour(5) == 5 and st.business_hour(4) == 28


# ── BUGSGEN-25: one lunch/dinner rule ───────────────────────────────────────

FIXTURES = [
    # (start, end, present, touched)
    ("10:00am", "3:15pm", ["morning"], ["morning"]),
    ("2:30pm", "10:00pm", ["night"], ["night"]),
    ("2:00pm", "10:00pm", ["night"], ["morning", "night"]),
    ("11:30am", "7:00pm", ["morning", "night"], ["morning", "night"]),
    ("10:30am", "6:25pm", ["morning"], ["morning", "night"]),
    ("3:00pm", "5:00pm", ["night"], ["night"]),
    ("6:00am", "10:30am", ["morning"], ["morning"]),
    ("12:30am", "4:00am", ["night"], ["night"]),
    ("4:30am", "12:30pm", ["morning"], ["morning"]),
    ("5:00pm", "2:00am", ["night"], ["night"]),
]


def test_present_primary_and_touched_on_fixtures():
    for start, end, present, touched in FIXTURES:
        row = _row("2026-10-09", start, end)
        assert st.present_dayparts(row) == present, (start, end)
        assert st.primary_daypart(row) == present[0], (start, end)
        assert st.touched_dayparts(row) == touched, (start, end)
        # the scorer's names are the same rule
        assert sq.present_dayparts(row) == present and sq.touched_dayparts(row) == touched


def test_every_surface_reads_the_same_dinner():
    import kitchen_stations as ks
    import schedule_memory as sm
    import schedule_skeleton as sk
    import schedule_dedicated as sd
    for start, end, present, _touched in FIXTURES:
        row = _row("2026-10-09", start, end)
        # a cook covers the stations of every daypart they are present for
        # (BUGSGEN-9: a 10am-3:15pm cook covered the DINNER grill)
        assert sorted(ks.parts_of(row)) == sorted(present), (start, end)
        assert sm._row_part(row) == present[0], (start, end)
        assert sk.row_daypart(row) == present[0], (start, end)
        assert sd._part_of(row) == present[0], (start, end)
        # a close is about the daypart a shift ENDS in, on the same boundary
        assert sm._end_part(row) == st.end_daypart(row), (start, end)
    assert st.end_daypart(_row("2026-10-09", "10:00am", "3:00pm")) == "morning"
    assert st.end_daypart(_row("2026-10-09", "10:00am", "11:00pm")) == "night"
    assert st.end_daypart(_row("2026-10-09", "12:30am", "4:00am")) == "night"


def test_a_restaurants_own_boundary_moves_only_what_covers_neither_core():
    changeover = _row("2026-10-09", "3:00pm", "5:00pm")
    assert st.primary_daypart(changeover) == "night"
    with st.daypart_scope("4:00pm") as split:
        assert split == 16 * 60
        assert st.primary_daypart(changeover) == "morning"
        assert sr.daypart_of("3:30pm") == sq.daypart_of("3:30pm") == "morning"
        # a shift covering a core window is still judged by it
        assert st.primary_daypart(_row("2026-10-09", "3:00pm", "10:00pm")) == "night"
    assert st.daypart_split() == st.DAYPART_SPLIT
    # an unreadable or out-of-range boundary keeps 3pm
    with st.daypart_scope("9:00am"):
        assert st.daypart_split() == st.DAYPART_SPLIT
    with st.daypart_scope("nonsense"):
        assert st.daypart_split() == st.DAYPART_SPLIT
