# API Reference — Cavnar AI

This documents **patterns and resource groups**, not every individual route — there are 751 URL rules (680 unique paths) across the app, and the specific handler is always one `grep` away once you know which file and prefix to look in. That lookup is intentionally cheap; re-deriving the whole route table from scratch every session is not. Use this file to answer "which file, which blueprint, which auth" before opening anything.

## Blueprints (registered in `hosted_dashboard.py`)

| Blueprint | File | URL prefix | Auth | Routes |
|---|---|---|---|---|
| `client_bp` | `client_api.py` | `/api/*` (+ page and public-token routes: `/approve/<id>`, `/e/<token>`, `/s/<token>`, `/join/<token>`, `/u/<token>`, `/m/<token>.jpg`) | web session cookie (`auth.login_required`) | 194 |
| `mobile_bp` | `mobile_api.py` | `/mobile/api/*` | Bearer token (`auth.mobile_login_required`) | 205 |
| `strategy_bp` / `strategy_mobile_bp` | `strategy_routes.py` | each `_ROUTES` entry at `/api/…` **and** `/mobile/api/…` | session / bearer | 68 + 68 |
| `issue_link_bp` | `strategy_routes.py` | `/i/<token>` | signed token | 1 |
| `admin_bp` | `admin_routes.py` | `/admin/*`, plus `/privacy`, `/terms`, `/sms-optin-preview`, `/.well-known/security.txt`, `/og-image.png`, `/favicon.*`, `/api/competitor-intel`, `/api/send-referral`, `/api/export-reviews` | `auth.admin_required` (writes need `is_admin`; the `support` role reads) | 96 |
| `audit_bp` | `sales_audit_routes.py` | `/admin/audits/*` (+ a public shared report by token) | admin | 22 |
| `staff_bp` | `staff_routes.py` | `/staff/*` — PIN sign-in, today, availability, time off, pre-shift | staff session (`auth.staff_login_required`) | 21 |
| `auth_bp` | `auth_routes.py` | `/login`, `/logout`, `/forgot-password`, `/reset-password/<token>`, `/verify-2fa`, `/auth/*`, and the account-security `/api/*` routes (sessions, change-password, update-email, 2FA toggles) | none → issues session; the `/api/*` ones need a session | 22 |
| `webhook_bp` | `webhook_routes.py` | `/stripe-webhook`, `/webhooks/resend`, `/docusign/callback`, `/docusign/callback2`, `/docusign/webhook` (inbound) | signature-verified | 6 |
| `social_bp` | `social_routes.py` | Instagram/Facebook OAuth + `/api/post-to-facebook`, `/api/post-insights` | mixed | 9 |
| `toast_bp` / `square_bp` / `clover_bp` / `rpower_bp` | `*_routes.py` | POS connect + `/admin/<pos>/*` sync/bootstrap | mixed | 8 / 7 / 7 / 6 |
| `status_bp` | `status_routes.py` | `/status` public page + `/admin/status/*` | public / admin | 6 |
| app-level | `hosted_dashboard.py` | `/`, `/health`, `/sitemap.xml`, `/robots.txt`, `/og-image-v2.png` | — | 5 |

The outbound-webhook config routes (`GET/POST/DELETE /api/webhook`, `POST /api/webhook/test`) are in `client_bp`, not `webhook_bp`. `audit_app.py` is a separate standalone Flask app (the digital audit scorecard on :9000), not a blueprint. There is no web `/register` and no web `/me`; both exist only under `/mobile/api/` (and `/staff/api/me`). `hosted_dashboard` refuses to boot if two rules share a (path, method).

Two 409s a client may meet on writes. **`location_changed`**: the dashboard names the location a page was rendered for in `X-Cavnar-Restaurant-Id` (sent by `templates/_csrf_fetch.html` from the `cavnar-restaurant-id` meta tag); `auth.login_required` refuses a request whose id is not the session's current location, except `/api/switch-location` (DATA-10). A request without the header is unaffected. **Stale form**: a whole-form settings save (the phone profile save, both alert-settings saves) may carry `expected_version` — the `row_version` it loaded — and gets 409 with `current_version` if the row has moved on (`models.expected_version_from`, DATA-28); without it the save is last-write-wins as before.

