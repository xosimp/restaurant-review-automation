# Cavnar AI — design system

The look of the product, as it is actually built. Every value here was read
out of shipping code, and each section says where it lives so the code stays
the source of truth and this file stays checkable.

**Use this as the source of truth when building UI. Do not introduce a new
pattern unless no existing one fits — and if none does, add the new pattern
here in the same commit.**

Two clients, one product:

| | Web | iOS |
|---|---|---|
| Tokens | CSS variables on `:root` / `[data-theme="dark"]` (`templates/dashboard.html`) | Named colour sets in `Assets.xcassets/Colors`, reached through `Color+Cavnar.swift` |
| Themes | light + dark | dark only, by design (a dim dining room) |
| Enforced by | `scripts/check_colors.py`, `tests/test_button_system.py`, `tests/test_frontend_rules.py` | code review + `CavnarRadius` / style structs |

---

## 1. Colour tokens

Semantic, never raw hex at a call site. The web names and the iOS names are
the same palette; `Color+Cavnar.swift` reads the `Assets.xcassets` colorsets,
whose light values match the web and whose dark values are deliberately
deeper (a true-black chrome, `#0c0c0c` paper) — the iOS column notes each
dark value that differs.

| Token | Web light | Web dark | iOS | Use |
|---|---|---|---|---|
| `--ink` / `.cavnarInk` | `#0e0c0a` | `#f0ebe0` | Ink | Primary text |
| `--ink2` / `.cavnarInk2` | `#3a3530` | `#c4bdb4` | Ink2 (`#e0d6c6` dark) | Secondary text, table cells |
| `--ink3` / `.cavnarInk3` | `#7a736a` | `#cdbfa9` | Ink3 | Labels, captions, "—" |
| `--paper` / `.cavnarPaper` | `#f7f4ef` | `#1a1714` | Paper (`#0c0c0c` dark) | Page ground, field fill |
| `--paper2` / `.cavnarPaper2` | `#edeae3` | `#231f1b` | Paper2 (`#121212` dark) | Card ground (iOS at 60%) |
| `--paper3` / `.cavnarPaper3` | `#e0dbd0` | `#4a4038` | Paper3 (`#262626` dark) | Hairlines, dividers |
| `--surface` | `#ffffff` | `#201d19` | Surface (`#121212` dark) | Raised card surface (web) |
| `--ember` / `.cavnarEmber` | **`#c84b2f`** | `#e06444` | Ember (`#D4583A` dark) | The brand accent — see §9 |
| `--ember2` / `.cavnarEmber2` | `#e8956a` | `#e8956a` | Ember2 | Chart lines, kickers, soft accent |
| `--green` / `.cavnarGreen` | `#2d6a4f` | `#4ead7a` | Green | Good / improved / on target |
| `--red` / `.cavnarRed` | `#c0392b` | `#e05555` | Red (`#e3333f` dark) | Bad / critical / destructive |
| `--amber` / `.cavnarAmber` | `#b7791f` | `#d4a030` | Amber | Warning, "watch", partial data |
| `--blue` / `.cavnarBlue` | `#1a56cc` | `#6aabff` | Blue | Informational only (rare) |

Each status colour has a `-bg` pair for tinted chips (`--green-bg`,
`--red-bg`, `--amber-bg`, `--blue-bg`; `.cavnarGreenBg` etc.).

Beyond the palette, `dashboard.html` defines three token families a
component may reach for and must not redefine: the surface layer
(`--sf-ai`, `--sf-ai-line`, `--sf-recess`, `--surface-glass`), elevation
(`--elev-card`, `--elev-float`, `--elev-hero`, `--glow-ember`) and the Home
semantics (`--hb-good`, `--hb-warn`, `--hb-bad`, `--hb-amber`, `--hb-tint`,
`--hb-line`, `--hb-glow`, …). Buttons have their own namespace in
`static/css/cavnar-buttons.css` (`--cb-accent`, `--cb-ink`, `--cb-surface`,
`--cb-tint`, `--cb-radius*`, `--cb-shadow*`, 26 in all). A restaurant's
`brand_color` rewrites `--ember`/`--ember2` at the top of the page
(`dashboard.html`, the `:root` override on line 18), so "ember" on a
white-labelled account is that restaurant's colour.

**Dark-mode depth (web).** The canvas is flat `--bg-base: #141110`; depth
comes from a three-step luminance staircase — base < header/tabs < card
(`--surface`) — plus `--elev-1` (resting) and `--elev-2` (hover) shadows and
the `--line-hi` hairline. No gradients, glow, grain or vignette on the
canvas: see the long comment above `:root` in `dashboard.html` for why.

**Rules**
- Colour comes from a token. `scripts/check_colors.py` fails a literal dark
  or light `color:#hex` unless the same rule pins its own background.
- Platform brand colours (Instagram, Facebook, Google) are the only
  non-ember fills allowed, and only on their own buttons.
- iOS: honour Increase Contrast for informational secondary text with
  `Color.cavnarInk3(contrast)`; plain `.cavnarInk3` is for decorative chrome.
- `.cavnarChrome` (true black) is nav/tab-bar chrome only, never a content surface.

---

## 2. Typography

Three faces, one job each. Clash Display and Apfel Grotezk are self-hosted on
web (`static/fonts/cavnar-fonts.css`; Apfel ships only 400 and 700, and iOS
snaps weights ≥550 to Fett); Space Grotesk is loaded from Google Fonts
(`dashboard.html` `<link>`), together with **Bricolage Grotesque**, a fourth
face used only for the tab badges and the `.stat-n` figures — a legacy of
the pre-rebuild stat cards that the "every number is Space Grotesk" rule
has not yet reached. iOS bundles all three (`Font+Cavnar.swift`).

| Role | Face | Web | iOS |
|---|---|---|---|
| Headlines | **Clash Display** (Regular/Medium/Semibold/Bold) | `font-family:'Clash Display'` | `.cavnarHeadline(_ size:, weight:)` |
| Body & chrome | **Apfel Grotezk** | default body face | `.cavnarBody(_ size:, weight:)` |
| **Every number** | **Space Grotesk** | `.hb-num`, `font-family:'Space Grotesk'` | `.cavnarNumber(_ size:, weight:)` |

