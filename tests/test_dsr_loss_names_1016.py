"""10/16/26 (owner): the daily report's give-aways read "Discount · No reason
given $381" — RPOWER sends no reason with a discount, but its own name says
what it is; and punch edits on shared POS logins ("Night Bar", "Pm Host")
read as role names."""
from dsr import block_service as bs
from rpower import is_station_name


def test_a_discount_with_no_reason_is_named_by_the_discount_itself():
    lines = [{"kind": "discount", "item_name": "50% Staff Disc", "sales": -25.97, "reason": None},
             {"kind": "discount", "item_name": "50% Staff Disc", "sales": -10.00, "reason": None},
             {"kind": "discount", "item_name": "Mgr 100% Comp`ITEM", "sales": -4.99, "reason": None},
             {"kind": "comp", "item_name": "Wings", "loss_amount": 16.99, "reason": "Error Made"}]
    given = bs._loss(lines, {}, {})["given"]
    reasons = {(r["kind"], r["reason"]): r for r in given["by_reason"]}
    assert reasons[("discount", "50% Staff Disc")]["lines"] == 2
    assert ("discount", "Mgr 100% Comp") in reasons and ("comp", "Error Made") in reasons
    assert not any(r["reason"] == bs.NO_REASON for r in given["by_reason"])


def test_a_shared_pos_login_is_marked_never_read_as_a_person():
    for n in ("Night Bar", "Pm Host", "To Am", "Rpower"):
        assert is_station_name(n), n
    assert not is_station_name("Callisandra Plauda")
    edits = bs._edits([{"employee_name": "Night Bar", "role": "Bartender PM", "edited_by": "m1", "clock_in": None},
                       {"employee_name": "Callisandra Plauda", "role": "Bartender PM", "edited_by": "m1", "clock_in": None}],
                      {"m1": "Erik Baylis"})
    assert [e["shared_login"] for e in edits] == [True, False]
