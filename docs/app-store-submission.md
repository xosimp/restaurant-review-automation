# App Store submission — what to enter, and where

Everything App Store Connect asks for that cannot be set from the codebase.
Written against audit #8. Keep it in sync with `PrivacyInfo.xcprivacy` — the
two are checked against each other by Apple, and a disagreement between them
is its own rejection.

---

## 1. Review account (required — this is the one that blocks approval)

The app has no self-serve signup. `/mobile/api/register` returns 403 unless
`ALLOW_PUBLIC_SIGNUP` is set, and Apple and Google sign-in both refuse an
unknown identity. **A reviewer with no account cannot get past the login
screen**, which is an automatic Guideline 2.1 rejection.

Create the account **against production**. The database lives on the Railway
volume, so this has to run inside that container — running it on a laptop
either fails (the path does not exist) or quietly seeds a local file Apple
will never see.

Easiest, from any browser you are signed into the admin panel with:

```bash
curl -s -X POST https://dashboard.cavnar.ai/admin/seed-review-account \
  -H "Cookie: session=YOUR_ADMIN_SESSION_COOKIE"
```

Or from a shell on the container:

```bash
railway ssh
cd /app && python3 scripts/seed_review_account.py
```

Either way it prints a username and password once. Put them in **App Store Connect →
your app → App Review Information → Sign-In Required**, with "Sign-in
required" ticked.

It also seeds demo content so no screen opens empty: ten reviews spread
across every response state, three weeks of shifts landing slightly over the
labor target so the savings figure has something to show, and a fourteen-item
inventory with two items critically low and three past the waste tolerance.
Total Value Delivered on Home comes out around $4,000. All of it is invented
for a restaurant that does not exist, and the account is flagged `is_demo`.

The script sets two-factor **off** deliberately — a reviewer cannot receive a
code — and marks the account `internal`, so it is fully entitled but never
counted as a client for revenue, onboarding emails or the monthly summary.

Verify it works before you submit, from a machine that is not yours:

```bash
curl -s -X POST https://dashboard.cavnar.ai/mobile/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"appreview","password":"THE-PASSWORD"}'
```

A pass looks like `"ok": true` and a token. Anything else, fix it before
submitting rather than after.

---

## 2. Review notes

Paste into **App Review Information → Notes**. The payment paragraph is the
one that matters: without it a reviewer sees the word Stripe and reaches for
Guideline 3.1.1.

> Cavnar AI is a business-to-business service for independent restaurants.
> Accounts are created by us for a restaurant after a service agreement is
> signed — there is no consumer signup, and nothing is sold inside the app.
>
> PAYMENTS: The app does not offer any purchase. There is no subscribe,
> upgrade or checkout flow. The Billing screen displays an existing business
> subscription and offers one link to Stripe's billing portal so an owner can
> update the payment method on the account their business already holds. No
> digital content or feature is sold to the user in the app.
>
> ACCOUNT DELETION: Account > Close account submits a deletion request in the
> app, which is recorded on our server and sent to us. Because service runs
> under a signed agreement with a 30-day notice period, the account is wound
> down by us rather than deleted instantly; the request is initiated entirely
> in the app and the screen explains the timeline.
>
> SIGN-IN: Please use the credentials above. "Continue with Apple" and
> "Continue with Google" are for existing account holders who linked those
> identities, and will report that no account exists.
>
> PERMISSIONS: The app requests notifications after sign-in, when the main
> screen loads, to deliver alerts about the restaurant's reviews, labor cost
> and food cost. Face ID is optional and only re-locks the app on return.
>
> DATA: The business data shown is the restaurant's own operational data —
> Google reviews, staff hours, ingredient costs. The review account is
> pre-populated so every screen has content.

---

## 3. Privacy nutrition label

Must match `ios/CavnarAI/CavnarAI/PrivacyInfo.xcprivacy` exactly. Set under
**App Privacy → Data Collection**.

Answer **"Yes, we collect data from this app"**, then for every item below:
- Linked to the user: **Yes**
- Used for tracking: **No**
- Purpose: **App Functionality**

| App Store Connect item | Under | What it is in the app |
|---|---|---|
| Email Address | Contact Info | Account holder's sign-in and alert address |
| Name | Contact Info | `owner_name`, and the sign-off on review replies |
| Phone Number | Contact Info | Owner's SMS alerts, plus guest text-club numbers a restaurant adds under its own consent flow |
| Other Financial Info | Financial Info | The restaurant's sales, labor cost and food cost figures. **Not** payment details — cards live only with Stripe and are never entered in the app |
| Device ID | Identifiers | A UUID the app generates for itself and keeps in the Keychain, to recognise a returning device at sign-in and address push. Not the IDFA or IDFV |
| Customer Support | User Content | The message a user writes in Report a Bug |
| Other Diagnostic Data | Diagnostics | App version, iOS version, device model and current screen, attached to a bug report |

**Do not tick** Purchases, Location, Contacts, Browsing History, Search
History, Health, Sensitive Info, or Advertising Data. None are collected.

