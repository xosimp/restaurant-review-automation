"""ai_reads — Cavnar AI's history of its own reasoning (memory audit 9/29/26:
ai_reads, claims, dsr_own, stale_diagnoses). SKELETON: workstream M4 owns
this module and replaces these bodies; the signatures are the contract.
"""


def record_read(restaurant_id, surface, text, subject=None, meta=None, call_id=None, db_path=None):
    """Keep one read (a module read, a diagnosis, a digest, a brief, a DSR
    narrative) as history instead of overwriting it. Returns its id or None."""
    return None


def recent_reads(restaurant_id, surfaces=None, days=30, limit=20, db_path=None):
    """What Cavnar AI told this restaurant lately: [{surface, subject,
    summary, created_at, id}], newest first."""
    return []


def claim_lines(req):
    """memory_context provider: the last claim on req.subjects for
    req.surface and its verdict ("LAST READ (9/2/26): ... Answered Done;
    complaints 9 -> 4 since (before and after, not proof)")."""
    return []
