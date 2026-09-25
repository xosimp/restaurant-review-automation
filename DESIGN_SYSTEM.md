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

**One exception (owner's call, 9/25/26):** Home's kicker above the greeting reads the date in words, "September 25th, 2026" (`longDate`). Everywhere else stays M/D/YY.

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
reference; never inflate one module's text on its own.

**Home's section heads are two levels, no more** (density fix #41, 9/25/26):
`.hb-sh` (a kicker and a Clash h2) between sections, `.hb-h3` (the small
uppercase ink3 label) inside a card — the focus card, Needs attention, the
Last night card, the milestone, the welcome, the value graph, the group
Home's Needs attention, the Results row. `.hb-kicker` is the page kicker
above the H1 only. No `h3` of its own, no ember kicker inside a card. Headings get
`text-wrap: balance` where supported; body copy uses `line-height:1.5–1.65`.

### iOS: `CavnarType`, and one 40pt figure per screen

iOS names the scale (`CavnarType` in `Font+Cavnar.swift`). A screen reaches
for a token, not a literal; the face stays the helper's (`cavnarBody` words,
`cavnarHeadline` titles, `cavnarNumber` figures), so Dynamic Type still maps
each size to its text style. The census that prompted it (9/25/26) found 18
body sizes, 4 kicker sizes and hero figures from 27 to 56pt.

| Token | pt | Use |
|---|---|---|
| `kicker` | 11.5 | uppercase tracked label above a section or figure |
| `caption` | 12.5 | meta, timestamps, basis lines |
| `secondary` | 13.5 | a line under a figure or title; helper copy |
| `body` | 15 | body copy, row titles |
| `emphasis` | 16.5 | the one sentence a card opens on |
| `section` | 21 | a section title (Clash) — `HomeSectionHeader` |
| `tileNumber` | 22 | a stat-strip or grid tile figure |
| `cardNumber` | 30 | a card's own figure |
| `heroNumber` | 40 | the screen's status figure |

