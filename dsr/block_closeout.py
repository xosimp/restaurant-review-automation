"""
dsr.block_closeout — the manager's own account of the night.

Source: the close-out filed for the business date (closeout.py, the
close_outs table): the four quick lines and the six DSR fields, including
`influence` — Erik's "Influence/Result" column.

The words are returned VERBATIM and marked as manager-written, with who
filed them and when. Nothing here summarises, scores or corrects them; the
narrative may quote them but may not present them as measured.

No close-out filed is a normal night, not an error: the block is
UNAVAILABLE with "No manager closeout filed". It is not AWAITING — a
close-out is not guaranteed to come, and "awaiting" would hold a report
provisional for something that may never be written. One filed after the
report ran reaches the next version the pipeline collects.
"""
import dsr
from dsr import common


def collect(ctx):
    import closeout
    gaps = []
    entry = common.guard(ctx, "closeout", "closeout",
                         lambda: closeout.get(ctx.restaurant_id, ctx.day, db_path=ctx.db_path), gaps)
    if gaps:
        return dsr.block(dsr.UNAVAILABLE, source="manager", block_name="closeout",
                         reason="Manager closeout unavailable", metrics={"filed": None})
    if not entry:
        return dsr.block(dsr.UNAVAILABLE, source="manager", block_name="closeout",
                         metrics={"filed": 0}, detail={"filed": False})

    fields = {k: (entry.get(k) or None) for k in closeout.FIELDS}
    filed_at = common.parse_utc(entry.get("created_at"))
    local = common.to_local(filed_at, ctx.restaurant) if filed_at else None
    return dsr.block(
        dsr.READY, source="manager",
        metrics={"filed": 1, "fields_written": sum(1 for v in fields.values() if v)},
        detail={
            "filed": True,
            # The manager's words, exactly as typed. Views render them as a
            # quotation; the narrative is told they are the manager's own.
            "written_by": "manager",
            "verbatim": True,
            "submitted_by": entry.get("submitted_by"),
            "user_id": entry.get("user_id"),
            "filed_at_utc": filed_at.strftime("%Y-%m-%d %H:%M:%S") if filed_at else None,
            "filed_at_label": common.time_label(local) if local else None,
            "fields": fields,
            "labels": dict(closeout.LABELS),
            "order": list(closeout.FIELDS),
        })
