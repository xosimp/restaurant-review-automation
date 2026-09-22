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

### Reviews (`/mobile/api/reviews/*`, `/mobile/api/review-stats`; web routes are flat — `/api/reviews/page`, `/api/review-stats`, `/api/topic-heatmap`, `/api/sentiment-trend`, `/api/response-performance`, `/api/regenerate-draft/<id>`, `/api/save-draft/<id>`, and the actions at root: `/approve/<id>`, `/skip/<id>`, `/undo/<id>`, `/retract/<id>`)
List/detail/approve/skip/regenerate a draft response, retry a failed Google post (`/reviews/<id>/retry-post`, both surfaces), bulk approve-all, sentiment trend, topic heatmap, response-performance, review-request send, review stats.

### Food Cost (`/mobile/api/food-cost/*`, `/api/food-cost*`)
Inventory CSV upload, custom items, purchase orders, waste/drift insight.

### Marketing (`/mobile/api/marketing/*`; web routes are mostly flat — `/api/mkt-stats`, `/api/mkt-performance`, `/api/mkt-insight`, `/api/content-calendar`, `/api/post-to-google`, `/api/brand-voice`, `/api/generate-content`, plus `/api/marketing/*` for drafts, schedule, links, media, attribution, diagnosis, tags)
Drafts (create/edit/list), media upload, scheduling, attribution per post, the text-club feature (`/api/guest-contacts*`, `/api/guest-campaign/draft|send`, `/api/guest-newsletter`, `/api/guest-qr`, `/api/guest-segments`, `/api/public/guest-optin/<token>`), templates.

### Intel (`/mobile/api/intel/*`, `/api/intel*`, `/api/ai-visibility*`)
Competitor snapshots, AI-visibility run trigger + results.

### Ask Cavnar (`/mobile/api/ask-cavnar/*`, `/api/ask-cavnar*`)
`opening` (deterministic briefing, no model call), `conversations` (list/create), `history` (a conversation's messages), the streaming chat endpoint itself (SSE), and the tool-confirmation route a `write`-kind tool's proposal card posts to.

### Connections (`/mobile/api/connections/*`)
Per-integration connect/disconnect/status: Google Business, Instagram, Toast, Square, Clover.

### Home (`/api/home/brief`, `/api/home/brief/group`, `/mobile/api/home`)
The deterministic Home-tab payload — see `home_brief.py`. `?fresh=1` forces recompute past the 60s per-restaurant cache. `/group` returns every location in an owner's `location_group` side by side.

### The owner's day (`strategy_routes.py`, web + mobile twins)
`actions` (everything still open, filtered by what the reader may see), `actions/snooze` `{key, days}`, `closeout` (GET tonight's + the questions, POST the four lines). `morning-brief` now returns the caller's own brief plus `can_edit`; `morning-brief/settings` also takes `hold_alerts` and `preshift_nudge_hour`.

### Strategic foundations (`strategy_routes.py`, each at `/api/…` and `/mobile/api/…`)
`issues` (GET list / POST create), `issues/<id>/resolve`, `issues/<id>/reassign`, `issues/routing` (GET/POST, principal-only), `goals` (GET/POST), `goals/<id>/end`, `outcomes` (GET/POST), `outcomes/<id>/abandon`, `metrics`, `food-cost/dish-scorecard`, `food-cost/reprice`, `food-cost/invoices` (GET list / POST multipart `file`), `food-cost/invoices/<id>`, `food-cost/invoices/<id>/apply`, `food-cost/recipe-drafts` (GET — drafts plus `missing` and `ingredients` counts), `food-cost/recipe-drafts/<id>/accept|reject`, `food-cost/recipes/draft` (POST `{menu}` — the pasted menu, one dish a line with an optional price; empty body drafts the POS dishes with no recipe; at most `RECIPE_DRAFT_LIMIT` model calls a request, `remaining` says how many are left), `food-cost/recipes/import` (POST CSV), `food-cost/recipes/scan` (POST multipart photo — iOS only now), `labor/demand?day=`, `labor/auto-draft` (GET/POST), `loss-signals` (LOSS_VIEW: owners, or a manager granted it), `morning-brief` (the caller's own brief) + `morning-brief/settings` (principal-only). Notifications: `GET notifications` (scoped to what the login may see; marks read per login), `GET notifications/unread-count`, `POST notifications/opened` `{type}`, `GET notifications/engagement` (types sent a lot and never opened — a suggestion, not a change), `POST account/send-test-push` (the caller's own devices only, delivered inline so the response is what APNs said). Team roles: `POST /api/account/team/invite` takes `role` (client | manager | member); `POST /api/account/team/<id>/role` `{role}` (owner-only) and its `/mobile/api` twin. Team access: `POST /api/account/team/<id>/access` and `/mobile/api/account/team/<id>/access` — `{permission, enabled}` and/or `{morning_brief}`, owner-only. Public: `GET/POST /i/<token>` (issue link; GET is side-effect free, POST `action=ack|resolve`). Staff portal: `GET /staff/api/preshift`.

### Webhooks (`/api/webhook*` outbound config, `/webhooks/*` inbound)
`GET /api/webhook` (current config or null), `POST /api/webhook` (save + return a secret), `POST /api/webhook/test` (fire a test delivery), `DELETE /api/webhook` (remove). Inbound: Stripe and Twilio webhook receivers, signature-verified, event-id de-duped (`stripe_events_seen`).

### Auth (`auth_routes.py`; `/mobile/api/login`, `/register`, `/apple-signin`, `/verify-2fa`, `/forgot-password`, `/reset-password`, `/logout`, `/me`, `/device-tokens`)
Login (web + mobile variants), mobile register, forgot/reset password, verify-2fa, Apple/Google sign-in, logout, mobile `/me`, APNs token registration.

### Staff portal (`/staff/*`, `staff_routes.py`)
`/staff/r/<token>/login` (PIN), `/staff/api/me`, today's schedule, availability, time off requests, `/staff/api/signup/claim`, `/staff/api/pin`, `/staff/api/preshift`.

### Other groups with web + mobile twins
`/api/tasks*` (today's checklist), `/api/team/inbox` + `/api/team/messages*`, `/api/changelog*`, `/api/switch-location` + `/api/group-locations`, `/api/home` + `/api/home/dismiss`, `/api/rpower/status` and the `/admin/rpower/*` bootstrap.

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