**Numbers are always Space Grotesk** — including a numeric run inside mixed
text. Web wraps them in `<span class="hb-num">` (Home's `num()` helper does
this automatically); iOS uses `HomeMixedText.make(...)` or
`.font(.cavnarNumber(...))`. Use `font-variant-numeric: tabular-nums` /
tabular figures wherever digits line up in a column.

### Dates and times (one format, everywhere)

Every date an owner reads is **M/D/YY with no leading zeros: `9/21/26`**. On
web, iOS, email, push, the activity feed, and in any sentence a job or a
model writes. A range is `9/14/26 – 9/20/26`; a time is `6:45pm` (no
seconds); a day with a time is `9/21/26 · 6:45pm`. Never an ISO
`2026-09-21`, never `Sep 21`, never a weekday alone where the date matters.
An ISO date in owner-facing text is a bug, not a style choice.

| Surface | Helper |
|---|---|
| Python — API text, emails, briefs, activity, alerts | `time_utils.mdy(value)` (date, datetime or ISO string); `time_utils.mdy_range(a, b)` |
| Jinja | `{{ value\|format_date }}` |
| Web JS | `mdy(x)` — global, exported from the Home closure |
| iOS | `CavnarDate.mdy` / `mdyRange` / `mdyTime` (`DesignSystem/Formatting.swift`) — one `M/d/yy` formatter, shared |

`tests/test_date_format.py` pins the helper and the feed text.

### Scale (the sizes actually in use)

| Step | Size | Where |
|---|---|---|
| Page title | 25–40px | `.hb-h1` (`clamp(30px,3.2vw,40px)`), hero figures, `AccountHero` |
| Section heading | 22–23.5px Clash | `.fc2-sec .hd h2`, sheet titles |
| Body | 14–16px | `.ac-row`, list rows, `.hb-row .t`, `.hb-tl .t`, `.cavnarBody(15)` |
| Secondary | 13–14.5px | `.ac-note`, `.hb-empty`, `.hb-focus .ev`, row subtitles |
| Caption | 11.5–13px | `.hb-tbl .iss`, `.hb-sg .cap`, metadata |
| **Kicker** | 10–13.5px, weight 700, `letter-spacing:.12–.16em`, UPPERCASE | `.hb-kicker` (ember), `.hb-tbl th` (ink3), `AccountKicker` |

Keep sizes uniform across modules. The Account scale (15 / 16 / 22) is the
reference; never inflate one module's text on its own. Headings get
`text-wrap: balance` where supported; body copy uses `line-height:1.5–1.65`.

---

## 3. Spacing & radius

Web spacing is a 4px-based ladder; these are the values in use:

| Step | px | Use |
|---|---|---|
| Hairline gap | 2–6 | Inside a row, label → value |
| Tight | 8–10 | Chip padding, icon gaps, `.cbtn-row` gap |
| Row | 11–14 | `.ac-row` / `.hb-row` vertical padding |
| Card | 16–22 | `.ac-card` padding (20px 22px), iOS `cavnarCard()` (16) |
| Block | 20–26 | Sheet padding, `VStack(spacing: 22)` |
| Section | 34 | Gap between sections (`.hb-act`, `.fc2-sec`) |

Radius — iOS `CavnarRadius`, web equivalents:

| Token | iOS | Web |
|---|---|---|
| control | 12 | 8–9px (`--r`, `--cb-radius`) |
| card | 16 | 12–14px (`.ac-card` 14) |
| sheet | 24 | modal corners |
| pill | 999 | `border-radius:999px` |

Nothing renders with square corners.

---

## 4. Cards & rows

**Web — `.ac-card`** (`dashboard.html`): `--ac-card` ground, 1px `--ac-line`
border, radius 14, padding 20/22, `0 2px 10px rgba(14,12,10,.05)`. Header is
`.ac-card-h` (h3 kicker + one-line description); body rows are `.ac-row`
(flex, space-between, 11px vertical, hairline top border); helper text is
`.ac-note`; status line is `.ac-status`.

**Web — Home language (`hb-*`)**, reused by Reviews (`rv2-`), Food Cost
(`fc2-`), Intel (`in2-`) and Ask (`ask-`): `.hb-kicker` label, `.hb-sig`
three-column signal grid, `.hb-act` two-column action grid, `.hb-row` list
rows with a status dot (`.critical` red, `.important` amber, `.watch` grey,
`.good` green), `.hb-chip` pills, `.hb-empty` for nothing-to-show.

**iOS — `.cavnarCard()`**: padding 16, `Paper2` at 60%, 1px `Paper3` at 50%,
radius `CavnarRadius.card`. Account sheets use `AccountSheetKit`:
`AccountHero` → `AccountSection(kicker:)` → `AccountKVRow(label:) { trailing }`,
with `AccountPill`, `AccountChip`, `AccountStatTile`, `AccountValue`.

**Not everything is a card.** Border, fill, radius and shadow each say
"separate object". Spend them on the one thing that needs lifting; a page of
identical cards flattens the hierarchy.

**Home below the hero (`hb-card` family, `dashboard.html`).** One surface,
built from the same `--surface` / `--elev-1` / `--line-hi` staircase as
every other card on the site, and a small set of shapes that sit on it —
each chosen so no two adjacent sections share a silhouette:

