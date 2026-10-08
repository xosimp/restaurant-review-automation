# A nonce-based Content Security Policy (#100)

Plan, 9/29/26 (admin-console fix round, finding #100). Nothing here is built.
Counts are from the templates at `fixround-0929` (48a2091d); the admin
console is being rebuilt by the UI wave, so re-count before starting.

## Where it stands

`security_headers.CSP` sends, on every response:

```
script-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://static.cloudflareinsights.com
style-src  'self' 'unsafe-inline' https://fonts.googleapis.com https://fonts.gstatic.com
```

`'unsafe-inline'` in `script-src` means an HTML-injection bug anywhere — one
tenant value rendered without escaping — becomes script execution in an
owner's or the operator's session. The escaping discipline is real (Jinja
autoescape; `jsq`; `fcEsc` / `fcNum` on the legacy admin pages with a scanner
test, `tests/test_fix_b1_admin_scoping.py`), but the policy is the backstop
that makes one missed escape harmless, and it cannot be one while the pages
run inline handlers.

## Inventory

Inline event-handler attributes (`onclick=`, `onchange=`, `oninput=`,
`onsubmit=`, `onkeydown=`, … — counted in the markup AND inside the JS strings
that build markup: `grep -oE "on(click|change|input|submit|keydown|keyup|blur|focus|load|error|…)=" <file> | wc -l`),
and inline `<script>` blocks (`<script>` without `src`):

| Template | Handlers | Inline scripts | Note |
|---|---|---|---|
| `dashboard.html` | ~510 | 28 | the whole client app; ES5 only (`tests/test_frontend_rules.py`); rows built as strings with `onclick` |
| `admin.html` | ~130 | 1 | the console builds most rows with `onclick` in template strings; being rebuilt (UI wave) |
| `client_settings.html` | ~30 | 2 | legacy admin page |
| `audit_tool.html` | ~30 | 1 | the sales-audit tool |
| `_review_card.html` | ~23 | — | a partial inside `dashboard.html` |
| `staff_login.html` | ~14 | 1 | public (staff PIN) |
| `client_data.html` | ~12 | 1 | legacy admin page |
| `audit_list.html`, `audit_cheatsheet.html` | 6, 5 | 1, 1 | |
| `_fc_receive_js.html`, `login.html`, `audit_report.html` | 3, 2, 1 | 1, 1, — | |
| `staff_portal.html` (rebuilt 10/7/26) | 0 | 2 (`_csrf_fetch.html` + the page) | events are delegated listeners, no inline handlers: a nonce is enough |
| `_csrf_fetch.html`, `admin_two_factor.html`, `billing_paused.html`, `forgot_password.html`, `reset_password.html`, `two_fa.html`, `guest_optin.html`, `status.html` | 0 | 1–2 each | inline scripts only: a nonce is enough |

No template uses a `javascript:` URL. There are about 2,200 inline `style=`
attributes — out of scope here (`style-src 'unsafe-inline'` stays; style
injection is the smaller risk and a nonce does not cover style attributes).

Two outside scripts matter: Google Fonts is a stylesheet, not a script, and
`static.cloudflareinsights.com` is Cloudflare's analytics beacon, which
Cloudflare injects at the edge — it cannot carry our nonce, so it is either
allowed by host (and `'strict-dynamic'` is not used) or turned off.

## The trap to avoid

A policy that lists a nonce makes browsers IGNORE `'unsafe-inline'`. The day
the header gains `'nonce-…'`, every inline handler still in a template stops
working. So the header changes LAST, after every handler has moved, and the
move is proven first with a report-only policy.

## Order of work

1. **Nonces on inline scripts, header unchanged.** A per-request nonce
   (`secrets.token_urlsafe(16)` on `flask.g`, a Jinja global) on every inline
   `<script>` — 48 blocks, no behaviour change.
2. **Report-only.** Add `Content-Security-Policy-Report-Only: script-src 'self'
   'nonce-<n>' https://static.cloudflareinsights.com; report-uri /csp-report`
   beside the enforced header, with a small, rate-limited report route that
   counts violations by page and directive (a table with a cap, like
   `admin_audit_refused`). This is the live inventory; it must reach zero.
3. **Move the handlers, smallest pages first**: the public and standalone
   pages (`login`, `staff_login`, `staff_portal`, `audit_*`), then the legacy
   admin pages (`client_data`, `client_settings`), then `_review_card` and
   `_fc_receive_js`, then `admin.html` (in the same change as a UI-wave
   rebuild of each area, not as a separate pass), then `dashboard.html` panel
   by panel. The pattern is the one Home's Ask already uses: `data-action` /
   `data-*` attributes on the element and ONE delegated listener per page
   (`document.addEventListener('click', …)` dispatching on `data-action`),
   ES5 in `dashboard.html`. Markup built as strings keeps its escaping and
   drops its `onclick`.
4. **A ratchet test** in the style of `tests/test_frontend_rules.py`: fail on
   a new `on<event>=` attribute or an inline `<script>` without the nonce, and
   hold each template's count at its current number until it is zero.
5. **Enforce**: move the nonce policy from report-only to the enforced header
   (`'unsafe-inline'` may stay listed as the CSP1 fallback — ignored where
   nonces are understood); keep the report route for regressions.
6. Later, separately: `style-src` (hashes or moving the inline styles to
   classes), and dropping `connect-src https://api.anthropic.com`, which no
   browser code needs (the model is called server-side).
