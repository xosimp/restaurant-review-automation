"""scripts/refresh_seasons.py's merge — the rules a season refresh keeps
(10/2/26). No network: the fetchers are not called here."""
import importlib.util
import os
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("refresh_seasons", os.path.join(ROOT, "scripts", "refresh_seasons.py"))
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

FILE = [
    {"external_id": "2026-reg-04", "date": "2026-10-04", "kickoff": "12:00", "home_away": "home",
     "opponent": "New York Jets", "venue": "Soldier Field", "broadcast": "CHSN, FOX", "status": "scheduled"},
    {"external_id": "2026-reg-18", "date": None, "kickoff": None, "home_away": "away",
     "opponent": "Minnesota Vikings", "venue": "U.S. Bank Stadium", "broadcast": None, "status": "scheduled"},
    {"external_id": "a1", "date": "2026-12-20", "kickoff": "07:00", "home_away": "away", "opponent": "Ottawa Senators",
     "venue": "PSD Bank Dome", "status": "scheduled", "attributes": {"neutral_site": True}},
    {"external_id": "x9", "date": "2026-11-01", "home_away": "home", "opponent": "Gone FC", "status": "scheduled"},
    {"external_id": "p1", "date": "2026-09-20", "home_away": "home", "opponent": "Minnesota Vikings",
     "status": "completed", "result": "L 3-9"},
]


def test_a_refresh_keeps_ids_fills_gaps_and_never_shrinks_or_deletes():
    fetched = [
        # same game, ESPN's own id, a day-matched date: keeps the file's id; TV never shrinks
        {"external_id": "401", "date": "2026-10-04", "kickoff": "12:00", "home_away": "home",
         "opponent": "New York Jets", "venue": "Soldier Field", "broadcast": "FOX", "status": "completed",
         "result": "W 24-10", "season_type": "regular"},
        # the TBD game gets its date
        {"external_id": "402", "date": "2027-01-09", "kickoff": None, "home_away": "away",
         "opponent": "Minnesota Vikings", "venue": "U.S. Bank Stadium", "broadcast": None, "status": "scheduled",
         "season_type": "regular"},
        # a neutral-site game listed as "home" by the league keeps its side
        {"external_id": "a1", "date": "2026-12-20", "kickoff": "07:00", "home_away": "home",
         "opponent": "Ottawa Senators", "venue": "PSD Bank Dome", "status": "scheduled", "season_type": "regular"},
        # a played game listed as scheduled again never goes back
        {"external_id": "p1", "date": "2026-09-20", "home_away": "home", "opponent": "Minnesota Vikings",
         "status": "scheduled", "season_type": "regular"},
        # a playoff game is added
        {"external_id": "403", "date": "2027-01-16", "kickoff": "15:30", "home_away": "home",
         "opponent": "Green Bay Packers", "venue": "Soldier Field", "broadcast": "FOX", "status": "scheduled",
         "season_type": "postseason"},
    ]
    events, changes = rs.merge(FILE, fetched, today=date(2026, 10, 2))
    by = {e["external_id"]: e for e in events}
    assert by["2026-reg-04"]["status"] == "completed" and by["2026-reg-04"]["result"] == "W 24-10"
    assert by["2026-reg-04"]["broadcast"] == "CHSN, FOX"
    assert by["2026-reg-18"]["date"] == "2027-01-09"
    assert by["a1"]["home_away"] == "away"
    assert by["p1"]["status"] == "completed" and by["p1"]["result"] == "L 3-9"
    assert by["403"]["season_type"] == "postseason" and "401" not in by and "402" not in by
    assert "x9" in by and any(c.startswith("? 2026-11-01 Gone FC") for c in changes)
    assert [e["date"] or "9999" for e in events] == sorted(e["date"] or "9999" for e in events)


def test_the_script_never_runs_from_the_app():
    for path in ("scheduler.py", "jobs_registry.py", "event_intel/store.py", "event_intel/engine.py"):
        with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
            assert "refresh_seasons" not in fh.read(), path
