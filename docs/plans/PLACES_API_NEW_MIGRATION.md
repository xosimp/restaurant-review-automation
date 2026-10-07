# Google Places: legacy API → Places API (New)

**Status: Not yet built.** Plan, 10/7/26 (AI cost audit #99). Grounded in the
code at that date; re-derive every call site with the commands beside them
before acting.

## Why, and why not for cost

Every Places request Cavnar AI makes goes to the **legacy** Places API
(`https://maps.googleapis.com/maps/api/place/<endpoint>/json`). Google stopped
letting new Cloud projects enable the legacy API (March 2025) and documents
the new API as the one that gets features; the legacy one is the one that will
be turned off. The migration is **deprecation insurance**, not a saving: on
the new price list the fields this product needs most — `rating`,
`userRatingCount`, `reviews`, `priceLevel` — are **Enterprise** (and
Enterprise + Atmosphere) fields, so a like-for-like request costs about the
same or a little more than today. Do it before Google announces a shutdown
date, on our schedule, not theirs.

## The one seam

`ai_utils.places_request(endpoint, params, restaurant_id, action, timeout)` is
the only way the code calls Places (#123): the key check, the per-restaurant
and global ceilings (`places_budget_exceeded`, `PLACES_ESSENTIAL_ACTIONS`),
the Places breaker, the metering (`meter_places`, `places_fee`) and the
day's Details cache (`places_details_cache`, `PLACES_UNCACHED_ACTIONS`, #13)
all live there, and it returns a `_PlacesResponse` whose `.json()` the
callers parse. The migration happens **inside that function**, so the
callers keep their legacy-shaped dicts:

1. Map the legacy request to the new one (endpoint, method, body, field mask).
2. Send it (`X-Goog-Api-Key`, `X-Goog-FieldMask` headers; `timeout` as now —
   `scripts/check_timeouts.py`).
3. Translate the new response back into the legacy shape the callers read
   (`result` / `results`, `status`), so no caller changes in the first step.
4. Meter it at the new SKU (`places_fee` grows a new price table keyed by
   SKU tier; `price_version` on the ledger row says which list priced it).

One outside caller bypasses the seam on purpose: `provider_health.py` probes
`/place/details/json` directly (an unmetered health probe). It moves with
step 1 of the rollout.

## The calls today

`rg -n "_?places_request\(" --glob '*.py' --glob '!tests/**'`

| Caller | Legacy endpoint | Fields | Action |
|---|---|---|---|
| `fetcher.fetch_google` | details | `REVIEW_FETCH_FIELDS` = reviews, user_ratings_total, rating, types, price_level (`reviews_sort=newest`) | `review_fetch` (essential, uncached) |
| `competitor` (own profile) | details | name, types, price_level, editorial_summary, menu_url, website, serves_* | competitor |
| `competitor` (search) | textsearch | — (all) | `competitor_search` |
| `competitor` (neighbours) | nearbysearch, `rankby=prominence`, `type=restaurant`, `keyword` | — (all) | competitor |
| `competitor` (each competitor) | details | `COMPETITOR_FIELDS` = name, rating, user_ratings_total, business_status, reviews; `CUSTOM_FIELDS` adds types, vicinity, price_level | competitor |
| `competitor` (daily ratings, 10/2/26) | details | rating, user_ratings_total, types, price_level | `competitor_daily` |
| `competitor` (closed check) | details | name, business_status | competitor |
| `first_look` | details | name, rating, user_ratings_total | first_look |
| `weather` | details | geometry | weather |
| `client_api` (address) | details | address_component | onboarding |

## Field mask mapping (legacy → new)

| Legacy field | New field (mask path) | New SKU tier |
|---|---|---|
| `place_id` | `id` | Essentials (IDs only) |
| `name` | `displayName` | Pro |
| `types` / `type` | `types`, `primaryType` | Essentials |
| `business_status` | `businessStatus` | Pro |
| `geometry` | `location`, `viewport` | Essentials |
| `vicinity` | `shortFormattedAddress` | Essentials |
| `address_component(s)` | `addressComponents` | Essentials |
| `website` | `websiteUri` | Enterprise |
| `editorial_summary` | `editorialSummary` | Enterprise + Atmosphere |
| `rating` | `rating` | Enterprise |
| `user_ratings_total` | `userRatingCount` | Enterprise |
| `price_level` | `priceLevel` (enum `PRICE_LEVEL_*`, not 0-4) | Enterprise |
| `reviews` | `reviews` (newest-first is not a request option on the new API: sort client-side by `publishTime`; at most 5 either way) | Enterprise + Atmosphere |
| `serves_*` | `servesBreakfast`, `servesBrunch`, `servesLunch`, `servesDinner`, `servesBeer`, `servesWine`, `servesCocktails`, `servesVegetarianFood` | Enterprise + Atmosphere |
| `menu_url` | no equivalent — drop, or read the website | — |

Search requests: `textsearch` → `POST places:searchText` (`textQuery`,
`locationBias.circle`); `nearbysearch` → `POST places:searchNearby`
(`includedTypes: ["restaurant"]`, `locationRestriction.circle`,
`rankPreference: POPULARITY`; **no `keyword`** — a meal keyword becomes a Text
Search with `includedType`). Both return `places[]` and are billed by the
highest tier in the field mask, so the mask must be explicit (the legacy
searches returned, and billed, every field). The new searches page with
`nextPageToken` like the old.

## Pricing, side by side

List prices per 1,000 requests as published when this was written
(re-check Google's Places pricing page before acting; free monthly caps per
SKU are not netted here, as `ai_utils._PER_CALL_PRICING` does not net them):

| Request | Legacy today (`places_fee`) | New, like-for-like mask |
|---|---|---|
| Review fetch (rating, count, reviews, types, price) | $17 base + $5 Atmosphere = **$22** | Place Details Enterprise + Atmosphere ≈ **$25** |
| Daily competitor rating (rating, count, types, price) | $17 + $5 = **$22** | Place Details Enterprise ≈ **$20** |
| Weather geometry | **$17** | Place Details Essentials ≈ **$5** |
| Address components | **$17** | Essentials ≈ **$5** |
| Nearby / Text Search (all fields) | $32 + $5 + $3 = **$40** | Pro mask ≈ **$32**; with rating/count (Enterprise) ≈ **$35**; with reviews ≈ **$40** |

So the reads that matter cost about the same; the small Essentials reads get
cheaper; nothing becomes materially cheaper while ratings and reviews are in
the mask. The Details cache (#13) and the quiet Places-only cadence (#45)
remain the cost levers either way.

## Rollout

1. **The seam translates** (`PLACES_API_VERSION=legacy|new`, default legacy):
   request mapping, response translation back to the legacy shape, the new
   price table, `provider_health`'s probe. Unit-tested on recorded new-API
   responses for each row of the table above; the cache keys on the new
   field mask so a legacy-cached answer is never served as a new one.
2. **Shadow a week**: one Cloud project with both APIs enabled; for a sample
   of `competitor_daily` reads (cheap, no owner sees them first) call both
   and compare rating, count and business status — differences recorded,
   never served.
3. **Switch by action**: `weather` and the address read first (Essentials,
   cheaper), then `first_look` and the competitor reads, the review fetch
   last (it is `review_fetch`, essential; watch `places_coverage` and the
   review-fetch gap record, MOD-REV-11, for a week).
4. **Callers move to the new shape** one at a time, and the translation in
   the seam is deleted when the last legacy reader is gone (the deletion
   checklist in CLAUDE.md applies).

## Risks

- `priceLevel` is an enum on the new API; every reader that compares it as
  0-4 (`competitor`'s price tier, the benchmarks' price band) must go through
  the translation.
- Review ids: a legacy review's identity here is `google_<time>_<author>`
  (`fetcher._places_external_id`); the new API gives `reviews[].name`. A
  switch that changed the id would store every recent review twice — keep
  the legacy id formula, built from `publishTime` and `authorAttribution`.
- The legacy `reviews_sort=newest` has no new-API equivalent: the five
  returned are Google's choice; the coverage figure (`fetcher.places_coverage`)
  will say whether that loses reviews.
- Terms: Places content may be cached only as Google's terms allow; the
  Details cache stays one day (`RETAIN_PLACES_CACHE_DAYS`).