| Shape | Class | Used for |
|---|---|---|
| Card | `.hb-card` (+ `.lift` hover, `.rail` / `.rail.good` / `.rail.ember` accent edge) | Needs attention, Ask, Open issues, Goals, results, worth, still open, close-out |
| Hero card | `.hb-card.hero.hb-focus` — ember radial in the corner, Clash `.lead`, `.why`, `.ev` chips, `.ft` footer with `.money` | **Today's focus** and **What connects** only. Two per page, never more. |
| Module join | `.hb-join` — pills joined by a lit ember line | The cross-module mark on a hero card |
| Numbered card | `.hb-card.hb-rec` in `.hb-recs` (3-up) — `.t` the action (verb first), `.why` why now, `.meta` pills (`.usd` "$N/mo at stake" only when measured, then timeframe · impact · evidence strength), `.ev` evidence, ONE `.hb-conf.{high,medium,low}` line ("**Medium confidence** — what it rests on"), `.ig` "If ignored:", then the footer: one-tap action (reprice) / Track this / open module / Could also be… (`data-explain`, only when the source has an alternative) / Assign (inline `.hb-assign` select, only with assignees) / Done · Not for us, and the ✕ hide. iOS: `HomeRecommendations` rows, same order | Recommendations — every card answers what, why now, $, how sure, if ignored |
| Quieter line | `.hb-quiet` under the cards — "Quieter: Trim day — the last four went by unanswered." + a text button per kind ("Show trim day again" → `restore_kind`). iOS: the same line under the card | A kind the owner has let expire unanswered four times running |
| Receipt strip | `.hb-rcpt .it` — check + sentence + module | What Cavnar AI did — finished things must not look like a to-do |
| Timeline | `.hb-tl .it` with a tone dot on a rail | The morning brief, read once top to bottom |
| Stat tile | `.hb-stats .hb-stat` (+ `.good` / `.ember`) | Four *kinds* of number that must never be added together |
| Ledger pill | `.hb-ledger span` — the leading count on an ember disc (`>.hb-num:first-child`), a figure mid-sentence stays plain | Since you started: distinct work counted, never dollars |
| Ops tile | `.lb2-ops .lb2-op` (+ `.ember` ambient) — obsidian mark `.ic.ob-tile`, count in the number face `.v` (+ `.warn` / `.good` / `.ember`), body at 15px, `.cap` footer | Labor's Time off and Covers: what the owner feeds the read. Card tier, so they weigh the same as the tiles above them |
| Position tile | `.fc2-position .fc2-pos` — ember rail, one number in the number face, its basis under it | Food Cost's actual %, recipe coverage, unexplained waste: three kinds of number in one row, never stacked in a column |
| Working block | `.fc2-block` with `fc2BlockHead(icon, kicker, title, count, sub)` — obsidian tile, kicker, Clash title with an ember count chip | Food Cost's recipes, count sheet and suppliers: full-width cards, each the owner's own work surface |
| Obsidian tile | `.ob-tile` (+ `.sm`) — the web twin of iOS `GlowBadge`: obsidian gradient, lit edge ember→dark from the top-left, hairline lip, cream glyph, one ember seated on the right edge. A solid object, not a light source | Every mark that names a block or a module; the glyph inside is a 22px stroke SVG |
| Goal bar | `.hb-goal .bar i` (width from `data-w`) | Progress from baseline to target; no bar when the baseline is unreadable |
| Checklist | `.hb-chk` | Still open |
| Section header | `.hb-sh` — `.k` kicker (ember, or `.dim`) + Clash `h2 small` | Between the big moments; the hairline is the rhythm |

Entrances are `.hb-rise` with `--i` for a 70ms stagger, 420ms, no overshoot.
The list row `.hb-row` stays for what is genuinely a list. A Needs-attention
row that is not critical carries the same answers as a card — "Not today"
(text button) and the ✕ hide (`.hb-x`); a critical row (a guest waiting, a
broken sync) never does. iOS: the `…` menu on the deck's top card.

---

## 5. Buttons

**Web — one component, `.cbtn`** (`static/css/cavnar-buttons.css`).
`tests/test_button_system.py` fails any web button without it.

| Variant | Use |
|---|---|
| `cbtn-primary` | The one dominant action in a view (ember) |
| `cbtn` / `cbtn-secondary` | Default: quiet surface + hairline |
| `cbtn-text` (+ `cbtn-inline`) | Text-only ember action inside a row or sentence |
| `cbtn-soft` | Tonal ember chip, gentle emphasis |
| `cbtn-danger` (+ `cbtn-ghost`) | Destructive |
| `cbtn-success` | Confirm / approve |
| `cbtn-glass` | On a dark hero card, either theme |

Sizes `cbtn-sm` 30px · default 36px · `cbtn-lg` 42px. Modifiers: `cbtn-icon`,
`cbtn-round`, `cbtn-block`, `cbtn-close`, `cbtn-muted`, `cbtn-link`,
`cbtn-done`. Group with `.cbtn-row` (10px gap). Busy state: `cbtnBusy(btn,
label)` swaps in the orb, disables the button and returns a restore function.

**iOS**: `CavnarPrimaryButtonStyle(isDisabled:)` (ember fill + glow),
`CavnarSecondaryButtonStyle()`, `.buttonStyle(.plain)` for a row that is
really a link. Destructive uses `Button(role: .destructive)` inside a
`.confirmationDialog` for anything irreversible.

**Hierarchy rule:** exactly one primary per screen or sheet. Everything else
is secondary or text.

---

## 6. Charts

House style: ember/ember2 line or bar on a transparent ground, a faint grid
at most, the endpoint emphasised, labels in `--ink3`, numbers in Space
Grotesk. Charts must be genuinely striking — gradient fills, glow, motion,
meaningful colour — never flat and generic.

- **Web helpers** in `dashboard.html`: `glowLine(vals,w,h,col,opts)` (glow
  line with optional floor/ceil), `bars(items,w,h,col,opts)` (with `target`
  line and outside labels), `stacked(weeks,w,h)` (positive/negative split).
  Food Cost adds `fc2-wt-*` (waste trend), gauge and donut renderers.
- **iOS**: `CavnarCharts.swift` — `CavnarAnimatedCanvas`, `CavnarChartStage`,
  `CavnarChartHeader`; plus `RecoverableGaugeChart`, `WasteLedgerChart`,
  `FoodCostTrendChart`.

Every label must name a value the chart actually reaches; a missing
measurement is drawn as a gap, never as zero.

---

## 7. Forms

**Web**: `.ac-field` (label + control, 5px gap) with `.ac-input`,
`.ac-select`, `.ac-textarea` — 10/12 padding, radius 9, `--paper` fill,
`--ac-line` border, ember focus ring (`box-shadow:0 0 0 3px var(--hb-tint2)`).
On/off is `.ac-switch` (44×26 track, ember when checked, 18px thumb travel,
visible focus outline).

