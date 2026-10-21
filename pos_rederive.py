"""pos_rederive — what a restated POS night derives again (Audit #23, 10/21/26:
A2-07, A6-05, A2-15).

A night the POS rewrote after it was archived (a void entered six days on, a
late comp, a record re-dated to the next night) was archived again, but each
figure DERIVED from it had its own window: labor history re-saved within 60
days, the nightly report re-checked only within 4 nights and only past $50
AND 2%, menu_item_sales never past the depletion overlap, and the night a
record moved AWAY from kept its bar night and its report as they were. So
Labor and the report disagreed about the same night for good.

Now pos_archive.archive_day queues ONE `pos_rederive` row per restated night
and per night a record moved away from (job_queue, the bulk group, keyed on
the night's newest POS write so a re-read that changed nothing queues
nothing), and the row derives every figure again from the archive — no POS
call of its own:

  bar night         bar.sales.derive_nights (the moved-from night; the
                    restated one is re-derived by archive_day already)
  labor day         labor_daily_history's sales and labor % for the night,
                    from the archive's net (rpower.fetch_business_days' rule)
  item sales        inventory_ledger.refresh_item_sales_day (units only)
  nightly report    dsr.pipeline.recheck_final — a final night whose net
                    moved by RECHECK_MIN_DOLLARS ($1) or more gets a new
                    version, at any age; no notice is sent again
                    (dsr.deliver: a re-check never re-notifies)

The restatement only reaches here through the change feed (A2-15 — RPOWER
question 6 on whether time_stamp is store- or cloud-side is F2's).
"""
import logging

log = logging.getLogger("pos_rederive")

KIND = "pos_rederive"


def enqueue(restaurant_id, provider, dates, stamp=None, reason="restated"):
    """Queue one re-derive row per night in `dates`. Never raises (the
    archive stands whatever the queue says); returns rows queued."""
    n = 0
    try:
        import job_queue
        for iso in sorted({str(d)[:10] for d in dates or () if d}):
            _id, created = job_queue.enqueue(
                KIND, {"restaurant_id": int(restaurant_id), "provider": provider, "date": iso, "reason": reason},
                restaurant_id=int(restaurant_id),
                idempotency_key=f"{KIND}:{int(restaurant_id)}:{provider}:{iso}:{stamp or ''}", wake=False)
            n += 1 if created else 0
    except Exception as e:
        log.warning("pos_rederive: not queued for %s %s: %s", restaurant_id, dates, e)
    return n


def run_queued(job):
    """job_queue's handler for `pos_rederive` rows."""
    p = job.payload if hasattr(job, "payload") else (job.get("payload") or {})
    return rederive(p.get("restaurant_id"), p.get("provider"), p.get("date"))


def _labor_day(restaurant_id, iso):
    """labor_daily_history's sales (and the labor % over it) for one night
    from the archive's net — only a row already on file, and never a NULL
    over a figure (models.save_labor_daily_history's rule)."""
    import pos
    from datetime import date
    from models import get_conn
    d = date.fromisoformat(iso)
    by, _provider = pos.fetch_business_days(restaurant_id, d, d)
    net = (by or {}).get(iso)
    if net is None or float(net) <= 0:
        return {"labor": "no net"}
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE labor_daily_history SET sales=?, labor_pct=CASE WHEN labor_cost IS NULL THEN labor_pct "
            "ELSE ROUND(labor_cost * 100.0 / ?, 1) END WHERE restaurant_id=? AND date=? "
            "AND (sales IS NULL OR ABS(sales - ?) >= 0.01)",
            (round(float(net), 2), float(net), restaurant_id, iso, float(net)))
        conn.commit()
        return {"labor": int(cur.rowcount or 0)}
    finally:
        conn.close()


def rederive(restaurant_id, provider, iso) -> dict:
    """Every figure of one night derived again from the archive (module
    note). Each step on its own: one failing is captured and the rest run.
    Raises only when every step failed (the queue retries the row)."""
    if restaurant_id is None or not iso:
        return {"skipped": "no restaurant or night"}
    rid = int(restaurant_id)
    out, failed = {}, []

    def step(name, fn):
        try:
            out[name] = fn()
        except Exception as e:
            failed.append(name)
            try:
                import ops
                ops.capture(e, job=KIND, context=f"restaurant_id={rid} date={iso} step={name}", restaurant_id=rid)
            except Exception:
                pass

    def _bar():
        from bar.sales import derive_nights
        return derive_nights(rid, [iso], provider=provider)

    def _items():
        import inventory_ledger
        return inventory_ledger.refresh_item_sales_day(rid, iso)

    def _report():
        from models import get_restaurant
        from dsr import pipeline
        r = get_restaurant(rid)
        if r is None or not getattr(r, "dsr_enabled", 1):
            return {"action": "off"}
        res = pipeline.recheck_final(r, iso)
        return {"action": (res or {}).get("action")}

    step("bar", _bar)
    step("labor", lambda: _labor_day(rid, iso))
    step("items", _items)
    step("report", _report)
    if failed and len(failed) == 4:
        raise RuntimeError(f"pos_rederive {rid} {iso}: every step failed")
    out["failed"] = failed
    return out
