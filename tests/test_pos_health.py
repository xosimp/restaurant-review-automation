"""pos_health.pos_sync_state — one provider-agnostic POS sync reading (confidence audit CA3 F6)."""
from datetime import datetime, timedelta, timezone

import pos
import pos_health

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def test_provider_names_track_pos_providers():
    assert set(pos_health.PROVIDER_NAMES) == set(pos.get_providers().keys())


def test_rpower_is_read_and_ages():
    # Stale by the registry's pos row (re-audit B6#5): 4 days reads aging
    # (64%), 6 days stale (36%) — the rule data_freshness scores.
    r = {"rpower_token": "t", "rpower_store_mid": "m",
         "rpower_last_synced": (NOW - timedelta(days=6)).replace(tzinfo=None).isoformat(timespec="seconds")}
    s = pos_health.pos_sync_state(r, now=NOW)
    assert s["provider"] == "rpower" and s["state"] == "stale" and round(s["age_days"]) == 6


def test_error_and_unknown_and_not_connected():
    assert pos_health.pos_sync_state({}, now=NOW)["state"] == "not_connected"
    assert pos_health.pos_sync_state({"square_access_token": "x"}, now=NOW)["state"] == "unknown"
    s = pos_health.pos_sync_state({"toast_restaurant_guid": "g", "toast_sync_error": "401",
                                   "toast_last_synced": NOW.isoformat()}, now=NOW)
    assert s["state"] == "error" and s["error"] == "401"


def test_current_within_a_nightly_sync():
    r = {"clover_api_token": "c", "clover_last_synced": (NOW - timedelta(hours=20)).isoformat()}
    assert pos_health.pos_sync_state(r, now=NOW)["state"] == "current"