**iOS**: `AccountField` / `CavnarFloatingField` for text,
`CavnarDropdown` for choices. For a setting that hits the network, use a
tappable `AccountPill` or `AccountActionChip` row — **not** a native `Toggle`,
whose height breaks the kit's row rhythm. Device-local settings (Face ID) may
use a real `Toggle`.

Validation reads as a plain sentence under the control in `--red` /
`.cavnarRed`: what went wrong and how to fix it.

---

## 8. Tables

Web only (iOS uses rows/cards). `.hb-tbl` is the reference: uppercase 10px
kicker headers with a `--hb-line` underline, 12px cells, hairline row
borders, right-aligned numerics (`th.r` / `td.r`), `.nm` name cell with a
status dot, hover tint, and a clickable row when the row has a destination.
`.fc2-table` / `.fc2-mt` are the Food Cost variants (14px, same anatomy).

Wrap wide tables in an overflow container (`.fc2-table-scroll`) so the page
never scrolls sideways.

---

## 9. When to use the brand colour (`#c84b2f`)

The ember means **"this is the product speaking, or this is the one thing to
do"**. Spend it in one place per screen.

**Do**
- The single primary action (`cbtn-primary`, `CavnarPrimaryButtonStyle`).
- Section kickers (`.hb-kicker`) and the active tab or selected state.
- The AI's own marks: orb, seal, shimmer, the Ask accent.
- One emphasised data point (a chart endpoint, the recommended row).

**Don't**
- Use it for status. Good/bad/warning are green/red/amber — an ember number
  is emphasis, not a verdict.
- Fill more than one large surface per view, or tint a whole card.
- Use it as a background behind body text (contrast).

Dark mode uses `#e06444` (web) / `#D4583A` (iOS) so the ember doesn't glare;
`--ember2` `#e8956a` is the soft form for lines and kickers. The web's dark
primary button is the iOS value, `#d4583a` (`--cb-accent`, pinned by
`tests/test_button_system.py`), not `--ember`.

---

## 10. Empty, loading and error states

**Loading is the sliding ember pulse — never a spinner and never "…".**
- Web: `.hb-skel` skeleton bars (`hbSkel` sweep), `.hb-load` + the orb canvas
  (`data-orb-state`: `working` / `searching` / `shaping` / `composing` /
  `solving` exists on iOS only), `cbtnBusy()` inside a button.
- iOS: `CavnarSkeletonBar`, `CavnarSkeletonLines(widths:)`,
  `CavnarShimmerText(text:)`, `CavnarLoadingOrb` / `CavnarOrb`.

**Empty** says what would fill it, in one sentence: `.hb-empty` ("Trend
appears after a few weeks of reviews"), `.hb-clear` for the good kind
("✓ Nothing open"). Never render an empty card with no explanation, and
never substitute zero for a number that isn't known.

**Confirmed success** is `.cavnarPostedOverlay(label)` on iOS and a `toast(…,
'success')` on web. Errors are one plain sentence in `--red`, no apology.

**Failures on web** (dashboard.html): every fetch parses with
`.then(apiJson)`, never `r.json()` — a non-JSON error page becomes
`{ok:false, error, http_status}` rather than a thrown "check your
connection". A panel whose load fails calls `loadFailed(el, d)`: the
server's sentence in `.load-err` (`--red`) where "Loading…" was, never an
empty box and never a placeholder left up. Errors are a `toast(…, 'error')`
or an inline line, never `alert()` (a browser can suppress repeated
dialogs); a failure that must outlive a toast sits inline under the control
that started it (`#sched-notice`). When a side-effecting request loses its
answer, say the outcome is unknown and leave the button off — never
re-enable it for a blind second send. An ended session is the one sticky
toast (`.toast.toast-sticky`, `#session-ended`, top of the page) with a Sign
in again button; the page stops polling behind it. Pollers skip ticks while
`document.hidden` and catch up on `visibilitychange`.

**Unverified or partial data** carries `CavnarCaveat` (iOS) or the caveat
line the module already uses — the reader must always be able to tell a
measured figure from an estimate.

**AI at work is a reasoning trail, not a label that keeps changing.** When
the Ask loop streams `progress` events, the label that was running becomes a
ticked line above the one running now (`.ask-steps .st` on web, `trail:` on
iOS `LoadingBubble`). Every line is the server's own event in the order it
ran — never a scripted sequence. The answer then arrives block by block
(`_askStagger` → `.ask-in`, ~90ms per paragraph): it exists in full when it
lands and is *revealed*, not typed, so nothing is pretended.

**The AI activity strip and feed** (`activity.py`, `/api/activity`): a
low-profile pill in the bottom-left (`.cbtn.ai-strip` web; `AIActivityStrip`
under the pulse strip on iOS) with one breathing ember dot and a rotating
sentence of what is armed right now, and a badge counting entries newer than
the last look. Tap for the feed (`.ai-feed` / `AIActivityFeedSheet`): *Right
now* (ember, breathing), *Recently* (green, timestamped), *Still holding*
(memory — trackers in flight, follow-ups). Rules: every line is read from a
row a job wrote; a restaurant with nothing running shows no strip at all;
lines from a module the viewer may not read are left out. It is not a
notification surface and never demands attention. On web its edge carries
the *orbit*: a single ember light travelling the pill's perimeter (3.6s,
linear, `aiOrbit` — a conic gradient behind an inset fill, so only a 1.5px ring
shows). It is the strip's only moving edge, runs only while the strip is shown,
and stops under `prefers-reduced-motion`. Reuse it for nothing else — one
"alive" surface per screen.

**The surface hierarchy (brand layer).** Not every panel gets the same
treatment. Five tiers, shared by web (`dashboard.html` → *Brand layer*) and
iOS (`CavnarSurface` on `cavnarCard(_:)`):

