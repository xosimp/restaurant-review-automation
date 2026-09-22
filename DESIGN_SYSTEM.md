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
| iOS | a `M/d/yy` `DateFormatter`, as `TimeOffSection.mdy` does |

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
| Numbered card | `.hb-card.hb-rec` in `.hb-recs` (3-up) | Recommendations, with Track this |
| Receipt strip | `.hb-rcpt .it` — check + sentence + module | What Cavnar AI did — finished things must not look like a to-do |
| Timeline | `.hb-tl .it` with a tone dot on a rail | The morning brief, read once top to bottom |
| Stat tile | `.hb-stats .hb-stat` (+ `.good` / `.ember`) | Four *kinds* of number that must never be added together |
| Ledger pill | `.hb-ledger span` — the leading count on an ember disc (`>.hb-num:first-child`), a figure mid-sentence stays plain | Since you started: distinct work counted, never dollars |
| Ops tile | `.lb2-ops .lb2-op` (+ `.ember` ambient) — ember mark `.ic`, count in the number face `.v` (+ `.warn` / `.good` / `.ember`), body at 15px, `.cap` footer | Labor's Time off and Covers: what the owner feeds the read. Card tier, so they weigh the same as the tiles above them |
| Goal bar | `.hb-goal .bar i` (width from `data-w`) | Progress from baseline to target; no bar when the baseline is unreadable |
| Checklist | `.hb-chk` | Still open |
| Section header | `.hb-sh` — `.k` kicker (ember, or `.dim`) + Clash `h2 small` | Between the big moments; the hairline is the rhythm |

Entrances are `.hb-rise` with `--i` for a 70ms stagger, 420ms, no overshoot.
The list row `.hb-row` stays for what is genuinely a list.

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
notification surface and never demands attention.

**The surface hierarchy (brand layer).** Not every panel gets the same
treatment. Five tiers, shared by web (`dashboard.html` → *Brand layer*) and
iOS (`CavnarSurface` on `cavnarCard(_:)`):

| Tier | Use | Web | iOS |
|---|---|---|---|
| hero | the module's focal point near the top | `.rv2-hero`, `.lb2-hero`, `.hb-card.hero` — ambient ember light (`:before`), deep shadow, cursor light | `.cavnarCard(.hero)` |
| card | informational | `.hb-card`, `.ac-card`, `.data-card` — hairline, lift on hover | `.cavnarCard()` |
| recessed | supporting stats inside a card | `.hb-sg`, `.rv2-sg`, `.lb2-sg` — inset tone on hover, no border | — |
| floating | above the page | `.ask-panel`, `.ai-feed`, modals — glass, long shadow | `.cavnarCard(.floating)` |
| ai | written by Cavnar AI | `.lb2-ai`, `.hb-rec`, `.ask-b.ai`, `.fc2-recipe` — warm surface, ember hairline, glow | `.cavnarCard(.ai)` |

A recommendation must always read as more elevated than a table. Every
lit surface carries the cursor light (`.lit`, `--mx/--my`).

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
| Diagnosis block | `.diag` via `renderDiagnosis(id, dg)` — cause / also fits / what would tell them apart / evidence pills / confidence. One shape for reviews, labor and marketing; renders only when `dg.cause` exists |
| Per-check dots | `.in2-qc` (`.on` lit ember, `.off` dim) in `.in2-q` rows — one dot per run, oldest first, with an `n/asked` count |
| Decision row | `.ac-row` + `.ac-chip` answer (`done` / `not for us` / `tracking` / `measured`) — Account → What you've decided |

**iOS** (`Features/…` + `DesignSystem/`)

| Need | Use |
|---|---|
| Card | `.cavnarCard()` |
| Sheet | `.accountSheetChrome(title)` + `cavnarTitleToolbar` |
| Sheet anatomy | `AccountHero` → `AccountSection` → `AccountKVRow` |
| Pill / chip / tile | `AccountPill`, `AccountChip`, `AccountStatTile` |
| Button | `CavnarPrimaryButtonStyle`, `CavnarSecondaryButtonStyle` |
| Field | `AccountField`, `CavnarFloatingField`, `CavnarDropdown` |
| Switch with its record | `AccountSwitchRow(label:detail:isOn:busy:)` — the `detail` is the owner's own record behind the switch (Automation & trust) |
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
   the closing summary. Compose the body from `report_eyebrow`,
   `report_paragraph`, `report_stats`, `report_lines`, `report_quote`,
   `report_action`, `report_rule`. **New reporting email starts here.**
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