## The `_m()` delegation pattern

58 of `client_bp`'s `/api/*` handlers are one line (the rest own their body or share a `_do_*` with mobile — see *Which pattern to use* below):
```python
@client_bp.route("/api/ask-cavnar/opening")
@login_required
def ask_cavnar_opening(current_user):
    return _m("mobile_ask_opening")(current_user)
```
`_m(name)` looks up `mobile_api.<name>` and unwraps its `@mobile_login_required` decorator (`__wrapped__`), so the web route calls the exact same function body the iOS route calls — just with the already-resolved `current_user` from the web session instead of a decoded bearer token. **When a route's real logic isn't there, look in `mobile_api.py` first.** A genuinely web-only route (no iOS equivalent — e.g. CSV export, a desktop-only settings page) is one of the minority that doesn't delegate.

**Which pattern to use for a new endpoint both surfaces need.** Prefer a `strategy_routes._ROUTES` entry: one body, registered at both prefixes by the loop at the bottom of that file, with `_rid`/`_body`/`_principal` helpers. Second choice: a `_do_*` body in `client_api.py` called from both routes. Last: `_m()`. Never a third: 34 pairs still re-implement the same logic on both sides (885 lines, one of which has already diverged in its error handling), and each is a fix waiting to be missed on the other surface.

## Resource groups

### Account (`/api/account*`, `/mobile/api/account/*` — largest single group, ~36 mobile + ~26 client routes)
Profile, security (2FA setup/verify, backup codes, trusted devices, sessions list + revoke-others), team (invite/list/revoke — owner-role only), sign-in history, notification/alert-channel preferences, data export, billing status, marketing-email opt-out, timezone, connections status.

### Labor (`/api/labor*`, `/mobile/api/labor/*` — ~27 mobile + ~17 client)
Shift CSV upload, labor % + ribbon data, schedule generation/history/sharing, staff availability, staff contacts (for schedule delivery), Operational Score (`team`), scheduling profiles (demand levels), overtime/overstaffed-day detail.

