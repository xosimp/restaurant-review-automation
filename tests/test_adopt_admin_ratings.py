"""Ratings entered through support (view-as) don't count until the account
holder confirms them — and the screen says so (Simple EJ's, 9/30/26: 57
ratings on screen, "0 of 57 rated", the scheduler using none)."""
import pytest

import models
from models import CapabilityError, Restaurant, adopt_admin_ratings, capability_coverage, create_restaurant


def _rated_by_support(db_path, rid, names):
    conn = models.get_conn(db_path)
    for i, n in enumerate(names):
        conn.execute("INSERT INTO staff_capabilities (restaurant_id, employee_name, attribute, score, updated_by, "
                     "updated_at, authority, via) VALUES (?,?,?,?,?,datetime('now'),'admin','view_as')",
                     (rid, n, "overall", 3 + i % 3, "support:will"))
    conn.commit()
    conn.close()


def test_support_ratings_are_named_then_counted_once_the_owner_confirms(db_path):
    rid = create_restaurant(Restaurant(name="Rated Co", owner_email="o@x.test"), db_path=db_path)
    _rated_by_support(db_path, rid, ["Dana Reyes", "Bo Park"])
    cov = capability_coverage(rid, ["Dana Reyes", "Bo Park", "Chidi O."], db_path=db_path)
    assert (cov["rated"], cov["admin_set"], cov["active"]) == (0, 2, False)
    with pytest.raises(CapabilityError):
        adopt_admin_ratings(rid, {"id": 1, "role": "owner", "acting_admin_id": 9}, db_path=db_path)   # view-as
    with pytest.raises(CapabilityError):
        adopt_admin_ratings(rid, {"id": 2, "role": "manager"}, db_path=db_path)
    assert adopt_admin_ratings(rid, {"id": 3, "role": "owner", "username": "erik"}, db_path=db_path) == 2
    cov = capability_coverage(rid, ["Dana Reyes", "Bo Park", "Chidi O."], db_path=db_path)
    assert (cov["rated"], cov["admin_set"], cov["active"]) == (2, 0, True)