| Tier | Use | Web | iOS |
|---|---|---|---|
| hero | the module's focal point near the top | `.rv2-hero`, `.lb2-hero`, `.hb-hero`, `.hb-card.hero` — ambient ember light (`:before`), deep shadow; the graph heroes (`.hb-hero`, `.rv2-hero`, `.lb2-hero`) also carry the cursor light | `.cavnarCard(.hero)` |
| card | informational | `.hb-card`, `.ac-card`, `.data-card` — hairline, lift on hover | `.cavnarCard()` |
| recessed | supporting stats inside a card | `.hb-sg`, `.rv2-sg`, `.lb2-sg` — no border, no hover tone | — |
| floating | above the page | `.ask-panel`, `.ai-feed`, modals — glass, long shadow | `.cavnarCard(.floating)` |
| ai | written by Cavnar AI | `.lb2-ai`, `.hb-rec`, `.ask-b.ai`, `.fc2-recipe` — warm surface, ember hairline, glow | `.cavnarCard(.ai)` |

A recommendation must always read as more elevated than a table. The
cursor light (`.lit`, `--mx/--my`) is for the big graphs ONLY — Home's
value graph and each module's hero chart. Cards, sections and stat tiles
get no spotlight and no hover background (owner's call, Sep 23 2026).

**The ember thread (signature motif).** A 2px ember line with one light
travelling along it, drawn from evidence to conclusion: Home's module pills
to the finding (`.hb-join .ln`), a chart to the read beneath it
(`.ember-thread`, inserted before every `.lb2-ai`), an answer's sources to
the answer (`.ask-eva`). iOS: `EmberThread(axis:length:fresh:)`. Rule: it is
drawn only where a real link exists in the payload — a thread that decorates
would spend the meaning the motif exists to carry. `fresh` adds a ring when
the conclusion was updated in the last few minutes (real `generated_at`).

**Premium empty states** (`.pe`): a breathing ember dot, the reason the
space is empty, and — from `/api/activity` — the real line for what Cavnar
AI is doing about it ("Watching for new reviews · next sweep at 4pm").

---

## 11. Animation

Web `cm-*` classes and keyframes in `dashboard.html`; iOS
`CavnarMotion.swift` plus `CavnarInteractions.swift`. The approved set covers
the seal draw-in and breathe, the wordmark stamp, the orb states, the compose
caret, the radar ripple, the week blocks, bar grow-in, skeleton sweep, row
fade-in and the posted overlay.

Rules:
- Ember is the only accent that moves. No bounce, no spring overshoot.
- Motion tells you something is happening or something changed; it never
  decorates a static page.
- Durations: micro-feedback 120–220ms, entrance 350–500ms, ambient loops
  1.8–6s. Easing `ease-in-out` or `cubic-bezier(.2,.9,.3,1)`.
- Respect `prefers-reduced-motion` / `accessibilityReduceMotion` — every
  looping animation must have a still fallback.
- Inline web JS is **ES5 only**, including comments
  (`tests/test_frontend_rules.py`).

---

## 11b. Home order (web and iOS, identical)

Fixed by role so the owner learns where things live: header strip (module
pulse, AI activity) → value delivered → **the day** (morning brief with open
issues; the close-out takes the slot after 8pm local, the weekly receipts
lead it on Monday) → needs attention → the one cross-module thing → what
Cavnar AI recommends → readiness (leads the page instead when nothing is
connected yet, hides once complete) → measured (goals, what your changes
did, what got better) → what connects, comps and voids → worth. The
close-out sits last before 8pm. Every block keeps its empty state; a quiet
day is a short page.

## 12. Reusable components

**Web** (all in `templates/dashboard.html` unless noted)

| Need | Use |
|---|---|
| Button | `.cbtn` + variant (`static/css/cavnar-buttons.css`) |
| Card | `.ac-card` + `.ac-card-h` + `.ac-row` |
| Section label | `.hb-kicker` |
| List row with status | `.hb-row` + `.d` dot + severity class |
| Signal tile | `.hb-sig` / `.hb-chip` |
| Table | `.hb-tbl`, `.fc2-mt` |
| Form control | `.ac-field`, `.ac-input`, `.ac-select`, `.ac-switch` |
| Chart | `glowLine`, `bars`, `stacked` |
| Loading | `.hb-skel`, `.hb-load` + orb, `cbtnBusy()` |
| Ask button | `.ask-fab` — the ember disc (highlight, underside shade, rim, long ember shadow, `askFabHalo` pulse) carrying the orb in cream ink: `CavnarOrb.mount(c,'working',{ink:'cream'})`, `searching` under the cursor. The brand colour's one large surface on the page; the label pill stays dark |
| Number badge | `.hb-ledger span>.hb-num:first-child`, `.lb2-cov span>.hb-num` — a 28px ember disc for a count that leads a chip |
| Empty | `.hb-empty`, `.hb-clear` |
| Tool with nothing to show | `.fc2-tool-empty` — a bold one-line state, then what is missing and where it gets fixed. A Load that resolves to a bare sentence reads as if nothing happened |
| Paste-to-draft | `.fc2-menu-draft` — ai tone (`--sf-ai`); a textarea, one secondary button, an `.ac-status` line that carries the result and the next batch |
| Escaping | `esc()` / `num()` (numbers → `.hb-num`) |
| Recommendation answer row | `.rec-ans` via `recControlsHtml(key, surface, module)` (JS) or `client_api.rec_controls_html` (server-rendered insight HTML) — Done / Not for us / Track as `cbtn-text cbtn-inline cbtn-sm` with `data-rec-key`; one delegated listener posts `/api/recs/event` and swaps the row for a muted `.rec-ans-done` sentence. Home's `.hb-rec-ans` answer row, inline under a module's recommendation line (`.rec-line` under a read, `.rec-cites` for the reviews an Intel recommendation cites). An answered line is not rendered again anywhere |
| Diagnosis block | `.diag` via `renderDiagnosis(id, dg)` — cause / also fits / what would tell them apart / evidence pills / confidence. One shape for reviews, labor and marketing; renders only when `dg.cause` exists |
| Per-check dots | `.in2-qc` (`.on` lit ember, `.off` dim) in `.in2-q` rows — one dot per run, oldest first, with an `n/asked` count |
| Decision row | `.ac-row` + `.ac-chip` answer (`done` / `not for us` / `tracking` / `measured`) — Account → What you've decided |
| Rules check | `.sr-panel` via `renderScheduleReview(d)` — `.sr-head` kicker + `.sr-chip` counts (`bad` hard / `warn` soft / `good` clean, `.sr-meta` for generation time), `.sr-line` rows (`.warn` for a ⚠ line, `.fix` for "Ana → Bob", `.bad` for what still needs a human), `.sr-soft` for a warning that is not a block, `.sr-actions` for the one fix button. Re-rendered from `POST /api/labor/schedule/violations` after every edit |
| Explanation drawer | a `tr.sched-why` under a clicked `tr.sched-row` — `.k` kicker "Why <name>" on the ai tone (`--sf-ai`), then the engine's own `why` sentence, or "No facts on file for this assignment." Never a hover tooltip: the reason is a paragraph |
| Flagged table row | `tr.needs-review` — amber wash and a 3px `--hb-warn` rail, `.rr` reason under the name. The rows the rules check names and the panel's counts must always agree |
| Inline table edit | `tr.sched-edit` replaces the row: `.ac-select` for who (legal replacements first, "· can take it"), `.ac-input.tm` for times, `.ac-input.rl` for role, Cancel (secondary) + Done (`cbtn-soft`). Edits stage into `#sched-edit-bar` (`.on`) with Discard + Save; the primary send button is disabled until Save |
| Publish gate | `#ps-blockers` via `_psRenderBlockers(list)` — `.sr-line.bad` per blocker, `.ack` checkbox "I've read these and want to send anyway" that enables a `cbtn-danger` Send anyway. A 403 shows the server's sentence in `--hb-bad` |
| Roster table | `.rst-tbl` — name cell `.nm` (role + "not on the roster" in `small`, `.car` chevron), `.rst-score` disc (`.none` when unrated), `.rst-rel` reliability in the number face (`.bad` ≥10% no-show, `.good` at 0), `.ac-switch` / `.ac-select` / `.ac-input.hrs` per fact, `tr.off` dims a deactivated person. Tap the name for `tr.rst-grid` → `.rst-days` seven `.ac-select`s (`any` / `morning` / `night` / `off`, coloured by value) |
| Pair row | `.lb2-pairs .pr` — `b` name, `.kd.prefer` / `.kd.avoid` pill, `.nt` note, `.x` remove |
| Close-out fields | `.co-grid` of `.ac-field`s for the four quick lines, then `details.co-more` — the same ember-triangle disclosure as the cost readout — holding the six the daily report reads (equipment, VIP guests, maintenance, shift notes, general notes, why the day went how it did); open when any of them is filled. iOS: `CloseOutSheet` — a second `AccountSection(kicker: "For the daily report")` of `AccountField`s |
| Inline form row | `.lb2-form` — `.ac-field`s side by side (`.w` wide, `.n` narrow number) with one secondary button at the end; `.lb2-sub` is the Clash sub-heading that separates it from the list above; `.lb2-ro` is the one-line read-only notice |
| Rules grid | `.rul-grid` of `.ac-field`s whose `placeholder` is the default and whose `label small` is the unit; `.rul-floor` per role (morning / night counts) with `.rul-ov` day-override chips and a `.rul-add` mini form |
| Version list | `.sv-list .sv-v` — `.n` disc (v1, v2…; `.published` green), `.t` "edited by will" + `small` date, `.ln` deterministic lines; `.sv-dvp` is the "Draft vs published" block |
| Request row | `.shr-row` — `.who` (name, when, reason, asked ago), a replacement `.ac-select`, Deny (secondary) + Approve (`cbtn-success`); `.shr-open` is the ember "open" mark on an unclaimed shift. `.shr-kind` is the small ember `drop` / `swap` mark; a swap reads "Ana ↔ Bob: Mon 4:00pm for Wed 4:00pm" and has no replacement select — approving moves both shifts |
| Week picker | `.lb2-weekpick` beside Generate — an `.ac-select` (Next week / The week after / A date…) that reveals a date `.ac-input`; sent as `week_start` |
| Redo-days row | `.sched-redo` under the preview header — `.sr-k` kicker, one pill `label` per date with its checkbox (`.hol` ember-edged on a holiday), `.sp` spacer, one `cbtn-soft` "Redo selected days" → `POST /api/generate-schedule {dates, history_id}` on the same job polling |
| Cost readout | `.sched-econ` — `.hb-stats` tiles for projected cost (`.ember` when over the dollar budget), overtime hours, premium and straight time, **never summed on screen**; `.cap` for the revenue basis sentence; `.notice` (amber) for "Demand blind since M/D/YY", `.notice.dim` for "no intraday sales yet"; `details` (ember triangle summary) listing trimmed shifts (`.sr-line` + `.why` reason) and staggered starts (`.sr-line.fix` with the `.arrow`) |
| Holiday mark | `.sched-hol` on a day header of the schedule table — name in an ember pill, the measured lift in the number face (green), the basis in `title`; no lift shown when none was measured |
| Edit cost | `.sched-cost` in `#sched-edit-bar` — "+6h · +$90 · 2h overtime" in `.hb-num`, `.up` ember when the edits cost money, `.down` green when they save it; from `POST /api/labor/schedule/violations` with `baseline_rows` |
| Save conflict | `.sched-conflict` (red wash) — "<who> saved this week after you opened it", their version's `.ln` lines, one secondary "Reload their version". Shown on a 409; the page never overwrites |
| Recommendation ledger | `.sq-rec` — the sentence in `.tx`, `.acts` with ✓ (`cbtn-success` icon) / ✕ (`cbtn-muted` icon) → `POST /api/labor/schedule/recommendation`; `.done` strikes it through with a `.st` "done" / "not for us"; `.sq-hidden` is the "N kinds hidden" note with an inline text button to What the record says |
| Score movement | `.sq-delta` beside the band in `#sq-delta` — "+3" (`.up`, green) / "−2" (`.down`, red) / "±0" in the number face; from `renderShiftQuality(quality, whatIf, prevScore)` after an edit, a fix pass, Improve with Cavnar or a live re-score. iOS: `ScoreDeltaChip(delta:)` |
| Provisional score | `.sq-prov` amber pill "provisional" beside the band when `quality.confidence.level` is low, and `.sq-prov-why` — the confidence's top reason — under the confidence pill. iOS: the `PROVISIONAL` capsule and the amber reason line. The history list's `.qpill` reads "84 · provisional" |
| What Cavnar changed | `.sq-opt` on the ai tone (`--sf-ai`, `--sf-ai-line`) in `#sq-optimizer` — `.hd` "Cavnar improved this draft from 71 to 84 — 5 changes" (`.st` "not saved yet" while proposed), a `details` of `.sr-line.fix` reasons with a green `.gain`, then an `.sr-grp` "Still needs you" of `.sr-line.bad` from `optimizer.unresolved`; `.sq-gate` is the quality gate's one line. iOS: `optimizerBlock` on `.cavnarCard(.ai)` |
| Rate in place | `.sq-rate` (recessed) in `#sq-rate` — shown only when the confidence says most of the week has no Operational Score: up to ten `.r` rows (name + hours this week) with a 1–5 `.chip-tog`, then a soft "Re-score with these ratings" (save:false). iOS: `ratePrompt` with the Operational Score five-number control |
| What if | `.sq-wi` at the foot of an expanded shift — "What if [person] works this shift, [added / instead of …]" (`.ac-select`s), Try it (`cbtn-soft`) → `POST /api/labor/schedule/score` with `save:false`; `.res` gives the shift and week before → after in the number face and "Nothing saved", with a text button to put them on (a staged edit). iOS: `whatIfRow` with two `Menu`s |
| Capping line | `.sq-cap` "This is what's holding the shift at N" (amber) leading an expanded shift, over the capping dimension's own weaknesses; the rest follow under "Also holding it back" |
| Unmatched ratings | `.rt-unm` (amber wash) in `#team-unmatched` above the Operational Score list — "3 ratings don't match anyone on your roster", one `.r` per rating: name + `small` score, a `cbtn-soft` "It's Kim T." one-tap confirm when the server suggests one, and an `.ac-select` of candidates then the team. iOS: `unmatchedBlock` in TeamStrengthSection |
| Draft learning | In What the record says: `.int-offer` (ai tone) — the auto-publish offer's reason and one `cbtn-primary` "Turn on auto-publish" (the Automation setting's own POST); "How much of the draft you keep" as `.int-bars` of unchanged share per published week with the trend sentence in `.ac-note`; `.int-cal` for the weight calibration — its reason when not ready, `.w` rows "default → suggested (±n%) · reading" when ready, never applied. iOS: `autoPublishOfferCard`, `acceptanceBlock`, `calibrationBlock` |
| Chip toggle | `.chip-tog .ct` — `cbtn cbtn-secondary cbtn-sm` pills that light ember with a ✓ when `.on` (`aria-pressed`); `.ro` when read-only. Rules: certifications per role, front-of-house and patio roles; roster: a person's certifications |
| Pack caveat | `.rul-pack` — `.k` kicker naming the jurisdiction pack, its `notes` as a list, `.cc` "starting values, not legal advice — check with counsel"; a rule field the pack fills shows `label small.rul-from` "from the California pack" and the pack's value as its placeholder |
| Rule switch | `.rul-sw` — `.ac-switch` + `.t` title with `small` record (manager on duty, trim to budget) |
| Rule row | `.rul-row` — `.role` + controls per role (arrival minutes `.ac-input.n` + before/after `.ac-select`; certification chips) |
| Reservation feed | `.rul-res` — system `.ac-select` (a provider not live says so in its option), key `.ac-input[type=password]`, one secondary "Sync now" → `POST /api/labor/reservations/sync`; `.msg` carries the feed's own status sentence, `.live` green, `.bad` red for a 400's message. The same sentence is the `.ac-note` caption on the Events card |
| Time windows | `.rst-win` in `tr.rst-grid` — seven columns, two `.ac-input`s each (earliest start, latest finish, "10:00am"), blank = any; saved as `time_windows` on change |
| Person facts | `.rst-more` — an `.rul-sw` "Experienced — knows the job" switch (`experienced` on the staff settings POST; iOS: an `AccountSwitchRow` in the person sheet), `.k` kickers over certification chips, `.said` (the person's own stated preferences, read-only here: "prefers nights · wants 30h a week"), and `.rst-up` green chips for stations they are trained up on (`could_hold`, from `/api/labor/intel`) |
| Suggested pair | `.lb2-pairs .pr.sug` — the pair, an ember `.kd`, `.acts` Add (`cbtn-soft`, the existing pairs POST) / Ignore (text, local dismiss), `.ev` evidence line. Never applied on its own |
| Learned pattern | `.lrn-row` — the engine's own sentence in `.tx`, `.n` state (`in use` green / `seen once` / `not used`), one text button "Stop using this" / "Use again" → `POST /api/labor/learned-patterns`; `.off` strikes a dismissed one through |
| Record card | `.lb2-srow.s8` "What the record says" — `.int-rev` (projected weekly revenue in the number face + `.src` basis), `.int-tbl` weekday × daypart outcomes (`.cell.trbl` carries a red rail and a `.flag` "troubled"), `.int-bars .int-bar` sales-per-labor-hour bars (ember gradient, glow, `cmBar` grow-in), `.int-cols` two columns of `.int-ledger` (most / fewest weekends and closes) and `.int-say` sentences ("Ana keeps asking to drop Sunday nights"), `.rst-up` trained-up chips, the suggested-pairs block, `.int-supp` for hidden recommendation kinds. Every section has its own `.hb-empty` sentence |

**iOS** (`Features/…` + `DesignSystem/`)

| Need | Use |
|---|---|
| Card | `.cavnarCard()` |
| Sheet | `.accountSheetChrome(title)` + `cavnarTitleToolbar` |
| Sheet anatomy | `AccountHero` → `AccountSection` → `AccountKVRow` |
| Pill / chip / tile | `AccountPill`, `AccountChip`, `AccountStatTile` |
| Button | `CavnarPrimaryButtonStyle`, `CavnarSecondaryButtonStyle` |
| Recommendation answer row | `RecAnswerRow` (`DesignSystem/RecAnswerRow.swift`) — Done / Not for us / Track as small text buttons, POSTs `/mobile/api/recs/event`, then a muted confirmation line. The same row under every module recommendation (Reviews, Food Cost, Marketing, Intel) |
| Field | `AccountField`, `CavnarFloatingField`, `CavnarDropdown` |
| Switch with its record | `AccountSwitchRow(label:detail:isOn:busy:)` — the `detail` is the owner's own record behind the switch (Automation & trust) |
| Score movement | `ScoreDeltaChip(delta:)` — "+3" green / "−2" red capsule in the number face (`ShiftQualityPanel.swift`) |
| Decide-in-place row | `TimeOffSection` row: name + dates, Deny (secondary) / Approve (primary) side by side, status text once answered |
| Mixed text with numbers | `HomeMixedText.make(...)` |
| Loading | `CavnarShimmerText`, `CavnarSkeletonLines`, `CavnarLoadingOrb` |
| Success | `.cavnarPostedOverlay(label)` |
| Sensitive figure | `.cavnarSensitive()` (privacy redaction) |
| Caveat | `CavnarCaveat` |
| Background | `.cavnarModuleBackground()` |
| Radius | `CavnarRadius.control / .card / .sheet / .pill` |

---

## 13. Checklist before shipping a screen

1. Tokens only — no literal colours (`scripts/check_colors.py`).
2. Every number in Space Grotesk; headings in Clash; chrome in Apfel.
3. One primary action; every web button carries `.cbtn`.
4. Loading uses the pulse/skeleton/orb, never a spinner.
5. Empty states say what would fill them; unknown is never zero.
6. Text sizes match the rest of the product.
7. Reduced motion has a still fallback; inline JS is ES5.
8. The same feature reads the same on web and iOS — same words, same order,
   same figures.

---

## Email

The third surface, and until the email audit (Sep 19 2026) the only one this
document did not govern — which is why 22 client-facing emails ended up
built across three unrelated frames, 14 of them as bespoke inline HTML, with
231 hex values re-typed that `emails.BRAND` already names.

Email is **not** the web app in a mail client. Three things are genuinely
different and none of them are style choices:

- **Light mode only, always.** Never read a theme column. `notify.py`'s
  `_alert_email_html` documents the incident: the web dashboard POSTs its own
  dark-mode switch to `/api/theme`, that column was read here, and a client
  who preferred a dark dashboard started getting dark alert emails they never
  asked for while every other Cavnar email stayed a light card.
- **Inline styles, tables, no CSS variables.** Mail clients strip
  `<style>` and know nothing about `var(--ember)`. This is the one surface
  where a literal colour is correct — it just has to come from `BRAND`.
- **No web fonts.** `_SANS` (system stack) for text, `_NUM`
  (`'Space Grotesk'` with a system fallback) for figures, matching the
  product's rule that every number is set in Space Grotesk.

### Colour

`emails.BRAND` is the palette. Use the token, never the hex:

```python
f'<p style="color:{BRAND["body"]}">…</p>'     # yes
f'<p style="color:#4a443d">…</p>'             # no — that IS BRAND["body"]
```

`scripts/check_email_tokens.py` enforces this as a ratchet: the count of
duplicated literals may never rise. Migrate the ones you touch and lower
`BASELINE`.

| Token | Use |
|---|---|
| `paper` `card` `border` `rule` | page ground, card, card edge, hairline |
| `strong` `ink` `body` `muted` | headline, text, body copy, captions |
| `ember` `ember2` | the one accent — spend it on a single CTA or a status stripe |
| `good` `warn` `bad` | verdicts only, never decoration; `_TINT` gives each a background |

### Frames

Three exist. Pick by what the email *is*, not by which is nearest:

1. **`report_shell(kicker, title, subtitle, sections, cta_label, cta_url)`** —
   anything an owner reads for information. The digest, the monthly review,
   the closing summary, the nightly DSR (`emails.dsr_email`). Compose the body
   from `report_eyebrow`, `report_paragraph`, `report_stats`, `report_lines`,
   `report_bullets`, `report_quote`, `report_action`, `report_rule`. **New
   reporting email starts here.** `report_bullets(items, accent)` is
   `report_lines` without a label — a list whose eyebrow already names it
   (the DSR's *Went well* on a `good` rule, *Needs attention* on `warn`,
   *Not in this report* on the quiet border).
2. **`_branded_email(inner_html)`** — short transactional mail: a code, a
   confirmation, a link. Wordmark, one white card, seal footer.
3. **Bespoke** — legacy. 14 emails still are. Not a starting point; migrate
   onto `report_shell` when you touch one.

Widths are 560px (`report_shell`) and 480px (`_branded_email`). Do not
introduce a third.

### Every client email owes the reader

- **A preheader.** `emails.deliver()` takes a `preheader` key and injects it
  hidden at the top of the body. It is the second line an owner reads, before
  opening anything. Never a greeting — lead with the substance
  (`emails.digest_preheader` leads with whichever metric moved most in
  dollars).
- **A subject with no emoji.** On a locked phone an emoji-led subject reads
  as a consumer app. Internal mail to Will keeps its glyphs; it is a triage
  queue.
- **One sender.** `emails.sender("client" | "ops" | "will")`. Ten display
  names on one address is ten weak reputation signals and a sender nobody
  learns to recognise.
- **The cost of waiting, where there is one.** `weekly_review.cost_of_waiting`
  and `monthly_review.cost_of_waiting` state what another period of a
  *worsened* metric costs, in dollars, and say nothing when nothing worsened
  or no dollar figure exists.
- **An unsubscribe, if it is marketing.** Applied centrally in
  `emails.deliver` for `_MARKETING_TYPES` — visible footer plus
  `List-Unsubscribe-Post`, which is what gets Gmail to show its own affordance
  instead of the spam button. Security and operational mail must never carry
  one.

### Never

- A figure a model produced that was not verified against its input
  (`ai_guard.unsupported_figures` / `verify_figures`).
- A raw `resend.Emails.send` for client mail — `emails.deliver()` owns retry,
  suppression, the flood guard and `email_log`.
- A restaurant's name as the display name on Will's address.
