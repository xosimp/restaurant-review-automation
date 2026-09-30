"""Where each cross-restaurant figure comes from — and the one rule that
keeps Google user data out of pooled learning.

public/privacy.html promises, and Google's API Services User Data Policy
(Limited Use) requires, that data received through an owner's Google OAuth
connection — Google Business Profile reviews, their text and ratings, and
anything derived from them (an average rating, a reply rate or reply time,
a complaint share by category, a measured change in any of those) — is used
only for the restaurant that authorised the connection, never to train a
generalized model. Every POOLED read in this package (bands, patterns,
trends and the cohort series, recommendation priors at every rung, the
confidence log, DNA norms, neighbour predictions) is exactly that kind of
generalized learning.

The rule: a restaurant whose reviews come — or ever came — through the
owner's Google connection (google_connected_ids) contributes NO
review-derived figure to any pooled read. Its own screens keep every figure
(Level 1 is its own data). The rule is conservative on purpose: a
Places-fetched review of a connected restaurant is treated as Google user
data too, and a restaurant that disconnected keeps its stored Google
reviews, so it stays out.

Google Places data about public businesses — a competitor's rating read
with the platform's Places key, the urbanity band from Intel's own
competitor search — is a different source and is not covered here.

The trace behind it (memory fix round, 9/29/26, workstream M8) — every
cross-restaurant read in intelligence/ and what it rests on:

  intel_features → benchmarks.compute (bands), patterns.discover (both
      modes), trends.panel_series / persist (cohort series), the peer
      ladder's coordinates (jobs.peer_partitions) and the admin dashboard,
      all through features.latest_by_restaurant / weekly_by_restaurant →
      features.cross_restaurant_view, which withdraws REVIEW_FEATURES and
      swaps the recommendation-loop features for their review-free
      variants for a Google-connected restaurant.
  intel_rec_events → scoring.kind_stats (the prior ladder's every rung),
      scoring.similar_prior, jobs.log_confidence, the admin rank_kinds /
      platform_totals and predict's taken arm: a row is `google_data` when
      it is review-derived (a review kind, or a result on a review metric)
      and its restaurant is Google-connected, and no pooled reader reads it.
  intel_dna → dna.platform_norms leaves a connected restaurant's
      REVIEW_DNA_DIMS out; predict leaves connected restaurants out of any
      review metric's neighbours (their taken, untaken and baseline rows).
  No Google user data: labor, staffing, food cost, waste and structural
      features (shifts, the POS, inventory, Intel's Places distances);
      marketing features (posts Cavnar AI published, Meta's post metrics,
      sales lift from the POS); DNA's structural distance weights.
"""
# Features computed from the reviews table (features.compute's review block).
REVIEW_FEATURES = ("reviews_30d", "avg_rating_30d", "avg_rating_prior_60d", "avg_rating_delta",
                   "reply_rate_30d", "response_24h_rate_30d")
# The recommendation-loop features that count review-derived work, and the
# review-free variant a pooled read takes instead (features._rec_loop).
REVIEW_FREE_VARIANT = {
    "recs_answered_28d": "recs_answered_28d_ex_reviews",
    "recs_done_28d": "recs_done_28d_ex_reviews",
    "recs_declined_28d": "recs_declined_28d_ex_reviews",
    "outcomes_evaluated_90d": "outcomes_evaluated_90d_ex_reviews",
    "outcomes_improved_rate_90d": "outcomes_improved_rate_90d_ex_reviews",
}
# Restaurant DNA's guests family, measured from reviews (dna.py B12–B15).
REVIEW_DNA_DIMS = ("rating_level", "negative_share", "rating_momentum", "wait_service_complaints",
                   "reply_rate", "reply_within_day")
# Outcome metrics read from reviews (metrics._REGISTRY; complaints takes a
# category parameter).
REVIEW_METRICS = ("avg_rating", "complaints", "response_hours")
# Recommendation topics (rec_ledger.KIND_TOPIC) whose advice is made from
# reviews — and "competition": Intel's market read sets the restaurant's own
# rating beside its rivals', so its advice rests on the reviews too (the
# rivals' side is Places data; the restaurant's own side may not be). Plus
# the kinds that ask guests for reviews.
REVIEW_TOPICS = ("guest_experience", "replies", "competition")
# dish_praise exists only because guests named a dish in positive reviews
# (marketing_opportunities.dish_praise_cards); its topic is "marketing", so
# the topic table missed it (memory re-audit 9/29/26, PLATFORM-5).
REVIEW_KINDS = ("review_requests", "dish_praise")
# The root rule since that re-audit: an episode whose card DECLARED reviews
# among its evidence (rec_instances.evidence_sources — dish_praise's
# ("reviews",), a dish_promote that states a praise count) is review-derived
# whatever its kind or topic. The kind and topic tables stay as the rule for
# a row with no episode.
REVIEW_SOURCES = ("reviews",)
# The rule's version (PLATFORM-6). Raise it whenever review_derived changes:
# feedback.sync then re-judges review_derived and google_data on EVERY
# intel_rec_events row once (its mark: job_cursors RULE_MARK) — a changed
# rule used to reach only rows written after it.
RULE_VERSION = 2
RULE_MARK = "intelligence_provenance_rule"


def review_sourced(sources) -> bool:
    """Whether an episode's declared evidence sources (a list, a JSON list
    or None) include reviews. Never raises."""
    if not sources:
        return False
    if isinstance(sources, str):
        try:
            import json as _json
            sources = _json.loads(sources)
        except (TypeError, ValueError):
            sources = [sources]
    try:
        return any(str(s or "").strip().lower() in REVIEW_SOURCES for s in sources)
    except TypeError:
        return False


