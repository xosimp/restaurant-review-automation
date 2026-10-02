"""
event_intel — the Event Intelligence Engine: one catalog of dated events
(teams, holidays, concerts, festivals, local happenings) that every
restaurant near them feels, made part of the data every module already
reads.

  store    the catalog's tables (event_series, catalog_events,
           event_follows), the bundled seasons in event_intel/seasons, and
           follows
  engine   who follows what (by distance), each restaurant's copy of its
           events in demand_signals, and what those events did here —
           measured, never assumed — for the forecast, the schedule, the
           nightly report, the brief, marketing and Ask

The first season is the 2026 Chicago Bears; a new team or festival is one
JSON file in event_intel/seasons or one upsert, never a code change.
"""
from event_intel import store as _store


def init_event_intel(db_path=_store.DB_PATH):
    """models.init_db calls it at boot: the catalog's tables and bundled
    seasons (store), then the materialised peer table (peers, re-audit
    10/1/26 P4-05). Never on a request path."""
    _store.init_event_intel(db_path)
    from event_intel.peers import init_peers
    init_peers(db_path)
