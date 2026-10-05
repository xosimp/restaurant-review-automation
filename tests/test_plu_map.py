"""Back Office PLUs against RPOWER's items (plu_map.py, 10/5/26)."""
import models
import plu_map
from models import Restaurant, create_restaurant


def _rid(db_path):
    models.init_db(db_path)
    return create_restaurant(Restaurant(name="PLU Co", owner_email="p@x.test"), db_path=db_path)


MENU = {"m1": "Coors Light`", "m2": "Coors Light`Bucket", "m3": "EJ's Smash Burger`", "m4": "Michelob 22oz`",
        "m5": "Side Ranch`", "m6": "Side Ranch`"}
SOLD = {"m1": (379, 1895.0), "m2": (80, 1600.0), "m3": (2658, 34500.0), "m4": (450, 3150.0),
        "m5": (120, 0.0), "m6": (151, 0.0)}


def test_names_match_through_apostrophes_and_the_variant_backtick():
    rows = plu_map.match([{"plu": "2122", "name": "Coors Light", "qty": 369},
                          {"plu": "2130", "name": "Coors Light`Bucket", "qty": 80},
                          {"plu": "2111", "name": "EJs Smash Burger", "qty": 2639}], MENU, SOLD)
    assert [(r["item_id"], r["how"]) for r in rows] == [("m1", "name"), ("m2", "name"), ("m3", "name")]


def test_a_shared_name_is_settled_by_quantity_and_a_rename_by_quantity_and_sales():
    rows = plu_map.match([{"plu": "2400", "name": "Side Ranch", "qty": 151},
                          {"plu": "2170", "name": "/D Michelob Ultra", "qty": 450, "gross": 3160.0},
                          {"plu": "2999", "name": "Mystery", "qty": 7, "gross": 9.0}], MENU, SOLD)
    assert (rows[0]["item_id"], rows[0]["how"]) == ("m6", "name+qty")
    assert (rows[1]["item_id"], rows[1]["how"]) == ("m4", "qty+sales")
    assert rows[2]["item_id"] is None and rows[2]["how"] is None, "never guessed"


def test_a_plu_two_items_share_is_answered_only_with_its_name(db_path):
    rid = _rid(db_path)
    plu_map.save(rid, [{"plu": "2159", "bo_name": "Coors Light", "item_id": "m1", "how": "name"},
                       {"plu": "2159", "bo_name": "Woodford", "item_id": "m9", "how": "name"},
                       {"plu": "2111", "bo_name": "EJs Smash Burger", "item_id": "m3", "how": "name"}],
                 source="test", db_path=db_path)
    assert plu_map.item_for(rid, "2159", db_path=db_path) is None, "a shared PLU alone is ambiguous"
    assert plu_map.item_for(rid, "2159", "Woodford", db_path=db_path) == "m9"
    assert plu_map.item_for(rid, "2111", db_path=db_path) == "m3"
    assert plu_map.item_for(rid, "2111", "EJ's Smash Burger", db_path=db_path) == "m3"


def test_a_manual_answer_survives_the_next_import(db_path):
    rid = _rid(db_path)
    plu_map.set_manual(rid, "2236", "HN Watermelon", "m7", "High Noon WMellon`", db_path=db_path)
    out = plu_map.save(rid, [{"plu": "2236", "bo_name": "HN Watermelon", "item_id": None, "how": None},
                             {"plu": "2109", "bo_name": "Charlies Sipper", "item_id": None, "how": None, "bo_qty": 64}],
                       source="pmix", db_path=db_path)
    assert out == {"written": 1, "kept_manual": 1, "unmatched": 1}
    assert plu_map.item_for(rid, "2236", db_path=db_path) == "m7"
    assert [u["bo_name"] for u in plu_map.unmatched(rid, db_path=db_path)] == ["Charlies Sipper"]