Tracking: answer **No** to "Do you or your third-party partners use data for
tracking purposes?" The app has no ads, analytics or attribution SDK.

---

## 4. The version page (1.0 Prepare for Submission)

Five fields on this page are required and three are not. Everything here is a
draft to read before pasting — it is your marketing copy, not mine.

### Required

**Screenshots.** At least one, up to ten, at exactly the pixel size the drop
zone names. Sign into the `appreview` account (section 1) and shoot Home,
Reviews, Labor, Food Cost and Marketing — it is seeded precisely so these have
content. From a simulator at the size App Store Connect asks for, Cmd-S saves a
correctly-sized PNG to the desktop.

**Subtitle** (30 chars, on the App Information page):

```
Restaurant intelligence
```

**Description** (4,000 chars):

```
Cavnar AI gives an independent restaurant one place to see what its reviews, its labor cost and its food cost are actually doing — and tells you the moment one of them needs you.

REVIEWS
Every Google review lands in the app as it arrives. Cavnar AI drafts a reply in your voice for each one; you read it, change what you want, and post. A bad review reaches your phone the day it lands instead of sitting for a week.

LABOR
Import your sales history and Cavnar AI builds next week's schedule against it — the right people on the right shifts for the volume you actually do. You see your labor percentage before you publish the schedule, not after payroll.

FOOD COST
Track what you buy against what you sell. Cavnar AI surfaces the items running short, the items going in the bin, and what each one costs you a week.

MARKETING
Schedule posts to your channels, drafted from what is actually happening in your restaurant this week.

INTELLIGENCE
See how you stand against the restaurants around you, and how you show up when someone asks an AI assistant where to eat nearby.

ALERTS
A new one-star review, labor running long on a Saturday, an item about to run out — it goes to your phone as it happens, not in a weekly digest you forget to open.

Cavnar AI is a service for restaurants we work with directly. Accounts are set up by us after a service agreement is signed. There is no signup inside the app, and nothing is sold inside the app.

Questions: will@cavnar.ai
```

**Keywords** (100 chars, commas, no spaces):

```
reviews,labor cost,scheduling,food cost,inventory,POS,bar,cafe,manager,margins,operations
```

Leave out anything already in the app name or subtitle — Apple indexes those
separately and a repeat wastes the character.

**Support URL**:

```
https://cavnar.ai
```

### Optional

**Promotional text** (170 chars). Worth filling because it is the one field you
can change without shipping a build:

```
Reviews, labor cost and food cost in one place — with an alert the moment one of them needs you.
```

**Marketing URL**: `https://cavnar.ai`.

**Copyright**: `2026 Cavnar AI`, using whatever the legal entity on the
Apple Developer account is.

### Skip

**App previews** — video, not required for a first release.

**Routing App Coverage File** — turn-by-turn navigation apps only.

### Not on this page, still needed

- **A build.** Nothing else unlocks Add for Review. Xcode > Product > Archive >
  Distribute App > App Store Connect. It then takes ten to thirty minutes to
  finish processing before it can be picked on the version page.
- **Age rating** and **Pricing and Availability** — both in the left sidebar.
- Everything in sections 1 to 3 above.

---

## 5. Everything else on the form

| Field | Answer |
|---|---|
| Export compliance | No — `ITSAppUsesNonExemptEncryption` is `false` in the plist; the app uses only standard HTTPS |
| Content rights | No third-party content |
| Age rating | 4+ (no objectionable content) |
| Category | Business (secondary: Productivity) |
| Privacy policy URL | `https://cavnar.ai/privacy` |
| Support URL | `https://cavnar.ai` |
| Account deletion | Supported in-app — Account > Close account |

---

## 6. Checked in code, no action needed

Recorded so nobody re-litigates them at submission time:

- **Sign in with Apple** is implemented natively with `ASAuthorizationController`
  and offered alongside Google, satisfying Guideline 4.8. The entitlement is
  in `project.yml`.
- **Release compiles out the API base-URL override entirely** and hardcodes
  `https://dashboard.cavnar.ai`, so a shipped build cannot be repointed and
  certificate pinning cannot be turned off.
- **Debug code is `#if DEBUG`**, including a Release stub for
  `DebugFrameWatchdog` so the archive compiles. Release builds clean.
- **No secrets in the binary.**
- **`aps-environment`** follows the build configuration rather than being
  hardcoded, so a TestFlight build registers against production APNs.
- **Permissions are push and Face ID only.** Push is requested after sign-in
  when Home has loaded, not at launch. Face ID has a specific usage string.
- **`NSAllowsLocalNetworking`** is present for the Debug `--lan` device-testing
  path. Apple documents it as usable without justification during App Review —
  it is not `NSAllowsArbitraryLoads` and should not be confused with it.
- **Dynamic Type** works: every custom font goes through helpers that pass
  `relativeTo:`.
- **Offline** writes queue to a durable outbox and drain on reconnect.
- Every outbound link in the app returns 200.
