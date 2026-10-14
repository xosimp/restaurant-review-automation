from test_schedule_people_identity import _db, _sync, _rid, HEADER  # noqa: F401
from datetime import date
import models
import people
from labor import load_shifts


def test_debug(db_path):
    rid = _rid()
    _sync(rid, [("Mike Smith", "7001"), ("Ana B.", "7002")])
    lines = [HEADER]
    for d in (8, 9):
        day = date(2026, 9, d)
        for name, ext in [("Michael Smith", "7001"), ("Ana B.", "7002")]:
            lines.append(f"{day.isoformat()},{day.strftime('%A')},{name},Server,16:00,22:00,6,6,1000,,,{ext},,1,0\n")
    rows = [dict(r) for r in load_shifts(csv_string="".join(lines))]
    print("RESOLVE", people.resolve_rows(rid, rows, "rpower", db_path=db_path))
