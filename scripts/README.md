# scripts/

Nothing here runs in the request path. Who runs each one:

| Script | Runs from | What it does |
|---|---|---|
| `check_colors.py` | CI (`.github/workflows/ci.yml`), `tests/test_color_lint.py` | fails a theme-unsafe literal text colour in the templates |
| `check_silent_handlers.py` | CI, `tests/test_silent_handler_lint.py` | fails a bare `except:` or a swallowed database write |
| `check_timeouts.py` | `tests/test_resiliency.py` | fails an outbound HTTP call with no `timeout=` |
| `check_email_tokens.py` | `tests/test_email_audit_fixes.py` | ratchet on literal colours in email HTML (`emails.BRAND` is the source) |
| `check_pin_pepper.py` | by hand — `docs/ops/PIN_PEPPER_RUNBOOK.md` | reports whether `CAVNAR_PIN_PEPPER` is set and which version PINs carry |
| `inventory.py` | by hand, when a doc needs a number | tables, routes by blueprint, job claim keys, model call sites, test count — from the code |
| `build_contract_pdf.py` | by hand — `pricing.py` docstring | renders `docs/contracts/Cavnar-AI-Service-Agreement.pdf` from `pricing.TIERS` |
| `docusign_create_template.py` | one-off (built the live DocuSign template, Sep 7 2026) | creates the DocuSign template from that PDF |
| `seed_review_account.py` | `admin_routes.py` (`/admin/seed-review-account`), `docs/app-store-submission.md` | the App Store reviewer's demo account |
| `seed_simple_ejs_reviews.py` | one-off, kept for the record | the seed used for Simple EJ's review history |
| `loadtest_staff_signin.py` | by hand | load test for the staff PIN sign-in |
| `rollback_employee_auth.py` | by hand, only if the employee-auth rollout has to be reversed | the rollback |

`__init__.py` exists so `docusign_create_template.py` can import `build_contract_pdf`.