**Schedule generation, review and publish** (both prefixes unless noted; bodies in `strategy_routes._ROUTES` except the first four):
- `GET /api/generate-schedule` / `POST /mobile/api/labor/generate-schedule` → `{job_id}` (needs `SCHEDULE_DRAFT`); returns `joined: true` when a generation for this restaurant is already running (`ops.active_job`) — poll that job. `GET …/schedule-status/<job_id>` → the finished payload: `preview_rows` (rows may carry `needs_review`/`review_reason`), `summary` (deterministic diff lines against the last published week), `narrative` (the model's own note), `quality` (each `shifts[].assignments[]` carries a `why` explanation), `review` `{hard, soft, by_kind, lines, hard_rows, fixes, unfixed}`, `rule_violations`, `pending_time_off`, `history_id`, `generation_seconds`, `chunked`, `roster`, `demand_by_date`, `learned_patterns`.
- `POST labor/schedule/score` `{rows, save?, history_id?}` → `{quality, violations, review}`; a save needs `SCHEDULE_DRAFT`, appends an `edited` version and writes `review_json`.
- `POST labor/schedule/violations` `{rows}` → `{violations, review, pending_time_off}` — re-check after an edit. `POST labor/schedule/apply-fixes` `{rows}` → `{rows, fixes, unfixed, quality, review}` — puts somebody legal on every hard breach; the owner still saves.
- `POST labor/publish-schedule` `{schedule_id?, acknowledge?}` (needs `SCHEDULE_PUBLISH`) → `409 {needs_ack, blockers[]}` until a human acknowledges (`client_api.publish_blockers`: NEEDS REVIEW rows, hard breaches, a weak or low-confidence quality verdict); on send stamps `published_at`/`published_by` and appends a `published` version. Friday auto-publish holds instead and tells the owner (`schedule_publish_held`).
- `GET labor/schedule-history/<id>/versions` → `{versions:[{version, reason: generated|edited|fixes|published, saved_by, changes, lines, score}], draft_vs_published}`.
- Roster and rules: `GET labor/roster` (everyone incl. deactivated, with settings, score, reliability, pairs), `POST labor/staff-settings` (active, employment_type, min/max hours, per-day daypart availability, is_minor), `POST labor/staff-pairs` / `DELETE labor/staff-pairs/<id>` (prefer/avoid), `GET/POST labor/rules` (compliance rules + per-role floors), `GET/POST labor/demand-signals` + `DELETE labor/demand-signals/<id>` (events and reservation counts per date; POST takes rows or a `date,covers` CSV).
- Shift requests (manager): `GET labor/shift-requests` → `{requests, open}` (rows carry `kind` drop|swap and, for swaps, the target shift); `POST labor/shift-requests/<id>/decide` `{decision: approve|deny, replacement?}` — approving a swap moves both shifts after the legality check runs both ways.
- Generation options: `POST generate-schedule` `{week_start?, dates?, history_id?}` — pick the week, or regenerate only some days of a draft with the rest pinned. The finished payload also carries `trimmed`/`hours_trimmed` (the deterministic trim back to the budget), `staggered`, `projected_cost` (overtime at 1.5× past the ceiling), `over_budget_dollars`, `projected_revenue_source`, `hourly_profile_ready`, `demand_data_through`, `reservation_feed`, `holiday_lift`, `could_hold`, `departments`, `regenerated_dates`.
- Saves: `POST labor/schedule/score` takes `version`; a newer version on file returns `409 {conflict, latest_version, saved_by, lines}`. A save to a published week emails only the people whose shifts changed and returns `changed_since_sent`. `POST labor/schedule/violations` with `baseline_rows` adds `cost` (hours and overtime-priced dollars the edit moves).
- Rules (extended): `GET/POST labor/rules` also carry `jurisdiction` + `pack` (compliance_packs), `role_arrivals`, `role_close_mins` (the last of a role stays until N minutes after close), `manager_rule_unusable`, `role_requirements` (certifications by role), `foh_roles`, `patio_roles`, `role_cross_training` (whole percents per role; GET also returns `cross_training_defaults` per scheduled role and the flat `cross_training_default`), `trim_to_budget`, `rules.manager_on_duty`, and the reservation provider (`reservation_provider`, `reservation_api_key`; `POST labor/reservations/sync`).
- Roster (extended): settings carry `time_windows`, `certifications`, `preferred_dayparts`, `desired_hours`; `GET labor/roster` adds `suggested_pairs`.
- `GET labor/learned-patterns` / `POST labor/learned-patterns` `{key, dismissed}` — the owner's say over what the draft learns. `POST labor/schedule/recommendation` `{kind, key, action}` — the accept/dismiss ledger. `GET labor/intel` — outcomes by daypart, the rotation ledger, behaviour-learned preferences, who could hold a station, suggested pairs, sales per labor hour, the revenue projection; `edit_prediction` (ready or why not, and its held-out hit rate on past drafts), `weight_calibration` (per dimension: current, suggested, step, the driving outcome and an `explanation`), `rotation` (the multi-week plan and its `lines`), `splh_objective` (the targets and their basis) and `starting_points` (a borrowed starting headcount for a restaurant with no history, or the reason there is none). A generated schedule also carries `likely_edits` (predicted rows carry `likelihood` and `reason`), `rotation_plan`, `splh_objective`, `splh_report` and `starting_headcount`.

### Reviews (`/mobile/api/reviews/*`, `/mobile/api/review-stats`; web routes are flat — `/api/reviews/page`, `/api/review-stats`, `/api/topic-heatmap`, `/api/sentiment-trend`, `/api/response-performance`, `/api/regenerate-draft/<id>`, `/api/save-draft/<id>`, and the actions at root: `/approve/<id>`, `/skip/<id>`, `/undo/<id>`, `/retract/<id>`)
List/detail/approve/skip/regenerate a draft response, retry a failed Google post (`/reviews/<id>/retry-post`, both surfaces), bulk approve-all, sentiment trend, topic heatmap, response-performance, review-request send, review stats.

### Food Cost (`/mobile/api/food-cost/*`, `/api/food-cost*`)
Inventory CSV upload, custom items, purchase orders, waste/drift insight.

### Marketing (`/mobile/api/marketing/*`; web routes are mostly flat — `/api/mkt-stats`, `/api/mkt-performance`, `/api/mkt-insight`, `/api/content-calendar`, `/api/post-to-google`, `/api/brand-voice`, `/api/generate-content`, plus `/api/marketing/*` for drafts, schedule, links, media, attribution, diagnosis, tags)
Drafts (create/edit/list), media upload, scheduling, attribution per post, the text-club feature (`/api/guest-contacts*`, `/api/guest-campaign/draft|send`, `/api/guest-newsletter`, `/api/guest-qr`, `/api/guest-segments`, `/api/public/guest-optin/<token>`), templates.

### Intel (`/mobile/api/intel/*`, `/api/intel*`, `/api/ai-visibility*`)
Competitor snapshots, AI-visibility run trigger + results.

### Ask Cavnar (`/mobile/api/ask-cavnar/*`, `/api/ask-cavnar*`)
`opening` (deterministic briefing, no model call), `conversations` (list/create), `history` (a conversation's messages), the streaming chat endpoint itself (SSE), and the tool-confirmation route a `write`-kind tool's proposal card posts to. Each proposal in an answer carries `proposal_id`, `details` (`[{label, value}]`), `preview` (the words that would go out) and `at_stake` (dollars, or null); `POST ask-cavnar/action` takes `{action, outcome, proposal_id?, reason?, summary, conversation_id}` — a `proposal_id` not this restaurant's (or naming another action) is a 404.

### Connections (`/mobile/api/connections/*`)
Per-integration connect/disconnect/status: Google Business, Instagram, Toast, Square, Clover.

### Home (`/api/home/brief`, `/api/home/brief/group`, `/mobile/api/home`)
The deterministic Home-tab payload — see `home_brief.py`. `?fresh=1` forces recompute past the 60s per-restaurant cache. `/group` returns every location in an owner's `location_group` side by side.

### The owner's day (`strategy_routes.py`, web + mobile twins)
`actions` (everything still open, filtered by what the reader may see), `actions/snooze` `{key, days}`, `closeout` (GET tonight's + the questions, POST the four lines). `morning-brief` now returns the caller's own brief plus `can_edit`; `morning-brief/settings` also takes `hold_alerts` and `preshift_nudge_hour`.

### Strategic foundations (`strategy_routes.py`, each at `/api/…` and `/mobile/api/…`)
`issues` (GET list / POST create), `issues/<id>/resolve`, `issues/<id>/reassign`, `issues/<id>/ask-cover` (POST `{name}` — LABOR_VIEW; texts, or emails, one of the coverage issue's suggested covers the request; SMS only to a consented number), `issues/routing` (GET/POST, principal-only), `goals` (GET/POST), `goals/<id>/end`, `outcomes` (GET/POST), `outcomes/<id>/abandon`, `metrics`, `food-cost/dish-scorecard`, `food-cost/reprice` (each suggestion carries `rec_key` `reprice:<dish>`; answered dishes are left out), `food-cost/reprice/apply` (POST `{dish, price?}` — one tap to the suggested price, or the owner's adjusted one; records suggested vs chosen, answers the recommendation and starts the outcome tracker; 409 when the dish has no live suggestion — web `/api` and `/mobile/api` twins in client_api / mobile_api), `food-cost/invoices` (GET list / POST multipart `file`), `food-cost/invoices/<id>`, `food-cost/invoices/<id>/apply`, `food-cost/recipe-drafts` (GET — drafts plus `missing` and `ingredients` counts), `food-cost/recipe-drafts/<id>/accept|reject`, `food-cost/recipes/draft` (POST `{menu}` — the pasted menu, one dish a line with an optional price; empty body drafts the POS dishes with no recipe; at most `RECIPE_DRAFT_LIMIT` model calls a request, `remaining` says how many are left), `food-cost/recipes/import` (POST CSV), `food-cost/recipes/scan` (POST multipart photo — iOS only now), `labor/demand?day=`, `labor/auto-draft` (GET/POST), `loss-signals` (LOSS_VIEW: owners, or a manager granted it), `morning-brief` (the caller's own brief) + `morning-brief/settings` (principal-only). Notifications: `GET notifications` (scoped to what the login may see; marks read per login), `GET notifications/unread-count`, `POST notifications/opened` `{type, alert_id?, rec_key?}`, `GET notifications/engagement` (types sent a lot and never opened — a suggestion, not a change), `POST account/send-test-push` (the caller's own devices only, delivered inline so the response is what APNs said). Team roles: `POST /api/account/team/invite` takes `role` (client | manager | member); `POST /api/account/team/<id>/role` `{role}` (owner-only) and its `/mobile/api` twin. Team access: `POST /api/account/team/<id>/access` and `/mobile/api/account/team/<id>/access` — `{permission, enabled}` and/or `{morning_brief}`, owner-only. Public: `GET/POST /i/<token>` (issue link; GET is side-effect free, POST `action=ack|resolve|ask_cover` — `ask_cover` with `name`, one of the coverage issue's suggested covers). Staff portal: `GET /staff/api/preshift`.

### Webhooks (`/api/webhook*` outbound config, `/webhooks/*` inbound)
`GET /api/webhook` (current config or null), `POST /api/webhook` (save + return a secret), `POST /api/webhook/test` (fire a test delivery), `DELETE /api/webhook` (remove). Inbound: Stripe and Twilio webhook receivers, signature-verified, event-id de-duped (`stripe_events_seen`).

### Auth (`auth_routes.py`; `/mobile/api/login`, `/register`, `/apple-signin`, `/verify-2fa`, `/forgot-password`, `/reset-password`, `/logout`, `/me`, `/device-tokens`)
Login (web + mobile variants), mobile register, forgot/reset password, verify-2fa, Apple/Google sign-in, logout, mobile `/me`, APNs token registration.

### Staff portal (`/staff/*`, `staff_routes.py`)
`/staff/r/<token>/login` (PIN), `/staff/api/me`, today's schedule (`/staff/api/shifts` — published weeks only; `published:false` when nothing is), availability, time off requests, `/staff/api/signup/claim`, `/staff/api/pin`, `/staff/api/preshift`, shift requests (`GET/POST /staff/api/shift-requests` — `kind: swap` with a named colleague's shift, `POST …/<id>/withdraw`), `GET /staff/api/colleagues`, `GET/POST /staff/api/preferences` (preferred dayparts, desired hours), and open shifts (`POST /staff/api/open-shifts/<id>/claim` — legality is `schedule_engine.replacement_is_legal`, the same check the owner's pickers use). Each shift carries a `break` window when the meal-break rule applies.

### Other groups with web + mobile twins
`/api/tasks*` (today's checklist), `/api/team/inbox` + `/api/team/messages*`, `/api/changelog*`, `/api/switch-location` + `/api/group-locations`, `/api/home` + `/api/home/dismiss` (hide / done / not_for_us / snooze `days`, `undo`, `restore_kind`; every answer also written to rec_ledger) + `/api/home/assign` (`{key, title, detail, contact_id}` → an issue keyed to the recommendation; owner only), `/api/rpower/status` and the `/admin/rpower/*` bootstrap.

### Admin (`/admin/*`, 96 routes on `admin_bp`; `/admin/status/*`, `/admin/audits*` and `/admin/<pos>/*` live on their own blueprints)
Client health rollup (owner → brand → location), job run history, manual contract send, sales-audit tool (`/admin/audits/*`), changelog authoring, status-incident management.

## Conventions

- **Success**: `jsonify(ok=True, ...)`, 200. **Failure**: `jsonify(ok=False, error=...)`, appropriate 4xx.
- **POST bodies**: `request.get_json() or {}` — never assume the body parses.
- **Side-effect emails/SMS**: wrapped in best-effort `try/except`, routed to `ops.capture()` on failure rather than failing the request the user is waiting on, and logged via `log_email()` / `email_log`.
- **CSRF**: every state-changing web POST needs the CSRF token (`_csrf_fetch.html`'s helper attaches it automatically to `fetch()` calls in the dashboard's own JS).
- **Rate limiting**: Ask Cavnar is capped at 5/min per restaurant **and user** (`ai_utils.ai_rate_limited`, keyed on both); AI budget is also enforced globally and per-restaurant (`ai_budget_exceeded`) before any model call fires.
- **Streaming**: Ask Cavnar's chat endpoint is Server-Sent Events, with named progress labels per tool call (e.g. `"read_alerts": "Checking what needs you"`) sent before the tool result, so the client can show what's happening rather than a bare spinner.

## Finding a specific route fast

```bash
grep -n "'/mobile/api/labor" mobile_api.py      # mobile handler
grep -n '"/api/labor' client_api.py             # web equivalent, if any (often `_m(...)`)
grep -n "def mobile_labor_" mobile_api.py        # by handler name pattern
grep -n '"/labor/' strategy_routes.py            # the _ROUTES table (registered at both prefixes)
python3 scripts/repo_inventory.py                     # every rule, by blueprint, from the live url_map
```

## Schedule experiments (admin only)

- `GET /admin/api/schedule-experiments` — per experiment and arm: weeks, draft
  acceptance (share of rows sent unedited) with a 90% interval, issues and
  labor % from `schedule_outcomes`, the draft's Shift Quality, the verdict
  under the minimum-sample rule, and every pin in force.
- `POST /admin/api/schedule-experiments/pin` `{restaurant_id, experiment, arm}` —
  pin a restaurant to an arm (`"off"` = the control) or unpin (`arm: null`).
- No owner route returns an arm. The generation payload's `optimizer.solver`
  carries the solver's stats (status, proved optimal, seconds, slots), not the arm.

## Intelligence engine

- `GET /admin/api/intelligence` (admin) — the Intelligence page payload.
- Home brief recommendations carry `confidence: {score, band, caution}`.
- Ask tools `read_restaurant_memory`, `read_platform_intelligence`.
- `POST /mobile/api/account/update-profile` and `POST /admin/api/brand/<id>`
  accept `category` (taxonomy in `intelligence/categories.py`).
- `POST /api/generate-content` and the mobile twin return `tags`
  (`menu_item_id`, `menu_item_name`, `occasion`, `post_kind`, `label`).
- `POST /api/marketing/posts/<id>/tags` (web) / `/mobile/api/marketing/posts/<id>/tags`
  — correct a post's tags.
- `GET /api/marketing/attribution` rows now carry `menu_item_name`, `occasion`,
  `post_kind`, `item_lift_pct`, `reviews_mentioning`, `guest_list_delta`,
  `engagement_rate`; the payload adds `weakest`, `by_kind`, `by_occasion`, `by_dish`.

**Recommendation trust (Reviews, Food Cost, Marketing, Intel).** Every model-written recommendation carries a rec_ledger key and is answered through `POST recs/event` `{key, event: completed|dismissed|accepted, surface, module, kind: not_for_us}`. `GET /api/review-insight` and `/mobile/api/reviews/insight` add `recs: [{key, text, kind}]` and, per diagnosis, `rec_key`, `answered`, `as_of`, `stale_note`. `GET /api/inv-insight` (HTML with controls, plus `recs`) and `/mobile/api/food-cost/analytics` (`insight_rec_keys`, aligned with `insight_recommendations`); `food-cost/cfo`'s `diagnosis` carries `rec_key` / `answered` / `stale_note`. `GET /api/mkt-insight` and `/mobile/api/marketing/insight` likewise (one stored read shared by both). `GET /api/intel/recs` → `{recs: [{key, text, cites: [{ref, competitor, rating, time, text}]}], withheld_recommendations, unverified, nothing_to_act_on}`; `/mobile/api/intel` carries the same as `recommendation_items`. Win-back: `GET guest-winback` → `{available, draft: {id, segment, segment_label, segment_size, message, rec_key, return, max_chars, sms_window}}`, `POST guest-winback/<id>/send` `{message?}` (through the campaign gates: consent, quiet hours, three-day cap, length), `POST guest-winback/<id>/dismiss` — web and mobile twins. `food-cost/auto-order` GET adds `count_freshness` (an automatic order is held on a count older than `ordering.COUNT_FRESH_DAYS`). Marketing attribution posts carry `verdict` (lifted / dropped / no_clear_change) and `noise_band_pct`.