def sources_by_key(conn, restaurant_id=None) -> dict:
    """{(restaurant_id, key): True} for every recommendation key some episode
    of which declared reviews among its evidence — one query; a reader that
    judges rows by key (a result with no episode id) looks it up here.
    Conservative: one review-sourced episode marks the key. Never raises."""
    out = {}
    try:
        sql = "SELECT restaurant_id, key, evidence_sources FROM rec_instances WHERE evidence_sources LIKE '%review%'"
        args = ()
        if restaurant_id is not None:
            sql += " AND restaurant_id=?"
            args = (restaurant_id,)
        for r in conn.execute(sql, args).fetchall():
            if review_sourced(r["evidence_sources"]):
                out[(int(r["restaurant_id"]), str(r["key"]))] = True
    except Exception:
        pass
    return out


def review_metric(metric) -> bool:
    """Whether an outcome metric (or an intel feature key) is read from
    reviews."""
    m = str(metric or "").strip()
    if not m:
        return False
    return m.split(":", 1)[0] in REVIEW_METRICS or m in REVIEW_FEATURES


def review_derived(key, kind=None, metric=None, sources=None) -> bool:
    """Whether a recommendation row rests on review data: its episode
    declared reviews among its evidence (`sources`, rec_instances.
    evidence_sources — the root rule, PLATFORM-5), its kind's topic is a
    review topic (rec_ledger._topic_of reads a DSR action's or a link's own
    kind from the key), it is a review kind or a link through reviews, or
    it measured a review metric. Never raises."""
    if review_sourced(sources):
        return True
    if review_metric(metric):
        return True
    k = str(key or "")
    for sep in ("#e", "#o"):
        if sep in k:
            k = k.rsplit(sep, 1)[0]
    kd = kind or (k.split(":", 1)[0] if not k.startswith("observed:") else "observed")
    if kd in REVIEW_KINDS:
        return True
    if kd == "link" and "reviews" in k:
        return True
    try:
        import rec_ledger
        topic = rec_ledger._topic_of(kd, k)
    except Exception:
        topic = None
    return topic in REVIEW_TOPICS


def _conn(db_path):
    import models as _m
    if db_path is None or db_path == getattr(_m, "DB_PATH", None):
        return _m.get_conn()
    return _m.get_conn(db_path)


def google_connected_ids(db_path=None, conn=None) -> set:
    """Ids of every restaurant whose reviews come, or ever came, through the
    owner's Google Business Profile connection: a GBP token or location on
    file, a revoked connection, or any stored review carrying a Business
    Profile resource name (reviews.review_name — set only by the GBP API,
    including on a Places row it later matched). Read in two queries. A
    database without those columns has none."""
    own = conn is None
    conn = conn or _conn(db_path)
    out = set()
    try:
        try:
            for r in conn.execute(
                    "SELECT id FROM restaurants WHERE COALESCE(gmb_refresh_token,'') != '' "
                    "OR COALESCE(gmb_access_token,'') != '' OR COALESCE(gmb_location_id,'') != '' "
                    "OR COALESCE(gmb_revoked_at,'') != ''").fetchall():
                out.add(int(r[0]))
        except Exception:
            pass
        try:
            for r in conn.execute("SELECT DISTINCT restaurant_id FROM reviews "
                                  "WHERE COALESCE(review_name,'') != ''").fetchall():
                out.add(int(r[0]))
        except Exception:
            pass
    finally:
        if own:
            conn.close()
    return out


def pooled_features(f: dict, google: bool) -> dict:
    """A feature row as a POOLED read may take it (features.
    cross_restaurant_view applies it): for a Google-connected restaurant,
    every review feature withdrawn (None — unmeasured, never 0) and each
    recommendation-loop feature replaced by its review-free variant; the
    variants themselves never leave. Pure."""
    out = dict(f or {})
    if google:
        for k in REVIEW_FEATURES:
            if k in out:
                out[k] = None
        for k, v in REVIEW_FREE_VARIANT.items():
            if k in out or v in out:
                out[k] = out.get(v)
    for v in REVIEW_FREE_VARIANT.values():
        out.pop(v, None)
    return out


# ── the admin calibration views (INT #5, the lead's decision, 9/29/26) ───────
#
# Outside intelligence/, three admin-only views pool measured results across
# restaurants to tune what Cavnar AI tells owners: the dollar calibration
# (admin_ops.recommendation_calibration), the support-score order check
# (admin_ops.confidence_calibration → confidence_engine.ordering) and the
# model's own confidence check (ai_reads.confidence_calibration). Only Will
# sees them, but calibrating a score on pooled results is generalized
# learning all the same, so the rule above applies to them: a Google-connected
# restaurant's review-derived results stay out of every POOLED calibration.
# A view of one restaurant (restaurant_id given) is that restaurant's own.

REVIEW_SURFACES = ("review_read", "review_diagnosis")


def pooled_row_excluded(restaurant_id, key=None, kind=None, metric=None, surface=None, google=()) -> bool:
    """Whether one measured row leaves a pooled calibration: its restaurant
    is Google-connected (`google`: google_connected_ids) and the row rests
    on reviews — a review kind or topic, a review metric, or a read of the
    reviews (REVIEW_SURFACES). Never raises."""
    try:
        if restaurant_id is None or int(restaurant_id) not in google:
            return False
    except (TypeError, ValueError):
        return False
    if surface and str(surface).split(":", 1)[0] in REVIEW_SURFACES:
        return True
    return review_derived(key, kind=kind, metric=metric)
