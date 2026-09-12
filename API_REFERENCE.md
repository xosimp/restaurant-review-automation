# API Reference — Cavnar AI

This documents **patterns and resource groups**, not every individual route — there are roughly 460 across the app, and the specific handler is always one `grep` away once you know which file and prefix to look in. That lookup is intentionally cheap; re-deriving the whole route table from scratch every session is not. Use this file to answer "which file, which blueprint, which auth" before opening anything.

## Blueprints (registered in `hosted_dashboard.py`)

| Blueprint | File | URL prefix | Auth | Routes |
|---|---|---|---|---|
| `client_bp` | `client_api.py` | `/api/*` (+ page routes) | web session cookie (`login_required`) | ~162 |
| `mobile_bp` | `mobile_api.py` | `/mobile/api/*` | Bearer token (`mobile_login_required`) | ~176 |
| `admin_bp` | `admin_routes.py` | `/admin/*` | `admin_required`, Will only | ~93 |
| `auth_bp` | `auth_routes.py` | `/auth/*`, `/login`, `/register`, ... | none → issues session | ~21 |
| `webhook_bp` | `webhook_routes.py` | `/webhooks/*` (inbound), `/api/webhook*` (outbound config) | inbound: signature-verified; outbound config: session | 6 |
| `social_bp` | `social_routes.py` | OAuth callbacks for social connections | mixed | 9 |
| `toast_bp` / `square_bp` / `clover_bp` | `toast_routes.py` / `square_routes.py` / `clover_routes.py` | POS OAuth + sync | mixed | 7–8 each |
| `status_bp` | `status_routes.py` | `/status`, public status page | public | 6 |
| `audit_bp` | `audit_app.py` | `/admin/audits/*` | admin | — |

## The `_m()` delegation pattern

Most of `client_bp`'s `/api/*` handlers are one line:
```python
@client_bp.route("/api/ask-cavnar/opening")
@login_required
def ask_cavnar_opening(current_user):
    return _m("mobile_ask_opening")(current_user)
```
`_m(name)` looks up `mobile_api.<name>` and unwraps its `@mobile_login_required` decorator (`__wrapped__`), so the web route calls the exact same function body the iOS route calls — just with the already-resolved `current_user` from the web session instead of a decoded bearer token. **When a route's real logic isn't there, look in `mobile_api.py` first.** A genuinely web-only route (no iOS equivalent — e.g. CSV export, a desktop-only settings page) is one of the minority that doesn't delegate.

## Resource groups

### Account (`/api/account*`, `/mobile/api/account/*` — largest single group, ~36 mobile + ~26 client routes)
Profile, security (2FA setup/verify, backup codes, trusted devices, sessions list + revoke-others), team (invite/list/revoke — owner-role only), sign-in history, notification/alert-channel preferences, data export, billing status, marketing-email opt-out, timezone, connections status.

### Labor (`/api/labor*`, `/mobile/api/labor/*` — ~27 mobile + ~17 client)
Shift CSV upload, labor % + ribbon data, schedule generation/history/sharing, staff availability, staff contacts (for schedule delivery), Operational Score (`team`), scheduling profiles (demand levels), overtime/overstaffed-day detail.

### Reviews (`/mobile/api/reviews/*`, `/api/reviews*`)
List/detail/approve/skip/regenerate a draft response, bulk approve-all, sentiment trend, topic heatmap, response-performance, review-request send, review stats.

### Food Cost (`/mobile/api/food-cost/*`, `/api/food-cost*`)
Inventory CSV upload, custom items, purchase orders, waste/drift insight.

### Marketing (`/mobile/api/marketing/*`, `/api/marketing*`)
Drafts (create/edit/list), media upload, scheduling, guest-contacts / guest-campaigns / guest-segments (the text-club feature), templates.

### Intel (`/mobile/api/intel/*`, `/api/intel*`, `/api/ai-visibility*`)
Competitor snapshots, AI-visibility run trigger + results.

### Ask Cavnar (`/mobile/api/ask-cavnar/*`, `/api/ask-cavnar*`)
`opening` (deterministic briefing, no model call), `conversations` (list/create), `history` (a conversation's messages), the streaming chat endpoint itself (SSE), and the tool-confirmation route a `write`-kind tool's proposal card posts to.

### Connections (`/mobile/api/connections/*`)
Per-integration connect/disconnect/status: Google Business, Instagram, Toast, Square, Clover.

### Home (`/api/home/brief`, `/api/home/brief/group`, `/mobile/api/home`)
The deterministic Home-tab payload — see `home_brief.py`. `?fresh=1` forces recompute past the 60s per-restaurant cache. `/group` returns every location in an owner's `location_group` side by side.

### Webhooks (`/api/webhook*` outbound config, `/webhooks/*` inbound)
`GET /api/webhook` (current config or null), `POST /api/webhook` (save + return a secret), `POST /api/webhook/test` (fire a test delivery), `DELETE /api/webhook` (remove). Inbound: Stripe and Twilio webhook receivers, signature-verified, event-id de-duped (`stripe_events_seen`).

### Auth (`/auth/*`, `/mobile/api/login` etc.)
Login (web + mobile variants), register, forgot/reset password, verify-2fa, Apple/Google sign-in, logout, session `/me`.

### Admin (`/admin/*`, Will-only, 93 routes)
Client health rollup (owner → brand → location), job run history, manual contract send, sales-audit tool (`/admin/audits/*`), changelog authoring, status-incident management.

## Conventions

- **Success**: `jsonify(ok=True, ...)`, 200. **Failure**: `jsonify(ok=False, error=...)`, appropriate 4xx.
- **POST bodies**: `request.get_json() or {}` — never assume the body parses.
- **Side-effect emails/SMS**: wrapped in best-effort `try/except`, routed to `ops.capture()` on failure rather than failing the request the user is waiting on, and logged via `log_email()` / `email_log`.
- **CSRF**: every state-changing web POST needs the CSRF token (`_csrf_fetch.html`'s helper attaches it automatically to `fetch()` calls in the dashboard's own JS).
- **Rate limiting**: Ask Cavnar is capped at 5/min per restaurant (`ai_utils.ai_rate_limited`); AI budget is also enforced globally and per-restaurant (`ai_budget_exceeded`) before any model call fires.
- **Streaming**: Ask Cavnar's chat endpoint is Server-Sent Events, with named progress labels per tool call (e.g. `"read_alerts": "Checking what needs you"`) sent before the tool result, so the client can show what's happening rather than a bare spinner.

## Finding a specific route fast

```bash
grep -n "'/mobile/api/labor" mobile_api.py      # mobile handler
grep -n '"/api/labor' client_api.py             # web equivalent, if any (often `_m(...)`)
grep -n "def mobile_labor_" mobile_api.py        # by handler name pattern
```