**One 40pt figure per screen, and it is the status figure** — the number
that answers "are we OK?" in three seconds: labor % against target, food
cost % against target, the report's score, the owner's rating against the
market on Intel. Anything else that wants to be big is `cardNumber` or
smaller. A measured-results figure is never the page's largest number
(Home's value band is `cardNumber`, inside the collapsed Results).

Migrated so far (density round, 9/25/26): Home (section headers, Results,
last-night card, recommendation rows, pulse strip, module tiles), the daily
report's score card and titled cards, Food Cost Analytics' hero and stat
strip, Labor's groups and schedule block, Intel's hero line, Reviews' why
line, Marketing's outcome row, Notifications' summary, the staff portal's
next shift. Other screens move module by module; a new screen starts on
the tokens.

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
| Hero card | `.hb-card.hero.hb-focus` — ember radial in the corner, Clash `.lead`, `.why`, `.ev` chips, `.hb-focus-cf` (claim tag + the confidence line), `.ft` footer with `.money` — a range stays a range ("$1,200–$2,400/month · opportunity: rating movement", `hbMoneyRange(money)`), never collapsed to its first figure | **Today's focus** only on Home — one per page (What connects is Needs-attention rows plus an explanation card in Results since density fix #5). |
| Module join | `.hb-join` — pills joined by a lit ember line | The cross-module mark on a hero card |
| Numbered card | `.hb-card.hb-rec` in `.hb-recs` (3-up) — `.t` the action (verb first), `.why` why now, `.meta` pills (`.usd` "$N/mo at stake" only when measured, with `.hb-dbasis` "covers one Tuesday's overstaffing, per month" under it from `dollars_basis` — one Home can carry three labor figures of different scope, and each says its own; the focus card's `.money` and a Needs attention row carry the same line — then timeframe · impact — **no evidence-strength pill**: it was a second verdict that could contradict the confidence on the same card, and the confidence's Evidence strength row says it, measured), an "AI-written" `.ck-tag` after the title when `model_written`, `.ev` evidence, ONE confidence line (`.cf.hb-conf`, see §12 **Confidence**: "72% confidence — what it rests on · Why?"), `.ig` "If ignored:", then the footer: one-tap action (reprice) / Track this / open module / Could also be… (`data-explain`, only when the source has an alternative) / Assign (inline `.hb-assign` select, only with assignees) / Done · Not for us, and the ✕ hide. iOS: `HomeRecommendations` rows, same order | Recommendations — every card answers what, why now, $, how sure, if ignored |
| Quieter line | `.hb-quiet` under the cards — "Quieter: Trim day — the last four went by unanswered." + a text button per kind ("Show trim day again" → `restore_kind`). iOS: the same line under the card | A kind the owner has let expire unanswered four times running |
| Receipt strip | `.hb-rcpt .it` — check + sentence + module | What Cavnar AI did — finished things must not look like a to-do |
| Timeline | `.hb-tl .it` with a tone dot on a rail | The morning brief, read once top to bottom |
| Stat tile | `.hb-stats .hb-stat` (+ `.good` / `.warn` / `.bad` — a gap still open, "Still on the table", is `.warn` amber; ember is never a tile's status, §9) | Four *kinds* of number that must never be added together. On Home's worth card they sit in `.hb-vsplit`: the measured tile alone under "What was measured", the estimate / alert total / gap under "What Cavnar surfaced · still available" ("estimates and gaps, never added in"), side by side, stacked under 760px, the second group absent when it has nothing (`.solo`). The section is "Worth · What Cavnar AI measured and surfaced" — never "Measured" over tiles that grow as the restaurant does worse. The measured tile reads **"Measured, net"** once anything got worse: `net_monthly` (below zero when that is what was measured, `.bad`), with "$X/mo from N that improved, less $Y/mo from M that got worse" under it (`hbNetSentence`: M is `worsened.priced_count` — the dollars only cover priced results — and any others are said as "(K more got worse with no dollar figure)"). A card whose only wins have no honest price (a rating rise, `unpriced_wins`) reads "N improved · not priced in dollars", never "Nothing yet" and never a figure |
| Worth rows | `.hb-vrows` — `.hb-row`s under the worth tiles, each what the measured figure rests on: "$1,006 measured since 4/26/26" (the cumulative total, only when `cumulative.total` is not null — nothing measured is not $0 — with a How? modal of its `basis`), "N held at their re-check — validated", each unpriced win in the server's words ("Average rating 4.2★ → 4.4★, improved — after "Brief the servers…" · not priced in dollars", up to three, then "N more improved with no dollar figure"), faded wins, and the server's `net_note` when the net is negative. `.vm` is the muted tail | The worth section's footnotes, same kind as the tile (measured), never added to the estimate, the alert total or the gap |
| Value hero (net) | `.hb-hero` on Home — "Measured results" (kicker and `aria-label`; never "Value delivered", which claims Cavnar caused it), the `.big` figure (count-up), "a month, measured", and the payload's `caveat` under it. Once anything tracked got worse (`value.worsened.count > 0`) the figure is `value.net_monthly` and the label reads "a month, measured, net", with a `.delta.hb-net` line saying both sides ("$1,310/mo from 3 that improved, less $180/mo from 1 that got worse") and "plus N improvements with no dollar figure" for `unpriced_wins`. Below zero it is "−$420" in `.big.neg` (`--hb-bad`), never floored; the range delta turns `.delta.bad` when the curve fell | Home's first section — the improvements alone read as if nothing had gone the other way |
| Result tone | `hbResTone(r)` — a result row's dot: improved → `.good`, worsened → `.critical`, anything else `.watch`; and `counts === false` (the owner said they didn't make the change, it faded at its re-check, or it is an alert read) is always `.watch`, whatever the number did. The server's `counts` is authoritative; an older row without it falls back to the verdict | "What your changes did" on Home and in the month card |
| Measured alongside your changes | `.hb-card.rail.ember.hb-worked` — kicker "Measured alongside your changes" (it was "What worked for you", which asserts causation over before-and-after data) + the window ("the past 6 months"), the server's sentences (`GET /api/recs/what-worked`, `owner_report`) as an `.hb-tl` rail, the caveat ("Measured before and after, not proven cause.") always under it, "See the whole record →". Not enough: one `.hb-empty` saying what would fill it, shown only where the worth section has something | On Home, right under the worth section |
| Ledger pill | `.hb-ledger span` — the leading count on an ember disc (`>.hb-num:first-child`), a figure mid-sentence stays plain | Since you started: distinct work counted, never dollars |
| Ops tile | `.lb2-ops .lb2-op` (+ `.ember` ambient) — obsidian mark `.ic.ob-tile`, count in the number face `.v` (+ `.warn` / `.good` / `.ember`), body at 15px, `.cap` footer | Labor's Time off and Covers: what the owner feeds the read. Card tier, so they weigh the same as the tiles above them. Time off shows the count and the answered record only — a request is answered in Waiting on you (density round #27) |
| Position tile | `.fc2-position .fc2-pos` — ember rail, one number in the number face, its basis under it | Food Cost's recipe coverage and unexplained waste. Food cost % itself is the page's hero (`.fc2-hero-fcp`, the same renderer without the card chrome, in the header's right column) and, once `/api/food-cost/cogs` answers, the h1's status ("31.4% · 1.4 pts over your target") — density round 9/25/26 |
| Working block | `.fc2-block` with `fc2BlockHead(icon, kicker, title, count, sub)` — obsidian tile, kicker, Clash title with an ember count chip | Food Cost's recipes, count sheet and suppliers. Each sits inside a one-line row in "This week's work" (`details.hb-results.fc2-work`, its summary line filled by `fc2WorkRow(key, line)`: "64 items · last counted 9/22/26"), after the why; the section keeps its id and `data-nav`, so a deep link opens its row first |
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
use a real `Toggle`. A small bounded count saved with a sheet's one Save
button (Schedule rules' "Never cut a role below N people", 1–10) is a
native `Stepper` with its label hidden, beside the value in the number
face ("2 people" via `HomeMixedText`), in a row laid out like the sheet's
other rows (label + detail left, `AccountKVRow.rowHeight`); web is an
`.ac-input.ac-num.n` inline in an `.rul-sw` sentence.

Validation reads as a plain sentence under the control in `--red` /
`.cavnarRed`: what went wrong and how to fix it.

**Prefill, never pre-save (friction audit, 9/25/26).** Where the system
already knows a value an owner would type — last week's budget, Google's
listed hours, the POS guest count, the owner's own phone for alert contact
1 — the form offers it and the owner saves. Web: a row of
`cbtn-secondary cbtn-sm` source buttons led by a small `--ink3` label
("Start from", `.dr-bud-pre`), or one `cbtn-text` "use it" link beside a
suggested figure (`.lb2-cov-offer`); the status line then says where the
figures came from and "Not saved yet". A field the source has nothing for
stays blank — never 0. Dates the owner enters are a `type=date` input plus
removable chips (`.rul-ov`, M/D/YY), never a comma-separated text box.

**Editable lines before an outward send.** A list that will be emailed
(the supplier order) shows each line with a numeric `.fc2-inv-in` input and
a running total; the send button names the recipient and the total
("Send to Sysco · $412"), and pressing it opens an inline `.fc2-inv-note`
strip that names the address, the item count and the total with "Send it" /
"Not yet" — the confirm surface for an outward action, never `confirm()`.
A read that the ledger keeps current (the ingredient price monitor) opens
read-only with one "Edit" button that unlocks it.

---

## 8. Tables

Web, and one iOS exception. `.hb-tbl` is the reference: uppercase 10px
kicker headers with a `--hb-line` underline, 12px cells, hairline row
borders, right-aligned numerics (`th.r` / `td.r`), `.nm` name cell with a
status dot, hover tint, and a clickable row when the row has a destination.
`.fc2-table` / `.fc2-mt` are the Food Cost variants (14px, same anatomy).

Wrap wide tables in an overflow container (`.fc2-table-scroll`) so the page
never scrolls sideways.

**iOS uses rows and cards — except the Daily Report's week**
(`DSRWeekGrid`, `Features/DailyReport/DailyReportWeekView.swift`): the
owner's own weekly sheet is a grid, and read any other way it loses the
columns he compares down. Same anatomy as `.hb-tbl`: a pinned first column
(the day, tappable when that night has a report, an amber dot when it is
provisional), the figures scrolling sideways inside the card (never the
page), uppercase 10.5pt ink3 headers, numbers right-aligned in the number
face, changes green/red, an unmeasured cell a muted "—", and the totals
rows on a faint ember wash under a heavier rule. The cells come from
`DSRWeekTable` (plain strings, unit-tested), not from the view. Don't
reach for it for anything that reads fine as rows.

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
- Web: `.dr-pulse` (a 3px track with an ember light sliding across, `pulseBarSlide`) for a quick page load or a running job, `.hb-skel` skeleton bars (`hbSkel` sweep), `.hb-load` + the orb canvas
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
| recessed | supporting stats inside a card | `.hb-sg`, `.rv2-sg`, `.lb2-sg` — no border, no hover tone | `DSRStatTile` (Paper3 at 35%, radius 12, no border) |
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

**Confirm and undo — one policy, three tiers** (Friction audit 9/25/26 #10;
web and iOS, and every surface that acts: buttons, Ask, the command palette,
notification actions).
1. **Reversible → act at once, then Undo.** No dialog. The thing leaves the
   screen immediately and the toast carries the way back:
   `toast(msg, type, {label: 'Undo', fn})` (7s instead of 3.6s). Where the
   server has no inverse, `cavUndoable(msg, commit, restore)` holds the
   request that makes it final until the Undo window ends — a page closed
   inside the window keeps the thing (the safe side). Deleting a chat,
   stopping a competitor, removing a webhook; skip/dismiss/snooze.
2. **Outward → a confirm card that names what goes out and how many.**
   Posting, publishing, texting, emailing, ordering. The card is Ask's own
   (`.ask-prop`, `cavPropCard(p, host, {onDone})` over a
   `build_proposal` payload — from Ask, from `/api/command/propose`, or a
   stored proposal reopened): every field the route receives, the words
   that go out, the dollars. **Enter never confirms** — a click, or ⌘Enter
   while the card is on screen. Bulk sends name their count and are capped.
   Home's "Publish N replies" opens this card; it used to post on one tap.
   So do Still open's "Send now" and a request's Approve / Deny (the queue
   item's `action.confirm`, re-audit F1-3). The card's own action request
   carries `X-Cavnar-Proposal`, so its confirm is recorded in that request.
   Where an automation queues the send (`delayed.py`), the Undo lives in the
   activity strip until it runs.
3. **`confirm()` only for security and account-destructive steps** — two-factor
   off, backup codes, revoking a teammate, a new join code, disconnecting an
   integration, co-owner access. Everything else is tier 1 or tier 2.

**Where a link lands.** Anything that sends the owner somewhere names the
item, not the module: a `nav` path (nav.py — `review/412`,
`reviews?filter=urgent`, `labor/schedule`, `account/notifications`) opened
with `cavNav(path)` on web and `NavPath` on iOS. A section is any element with
`data-nav="<module>/<section>"`; a button that goes somewhere carries
`data-nav-go="<path>"`. The landed item gets one ember ring that fades
(`.cav-flash`, 1.5s; a still outline under reduced motion). Back returns to
the previous place, never out of the app.

### 10b. Money labels and positive status

The rules that keep an owner from reading a gap as money made (never-say
audit, 9/24/26). Web: the Jinja `_status_ok` / `hbAllClear` / `hbLaborFlag`
/ `cavMoneyKindWord` helpers in `dashboard.html`. iOS: `Models/OwnerCopy.swift`
(pure, unit-tested in `OwnerCopyTests`). Enforced as banned strings by
`tests/test_owner_copy_clients.py`.

**Every money figure says its kind, its period and its basis.**

| Kind (`kind` / `dollars_kind` when the server sends one) | Label | Tone |
|---|---|---|
| measured (`value_delivered.delivered`) | "Measured results", "a month, measured" | green / red by sign |
| opportunity (gap to target, recoverable, at stake) | "Gap to target / mo", "At stake · opportunity", "available, not captured" | warn amber or ink — **never green, never "savings"** |
| projection (a week ×52÷12, a month ×12) | "Per year · projected", "/mo projected · from one week's count" | ink |
| benchmark gap | "Under 34.5% industry / mo" — "a benchmark gap, not savings"; hidden at $0 | ink |
| estimate | "estimated", with its stated rate | ink |
| per-order difference | "↓ $X vs last order" | ink3 |

- Every money tile names its period: `/wk`, `/mo`, `/yr` or "N days". A
  whole-window figure says its days ("Overtime premium · 28 days").
- Sample data carries no dollars: every money tile requires `is_live`.
- A synthetic series renders only with "Illustration only" in the same view.
- Never on an opportunity or projection: "savings", "saved", "advantage",
  "claw back", "the one number", "this month" (it is projected), "Value
  delivered". Labels over a model-written dollar sentence name the kind
  ("Biggest opportunity · not captured", "Largest dollar gap · an
  opportunity, not savings"), never "Largest saving".
- A payload `kind` picks the word (`cavMoneyKindWord` / `OwnerCopy.kindWord`);
  without one the surface keeps its own default ("at stake").

**Positive status needs live, complete, fresh data.** "On target ✓",
"Excellent" (retired — "Well under target"), "All clear", "Running well",
"labor · on target" and every green verdict render only when the payload
says the read is live (`is_live`), complete (no `days_missing_sales`,
`hours_are_estimated`, `sales_data_missing`), long enough
(`!period_too_short_to_project`, `projectable`) and fresh
(`monitoring.all_clear`, no stale source). Otherwise the chip is "—" (or
neutral ink) and the reason ("Sample data — add your shifts", "partial
data", "out of date", "too few days"). Over target always says so, partial
data or not.

**Model forecasts and predictions are conditional.** A diagnosis is headed
"What the evidence points to"; its `expected_outcome` sits under "If this is
the cause, you'd expect…" and a certainty ("will eliminate", "guaranteed")
is not shown. "If ignored:" reads "Risk if left alone:". Schedules are
"drafted" / "generated", never "optimized"; a progress step names an input
("last year's same days") only when the payload says it exists. Ask shows
`unsupported_causes` and `unsupported_names` inline beside the untraced
figures. Recipe lines carry a % or nothing, never a confidence word.

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

Fixed by role so the owner learns where things live — and **the work leads**
(friction audit #11, 9/25/26: the one thing to do sat under a results chart
and the full brief, tenth on the phone). The density round (9/25/26) holds
it to the 3-30-300 rule: the header answers health in 3 seconds, the one
thing and Needs attention answer what to do in 30, and everything else is
the 300-second drill-down.

1. Header: the greeting and headline, then **the status line** — "Last night
   · ● Good day 78/100 · $8,420 net · +$420 vs budget" in the verdict's tone
   (web `#hb-status`, from `dsr.access.summary`'s `verdict` / `tone` /
   `overall` / `net` / `vs_budget`; a manager's line has no verdict or budget,
   their report has neither), then the sub line (what changed, **one**
   freshness chip "Data 92% · as of 9/24/26" that opens the Data Health
   drawer), the module pulse chips (the at-a-glance row) and the quick
   actions that no Needs-attention row already carries. On web the location
   chips for a multi-location login.
2. **The one thing** (web `#hb-focus`; iOS `HomeOneThingCard`) — one lead, one
   why, one $ figure, one action (the page's one `cbtn-primary`) — and **Needs
   attention** directly under it: one deterministic sentence ("7 flagged → 2
   need you now: …"), then every item — each row is its title, one evidence
   clause, its confidence as a "72%" pill (`cavConfLine(c,{pill:1})`, the
   same Why? panel) and its action, secondary. Rows past the third wait
   behind "+N more", which expands the list **in place** (never the bell).
   The comps / voids / refunds flags and every cross-module link past the
   one the focus card leads with are rows here, with Done / Not for us —
   never inside the collapsed Results. When a finding takes the focus card,
   the attention item it displaced is the first row. iOS keeps the lead deck
   card and lists items 2–n under it as one-line rows with their action.
   When nothing is flagged, the one-line all-clear row.
3. The per-source freshness strip — **only when a source is aging, stale,
   undated or disconnected** — and How you compare.
4. **The day** — last night's Daily Sales Report card (its summary, "Watch:"
   the first risk, Open report; the verdict and net are the status line's),
   then the morning brief ("Before service"; the brief's DSR "Last night"
   line is left out, `morning_brief.shown_on_home`). After 8pm local the
   close-out leads the day; the weekly receipts lead it on Monday only; a
   milestone, when one lands, is an inline card at the top of the day —
   never a modal. iOS: `HomeLastNightCard`.
5. What Cavnar AI recommends — three cards of title, why, $, confidence pill
   and one action with Done / Not for us; what it rests on, "Risk if left
   alone", "Could also be…", Assign and Track under the card's Details —
   then readiness (leads the page instead when nothing is connected yet,
   hides once complete; a module with no data is a readiness item, never a
   placeholder tile).
6. Still open (web) and the close-out: before 6pm local one row, "Close-out
   for 9/25/26 · not filed · Write it →", that opens the form in place; 6pm
   to 8pm the full form.
7. **Results**, collapsed by default (web: one `details.hb-results`; the
   owner's open/closed choice is remembered in this browser). Its closed row
   says what happened — "3 improved · 1 worse · $1,310/mo net measured · 2 of
   3 goals on track" — measured figures only, never an opportunity. Inside:
   the measured value graph, the signal tiles (the trend behind each chip),
   measured (goals, what your changes did with its check-ins), the weekly
   receipts Tuesday to Sunday, the explanation of what connects and of the
   comps / voids flags, what got better, worth, measured alongside your
   changes, last month. The Recommendations page (`#recs`) is reached from
   both.

**Group Home (web, all locations).** Header with the group headline; the
total strip ("9/24/26 · $24,310 net across 3 of 4 locations · +$820 vs
budget · 2 need a look" — one night only, never averaged, vs budget only when
every counted location has one); **Needs attention · all locations** with the
sentence naming the location that needs a look and every item ("+N more" in
place); then the table — Location, Last night (verdict dot, net, vs budget),
Labor, Reviews in one cell, and Food cost / Last active only where there is
room; then How your locations compare. The header's location switcher shows
each location's status dot, what needs you there and last night's net.

**One job, one place.** A job reaches Home from four sources (the focus
card, a Needs-attention row, a brief line, a Still-open row). They share one
de-dup key (web `hbSame`: every "reply to reviews" key → `replies`, low
stock → `stock`, open issues → `issues`, everything else its own key); a
brief line or Still-open row whose job is already in the focus card or
Needs attention is left out. iOS applies the same map.

**Answer in place.** Done, Not for us, Not today, Track, hand-off and publish
take the answered card off the page at once (`.hb-gone`, a 200ms fade) and
re-read only the brief (`hbSoft`), never the whole page; the focus card is
redrawn only when its lead changed. A hide or snooze gets an Undo on its
toast. A write anywhere else marks Home stale (`hbDirty`), so returning to
it re-reads.

Every block keeps its empty state; a quiet day is a short page.

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
| Loading | `.hb-skel`, `.hb-load` + orb, `cbtnBusy()` (the one busy helper; Account's `busy()` is a wrapper over it). A post or send in flight is a busy button, never a full-screen overlay; a header popover opens on `.dr-pulse`, never "Loading…" |
| Collapsed section | `details.hb-results` (Home's Results) / `details.rv2-analytics` (Reviews' trends under the inbox): one hairline summary row — `.hb-kicker` + one line saying what is inside, a `›` that turns when open, `:focus-visible` ring. For proof that sits under the work, not for anything that needs a decision. Content loads on first open when it costs a request (`rvOpenAnalytics`) |
| Bell badge and summary (9/25/26) | `#notif-badge` is a **red count pill** (`--red`, the number face) of the urgent rows nobody has handled (`GET /api/notifications/unread-count` → `urgent`); a 7px `--ink3` dot (`.dot`) when something is unread but nothing is urgent; nothing otherwise. The panel opens on `.notif-sum`, one deterministic line counted from the rows: "**3 need you**: 2 replies ready to post, 1 health mention" (or "Nothing needs you · 4 unread") |
| User menu | `#user-menu-btn` (the owner's name + chevron, `cbtn cbtn-text`) opens `.user-menu` (`role="menu"`): **What's new** (its unread dot on the button and the item, `--ink3` on the button — never ember, it is not a status) and **Sign out**. Sign out is never a bordered button in the header |
| Alert inbox row (bell) | `.notif-item` > `.notif-row` (`cbtn cbtn-text`): the dot (red for an unresolved P0/P1), the label, the time, then `.nx` — the location (`.nloc`, group bell only) and the review's own words in quotes, two lines at most. `.is-unread` until the row is **opened** (not when the bell is), `.is-resolved` greys a handled row ("handled") rather than hiding it. One inline action at most, under the row (`.notif-act`): for a drafted reply, "Read the reply" shows the full text, then "Post this reply" / "Edit it first" — an outward send shows what goes out before the button that sends it. Opens on "Needs you" when anything unresolved is urgent; "Mark all read" at the foot |
| Milestone | `.hb-card.rail.good.hb-mile` at the top of Home's day: kicker (`MILESTONE_KICKER`), the title in Clash, the body, a ✕ (`data-mile-close`). Shown once (marked seen when shown); never a modal over the morning read |
| Direct action on a line | A brief line (web and email) carries the server's `action` {label, nav}: a `cbtn-secondary cbtn-sm` (`data-nav-go`) before the line's "Ask →", which stays as the secondary. In the brief email the action link leads in ember, "Ask about this" follows in muted ink |
| Ask answer | `_askMd`: `## ` → `.ask-h` (15px/700), `1. ` → `ol.ask-ol` (ember numbers in the number face), `- ` → `ul.ask-ul`, the rest `<p>`; every `$`, `%`, number and M/D/YY in `.hb-num`; body 15px. After an answer the panel scrolls to the **top** of the answer (`_askAnchor` / `_askScroll`), never to the feedback row. Under it one `.ask-eva-line`: modules · the confidence line · "N unverified · Why?" (red text button) with the warnings in its drawer. `.ask-panel.wide` (the expand chip, ≥900px) is a 720px side sheet, remembered per browser |
| Ask button | `.ask-fab` — the ember disc (highlight, underside shade, rim, long ember shadow, `askFabHalo` pulse until Ask is first opened in a session — `.seen` stops it) carrying the orb in cream ink: `CavnarOrb.mount(c,'working',{ink:'cream'})`, `searching` under the cursor. The brand colour's one large surface on the page; the label pill stays dark |
| Number badge | `.hb-ledger span>.hb-num:first-child`, `.lb2-cov span>.hb-num` — a 28px ember disc for a count that leads a chip |
| Empty | `.hb-empty`, `.hb-clear` |
| Tool with nothing to show | `.fc2-tool-empty` — a bold one-line state, then what is missing and where it gets fixed. A Load that resolves to a bare sentence reads as if nothing happened |
| Paste-to-draft | `.fc2-menu-draft` — ai tone (`--sf-ai`); a textarea, one secondary button, an `.ac-status` line that carries the result and the next batch |
| Escaping | `esc()` / `num()` (numbers → `.hb-num`) |
| Command palette | `#cav-palette` (`.cpal`, `<script id="cav-palette-js">`, `cavPalette.open()`) — ⌘K / Ctrl+K from anywhere, `/` when not typing, and the `⌘K` hint in the header (`.hdr-cmdk`). A 560px floating panel over `--scrim` at 12vh; the field (`Apfel` 17px), grouped rows (`.cpal-g` kicker, `.cpal-row` = `cbtn cbtn-text` with label, `.s` sub-line, `.t` tag — ember when it sends), a key-hint footer. Empty = the day: Waiting on you (`/api/actions`), One tap (Home's quick actions), Go to, Locations. Typing = Actions, Go to, Found (`/api/command/search`), and always last "Ask Cavnar: “…”" (Tab). An action opens its confirm card in the panel (tier 2 of §10); nothing else in it acts. Phone width: full-bleed, no footer, no header hint |
| Modal | `cModal.open(id)` / `cModal.close(id)` — Escape closes the top one (then the Ask panel, then a header popover), a click on the backdrop closes it (not the two-factor setup), focus returns to what opened it, one stacking level (`.cmodal` → `--z-modal`). Older modals are adopted by id (`CAV_MODALS`) and close through their own close function. Busy overlays (posting, upload) are not modals |
| Inline field | `cField(anchor, {label, value, placeholder, hint, inputmode, save, after, onSave(value, done)})` → `.cav-field` under the anchor's row (`.cav-field-row`, `.ac-row`, `li`, `tr`) with the current value in it; Enter saves, Escape cancels, `done(msg)` keeps it open with the line in `--red`. Never `prompt()` |
| Undo toast | `toast(msg, type, {label: 'Undo', fn})` → `.toast.has-act` with one `.toast-act` text button; `cavUndoable(msg, commit, restore)` for a delete with no server inverse (§10, tier 1) |
| Confirm card (any surface) | `cavPropCard(p, host, {onDone})` — the `.ask-prop` card on Home (`.hb-pub-confirm`), in the palette (`.cpal-card`) and in Ask, one renderer; Confirm (`[data-prop-confirm]`) posts only `fields_shown` and records the answer on `proposal_id` |
| Staffing board | `.sb` (Labor → Every day and person, from `labor.staffing_board`, 9/25/26): decision cards, not a table. An executive strip first (`.sb-tile` ×3: at stake in the window, costliest, fastest win), then one `.sb-lane` per kind toned by `--tc` (overstaffed ember, strong days run lean amber, overtime red) with an icon, title, one-line meaning and a count. Each `.sb-card`: a severity chip (`.sb-sev`), a measured consistency ring (`.sb-ring`, the share of that weekday's days in the window with the same pattern; "—" under two), who/when, one dollar figure in the lane tone, one sentence of what happened and what to do, fact chips (`.sb-chips`, a green `.ok` chip for a teammate with room), and actions under a hairline: Explain why (the arithmetic, in the explain modal), Plan next week, Ask Cavnar AI. The worst card in a lane is `.worst` (lifted, ringed in its tone). Six cards show, the rest behind Show all. Figures drop a trailing .0 (`labor._n1`). Motion: staggered rise, hover lift, ring draw-in; none under reduced motion. Nothing here is estimated beyond hours × the blended rate |
| Section rail | Removed 9/25/26 (owner's call): the row of truncated section links under the tabs read as stray orange text. A section is reached by its own heading, a `data-nav` deep link or ⌘K; a module that pairs two sections names them in its kicker (Reviews: `INBOX \| ANALYTICS`). Account keeps its own `.ac-nav` rail |
| Sticky chrome | `.tabs` sticks under `.hdr` (`top:56px`); `--cav-chrome` (header + tabs, measured) is what anything sticking or scrolling into view clears (`[data-nav]{scroll-margin-top}`, `.ac-nav`) |
| One tap | `#hb-quick` (`.hb-quick`) under Home's header — the server's `quick_actions` as `cbtn-secondary cbtn-sm` buttons (Home's one primary is the focus card's), each landing on its `nav`, less any whose action a Needs-attention row already carries (`hbQuickUnsaid`; the palette's One tap keeps them all); Ask and All alerts are left to the FAB and the bell |
| Data health (how current the data is) | A composition, no new pattern: Home's header chip "Data 71% · as of 9/22/26" (`hbDataChip`, in the sub line) and, when a source is behind, the `.hb-fresh` strip's kicker "DATA HEALTH 71% · DATA AS OF 9/22/26" are `cbtn-text cbtn-muted` buttons (`data-dh-open`) that opens the explanation modal with the Why? panel's `.cf-p` rows (one per source, a `.dh-dot` in the row's tone, reliability and "next sync" as its detail line), "Not connected" rows with the one fix, the module confidence-impact rows, and a `cbtn-primary` Sync now with the `.dr-pulse` bar while it waits. Under each module header one `.dh-badge` holding a single `.hb-fresh-it` chip (the weakest source's line) + a `cbtn-text` "Data health". Amber (`--amber`) for a stale or failing line (`.dh-line.warn`), never red. iOS: `HomeFreshnessStrip`'s kicker opens `DataHealthSheet` (built on `AccountSheetKit`: hero % + sections), `DataHealthModuleBadge` under a module's own freshness line, `ServerStatusCaption` for a server status line (Reviews' fetch line, Marketing's "Metrics synced", AI visibility's "Measured") |
| Recommendation answer row | `.rec-ans` via `recControlsHtml(key, surface, module)` (JS) or `client_api.rec_controls_html` (server-rendered insight HTML) — Done / Not for us / Track as `cbtn-text cbtn-inline cbtn-sm` with `data-rec-key`; one delegated listener posts `/api/recs/event` and swaps the row for a muted `.rec-ans-done` sentence. Home's `.hb-rec-ans` answer row, inline under a module's recommendation line (`.rec-line` under a read, `.rec-cites` for the reviews an Intel recommendation cites). An answered line is not rendered again anywhere. What the answer did is the server's: a tracker that started reads "Measuring labor % until 10/21/26" (`recTrackerLine`), a refused one its `tracker_refused.reason`; Home's toasts say the same. `recNotForUsHtml(key, surface, module)` is the lone "Not for us" for a line whose own button is its yes (an overtime move, a content idea, a roadmap card) |
| Why not? (reason picker) | `.rsn` via `recReasonPicker(after, {onPick(code, note, picker), onCancel, skipLabel})` — ONE component behind every "Not for us": module lines (the `[data-rec-key]` listener), Home's cards (`hbAskWhy`), the second ✕ on Needs attention ("Just hide it" as its skip), content ideas, roadmap cards, overtime moves, Ask's "Not now", the brief's lines, the win-back text's "Not for us" and the schedule review's ✕ (both send `reason_code` + `reason` with their own routes). A recessed strip under the line it answers: `.rsn-k` "Why not?" kicker (ember2) + one-line hint, `.rsn-opts` the six `rec_ledger.REASON_CODES` as `cbtn-secondary cbtn-sm` in the owner's words (Already doing this · Doesn't fit us · Too costly · Bad timing · Don't trust the numbers · Other), `.rsn-ft` an optional note `.ac-input` (Enter with a note = Other) + Skip + Cancel. One tap on a reason answers; the caller posts `reason_code` (+ `reason`) |
| Check-in card | `.rck` via `recCheckinHtml(c, surface)` over `recCheckinCandidates(outcomes, timelineItems)` — a result that landed (`status` evaluated, a clear verdict) with no `owner_checkin`, joined to its recommendation by the timeline's `tracker_id`. Ember left rail on `--hb-tint`, "Check in · result landed 8/12/26", the title, the `result_line` in the number face, "Did you make this change?" Yes / Partly / No (one tap posts `POST /api/recs/checkin`), then — after Yes or Partly — "Did anything else change these weeks?" No / Yes, something else changed (Yes re-posts with `conditions_changed`), then the result's new `attribution_label`. "Not now" hides it in this browser. Home shows one, inside "What your changes did"; the Recommendations page shows up to five |
| Confidence | `cavConfLine(confidence, {key, surface, module, cls})` (`<script id="cav-conf">`, global, `window.cavConf`) — ONE line: a 38×6 meter (`.cf-m`, the tone colour with a sheen and a soft glow, `cmBar` grow-in; hatched `.none` when the overall is not measurable; no meter at all for a band from an older server), "**72% confidence**" (digits in the number face), "— the weakest dimension's basis" in ink3, and a **Why?** `cbtn cbtn-text cbtn-inline cbtn-sm` that opens the explanation modal (`data-explain`, logging `evidence_viewed` for `key` on its own `data-explain-surface` / `-module`). The modal body (`.cf-p`, the modal's own dark tokens) **leads with what the figure means** — `.cf-p-mean`, the payload's `meaning`, "How well supported this is — not the chance it works." (the owner's support-score decision, 9/24/26: never present the % as a probability) — then the overall figure large with a wide meter and the reason (the cap that set it, when one did), the caution (amber), then **What it rests on**: Evidence strength NN% with "Sample: 12" (of the full sample when the server sends `n_full`) · Historical accuracy NN% — the **lift against doing nothing**: its basis is the lift sentence ("improved 4 of 6 times vs 1 of 6 when not acted on" / "vs about 5% by chance") and its detail "93% likely to beat doing nothing · improved-rate range 12–76%" (`beats_label`) — or "—" with "Not enough history yet (2 measured, needs 5)" · Data freshness NN% ("as of 9/23/26", never ISO) — each with its meter and basis — and the footer ("the weakest pulls it down most"; without a track record "it stays at 70% or below"; a record that doesn't yet beat doing nothing "it stays at 70% or below"; one that leans against it "holds it at 49% or below"; on stale data "Data under 50% fresh holds it at 49% or below"; undated data "holds it at 49% or below" — every ceiling from the payload's `caps` / `caps_applied` when sent). A FACT (reviews or drafts waiting, a failing sync, a count) carries no confidence line at all. The **caution rides on the line** (`.cf-c`, amber) as on iOS, not only behind Why? (`noCaution:1` for a place that shows it elsewhere). Only a measured object draws (`cavConf.k1(detail, legacy)`): a bare band or a model's own word draws nothing, never "Low confidence". Sample or demo data (evidence 0 with nothing counted) reads **"Confidence not yet measurable"**, never "0%". Tone everywhere: **the payload's `band` — high `.good` green, medium `.mid` ink2, low or not measurable `.warn` amber — never red, never ember**; a dimension row reads the payload's `thresholds` {high, medium} when sent, else `cavConf.AT` (held to `confidence_engine.HIGH_AT` / `MEDIUM_AT` by a test). Percentages stay (the owner's call); every one is the server's (contract K1), a missing one is a dash. On: Home cards, Needs attention rows, the focus card, the review / food / labor / marketing diagnoses, food drivers, Ask answers, daily-report actions |
| Claim tag | `cavConf.claimTag(kind, modelWritten)` → `.ck-tag` — tiny uppercase ink3 on the recess: "AI-written" (beats the kind), "Measured", "Computed", "Forecast", "Inferred", "Estimate"; also "checked" / "unchecked" / "few reviews" as the same quiet shape. `cavConf.claimStrip(claim_kinds, names, modelWritten)` → `.ck-strip` groups a payload's `claim_kinds` by kind under a read ("AI-written read by Cavnar AI · Measured this week, severity · Inferred what to watch · Forecast next week"). Never a status colour |
| All clear | `.hb-clear` "All clear — Cavnar AI is watching." with the green check only when every source under Home is current and `monitoring.all_clear` is not false (`hbAllClear(d)`); with a stale or undated source it is `.hb-clear.warn` (amber, no check) "Nothing flagged — but N sources are out of date or undated, so this is not a clean bill", and with no live source at all it says there is nothing live to watch. The focus card's last fallback obeys the same rule (kicker "Watching", `hbNotClearWhy`). iOS: `AllClearRow(notClearReason:)` over `OwnerCopy.allClear` |
| Older read | `aiCaveat('Older read', stale_note)` above an AI read the server served from cache because a new one failed (Labor, Marketing) — kept through the five-minute session cache. iOS: `CavnarCaveat.olderRead` under the strip / above the read |
| Trend strength | A rating or waste trend says its measured strength, "trend strength 62%" (`trend_strength_pct`) with its weeks — never "medium confidence" / "early read". An older server with no figure shows the weeks alone. iOS: `ReviewsAnalyticsSection.trendStrengthLabel` |
| Reliability bar (admin) | `.calbar` in `admin.html`'s Confidence calibration card (`confidenceCalibration(GET /admin/api/calibration)`) — a 0–100% track, the observed rate's 90% range as a gradient band (green when the shown figure's ember2 tick sits inside it, amber when not), the observed rate as a glowing dot; rows under `floor_n` dimmed and not read. Beside it the Brier score and the same table by kind and per dimension |
| Freshness strip | `.hb-fresh` under Needs attention, drawn only when a source is aging, stale, undated or disconnected (the header's one "Data 92% · as of 9/24/26" chip says it otherwise, density fix #21) — "DATA AS OF 9/22/26" (`data_as_of`) then one `.hb-fresh-it` pill per `freshness[]` source: a state dot (`.current` green glow, `.aging` / `.stale` amber, `.off` "not connected", `.unknown` "age unknown", `.sample` hatched), the name, its basis ("POS synced 9/23/26") and its % in the number face. `hbLive` reads `monitoring` ("Monitoring 4 live sources · oldest data 9/20/26") and never says "just now" about data that isn't |
| How you compare | A composition, no new pattern (Benchmarking #23 / #18 / #19; `benchmark_views` over the Benchmark Engine): `section.hb-card.bm-card[data-bm-module]` on Labor, Food Cost, Reviews and Marketing, filled by `<script id="cav-bench">` from `/api/benchmarks/card` — `.hb-kicker` "How you compare", who in ink2 ("Compared to 11 other Pizza restaurants on Cavnar" / "vs your own previous 13 weeks"), as-of under it, the **comparison strength as the confidence line** (`.cf` meter + "68% comparison strength" + a Why? whose panel is the `.cf-p` drawer with four rows — Peer count, Band freshness, Your own figure, Type match — and the meaning first: how well supported the comparison is, not how well you are doing), below the minimum the fixed sentence "Not enough restaurants like yours yet — here's how you compare to your own last 13 weeks." with the engine's reason under it, then one `.hb-row` per metric (dot: `.good` green ahead, `.important` amber behind, `.watch` grey level — never red, never ember), the value in the number face, the standing in words, "vs … (middle) · N% comparison strength · as of M/D/YY", and for a metric it is behind on one `cbtn-text` Ask action (`data-ask`). Home: `#hb-bench` under the freshness strip, the same rows as `.hb-fresh-it` pills, behind first. Group Home: "How your locations compare" (`cavBench.locations`), one `.hb-row` per location per metric, "in line with your other locations" unless the gap beats both locations' own swing. Strength is always a %; a band comparison is the only kind that has one. **Density round (9/25/26, #34):** a row reads label, value and standing in words only; the who line, the strength line (its Why? panel still opens from there) and each row's basis and dates sit in one `details.bm-why` "Why? · who, how strong, as of when" under the rows, and a row's strength is left out when it equals the card's. One slot per module: after the module's own read (Labor and Food Cost under the AI read, Reviews in Trends after the diagnosis, Marketing under the brief) |
| Diagnosis block | `.diag` via `renderDiagnosis(id, dg)` — cause, the confidence line (`.cf.diag-cf`), also fits / what would tell them apart / evidence pills. One shape for labor and marketing; Reviews (`.rv-diag`) and Food Cost (`.fc2-cfo`) put the same `.cf` line in a "How sure" row and an "AI-written" `.ck-tag` in the header, and a verified cross-check carries a "checked" tag. The per-module pills (`.rv-conf`, `.fc2-conf`, `.diag-conf`) are gone. Renders only when `dg.cause` exists |
| Model-output caveat | `aiCaveat(title, detail)` (`.ai-caveat`, prepended to the read) — one shape, four titles: "Unverified numbers" (a figure that didn't trace, `applyFigureCaveat` on `figures_verified:false`), "Unsupported cause" (`causes_verified:false` — the sentence stays, said to be a guess, the first `unsupported_causes` quoted), "Unverified" (a structured `UNVERIFIED:` note from `ai_guard.unverified_note`, said whole), "Older read" (a stale fallback, the server's `stale_note`). Never deletes the model's text |
| Forecast record line | `fc2AccLine(accuracy, what)` — one sentence for any `forecast_log.accuracy` payload (K8): "past waste forecasts here have been close (6.2% mean error over 5 weeks), running 7.5% high on average"; `withheld` says the next one is held back and why; below the scoring floor it repeats the server's reason ("1 closed month scored, needs 3") and shows no figure. Sits in a `.fc2-row alt` / `.sr-note`, numbers in `.hb-num` |
| Calibrated dollar figure | When a recommendation carries `dollars_adjusted` (rec_learning.attach_dollar_calibration), the figure shown is the adjusted one and a plain `.meta` span beside it says `calibration_note` ("adjusted from 6 measured results"); the stated figure moves to the title. `dollars_adjusted: null` → the stated figure as before. Home cards (`hbRecDollars`), the one-thing hero, DSR actions |
| Result baseline line | Recommendation record results: the attribution sentence, then one `.x` line — "Compared with <baseline phrase>; normal variation is <band_basis>" — and a `.rh-pill.partial` "Not counted" when `baseline_overlaps_trigger`. The check-in card says the `grade_phrase` before it asks |
| Recipe draft flags | `.fc2-recipe` header carries an "Estimate" `.ck-tag` (`is_estimate`) or "From your card"; a line whose unit didn't convert (`unit_ok:false`) reads as `.low` with its `unit_note` in the title; `unit_warnings` and a two-plus `confidence_levels` legend as `.fc2-recipe-note`; `needs_yield` puts `.fc2-recipe-yield` (a number `ac-input`, "Plates this batch makes") at the left of the footer and Accept sends it as `yield`; `unit_skipped` is named in the toast |
| Per-check dots | `.in2-qc` (`.on` lit ember, `.off` dim) in `.in2-q` rows — one dot per run, oldest first, with an `n/asked` count |
| Account overview (9/25/26) | The hero carries the plan chip only, then `.ac-say` — one deterministic sentence from the six health items ("**2 things need you:** your POS stopped syncing; nobody receives alerts.") — and `#ac-fix-btn`, the page's one primary: the worst item's fix (Integrations → Notifications → Subscription → Security → Profile → People). Everything else is under a quiet `details.ac-more` "More". The ring (`.ac-ring`) is toned by the score: green 90+, amber 60–89, red under 60 — never ember. Account's helper text (`.ac-h p`, `.ac-card-h p`, `.ac-row .l span`) is 13.5px ink3, and a row explanation over eight words sits behind `.ac-info` ("i", `aria-expanded`). A primary button only for the open editor's save. Integrations opens on **Data health** (`#acct-dh-card`, the drawer's `overall.pct`, which also drives the health grid's Integrations item); once a POS is live the others fold into one **Switch POS** row. Rail: Overview, Restaurant, Daily report (owner only), People, Billing, Notifications (led by the Calm / Normal / Everything `.dr-views` dial), Automation, AI & memory (memory, decisions and trust live here), Integrations, Security, Data & privacy, Support (one card; Help & FAQ, Report a problem and Refer an owner open from it). Billing shows "Measured, net: $X/mo · N changes measured" or "Nothing measured yet" under the amount — the measured figure only |
| Staff portal | `staff_portal.html` on the DS faces (Apfel Grotezk, Clash for the name, Space Grotesk `.num` for every time and count) and DS token names. Schedule tab: `.hero` — the next shift ("Today" or the weekday and M/D/YY), the time at 30px, role · hours, "2 tasks left today" and "1 swap waiting on you" — then **Asked of you** on an ember rail, the brief, working days as cards (each shift's Can't work / Swap in a `…` menu), the days off as one `.offline` line, requests and open shifts, and one closed **Availability & time off** disclosure. The Tasks tab label carries "· N left" and a `.pbar` |
| Decision row | `.ac-row` + `.ac-chip` answer (`done` / `not for us` / `tracking` / `measured`) — Account → What you've decided, whose foot links to the Recommendations page |
| Brief line answers | Home's "Before service" rail (`.hb-tl .it`): a line with `rec_key` and `answerable` carries `recControlsHtml(rec_key, 'home', <the line's module>, {noTrack: 1})` on its own row under the text (`.hb-tl .it .t .rec-ans`) — Done / Not for us, never Track (a brief line names a move, not a number). The reason picker opens under the whole line (`recPickerHost` knows `.hb-tl .it`). A line the server marks not answerable (the money ranking, owed replies) has none |
| Period scheme picker | Account → Daily report: one `.ac-select` of 13 × 4 weeks · 4-4-5 · 4-5-4 · 5-4-4 · Custom (from your accounting system). A stored list of lengths shows as Custom with the lengths in the `.ac-inline` field under it (and its own Save) — a select never silently matches nothing. "Gross sales means" is a second select (items at the price rung / everything rung). "Years that run differently" lists each listed year (`.as-dsr-y`: "Fiscal 2027 from 12/30/26 · 53 weeks · 4-4-5-…", Remove) with a date + lengths + "Add year" row |
| Sales definition | Under the Sales block's loss tiles: "Gross is … Net is …" from the night's own `definition` (`gross`, `net`) — the basis it was built under, not today's setting. The Tax collected tile's caption follows it: "in gross, not net" under everything rung, "not in gross or net" under items. iOS: the same tile and note |
| Ask confirmation card | `.ask-prop` — the one-line summary, `.stake` (dollars), `.dl` details, `.pv` the words that go out, then **every field the confirmed route receives** (NS5 C1): an `.ask-sug-k` kicker "Sent with this" over a second `.dl` built from the proposal's `fields_shown` (label / value, nothing hidden). Confirm posts only those keys (`_askShownBody`). iOS `ProposalCard` mirrors it: the same "SENT WITH THIS" kicker (ember2, tracked caps) over label/value rows, and `AskProposal.postedBody` |
| Ask suggestions and rating | `.ask-sug` under an answer — an ember left edge, `.ask-sug-k` "Suggested in this answer", one `.ask-sug-i` per `suggestions[]` item with its `recControlsHtml(rec_key, 'ask', 'ask')` row; `.ask-fb` "Was this useful?" Yes / No (text buttons, `POST /api/ask-cavnar/feedback` with `message_id`), a No then offers one optional "What was missing?" line (`.ask-fb.note`: the bubble's width, field and Send on one line) |
| Rules check | `.sr-panel` via `renderScheduleReview(d)` — `.sr-head` kicker + `.sr-chip` counts (`bad` hard / `warn` soft / `good` clean, `.sr-meta` for generation time), `.sr-line` rows (`.warn` for a ⚠ line, `.fix` for "Ana → Bob", `.bad` for what still needs a human), `.sr-soft` for a warning that is not a block, `.sr-actions` for the one fix button. Re-rendered from `POST /api/labor/schedule/violations` after every edit |
| Explanation drawer | a `tr.sched-why` under a clicked `tr.sched-row` — `.k` kicker "Why <name>" on the ai tone (`--sf-ai`), then the engine's own `why` sentence, or "No facts on file for this assignment." Never a hover tooltip: the reason is a paragraph |
| Flagged table row | `tr.needs-review` — amber wash and a 3px `--hb-warn` rail, `.rr` reason under the name. The rows the rules check names and the panel's counts must always agree |
| Inline table edit | `tr.sched-edit` replaces the row: `.ac-select` for who (legal replacements first, "· can take it"), `.ac-select.tm` for times in 15-minute steps (a stored time off the grid stays selectable), `.ac-input.rl` for role (filled from the person picked), Cancel (secondary) + Done (`cbtn-soft`); "+ Add a shift" defaults to the day and times of the row being looked at. Edits stage into `#sched-edit-bar` (`.on`) with Discard + Save; the send button stays live and saves them first (Friction #16/#44) |
| Publish gate | ONE send button in the draft's header (`#ps-send-btn`, `cbtn-primary`): "Send to N staff", "Save & send to N staff" with unsaved edits. `#ps-blockers` via `_psRenderBlockers(list, arm)` lists each blocker as `.sr-line.bad` under "Read before this goes out" (from `labor/publish-check` for the saved week, or a `409 needs_ack`), and the same button becomes `cbtn-danger` "Send with N rule warnings…" — that press is the acknowledgement; any edit disarms it. `#ps-reach` under it says who each channel reaches ("Reaches 14 of 16 · 3 in the app, 2 by text, 9 by email") with a Fix contacts text button for the email drawer. Home's "Send now" answers a `needs_ack` in place the same way (`.hb-chk-ack`). A 403 shows the server's sentence in `--hb-bad` |
| Draft verdict | `#sched-verdict` — one `.sr-note` line in the draft's header: "Every rule kept · 412h vs 420h PAR · quality 82" (hard/soft counts when broken), digits in the number face. The grid opens with the draft; what changed, PAR, the economics and Shift Quality sit under `details.co-more#sched-details` "Details". Download CSV is a `cbtn-text` in the table foot (Friction #19) |
| Waiting on you | A composition, no new pattern (Friction #18): `section.hb-card#lb2-wait` at the top of Labor, `.hb-kicker` then `.ac-row`s — a drafted week (Open it / Send to N staff), each pending time off and shift request (Deny as `cbtn-text`, Approve as `cbtn-primary`), and, after an approval that touches the open draft, "Redo these days". Hidden when empty; `data-nav="labor/requests"` |
| Module Today line | `.mod-today` under a module's h1 — the ember `.k` kicker "Today", then one deterministic sentence built from counts and stored reads the page already holds (Labor: waiting count, the costliest day to trim, who is in overtime; Food Cost: the top CFO driver, what is running out; Reviews: what to answer, the top complaint; Marketing: what goes out next; Intel: ways to improve open; the counts-only view: last counted, deliveries to receive). Never a model call; hidden rather than guessed when its source has not loaded (density round #48) | Every module page |
| Neutral chip | `.hb-chip.neutral` — no dot, no glow. For a constant (a target, a balance, a count of places tracked): a green dot beside a number that cannot be good or bad makes the real green dots mean less (density round #31) | Labor's target, Food Cost's inventory value, Intel's tracked, Marketing's chips until they carry a direction |
| How this works | `details.how-this` — one muted summary line and a `›`; the helper paragraph opens under it. Helper copy is one line at most; anything longer goes here (density round #45) | Labor's Heads up, the AI-visibility footer, the receipts help, the counts-only instructions |
| Where the money went | `section.hb-card.lb2-went` directly under Waiting on you — the three costliest overstaffed days by dollars above target (`.hb-row.important`) and who is in overtime with the premium (`.hb-row.critical`), each figure an opportunity or a premium, never "saved"; "Show all N" opens `details#lb2-money-all`, the full lists as `hb-card` + `hb-row` (the dark slabs are gone) | Labor (density round #9, #44) |
| Team & rules | `details.hb-results.lb2-team` — Labor's setup rows as one quiet list (`.lb2-srow`: a hairline between rows, no per-row ember). Availability lives inside Roster & settings. The schedule container is the plain card surface; the ember is spent on Generate | Labor (density round #28) |
| Why line | `.rv2-why` above the Reviews inbox — the rating and its direction over the last 4 weeks against the 4 before (only past 3 reviews a side, else not stated), the top complaint from the stored diagnosis, and "See why →" into Trends. `/api/reviews/why-line` (`review_intelligence.inbox_why`); no model call | Reviews (density round #32) |
| Answered divider | `.rv2-answered-div` — "Answered (N)" above the trailing run of answered reviews; the server sorts posted / approved / skipped below everything still waiting (`models.get_reviews_data`) | Reviews inbox (density round #33) |
| Person sheet | The explanation modal's frame (`#person-modal` / `#person-inner`, dark in both themes) holding one person (Friction #25): kicker role, title name, then `.sr-k` groups — Contact, Hours, Availability (seven `.ac-select`s), Rating and skills (certification chips as `cbtn-primary`/`cbtn-secondary` toggles), Pay (read-only, links to Account), Login (owner only: new PIN), On the roster (`.ac-switch`). Every control saves on change with one `.ac-status` "Saved". Opened by any `[data-person-open="name"]`; Escape, the backdrop and Done close it |
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
| Provisional score | `.sq-prov` amber pill "provisional" beside the band when `quality.confidence.level` is low, and `.sq-prov-why` — the confidence's top reason — under the completeness pill. The pill (`.sq-comp`) is the read's measured completeness as its percentage, **"Read completeness 45%"**, never a band word; when the server sends the panel's own K1 `confidence_detail` the pill is that confidence line instead, so the panel and its items read from one engine. Each recommendation item carries its own K1 line (Why? logged on `schedule_review`). iOS: the `PROVISIONAL` capsule, the same completeness pill (`QualityConfidence.completenessLabel`) or `ConfidenceLine`, and the amber reason line; each recommendation's `ConfidenceLine` under it. The history list's `.qpill` reads "84 · provisional", its title the completeness % when the row carries it |
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
| Recommendations page | `#panel-recs` (`rh-*`), a Home sub-page like the Daily report (no tab; Home stays lit; hash `#recs`), reached from the worth section's "Your recommendations →", "Measured alongside your changes", Home's "N to check in on →" and Account → What you've decided. `.rh-nav` "← Home" + a `.dr-views` 30 / 90 / 180 days toggle; `.rh-head` kicker "Your record" + Clash `.hb-h1`; then **Check in** (`.rck` cards — first, when there are any: the one thing on the page only the owner can do, 9/25/26), the **record strip** (`.hb-stats.rh-strip`, three `.hb-stat` tiles from `summary.totals`: Taken "6 of 14", Measured better "3 of 5" (`.good` at half or more), Open now; a rate below its floor is "—" with "6 of 10 settled — not enough yet"), **Measured alongside your changes** (the page's one `.hb-card.hero.hb-focus`: the first sentence as the `.lead`, the rest as `.why`; not enough → `.hb-card.quiet` saying what would fill it), **What you followed** (`.rh-mod` per area, see Accept-rate bar; `.rh-best` "Most often followed by an improvement: Weekend staffing — 5 of 6 measured results improved" only when `most_effective` (never "effective": it is a before/after, not a cause)), and **Every recommendation** (`.rh-tl`, see Record timeline) paged with "Show older" (`next_before`). Every rate carries its denominator; below `enough` it says "Not enough yet · 6 of 10 settled", never a number |
| Accept-rate bar | `.rh-mod` — area name + "N shown", a 10px `.bar` with the ember→ember2 fill (`cmBar` grow-in, glow) and its 90% interval as a translucent `.rg` band behind it, the rate in the number face with "likely 46–83%" under it, and `.c` "10 taken · 2 declined · 3 left unanswered · 1 still open". Not enough: a hatched `.bar.none` and "Not enough yet" in the text face |
| Record timeline | `.rh-tl .rh-it` — a rail with a dot per recommendation (`.taken` green, `.declined` amber, `.open` ember), the title, the local M/D/YY it was first shown, `.rh-ans` answer pill (Accepted / Done / Made the change / Declined / Snoozed / Left unanswered / Replaced by a newer one / Open), the area and surfaces, "Why: Too costly — "short staffed"", and `.rh-res` for its tracker: while measuring, "Labor % until 10/9/26" + a `.rh-pill.partial` interim line ("12 days in … A hint, not a result.") + "Stop measuring" (`POST /api/outcomes/<id>/abandon`); once measured, the `result_line`, the `attribution_label`, "Also changed in those weeks: …", `.rh-pill.valid` Validated / the re-check date and verdict, and `.rh-pill.you` for the owner's check-in |
| Daily report page | `#panel-dsr` (`dr-*`), a Home sub-page with no tab of its own (the Home tab stays lit). `.dr-nav` = "← Home" text button + the `.dr-views` Night / Week / Period toggle; `.dr-head` = `.hb-kicker` + Clash `.hb-h1` (weekday + M/D/YY) + prev/next night buttons; `.dr-meta` = status pill, version `.ac-select`, provenance ("Closed by the POS", "Went out 9/23/26 · 7:10am"), and the page's one action. Hash routes `#dsr`, `#dsr/YYYY-MM-DD` (the email link, with `?rid=` naming its location), `#dsr/week/…`, `#dsr/period/…`. iOS: the same order as expandable cards |
| Segmented view toggle | `.dr-views` — `cbtn cbtn-secondary cbtn-sm` buttons with `aria-pressed`; the pressed one takes the ember hairline and `--hb-tint`. For switching views of one thing, never for filters |
| Status pill | `.dr-pill` + `.final` (green) / `.provisional` / `.awaiting` (amber) / `.failed` (red) / `.running` (ember, with a breathing `i` dot) / `.est` (quiet "Estimated"). Uppercase 11.5px, pill radius, the `-bg` token behind the status colour |
| Older version | `.hb-card.rail.dr-old` — "You're reading version 1 of 2, as it went out at …" + "Read the latest". An older version is shown exactly as it went out; the same strip holds the owner's "Re-run" confirmation |
| Morning read | `.hb-card.dr-read` — the executive (owner) or operations (manager) summary; `.lead` at 19–23px Clash because it is a paragraph, not a headline. It is the hero (`.hero.hb-focus`) only when there is no Today's score — the manager's report, or an owner night that couldn't be scored. With no narrative: `.hb-card.quiet` with the server's reason, never an empty box |
| Calls | `.dr-calls .dr-call` — recessed tiles, an `--ember2` kicker ("Staffing", "Guests", "Biggest opportunity"), the sentence; at most two, never one that restates a win, a risk or a priority (`access.insights`) |
| Block section | `details.dr-sec` — the ember-triangle disclosure (as `.co-more`) on a card; `summary` = Clash `.ttl`, `.src` source, a `.dr-pill` only when the block is not ready, a one-figure `.sum` on the right. A block that isn't ready shows its reason (`.dr-why`), never tiles of zeros; a block the view withholds is named once in `.dr-withheld` |
| KPI hero | `.lb2-hero.dr-hero` — the report's one big graph (and the only cursor light): `.big` figure in the number face, `.subl` lines (gross, or "Gross not measured — the POS didn't report voids" when an everything-rung night is missing its tax or voids; never the items figure in its place), `.dr-cmp` comparison rows (`.l` label, `.p` signed % green/red, `.b` the baseline or why there is none), then `.dr-hours` |
| Hour bars | `.dr-hours .c` — HTML bars (crisp at any width) on an ember→ember2 gradient at 62%, the peak hour at full strength with a glow and its figure above it (`b`), hour labels in the number face; grow-in staggered 45ms, still under reduced motion |
| Share bars | `.dr-bar` — name, an 8px track with an ember gradient fill (`cmBar` grow-in), dollars, share; `.un` for an unmapped department (ink3, labelled "unmapped", never folded into a category) |
| Progress checklist | `.dr-prog` card with `.dr-pulse.wide` (the sliding ember pulse) over `.dr-step` rows — `.done` green tick, `.now` breathing ember dot, `.gap` amber "!" with the block's reason, `.todo` dim — each with the server's own local time (`.at`, "10:14pm"). Polled every 5s, skipped while `document.hidden`, caught up on `visibilitychange` |
| Weekly grid | `.dr-gridbox` (its own horizontal scroll, `.fc2-table-scroll`) around `table.dr-grid` — a `tr.grp` group row (Sales by category · Sales · Budget · Last year · Labor · The day, `--ember2`), right-aligned tabular figures, the day column sticky (`.day`, status dot, the date a text button to that night), `—` in `.dr-dim` for anything unmeasured, `small` under a variance for its %, `tfoot` week and period-to-date rows. A total's gross cell carries a `small` "6 of 7 nights" when gross was measured on fewer nights than net (`gross_days < days_measured`) and "mixed bases" when the range adds nights built under both gross bases (`gross_mixed`), with one note under the grid saying so. Budget columns only in the owner's view. The .xlsx export (`dsr.xlsx`) has the same columns |
| Week budget editor | `.hb-card.dr-bud` (hidden until "Edit budget") — `.dr-bud-grid` of night · gross · net `.ac-input`s, one `cbtn-primary` Save and a Cancel; blank means no budget |
| Import card | `.hb-card.quiet.dr-imp` — what the import is for, a template link, a file `.ac-input` + secondary button, an `.ac-status` result line and `.dr-imp-err` rows the server rejected. Once the week has a last-year figure it starts hidden behind a `.dr-imp-link` text button |
| Last night card | `.hb-card.dr-home` at the top of Home's day — "Last night · 9/22/26" + status pill, net sales in the number face (or why there is none), the summary's lead clamped to three lines (or why there is none), "Open report" / "The week's grid" |
| Record card | `.lb2-srow.s8` "What the record says" — `.int-rev` (projected weekly revenue in the number face + `.src` basis), `.int-tbl` weekday × daypart outcomes (`.cell.trbl` carries a red rail and a `.flag` "troubled"), `.int-bars .int-bar` sales-per-labor-hour bars (ember gradient, glow, `cmBar` grow-in), `.int-cols` two columns of `.int-ledger` (most / fewest weekends and closes) and `.int-say` sentences ("Ana keeps asking to drop Sunday nights"), `.rst-up` trained-up chips, the suggested-pairs block, `.int-supp` for hidden recommendation kinds. Every section has its own `.hb-empty` sentence |

**iOS** (`Features/…` + `DesignSystem/`)

| Need | Use |
|---|---|
| Card | `.cavnarCard()` |
| Sheet | `.accountSheetChrome(title)` + `cavnarTitleToolbar` |
| Sheet anatomy | `AccountHero` → `AccountSection` → `AccountKVRow` |
| Pill / chip / tile | `AccountPill`, `AccountChip` (`tint:` a status colour for a chip that states a status — the daily report's urgency chip is amber for today, ink otherwise; never the ember), `AccountStatTile` |
| Button | `CavnarPrimaryButtonStyle`, `CavnarSecondaryButtonStyle` |
| Recommendation answer row | `RecAnswerRow` (`DesignSystem/RecAnswerRow.swift`) — Done / Not for us / Track as small text buttons, POSTs `/mobile/api/recs/event`, then a muted confirmation line (the server's `message`) and, when a tracker started or could not, a `RecTrackerLine` under it. Track only where `RecAnswer.trackableModules` (reviews, food, labor — never marketing or intel). The same row under every recommendation the phone shows: module reads (Reviews, Food Cost, Marketing, Intel, Labor), diagnoses, the Daily Report's Tomorrow actions (surface `dsr`), Home's one thing, What connects, loss flags and the brief's answerable lines (surface `home`; the brief's without Track), content-calendar ideas, the AI-visibility roadmap, Ask's suggestions |
| Why not (reason picker) | `.recReasonDialog(isPresented:title:message:skipLabel:onSkip:onPick:)` (`RecReasonDialog`) — a confirmation dialog of the six `RecReason`s in owner wording ("Already doing this", "Doesn't fit us", "Too costly", "Bad timing", "Don't trust the numbers", "Other"), sent as `reason_code`. Every Not for us asks it — the win-back text's and the schedule review's ✕ too (skip "Skip", `reason_code` on their own routes); a second hide asks it with "Just hide it for two weeks" as the skip; Ask's "Not now" with "Just not now". Never a text field on the floor. Hold the target in its own `@State`, apart from the dialog's flag |
| Tracker line | `RecTrackerLine(text:)` — gauge glyph in ember2 + "Measuring labor % until 10/21/26" (the server's `label_text`) or the refusal's own `reason`, mixed text. `RecTrackerNote.extraLine` drops it when the message already says it |
| Answer pill | `RecAnswerPillStyle(selected:)` — capsule, ember hairline on a faint ember wash, 13.5 bold ember2; the chosen one fills. For one-tap answers to a question Cavnar asks (Yes / Partly / No, Was this useful? Yes / No), never for navigation |
| Value, net (iOS) | `HomeValueBand` / `ValueChartCard` — "MEASURED RESULTS · PER MONTH"; with nothing measured the band reads "Nothing measured yet" in ink (no green, no glow) and draws no curve at all (the hard-coded rising curve it fell back to was a synthetic series with no label); `ValueChartCard`'s example curve is captioned "Illustration only — not your data or any restaurant's". Nav title and VoiceOver say "Measured results". Once `value.worsened.count > 0` the kicker reads "MEASURED RESULTS · NET PER MONTH", the figure is `net_monthly` (red "−$X" below zero, never floored) and a breakdown line ("$X improved, less $Y from N that got worse", N = `priced_count`) replaces the delta; the chart gains the caption "The line is the improvements measured each day; the figure above is net of what got worse." The worth card lists each unpriced win as a green row ("…, improved — measured, no dollar figure") and shows the card for them alone. An older payload reads as before |
| Result tone (iOS) | `RecOutcome.standing` → `tone`: `counts == false` is neutral whatever the verdict; the server's `counts` rules, the verdict only when it is absent. Home results, the record and the month card |
| Check-in | `RecCheckInCard(outcome:surface:onAnswered:)` — "CHECK IN" kicker, the result line, "Did you make this change?" as three answer pills and a "Something else changed these weeks too" check row; one tap posts `/recs/checkin` with the result's `tracker_id` (web and iOS both: the answer lands on the result the card shows, not the key's newest episode) and the result reloads (its attribution changes; web re-reads it by id, `/api/outcomes?ids=`). Shown when `RecCheckIn.isDue`: evaluated, clear verdict, no `owner_checkin`, not informational, a recommendation's key. Home shows at most two under What your changes did; the rest wait in the record |
| Measured alongside your changes | `WhatWorkedCard(whatWorked:)` — `HomeSectionHeader("Your record", "Measured alongside your changes")` over the server's sentences, verbatim, green dots, and the `CavnarCaveat` "Before and after, not proof" always under them, `.cavnarCard()`. Renders nothing unless `enough` and a sentence exists |
| Recommendation record | `RecommendationHistoryView` (`Features/Recommendations/`) — the Account identity-card kit: `AccountHero` → 30/90/180 `CavnarSegmentedControl` → "What you followed" (per module: shown · followed · said no · ignored, the rate in the number face with its likely range only when `enough`, "Not enough yet" otherwise) → "Most effective for you" → the timeline (title, module · shown date, answer chip + `AccountPill("Validated")`, the why, then its tracker: `RecTrackerLine`, an amber `PARTIAL` capsule before the interim reading, "Stop measuring" in red behind a confirmation dialog; or the result line, attribution sentence, other changes those weeks, the re-check, and a `RecCheckInCard`), paged with "Show older ones". Opened from Account → Recommendations and Home's "What you followed →" |
| Confidence (iOS) | `ConfidenceLine(confidence:recKey:surface:module:)` (`DesignSystem/ConfidenceLine.swift`) over `TrustConfidence` (`Models/TrustConfidence.swift` — decodes the K1 object, a bare band string, or an older `{band, label, reason}`; every field optional; `thresholds` and `caps` when sent; `TrustConfidence.measured(detail, legacy)` so a bare model band draws nothing) and the pure `ConfidenceDisplay` (tone from the payload's band, label — sample data "Confidence not yet measurable", never 0% — rows with "Sample: N" and the accuracy row's lift sentence + "N% likely to beat doing nothing" (`beatsLabel`), the `meaning` line the Why? sheet leads with, footer with every ceiling in `caps_applied` — unit-tested): the meter (`ConfidenceMeter`: tone gradient, glow, grow-in), "72% confidence" in the number face, the reason in ink3, and a plain **Why?** text button that opens `ConfidenceWhySheet` — the Account kit (`AccountHero` with the overall figure and a wide meter, `AccountSection("WHAT IT RESTS ON")` with Evidence strength / Historical accuracy / Data freshness, each value, meter and basis) — and calls `RecEvidenceLog.viewed`. Same tone map as the web (`.cavnarGreen` / `.cavnarInk2` / `.cavnarAmber`; never red, never ember). Replaces the bare "MEDIUM" capsules and the strength chip |
| Freshness strip (iOS) | `HomeFreshnessStrip` (`Features/Home/`) — the web `.hb-fresh`: "DATA AS OF 9/22/26", then a capsule per `freshness[]` source with its state dot (current green, aging / stale amber, not connected / age unknown ink3, sample hatched), name, basis and % in the number face. An older server's "fresh" with no date reads "age unknown", never current |
| How you compare (iOS) | `HowYouCompareCard(module:)` (`Features/Home/HowYouCompareCard.swift`, models in `Models/BenchmarkCard.swift`, one `BenchmarkCardStore` per session) — `.cavnarCard()`, the ember2 kicker, who and its as-of, the strength line (`ConfidenceMeter` + "68% comparison strength" + Why? → `ComparisonStrengthSheet`, the Account kit with the confidence sheet's rows through the shared `ConfidenceDimensionRow`), the below-the-minimum sentence and its reason, one `HowYouCompareRow` per metric (tone dot good green / behind amber / level ink3, `HomeAskLink` for a behind metric). Under Labor's benchmark bar, Food Cost's stat strip, Reviews' period picker and Marketing's attribution card. Home: `HomeBenchmarkStrip` under `HomeFreshnessStrip`. The locations sheet: `LocationComparisonSection` (Account-kit sections per metric). Draws nothing until the server answers or for a login that can't see the module |
| Worth split (iOS) | `HomeFollowThrough`'s worth card: "What was measured" over the measured figure, "What Cavnar surfaced / still available" over the estimate (each avoided item with its stated rate and basis), the alert total and the gap (amber, not ember) — never summed |
| Server tone (iOS) | `ServerTone.color` (`DesignSystem/ConfidenceLine.swift`) — a tone the server decided (`*_tone`: AI visibility, listing strength, market standing, holiday/benchmark words): good `.cavnarGreen`, warn `.cavnarAmber`, bad `.cavnarRed`, neutral `.cavnarInk2`; nil when none was sent and the caller's fallback applies. Never ember |
| Unverified cause (iOS) | `CavnarCaveat.unverifiedCauses(_:)` — the third caveat beside unverified figures and names, titled "Unverified cause", when a read carries `causes_verified == false` (Reviews, Marketing); quotes the first `unsupported_causes` sentence. The web twin is the "Unsupported cause" `.ai-caveat` |
| Calibrated dollar figure (iOS) | `RecDollarCalibration.figure(raw:adjusted:)` / `.note(adjusted:n:note:)` (`Models/HomeSummary.swift`) — "$X/mo · adjusted from N measured results": the adjusted figure in place of the raw one when `dollars_adjusted` is non-null, else `dollars_monthly` as before. Home recommendations, the one-thing card, nightly-report actions |
| Not counted (iOS) | An outcome row whose baseline overlaps the trigger (`baseline_overlaps_trigger`) gets an amber "Not counted — …" line under its result; the grade phrase and what it was compared with follow in ink3 (`RecommendationHistoryView`, `HomeFollowThrough`) |
| POS sync line (iOS) | `AccountConnectionsDetailView` — under each POS connection header a 6pt dot plus text from `sync_state` / `age_days` (current green, aging/stale amber, error red), dates M/D/YY; RPOWER's row uses a `GlowBadge(systemImage: "server.rack")` tile (no brand mark). Google reads "Google reviews (sampled — Places returns 5 at a time)" when `source` is `places_sampled` |
| Recipe yield (iOS) | `RecipeDraftsSheet` — a batch draft (`needs_yield`) shows a "Plates" field (`cavnarTextFieldStyle`, number pad) and Accept stays disabled until it holds a yield, sent as `yield`; `unit_skipped` lines are named after Accept |
| Forecast line tag (iOS) | A computed forecast line (Reviews and Marketing `forecast`, the brief's forecast line) carries `ClaimKindTag("forecast")` |
| Claim tag (iOS) | `ClaimKindTag` — the web `.ck-tag`: tiny uppercase ink3 on a Paper3 capsule, "AI-written" / "Measured" / "Computed" / "Forecast" / "Inferred" |
| The one thing | `HomeOneThingCard` — `.cavnarCard(.hero)`: module names joined by `EmberThread` (two or more only), the action at 18, why, "To confirm:", up to three evidence lines, its `ClaimKindTag` and `ConfidenceLine`, $/month in the number face with what it covers under it (`dollars_basis`; a `money {low, high, label}` range stays a range), "Could also be…" (an alert; records `evidence_viewed`), an Ask link, and its `RecAnswerRow`. After Needs attention, before the recommendations (§11b) |
| Labor diagnosis | `LaborDiagnosisCard` — `.cavnarCard(.ai)`: "WHY LABOR RAN OVER", the `ConfidenceLine`, summary, most likely cause, it could also be, "Check this" (the action) with its `RecAnswerRow`, cross-checked against. Nothing when there is no cause |
| Evidence viewed | `RecEvidenceLog.viewed(key:surface:module:)` — call when the owner opens a keyed recommendation's reasoning ("Could also be…", "Why this matters"); once per key per launch, fire and forget |
| Field | `AccountField`, `CavnarFloatingField`, `CavnarDropdown` |
| Switch with its record | `AccountSwitchRow(label:detail:isOn:busy:)` — the `detail` is the owner's own record behind the switch (Automation & trust) |
| Score movement | `ScoreDeltaChip(delta:)` — "+3" green / "−2" red capsule in the number face (`ShiftQualityPanel.swift`) |
| Decide-in-place row | `TimeOffSection` row: name + dates, Deny (secondary) / Approve (primary) side by side, status text once answered |
| Needs attention (iOS) | `HomeActionDeck` — the lead `ActionDeckCard` (primary CTA, secondary link, ⋯ Not today / Hide) and every other item as an `ActionDeckRow` under it in one Paper2 card: title and detail on one line each, the CTA as an ember2 text button with a chevron, long-press for Not today / Hide. The lead plus three rows show (`shownByDefault = 4`, the server's `HOME_ATTENTION_SHOWN`); "+N more" (number face, 44pt) opens the rest in place. No swipe deck, no dots. Directly under the header strip (§11b). A card's tap follows its `nav` (the filter, section or item), never just the module |
| Bell and location (iOS) | `CavnarBellButton` (`Core/AppChrome.swift`) — the one bell, on Home and every module screen's trailing toolbar, the unread dot on the disc's corner; the sheet opens at once on its skeleton. The Home tab carries the unread count as `.badge`. `CavnarScreenTitle` (drawn by `cavnarTitleToolbar`) adds the location's name under a module screen's title — ember2, 11.5, a chevron-down — for an owner with more than one location; tapping it opens `LocationSwitcherView`. A switch resets the Modules stack and never replays the landing intro |
| Queued send (iOS) | `PendingActionSheet` — medium detent, Account-kit chrome "Queued send": "GOING OUT ON ITS OWN" kicker, the action's label (Clash 20), "Goes out at 11:00am" on the restaurant's clock (M/D/YY when not today), Review (secondary → the schedule or order it sends) and Undo (primary, acts at once — it only changes a row nothing has run yet). Opened by an `action/<id>` nav path: the "goes out at" push, its notification row, a card |
| Review queue (iOS) | `ReviewDetailView` pins Skip / Approve to the bottom (`.safeAreaInset`, Paper at 94% over a hairline). With another drafted reply after this one in the list's order the primary reads "Approve & next": a 0.45s check capsule (green check + "Posted to Google"), then the next reply in place; the full `CavnarPostedCheck` plays only on the last. The inbox opens on "To approve" when replies are waiting; a trailing swipe "Approve" (green) exists only for a reply that may go out unread (drafted, not flagged, not urgent — the bulk-publish bar) |
| Notification row actions (iOS) | `NotificationsListView` rows: the tap opens the row's `nav`; an actionable row carries its one action as an ember2/red text button at the right (44pt) and the same as a trailing swipe — Undo on a "going out" row when exactly one such send is pending, Approve on a reply that may go out unread — then says what happened ("Undone", "Posted") in green. Errors are one red line under the row |
| Lock-screen actions (iOS) | Push categories (`PushManager`): `CAVNAR_REVIEW_DRAFTED` Approve & post · Edit, `CAVNAR_UNDOABLE` Undo (destructive) · Review, `CAVNAR_REQUEST` Approve · Deny (destructive), `CAVNAR_BRIEF` Ask about this (sends), `CAVNAR_REVIEW` Reply. Approve / Undo / Deny run without opening the app, behind the phone's unlock; a failure comes back as a notification that opens the item |
| Tap targets (iOS) | 44pt minimum: `cavnarToolbarIconGlass` keeps its 34pt disc inside a 44pt hit area; review chips, deck buttons, row actions and the daily report's night stepper grow their frame, not their look |

| Waiting on you (Labor) | `LaborWaitingOnYou` — a `.cavnarCard()` directly under the Labor hero, ember2 kicker "WAITING ON YOU" + count in the number face, one decide-in-place row per pending time-off / shift request (both buttons secondary here: the screen's one primary is Send), the name opens the person sheet, a drop carries the text link "Approve opens it for anyone to claim · Name who covers". Hidden when nothing waits |
| Pinned send bar | `LaborSendBar` in `.safeAreaInset(edge: .bottom)` once a saved draft exists: "Review N" (secondary, scrolls to the rules check) + "Send to staff" (the screen's one primary, disabled with "Save your changes before sending" until saved) on a Paper wash with a hairline top rule. The inline Send under the table is gone for a saved draft |
| Action row | `FoodCostActionRow` under Food Cost's sub-tab control, on both sub-tabs: three equal Paper2 tiles (icon over a 12.5 bold label, 44pt minimum) — Scan invoice · Count · Order — and a 44pt "…" menu (Recipes, Menu margins, Invoice from a photo). Secondary weight: the one primary stays the form's own |
| Document scan | `InvoiceScanSheet`: "Scan with camera" (primary, VisionKit `DocumentCameraView`, full-screen cover) over "Choose a photo instead" (secondary); only the picker when the device has no camera. More pages than the server reads says so in amber, never drops them silently |
| Command sheet | `CommandSheet` (`Features/Command/`), a `.large` sheet with `accountSheetChrome("Find or ask")` and the field pinned at the bottom (`safeAreaInset`, Paper2, ember hairline when focused). Empty: "One tap" capsule chips (44pt), "Waiting on you" rows (severity dot, mixed-text title, detail; pending sends carry "Goes out in …" + Undo; requests carry Deny / Approve, both secondary), "Locations" chips with a ✓ on the current one. Typing: "Go to or do" rows (arrow for a place, bolt for an action — an action opens Ask's `ProposalCard` in place), "Found" rows with the store named when it isn't this one, and always last "Ask Cavnar AI: “…”" in ember2. Return opens or asks, never confirms. Reached from Modules' toolbar magnifier, `cavnarai://command`, the widget and the "Find in Cavnar AI" shortcut |
| Person sheet | `PersonSheet(target:)` (`Features/People/`) — the Account identity-card kit: name in Clash, role + Active chips, `AccountSection`s "Scheduling" (hours, availability, rating, certifications — "Not set" / "Not rated", never 0), "Login" (PIN set), "Contact and pay" (role, phone, email, POS id, pay rate fields; one primary Save that posts only what changed). Opened from a roster person, Account → Staff accounts (menu "Person record"), a name in Waiting on you and a person in the command sheet |
| Post to all connected | Marketing's social publish: one Toggle per connected channel (ember tint; Instagram disabled with "Needs a photo" until there is one; "Posted" in green once up), then ONE primary "Post to Instagram and Facebook" behind a `.confirmationDialog` naming the channels — it goes outside the restaurant |
| Widget | `CavnarWaitingWidget` (`CavnarWidgets/`) — systemSmall: "CAVNAR AI" ember2 kicker, the waiting line in Clash 17, then "LAST NIGHT · M/D/YY", the net in the number face and its change (green/red) with its basis ("vs last Friday", else "vs yesterday"; none shown when unmeasured). Lock Screen rectangular / inline / circular carry the same two facts. Money is `.privacySensitive()`; a snapshot older than 36h says "Open Cavnar AI to refresh" |
| Countdown with Undo | `PendingSendLiveActivity` — Lock Screen: ember2 kicker ("SCHEDULE GOES OUT" / "SUPPLIER ORDER GOES OUT"), the title, "in 12:04" as the system timer in the number face, an "Undo" capsule with an ember hairline; after Undo "Stopped. Nothing went out." in green, a refusal in amber. Dynamic Island: calendar / box icon + the timer compact, the Undo button expanded |
| Mixed text with numbers | `HomeMixedText.make(...)` |
| Loading | `CavnarShimmerText`, `CavnarSkeletonLines`, `CavnarLoadingOrb` |
| Success | `.cavnarPostedOverlay(label)` |
| Sensitive figure | `.cavnarSensitive()` (privacy redaction) |
| Caveat | `CavnarCaveat` |
| Background | `.cavnarModuleBackground()` |
| Radius | `CavnarRadius.control / .card / .sheet / .pill` |
| Night status | `DSRStatusPill(phase:)` — Final green / Provisional amber / Running ember with a `BreathingDot` / Couldn't finish red; the word always, never colour alone |
| Expandable block card | `DSRBlockCard` (Daily Report) — `.cavnarCard()` whose header is always the Clash title, the source, a `DSRBlockStatus` capsule (Ready / Waiting / Unavailable / Not connected, from `dsr.STATUSES`) and ONE headline line (`DSRHeadline`, numbers in the number face) so a page of them reads collapsed in under two minutes; the detail opens on tap (the `CavnarDropdown` transition). A block that isn't ready shows the server's `reason` in amber instead, and doesn't open |
| Recessed stat tile | `DSRStatTile` in a `DSRTileRow` (three across, wrapping) — kicker, one number, optional mixed-text detail; "—" in ink3 for a null. For supporting figures inside a card; `AccountStatTile` stays the sheet-strip tile |
| Stage checklist | `DSRProgressChecklist` — a run's stages on a thin rail: green tick + local time when done, the breathing ember on the one running now, a hollow ring for what is to come; then each block's state with its reason. Lines are the server's own stages in its order, never a scripted sequence (same rule as the Ask trail) |
| Bars | `DSRHourlyBars` (vertical, ember gradient, peak hour glowing, bar grow-in; a missing hour is a hairline gap) and `DSRCategoryBars` (horizontal against the largest) |
| Weekly grid | `DSRWeekGrid` — see §8 |

---

### The nightly report's order (SCORE FIRST, 9/25/26 owner decision)

One order on web (`#panel-dsr`), iPhone (`DailyReportView`) and email (`emails.dsr_email`); this paragraph is the only statement of it. It answers the 3-30-300 rule: health, the biggest risk and the biggest opportunity in 3 seconds, the why and the top actions in 30, the drill-down at 300.

**Owner:** **Today's score** (the hero) → the **executive summary** → **Today's wins** and **Today's risks**, three each (`scorecard.SHOWN_ITEMS`; the web keeps the rest behind a quiet `details.dr-more` "2 more") → **Tomorrow's priorities**, three visible and the rest behind "N more priorities" (the email prints three and "N more in the full report", and presents only those three to `rec_ledger`) → **Tomorrow** → **Top KPIs**, the payload's `kpis_headline` (at most `kpis.HEADLINE_MAX` = 4, never one the score already states: net, labor %, food cost % and the rating are its components) → **All KPIs** (`details.dr-sec.dr-allk`, closed: every other KPI, the score's four included, with its direction) → **AI insights**, at most two (`access.INSIGHTS_MAX`), never a win, risk or priority said again and never the Food block's money at stake (its own "At stake · opportunity" tile says it) → the blocks, closed → **How did yesterday turn out?** (web; the email says it as one line, "Yesterday's predictions: 1 of 2 correct · 3 of 4 right so far") → **How the night was built** (closed; it holds the verification count, "8 of 9 lines kept", and "each block carries its source").

**Manager:** the **Operations summary** (the hero; there is no score) → **Today's shift** under its one verdict line (`kpis.shift_verdict`: "Labor 27.5%, on target · 1 no-show", toned like Today's score's labor — over a starting target amber, never red) → **Top KPIs** (labor against target, the rating) → **Operations**, four tiles at most (`kpis.OPERATIONS_MAX`; discounts, voids and comps stay in the Sales block where the view allows them) → **Went well** / **Needs attention** → Tomorrow's priorities → Tomorrow → AI insights → the blocks → the same closing sections. The manager's Top KPIs never repeat a tile Operations already shows.

**Today's score** (`.hb-card.hero.dr-score`, iOS `DSRScorecardCard`, email `emails.dsr_scorecard_sections`): a verdict with a status dot (Excellent / Good = good, Mixed = warn, Tough = bad) in Clash at 24–32px; the night's **net** at 44–62px in the number face (`.net`, the page's largest figure) with the Sales component's own comparison under it in its tone (`.vs`: "+$420 vs budget" — the component's basis, budget else forecast else last week, never re-derived); the overall score as `78/100` on the right; then the other three components (Labor, Food cost, Guest experience) as stat tiles in their tone, an unmeasured one saying why in muted text, never a dash pretending to be zero; the guest tile is whole stars in ember (rounded down unless within a quarter). When Sales wasn't measured it is a tile like the others and there is no net. The **executive summary** under it is an ordinary card (`.hb-card.dr-read`, `.lead` at 19–23px): a paragraph is never the hero when there is a score. Status is a dot, a check or a triangle glyph — not emoji (email subjects stay emoji-free).

**Blocks** are closed by default; each summary line carries the block's number (Sales net, Labor % of sales, est. food cost, reviews new · rating, posts · reach, the night's high · events, who filed the close-out). Sales opens on its own only when there is no score to read.

**A KPI tile** (`.dr-kpi` / `DSRKPIGrid`) is the value in the number face with a 14-night trend line (ink stroke, the latest point ember), then its direction — "↓ 1.4 pts vs last Saturday" in good/bad tone by the metric's better side — a streak ("Best Saturday in 5 weeks"), the target and, only when fair, "Restaurants like yours" (otherwise "no fair comparison yet" in muted text). **Tomorrow** lists prep items with an amber triangle (a plain dot when informational), a pointer to the staffing priority ("Staffing for tomorrow: see priority #2 above" — never the action said a second time), Cavnar's forecast and the **AI confidence** as a percentage with "Based on …" — the forecast's measured record (`confidence.track`), and "—" with that line when there is no record yet (`pct` null); the weather, schedule and events it lists are `watch`, things to keep an eye on, never "confidence". **How did yesterday turn out?** marks each prediction ✓ correct (good) / ✗ incorrect (bad) / • not graded, with the measured figure, and the prediction accuracy as a percentage once five are graded — the count before that. Labor over a target the owner never set (Cavnar's starting target, `detail.target_source` "default") is amber, never red — the Labor tile, the email's Labor stat, the shift verdict and Today's score agree. An estimate (food cost, prime cost) says so everywhere: `est.` on the tile, "(est.)" in the email.

**The email** follows the same order: the score (verdict, `78/100`, the net at 38px with the sales comparison in its tone), the other components, the summary, three wins, three risks, the priorities (`report_action`, the one loud block), Tomorrow, four KPIs (`kpis_headline`), at most two insights, yesterday as one line, what's missing, the CTA.

### Week and period summary (9/25/26, ID1-22)

Above the grid, one `.hb-card.dr-wsum`: four tiles from the grid's own totals row — "Week to date · net" (with "N of 7 nights measured"), **vs budget** (owner only; "—" with the grid's reason when a measured night has no budget), **vs last year**, **Labor %** (only when the view reads labor) — then `.dr-wbars`, one bar per night (a period: per week) of net, green when it made its budget, red when under, a dashed ink mark at the budget (owner only; a manager's bars are plain ember and the legend says "Net sales"), a hairline for a night not measured; then `.dr-story`, the one sentence `dsr.rollup.story` writes from the view's redacted grid ("Thursday carried the week ($8,420 net, 48% of it); Friday missed budget by $610." — a view without the budget reads the miss against last year). Nothing on it is summed in the browser. Once any night in the week has a last-year figure, the import card folds to a "Import more last-year nights" text link (`.dr-imp-link`) that opens it.

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
   `report_confidence(conf)` is the one line under a recommendation that
   carries a measured confidence — "72% confidence · data through 9/23/26"
   (`rec_trust.outbound_label`), 12px, `muted` — never a band word; "" for
   a fact, which carries none.
2. **`_branded_email(inner_html)`** — short transactional mail: a code, a
   confirmation, a link. Wordmark, one white card, seal footer.
3. **Bespoke** — legacy. 14 emails still are. Not a starting point; migrate
   onto `report_shell` when you touch one.

Widths are 560px (`report_shell`) and 480px (`_branded_email`). Do not
introduce a third.

The morning brief email (`morning_brief._email_html`) and every alert email
(`notify._alert_email_html`) are on `report_shell` since 9/25/26.

### Reporting email order (weekly digest, monthly review — 9/25/26)

Verdict first, one action. In this order, each part optional:

1. **H1 = the verdict** — the deterministic `weekly_review.headline` /
   `monthly_review.headline`, when the period measured anything; the
   restaurant name moves to the subtitle. The **subject** carries the same
   sentence (`reporter.digest_subject`: "A better week: sales improved — your
   week at X"). The headline is never repeated in the body.
2. **One stat row** of the period's figures (`emails.review_kpi_stats`, the
   review's own measured metrics, toned by verdict, an unmeasured one left
   out).
3. **ONE action** — the cross-module one thing (`_one_thing_block(…,
   loud=True)`, in the `report_action` frame); the model's "This week's move"
   / "Your next move" only when there is no one thing. Never both.
4. **Needs a reply** — guests waiting.
5. The detail: the period against the last, "What your changes did" (once),
   worth your time, one cross-module finding, module sections, follow-through.
6. **One caveat footer line** (`report_caveats`): every generic caveat, said
   once. A caveat that belongs to one signal (a link's innocent reading, a
   loss note, the value block's "not added together") stays beside it.

The monthly's owner-record block is "Measured alongside your changes" (never
"What worked for you"). The morning brief email: H1 is a state ("2 things
need you this morning"), last night's verdict from the shared `access.summary`
contract under it, at most 5 lines (`morning_brief.EMAIL_MAX_LINES`, the
lines that need the owner kept first), and "N more in the app →" as the one
button. An alert email's H1 carries no emoji; when a reply is already drafted
its button reads "A reply is drafted: read and post it".

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
