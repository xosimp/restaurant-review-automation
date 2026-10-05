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
| Themes | **dark only** — `dashboard.html` sets `data-theme="dark"` before first paint; the light palette on `:root` is still in the CSS but no screen shows it (kept, not deleted) | dark only, by design (a dim dining room) — `RootView` `.preferredColorScheme(.dark)` |
| Enforced by | `scripts/check_colors.py`, `tests/test_button_system.py`, `tests/test_frontend_rules.py` | code review + `CavnarRadius` / style structs |

**Platform roles.** iOS is where the owner *decides and acts* — the bell,
the lock-screen actions, approve / undo / deny in one tap, the widget.
The web is where they *plan, configure and analyse* — the schedule
builder, settings, exports, the long reads. Both show the same numbers
in the same words; a platform may add a capability the other can't have
(below), never a different meaning for the same component.

**Platform-native capabilities** (one side only, by nature — not drift):

| iOS | Web |
|---|---|
| Push with lock-screen action categories (`PushManager`, §12 *Lock-screen actions*) · the Home-screen / Lock Screen widget (`CavnarWaitingWidget`), and the staff tier's "Next shift" widget (`CavnarNextShiftWidget`, from `StaffShiftSnapshot`) · the queued-send Live Activity (`PendingSendLiveActivity`) · App Intents / Shortcuts (`Core/CavnarAppIntents.swift`) · Home-screen quick actions (`Core/SystemEntry.swift`) · haptics (`Haptic`) · document scan (`InvoiceScanSheet`, VisionKit) · the offline write queue (`PendingWriteQueue`; the staff tier's task ticks: `StaffOfflineQueue`) · Face ID and app passcode (`LockedView`, `CavnarPasscodePad`) · the privacy shield over the app switcher | The ⌘K / Ctrl+K / "/" command palette (`cavPalette`) · URL routing — every panel and deep link is a real URL (`history.pushState`) · CSV exports and templates · the wide multi-column plan and settings screens |

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
| `--ember` / `.cavnarEmber` | **`#c84b2f`** | `#d4583a` | Ember (`#D4583A` dark — the same) | The brand accent — see §9 |
| `--ember2` / `.cavnarEmber2` | `#e8956a` | `#e8956a` | Ember2 | Chart lines, kickers, soft accent |
| `--green` / `.cavnarGreen` | `#2d6a4f` | `#4ead7a` | Green | Good / improved / on target |
| `--red` / `.cavnarRed` | `#c0392b` | `#e3333f` | Red (`#E3333F` dark — the same) | Bad / critical / destructive |
| `--amber` / `.cavnarAmber` | `#b7791f` | `#d4a030` | Amber | Warning, "watch", partial data |
| `--blue` / `.cavnarBlue` | `#1a56cc` | `#6aabff` | Blue | Informational only (rare) |
| `--bg-base` (web only) | — | `#141110` | Paper (`#0c0c0c`) plays this role | The web canvas behind every panel — darker than `--paper` so cards step up from it (below) |

**The accent and the status colours are one value on both platforms**
(parity pass, 9/25/26): dark ember `#D4583A` and dark red `#E3333F` —
the web's old `#e06444` / `#e05555` (and every `rgba(224,100,68,…)` /
`rgba(224,85,85,…)` tint built from them) put two embers and two reds on
one screen next to the dark buttons, which already used the iOS values.
The red is deliberately further from the ember than `#e05555` was, so a
red status never reads as the brand. Pinned by `tests/test_design_parity.py`.

**The ink2 and paper tiers differ on purpose.** iOS draws on a true-black
chrome (`#0c0c0c` / `#121212` / `#262626`) because an OLED phone in a dim
dining room reads black as "off", and its Ink2 is lifted to `#e0d6c6` to
hold contrast against that deeper ground. The web keeps warm browns
(`--paper` `#1a1714`, `--surface` `#201d19`) over the `#141110` canvas
because a desktop monitor is backlit — pure black there reads as a hole,
and the three-step luminance staircase below needs room between tiers.
Everything a reader compares — the accent, the status colours, ink and
ink3 — is identical.

Each status colour has a `-bg` pair for tinted chips (`--green-bg`,
`--red-bg`, `--amber-bg`, `--blue-bg`; `.cavnarGreenBg` etc.).

Beyond the palette, `dashboard.html` defines three token families a
component may reach for and must not redefine: the surface layer
(`--sf-ai`, `--sf-ai-line`, `--sf-recess`, `--surface-glass`), elevation
(`--elev-card`, `--elev-float`, `--elev-hero`, `--glow-ember`) and the Home
semantics (`--hb-good`, `--hb-warn`, `--hb-bad`, `--hb-amber`, `--hb-tint`,
`--hb-line`, `--hb-glow`, …). `--hb-amber` **is** `--amber` — there is no
second amber (it was `#e6a862`, with no iOS twin); Home's hero kicker and
the recommendation numerals that used it read `--ember2`. `--hb-copper` is
the one web-only warm secondary: a hover edge on Home's cards, never a
fill and never a status. The stat glows (`.stat-glow-green/-red/-amber`)
are `--green` / `--red` / `--amber`, no raw hex. Buttons have their own namespace in
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
face used only for the tab badges (`.tab .badge`) and the `.stat-n` figures
— a legacy of the pre-rebuild stat cards that the "every number is Space
Grotesk" rule has not yet reached (a `.stat-n` inside `.hb-num` is already
Space Grotesk). Nothing else: it used to be forced onto every inline
`font-weight:800` (the wordmark's "AI" tag among them) and every `strong`
in Labor; those now keep their own face — inline 800s snap to 700, the
heaviest weight Apfel and Space Grotesk ship — and Labor's PAR figures
carry `.hb-num`. Pinned by `tests/test_design_parity.py`. iOS bundles all
three (`Font+Cavnar.swift`).

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

### Phone numbers (one format, everywhere)

A US number reads **(334) 568-9292** wherever it shows — web, admin, iOS (Will, 9/29/26). Python `auth.display_phone` (Jinja `|format_phone`), web `fmtPhone()` (`static/cavnar-phone.js`, loaded by every page with a phone), iOS `PhoneFormat.display`. International numbers and fragments show as entered. A phone field (`type="tel"`, or `data-phone`) formats as it is typed and on blur; iOS fields use `PhoneFormat.typing` in `onChange`. A one-time-code field (`inputmode="numeric"`) is left alone. Owner phones are saved in this form (`create_restaurant` / `update_restaurant`); sending and matching always go through `notify._normalize_phone`, never the displayed string.

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
| Web JS | `mdy(x)` — global, exported from the Home closure (`window.mdy`); `cavClock(d)` for the time ("6:45pm", "6pm" on the hour). Never `toLocaleDateString` / `toLocaleTimeString` or a `toLocaleString` with a date part — they print "Sep 21" or "9/21/2026, 6:45 PM" |
| iOS | `CavnarDate.mdy` / `mdyRange` / `mdyTime` / `time` ("6:45pm") (`DesignSystem/Formatting.swift`). Never a `DateFormatter` with `MMM` or `h:mm a` for anything an owner reads |

`tests/test_date_format.py` pins the helper and the feed text, and ratchets the clients: no locale date formatting in the owner-facing templates, no `MMM` / `h:mm a` display formatter in the iOS app.

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

### Page width and wide screens (web)

Module pages sit in one centred column: `.hb-wrap` / `.dr-wrap` /
`.rh-wrap` at 1120px, Account's `.ac-page` at 1180px, the legacy `.panel`
at 1080px, **at every width** (owner, 9/26/26): no module widens on a
large screen, so every tab's margins line up. The one exception is the Schedule Studio (`/schedule/studio`, a full-page application over the dashboard — see Schedule Studio), which the owner asked for; Labor itself stays in the column. Reviews is one column too:
Trends opens below the inbox, and Inbox | Analytics scroll to each. **Home keeps its single column at every width**:
its order is §11b, and a second column would reorder it. A new wide rule
goes in the "Wide screens" block at the end of `dashboard.html`, never on
Home.

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
| Numbered card | `.hb-card.hb-rec` in `.hb-recs` (3-up) — `.t` the action (verb first), `.why` why now, `.meta` pills (`.usd` "$N/mo at stake" only when measured, with `.hb-dbasis` "covers one Tuesday's overstaffing, per month" under it from `dollars_basis` — one Home can carry three labor figures of different scope, and each says its own; the focus card's `.money` and a Needs attention row carry the same line — then timeframe · impact — **no evidence-strength pill**: it was a second verdict that could contradict the confidence on the same card, and the confidence's Evidence strength row says it, measured), an "AI-written" `.ck-tag` after the title when `model_written`, `.ev` evidence, ONE confidence line (`.cf.hb-conf`, see §12 **Confidence**: "72% confidence — what it rests on · Why?"), `.ig` "If ignored:", then the footer: one-tap action (reprice) / Measure it / open module / Could also be… (`data-explain`, only when the source has an alternative) / Assign (inline `.hb-assign` select, only with assignees) / Done · Pass, and the ✕ hide; "Restore hidden" in the section head brings back the last one hidden (`dismissed[0]`). iOS: `HomeRecommendations` rows, same order, "Restore hidden" under the card | Recommendations — every card answers what, why now, $, how sure, if ignored |
| Quieter line | `.hb-quiet` under the cards — "Quieter: Trim day — the last four went by unanswered." + a text button per kind ("Show trim day again" → `restore_kind`). iOS: the same line under the card | A kind the owner has let expire unanswered four times running |
| Check, warning and miss marks (9/27/26) | One disc everywhere a status is marked: `.cv-ok` (the soft green disc, a CSS-drawn dark-green tick), `.cv-warn` (the same disc in amber, a dark "!"), `.cv-bad` (red, a dark ×) - 18px inline, `.cv-md` 20px in a step row. The tokens sit on `:root` (`--hb-pop*`, `--hb-wpop*`, `--hb-bpop*`, redefined under `[data-theme="dark"]`), so a disc reads the same on every page. Used by the Daily report (How the night was built, Today's wins and risks, Tomorrow, the graded predictions), Home's and the invoice's all-clear rows, the group table, Intel's read and listing checklist, the AI visibility road, the Studio's "every rule kept", the price monitor, schedule availability, Account's security list and recovery email, the Labor all-clear and the notifications calm state. A ✓ inside a button or a toast is text, not a mark, and stays | Never a pale tint and a bare glyph for status |
| Receipt strip | `.hb-rcpt .it` — check + sentence + module. The check is a soft green disc with a dark-green tick (`--hb-pop` / `--hb-pop2` radial, the tick `--hb-pop-ink`; the all-clear row's `.hb-clear .ok` wears the same disc - every Home check is one, a `--hb-pop-ring` halo and `--hb-pop-glow`), never a pale tint: done is good news (owner, 9/26/26) | What Cavnar AI did — finished things must not look like a to-do |
| Timeline | `.hb-tl .it` with a tone dot on a rail | The morning brief, read once top to bottom |
| Stat tile | `.hb-stats .hb-stat` (+ `.good` / `.warn` / `.bad` — a gap still open, "Still on the table", is `.warn` amber; ember is never a tile's status, §9) | Four *kinds* of number that must never be added together. On Home's worth card they sit in `.hb-vsplit`: the measured tile alone under "What was measured", the estimate / alert total / gap under "What Cavnar surfaced · still available" ("estimates and gaps, never added in"), side by side, stacked under 760px, the second group absent when it has nothing (`.solo`). The section is "Worth · What Cavnar AI measured and surfaced" — never "Measured" over tiles that grow as the restaurant does worse. The measured tile reads **"Measured, net"** once anything got worse: `net_monthly` (below zero when that is what was measured, `.bad`), with "$X/mo from N that improved, less $Y/mo from M that got worse" under it (`hbNetSentence`: M is `worsened.priced_count` — the dollars only cover priced results — and any others are said as "(K more got worse with no dollar figure)"). A card whose only wins have no honest price (a rating rise, `unpriced_wins`) reads "N improved · not priced in dollars", never "Nothing yet" and never a figure |
| Worth rows | `.hb-vrows` — `.hb-row`s under the worth tiles, each what the measured figure rests on: "$1,006 measured since 4/26/26" (the cumulative total, only when `cumulative.total` is not null — nothing measured is not $0 — with a How? modal of its `basis`), "N held at their re-check — validated", each unpriced win in the server's words ("Average rating 4.2★ → 4.4★, improved — after "Brief the servers…" · not priced in dollars", up to three, then "N more improved with no dollar figure"), faded wins, and the server's `net_note` when the net is negative. `.vm` is the muted tail | The worth section's footnotes, same kind as the tile (measured), never added to the estimate, the alert total or the gap |
| Value hero (net) | `.hb-hero` on Home — "Measured results" (kicker and `aria-label`; never "Value delivered", which claims Cavnar caused it), the `.big` figure (count-up), "a month, measured", and the payload's `caveat` under it. Once anything tracked got worse (`value.worsened.count > 0`) the figure is `value.net_monthly` and the label reads "a month, measured, net", with a `.delta.hb-net` line saying both sides ("$1,310/mo from 3 that improved, less $180/mo from 1 that got worse") and "plus N improvements with no dollar figure" for `unpriced_wins`. Below zero it is "−$420" in `.big.neg` (`--hb-bad`), never floored; the range delta turns `.delta.bad` when the curve fell | Home's first section — the improvements alone read as if nothing had gone the other way |
| Result tone | `hbResTone(r)` — a result row's dot: improved → `.good`, worsened → `.critical`, anything else `.watch`; and `counts === false` (the owner said they didn't make the change, it faded at its re-check, or it is an alert read) is always `.watch`, whatever the number did. The server's `counts` is authoritative; an older row without it falls back to the verdict | "What your changes did" on Home and in the month card |
| Measured alongside your changes | `.hb-card.rail.ember.hb-worked` — kicker "Measured alongside your changes" (it was "What worked for you", which asserts causation over before-and-after data) + the window ("the past 6 months"), the server's sentences (`GET /api/recs/what-worked`, `owner_report`) as an `.hb-tl` rail, the caveat ("Measured before and after, not proven cause.") always under it, "See the whole record →". Not enough: one `.hb-empty` saying what would fill it, shown only where the worth section has something | On Home, right under the worth section |
| Ledger pill | `.hb-ledger span` — the leading count on an ember disc (`>.hb-num:first-child`), a figure mid-sentence stays plain | Since you started: distinct work counted, never dollars |
| Ops tile | `.lb2-ops .lb2-op` (+ `.ember` ambient) — obsidian mark `.ic.ob-tile`, count in the number face `.v` (+ `.warn` / `.good` / `.ember`), body at 15px, `.cap` footer | Labor's Time off and Covers: what the owner feeds the read. Card tier, so they weigh the same as the tiles above them. Time off shows the count and the answered record only — a request is answered in Waiting on you (density round #27) |
| Position tile | `.fc2-position .fc2-pos` — ember rail, one number in the number face, its basis under it | Food Cost's recipe coverage and unexplained waste. Food cost % itself is the page's hero (`.fc2-hero-fcp`, the same renderer without the card chrome, in the header's right column) and, once `/api/food-cost/cogs` answers, the h1's status ("31.4% · 1.4 pts over your target") — density round 9/25/26. **Food Cost hero (9/26/26):** no Today line under the h1; the food cost block has no surface in either theme (`.fc2-hero-fcp.fc2-pos` - the dark `.fc2-pos` fill outranked it), a 460px measure, a 12.5px kicker, a 16.5px sentence and its missing lines 15px, 12px apart. The week's waste beside it is green at or near the target and red over it (`_wtone`), and lines up row for row with the block (`.fc2-hero-nums` is one grid, both blocks `subgrid`: the figure beside the figure, WASTE THIS WEEK beside the first missing line or the basis, the verdict beside the second). The waste trend's outer shell (`.fc2-wt-shell`) is a step darker than the card inside it, no ember wash. **9/27/26:** nothing under the h1 (the count-date and benchmark basis line and the data-health chip are gone). **9/28/26:** no list of missing inputs under an unmeasured food cost (the dash, FOOD COST, "Not enough recorded to work it out yet", the inputs on its hover); the figures sit 32px under the chips (`margin-top:12px` on the column's 20px gap) and the waste trend 30px under them (an empty `.fc2-position` row is `display:none`), so the hero is no taller than it needs to be. In the trend, `.on` is the hover only; the week whose details are open is `.sel`, an ember outline that dims with the rest while another bar is hovered. **Later the same night:** each figure leads its column and its heading sits under it - the food cost figure and the week's waste share one line box (same size, line height 1, no margin), FOOD COST and WASTE THIS WEEK share the next row in the headings' ember (`--ember`, never `--ember2`), the sentence and the verdict the row after. Save count sits in the count grid, in the column of the last ingredient on the row under it (`fc2PlaceCountFt`). A logged waste line updates the figure, its colour, the chip, Biggest waste items, the trend and the drivers in place from the save's own `waste` (`fc2ApplyWaste`). Send to suppliers folds (`details#fc2-order-send-d`: "2 suppliers · 17 items · $9,901"). No dishes to score is a null (`.fc2-null`, a 44px dash in the number face). Recipes load from the inventory system only (`GET /food-cost/recipes`): before a sync the block says where they will come from; after it, every dish and its ingredients, read-only |
| Content calendar (9/28/26) | `.cal-grid` four across (Sunday–Wednesday over Thursday–Saturday; two under 1100px, one on a phone), every day of the week a `.cal-card` (22px padding, 250px min height): `.cal-hd` the day in Clash 20px and its date in the number face, the platform pill (12px caps), `.cal-angle` at 16px, then `.cal-acts` - "Write this →" | Pass (or Answered) as one row at the foot with the section's separator. A day with no idea is `.cal-empty` (dashed, no fill): "Nothing planned for Monday." and "Write a post →", which focuses the composer. The section's Generate week and Download are regular-size secondary buttons, as the Copy / Preview row above. Marketing's brief is the modules' AI read box (`.lb2-ai.mkt-brief`, the tag with its breathing dot), no footer line; no Today line on Marketing. Guest contacts reads at page size (`.gc-title` 22px Clash, `.gc-help` 14.5px, 44px fields, 15.5px names). Marketing's fixed overlays (Preview, Brand voice, Posting) live on `<body>` so they open where the owner is | |
| Most likely cause (9/26/26) | Never a card in place: Reviews (`#review-diagnosis`), Labor (`#lb2-diag`) and Marketing (`#guest-diag`) keep their hosts hidden, and the AI read above each carries one centred `cbtn-primary` "What's the most likely cause?" (`.ai-cause-go`, shown only while the host has content). It opens the whole card - cause, how sure, what else fits, the answers - in `#cause-modal`, a centred `.so-card` over a dimmed page (`cModal`, Escape and the scrim close it, a Close button at its foot); the content moves into the modal and back (`causeOpen` / `causeClose`). A Why? inside it (or inside any `.cmodal`) closes the modal and opens the explanation in its place (9/27/26) - it opened behind the card |
| Food Cost controls (9/26/26) | `.fc2-ctl` — one control for Food Cost's rows: 42px tall, 10px radius, `--sf-recess`, the orange chevron on a select; 220px for a select, 120px for a quantity (`.fc2-qty`, right-aligned, its figure 10px clear of the arrows), `.fc2-ctl-btn` (a regular `cbtn`, 42px, 120px min) beside them. Log waste is a kicker then one row of these, spaced from Save count - no rule between. The supplier dropdowns wear it too (190px), and so does Marketing → Analytics' period select (190px, 9/29/26), whose three figures are `.hb-stats` tiles with the change under each. Biggest waste items and Cash sitting over par sit under What to order this week. Order now's quantities are red pills (`color-mix` of `--hb-bad` at 26%, red figures), as Reduce order's are green (9/27/26). Send to suppliers lives inside the Suppliers row under This week's work (9/27/26), its own fold after the supplier picks; a deep link opens every fold around its section (`cavNavSection`). A supplier set in the Suppliers block regroups Send to suppliers at once (`fc2ReloadOrderDraft`). Send to suppliers is a fixed-column table (`.fc2-so-t`: quantity 190px, line 130px, tabular figures; a quantity caps at 9,999) with no rule under the supplier line, and its confirm is a centred modal over a dimmed page (`#so-confirm-modal`, `cModal`: kicker, "Email Sysco this order?", the count and total, Not yet / Send it). A section's action sits level with its title (`#panel-inventory .fc2-sec .hd`: fixed 16px kicker and 30px title lines). Once an inventory system has synced (`inv_sync.synced`), the count sheet, supplier picks and price monitor are read-only and say where the data comes from | Log waste, Suppliers |
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

**The web table is canonical; iOS maps onto it** (`DesignSystem/ViewModifiers.swift`):

| iOS style | Web twin | Look |
|---|---|---|
| `CavnarPrimaryButtonStyle(isDisabled:)` | `cbtn-primary` | Ember fill, ember + ember2 edges, soft ember glow. Dims at 40% when `isDisabled` OR the button is `.disabled(...)` (it reads `@Environment(\.isEnabled)`, 10/2/26) - a disabled primary never looks tappable |
| `CavnarSecondaryButtonStyle(isDisabled:)` | `cbtn-secondary` | **Quiet**: white 5% surface, 1pt ink hairline at 16%, ink text. It was ember text on an ember fill with an ember gradient border — two embers per screen wherever a primary sat beside it; neutral since the parity pass (9/25/26) |
| `CavnarSoftButtonStyle(isDisabled:)` | `cbtn-soft` | Ember text on an 18% ember fill, no border — gentle emphasis, sparingly |
| `.buttonStyle(.plain)` + ember2 text | `cbtn-text` | A row or an inline action that is really a link |

Both primary and secondary sit on `CavnarRadius.control` (12), like every
control; they were on `card` (16). iOS-only styles, each with a job the
web does another way:

- `CavnarGlassButtonStyle(isProminent:isDisabled:)` — Liquid Glass (iOS 26,
  Material below) matching `CavnarSegmentedControl`; prominent = ember-tinted
  glass, else plain glass. For paired actions on a glass surface (Skip /
  Approve). Web: `cbtn-glass` on a dark hero.
- `CavnarChipButtonStyle(tone:)` — a small solid chip in a status tone with
  a receding shadow on press, `CavnarRadius.control`. For an inline action
  inside a card (AI Visibility's suggestions). Web: `cbtn-sm` + variant.
- `CavnarSplitButton` — "do the usual thing ▾ or pick another": icon + label
  on the premium ember surface, a hairline, a chevron that opens a `Menu`.
  Web has no split button; it shows the alternatives in place.

Destructive uses `Button(role: .destructive)` inside a
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

**A form row is one height (owner, 9/30/26).** In a row of fields
(`.lb2-form`, `.ts-form`, `.rul-grid` on Labor) every control — input,
select, button — is 44px tall, radius 10, 15px text, so the labels and
boxes line up across the row. A select is always the branded one:
`appearance:none`, the 12×8 chevron at right 13px, never Safari's native
control. A set of days to pick is `.lb2-days`: 44px toggles, ember outline
and tint when checked, never bare checkboxes.

**Pay is per person (owner, 9/30/26).** Account → Targets & pay rates →
**Hourly pay** is one closed `details.co-more.tg-pay` whose summary counts
the people, those paid from the POS and those without a rate. Each role
row shows the range its people are paid (`$15–$17 an hour`) and opens to
them; the POS's rate shows as text, a box only for someone with no rate
(amber `.tg-need`: what they are costed at meanwhile). A role gets a rate
box only when nobody in it has a rate.

**The sign-in card (owner, 9/30/26).** Every field and button on
`login.html` is one box: 46px tall, the Sign in button's radius
(`--cb-radius-lg`, 11px), 15px Apfel Grotezk. A focus ring follows the
control's own corners (never a fixed `border-radius` on `:focus-visible`).
"Sign in with Google", "Sign in with Apple" (when set up) and "Sign in with a
passkey" stack under "or continue with" as `.cbtn.cbtn-lg.sso` on the card's
dark ground; small print is 12–14px. The card is 430px wide so the legal
links sit on one line with a `·` between each (they wrap only on a phone
too narrow for them). The living background has no banding: the ground —
blooms, vignette and the card's own soft shadow — is a WebGL shader computed
in float per device pixel and dithered ±1 level at the very last step
(`cavnar-field.js` `GL_FS`); the card keeps only its 1px edge in CSS. Canvas
2D (smooth 21-stop falloff, 0–2 level dither) is the fallback.

**An open section shows it is open.** A Team & rules row that is expanded
becomes a card — `--sf-recess` fill, `--hb-line` outline, a 3px ember inner
edge, its header ruled off from the content — and a section opened inside
it is its own outlined `--surface` box.

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

**Dates and times the owner picks on iOS (schedule fix UI wave, 10/3/26).**
`CavnarDateChip` (`DesignSystem/CavnarDateTimeChips.swift`) shows a picked
date the house way — `10/3/26` in the number face on a 34pt Paper2 control
with a small ember2 calendar glyph, "—" when unset — and a tap opens the
graphical calendar in a popover (`presentationCompactAdaptation(.popover)`),
where nothing is printed as a date. `CavnarTimeChip` is its time twin:
"6:00pm" on the same control, a wheel of the day's quarter hours
(`SetupWords.quarterHours`, the web's `cavTimeOptions`) in a popover. The
compact system `DatePicker` prints the locale's "Oct 3, 2026" / "6:00 PM"
and is not used for a new date or time field. In use: acting-manager dates,
standing shifts, training dates (Roster → person), the staff time-off form's
"Off until / Off from".

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
Each chip **saves the moment it is added or removed** (a change, not the whole list), and a
date picked but not added is added by the section's Save rather than dropped. A time of day is an
`.ac-select` of 15-minute steps (`cavTimeOptions`, unset reads "—"), never `<input type=time>`:
Safari draws an empty one as a grey "12:30 PM" that reads as a value (owner, 9/28/26). An empty `type=date` box carries `.is-empty` (`cavDateEmpty`), which draws it blank until it is focused — Safari draws an empty date as today's in grey, and Simple EJ's unset Period 1 start read as 9/30/26 (9/30/26).

**One figure in two units.** Where one stored value is thought of in two
units (the revenue target by the month and by the week), each unit gets its
own `.ac-input.ac-num` row and the rows carry `data-tg-pair` naming each
other: typing in one fills the other as you type, the helper text states
the conversion ("× 4.33 (52 weeks over 12 months)"), and only the box typed
in is saved — the other is display, rounded (whole dollars by the month,
cents by the week), and is repainted from what the server stored.

**Editable lines before an outward send.** A list that will be emailed
(the supplier order) shows each line with a numeric `.fc2-inv-in` input and
a running total; the send button names the recipient and the total
("Send to Sysco · $412"), and pressing it opens an inline `.fc2-inv-note`
strip that names the address, the item count and the total with "Send it" /
"Not yet" — the confirm surface for an outward action, never `confirm()`.
A read that the ledger keeps current (the ingredient price monitor) opens
read-only with one "Edit" button that unlocks it. **iOS** (`SupplierOrderSheet`):
the same editable quantity per line and a total at the owner's quantities;
"Send to …" and "Send all N orders" each open a `.confirmationDialog` whose
message names the supplier(s), the line count and the total ("Send it" /
"Not yet"); an order already on an open PO asks again as "Already sent" with
"Send it again" / "Leave it". Receiving (`DeliveriesSection`): "Received as
ordered" (secondary) and a "Some were short" text link that opens each line
with an "Arrived" number field and one primary "Receive these"; an error
stays under its order. The price monitor's ledger rows open read-only with
an "Edit prices" text link.

---

## 8. Tables

Web, and one iOS exception. `.hb-tbl` is the reference: uppercase 10px
kicker headers with a `--hb-line` underline, 12px cells, hairline row
borders, right-aligned numerics (`th.r` / `td.r`), `.nm` name cell with a
status dot, hover tint, and a clickable row when the row has a destination.
`.fc2-table` / `.fc2-mt` are the Food Cost variants (14px, same anatomy).

Wrap wide tables in an overflow container (`.fc2-table-scroll`) so the page
never scrolls sideways.

**Sortable tables** (web desk #1): opt a table in with
`<table data-sortable="name">` — the one helper (`cavSortTables`, at the
end of `dashboard.html`) turns each header in the last `thead` row into a
`.cbtn.csort-btn` (keyboard: Enter / Space), toggles ascending /
descending, sets `aria-sort` on the sorted header and shows a small chevron
(ink3 at rest, ember on the sorted column, turned for descending). Cells
sort as numbers when every filled cell reads as one (`$1,234`, `12.5%`,
`4.5★`), else as text (natural order); a dash or empty cell always sorts
last, ties keep their order, and a one-cell full-width row (a "why" row)
stays under its row. `td[data-sort-key]` overrides the text (a date, a
raw figure), `th[data-sort-type="num"|"text"]` forces the kind and
`th[data-sort="none"]` opts a column out; a header with no text is never
sortable. The choice survives a re-render. In use: Group Home's
locations, the DSR week grid, the schedule, menu margins, the dish
scorecard. Opt in where an owner compares down a column; leave out a
table whose order IS the meaning (a ranked list, a timeline).

### Print (web)

`@media print` (the "Print" block at the end of `dashboard.html`) prints a
page as a report: its own light tokens (white paper, ink text — colours
still only through variables), no header, tabs, toasts, Ask or buttons (a
`.cbtn` that IS a value inside a table cell prints as its text), cards and
table rows never split across pages, `thead` repeating, charts in colour
(`print-color-adjust:exact`) and a "Cavnar AI · printed 9/25/26" line on
top. A **Print** button is `.cbtn` with `data-print="<id>"`: it prints that
element alone (every sibling on its path to `<body>` gets
`.cav-print-hide`, removed on `afterprint`) and its title says the PDF is
saved from the print dialog. On the daily report (`dr-root`, night / week /
period), the monthly review on Home (`hb-month`), the draft schedule
(`schedule-preview-panel`) and a published week (`sched-history-detail`).

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

Dark mode uses `#D4583A` on both platforms so the ember doesn't glare
(the web's `--ember` was `#e06444` until the parity pass; its dark primary
button, `--cb-accent`, already used `#d4583a`, pinned by
`tests/test_button_system.py`); `--ember2` `#e8956a` is the soft form for
lines and kickers.

**"No verdict" is neutral, not ember.** A chip, pill, badge or bar that
carries no good/bad/warning — sample data, not yet measured, a plain count
— reads in the ink: web `.hb-chip.neutral` (no dot), iOS `CavnarTone.neutral`
(Ink2 on a Paper3 chip; it rendered ember until 9/25/26, which made "no
verdict" look like the brand's "do this").

**Cards (iOS).** `.cavnarCard()` is the default and carries no colour.
Two tinted cards exist, each once:
- `cavnarGlassCard(tint:)` — **a documented exception**: the hero card for
  a screen's one status figure (Labor's % against target), washed by that
  figure's *tone* (green on track, red over, neutral ink when it can't be
  judged) at 20% → 6% over Paper2, with a same-tone hairline. The wash
  repeats the verdict the figure already carries; it is never ember (it was
  an ember-by-default 55% → 22% gradient).
- `cavnarGlossyCard()` — untinted Liquid Glass with an ember **hairline**
  (35%), the stat tile on Marketing Analytics. The glass used to be tinted
  ember; the edge is now the card's only ember.

---

## 10. Empty, loading and error states

- **A chart with nothing to draw yet draws its empty frame** (9/28/26, Home's results curve, `hbHeroEmpty` / `.hb-hero-empty`): the chart's own height, a faint dashed grid (`--hb-line`), a dashed `--ember2` ghost of the line to come drifting slowly (still under reduced motion), the seal at ~8% opacity on the right, and a Clash headline plus one `--ink3` line saying what fills it — never a lone grey sentence where the chart belongs.
- **One tooltip per chart:** a chart that draws its own hover card (Labor's day card) carries `data-own-tip`, and the generic value chip and crosshair (`hbChartHover`, `.hb-xh`) stay off it.

**Loading is the sliding ember pulse — never a spinner and never "…".**
- Web: `.dr-pulse` (a 3px track with an ember light sliding across, `pulseBarSlide`) for a quick page load or a running job — markup `<div class="dr-pulse" role="status" aria-label="Loading …"><i></i></div>` (the `<i>` is the light; a pulse without it is a dead grey line; a `<span class="dr-pulse">` works inline, the class is `display:block`) — `.hb-skel` skeleton bars (`hbSkel` sweep), `.hb-load` + the orb canvas, `cbtnBusy()` inside a button (never a hand-rolled CSS spinner).
- The orb has **the same nine states on both platforms** (`static/cavnar-orb.js`, `CavnarOrbState`): `connecting`, `solving`, `searching`, `working`, `shaping`, `composing`, `breathing` (idle), `listening` (reserved, voice), `weaving` (several reads in one round).
- iOS: `CavnarSkeletonBar` (a 3pt one is the `.dr-pulse` twin), `CavnarSkeletonLines(widths:)`,
  `CavnarShimmerText(text:)`, `CavnarWorkingLine`, `CavnarLoadingOrb` / `CavnarOrb` / `CavnarWorkingOrb`. Never a system `ProgressView()`.
- A placeholder is never the word "Loading…": the pulse, with a plain label beside it when one helps ("Loading your shifts").

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

**Task sheet lines (staff app, iOS).** The whole row is the tick (44pt
minimum, `contentShape`), led by `StaffCheckDisc` — the `.cv-ok` disc at
28pt (scaled with Dynamic Type): `.cavnarGreen` lit from the top-left, a dark
tick, a faint halo; a hollow ink3 ring when not done, red once overdue; a
tick still travelling or parked offline is the done disc held back. A sheet's
progress is `StaffEmberProgressBar`, a 6pt ember capsule (the web `.ts-bar`),
never a system `ProgressView`. A reading or note saves with a compact
`CavnarChipButtonStyle(tone: .cavnarPaper3)` "Save" or Return, never an
ember primary per line; a photo line offers "Take photo" (secondary, camera
glyph) then "Choose from library" (text). What a line has to say sits under
it (`StaffLineNoteView`): a critical reading out of range in a red-bordered
`RedBg` block headed "Tell your manager now" (online, with a "Message your
manager" text button under it that opens the manager thread about that line,
the draft naming the reading and its range), a refusal in red, "Will send
when you're back online" in amber. The phone's copy reads "Your sheets as of
4:05pm" above the sheets, with Try again.

**Unsent and dropped offline changes (iOS).** The amber pill at the top of
RootView says what the offline queue holds ("Offline — 2 changes will send
when you're back", "1 change waiting to send"). A queued change the app gave
up on — the server refused it, it depended on one that was refused, it was a
day old, or it was for the location the owner switched away from — gets a
second amber note under it, in the same colour and type, rounded rather than
a capsule because it wraps: "1 change couldn't be sent: Approve response for
Ann. <the server's reason> Tap to dismiss." It stays until tapped
(`PendingWriteQueue.droppedNote`); a change is never dropped silently.

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
a *breathing outline* (9/28/26 - the ember dash lapping the perimeter read
as a loading ring, and Safari drew it twice): the 1.5px SVG outline sized by
`aiOrbitSize` fades between 12% and 70% over 6s (`aiRing`), and a soft light
crosses the pill once every 9s (`aiSweep`, `::after`). Nothing travels round
the edge. It is the strip's only motion besides the dot, runs only while the
strip is shown, and stops under `prefers-reduced-motion`. Reuse it for nothing else — one
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
   Erasing a departed employee's record (`people.erase_person`) is the one
   data-destructive step with its own shape: the person sheet's in-place
   `.mem-confirm` card (the merge's) saying what is deleted and what stays,
   with their name typed back as the confirmation — the route refuses any
   other text. A merge, by contrast, is undoable for 30 days from the
   team's waiting list ("Undo merge"), so it keeps its one in-place ask.

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
`CavnarMotion.swift` (the numbered set, `// MARK: - NN ·`) plus
`CavnarInteractions.swift` (working line, posted overlay, tile flash, row
entrances, code entry). The fifteen approved motions:

| # | Motion | Web | iOS |
|---|---|---|---|
| 01 | Banked Ember — the seal's slow breathe | `.cm-seal` (`cmBreathe*`) | `CavnarSealMark` |
| 02 | Seal draw-in | — (no web screen draws it) | `CavnarSealDrawIn` — defined, no screen draws it today (the login and lock screens show the wordmark alone) |
| 03 | Wordmark entrance | `login.html` `.logo` — one fade-rise, as a unit | Warm (re-lock, sign-out): `CavnarWordmarkStampIn`, now the same single-unit rise (the letter-by-letter stamp was retired on the web as choppy, and follows it here, 9/25/26). Cold launch: `CavnarWordmarkTraceIn`, the traced letters — iOS only, the one brand moment a fresh process gets |
| 04 | Composing | `.cm-compose` (driven by `_cmComposeFrame`) | `CavnarComposingLines` |
| 05 | Reading the Room (radar) | `cavnarRadarHtml` | `CavnarRadarSweep` |
| 06 | Building the Week | `cavnarWeekHtml` | `CavnarWeekBuilder` |
| 07 | Counting the Pantry | `.cm-ledger` | `CavnarLedgerFill` |
| 08 | Sparkline trace | — (web charts use `glowLine`, drawn whole) | `SparklineCanvas` (Home value chart), `DSRSparkline` |
| 09 | Count-up stat | **One engine, `cavCount(el, target, {prefix, suffix, decimals, duration, fmt})`** (9/26/26); `countUp`, `countUpMoney`, Home's `countUp` and the role donut's "$19k" all call it. It starts two frames after it is asked (past the render that asked), reserves the final width with tabular figures, eases with a sine (no racing start, a soft landing), runs 1.5-2.4s by magnitude, and advances at most two frames of time per frame - a stall pauses the count, it never jumps. Reduced motion or a hidden tab writes the value at once. Never write a private tween | `CavnarAnimatableNumber` |
| 10 | Donut sweep | — (no web donut) | `RoleDonutChart`, `FoodCostDonutChart` |
| 11 | Posted | `cavnarPostedHtml`, `cmPostedThen` | `CavnarPostedCheck`, `.cavnarPostedOverlay` |
| 12 | Handshake | `cavnarHandshakeHtml`, `cmFloat` | `CavnarHandshake` |
| 13 | Alert fired — the badge pops (0 → 1, no overshoot) with one ember ring | `#notif-badge.cm-pop` | `CavnarAlertBadge` + `CavnarRippleBurst` |
| 14 | Cold Hearth — the empty state | `cavnarHearthSvg` | `CavnarEmptyHearth` |
| 15 | Pull-to-refresh ember | — (no pull on web) | `cavnarEmberRefreshable` |

Rules:
- Ember is the only accent that moves. No bounce, no spring overshoot —
  no `.spring` on iOS (use `Animation.cavnarEase(_:)`, the
  `cubic-bezier(.2,.9,.3,1)` twin), no keyframe past its resting scale on
  the web (the badge pop, the toast and the tab indicator all overshot
  until 9/25/26). Pinned by `tests/test_design_parity.py`.
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
   their report has neither). **Nothing under the status line** on web
   (owner, 9/26/26): no since-yesterday line, quiet-hours note, data chip or
   refresh icon — `#hb-sub` is hidden; only the group Home uses it, for its
   portfolio line. Then the module pulse chips (the at-a-glance row) and the
   quick actions that no Needs-attention row already carries. On web the location
   chips for a multi-location login.
2. **The one thing** (web `#hb-focus`; iOS `HomeOneThingCard`) — one lead, one
   why, one $ figure, one action (the page's one `cbtn-primary`). Its
   footer is one line: the $ figure on the left, the actions (`.ft .acts`)
   one group flush right, centred on the figure — and **Needs
   attention** directly under it: one deterministic sentence ("7 flagged → 2
   need you now: …"), then every item — each row is its title, one evidence
   clause, its confidence as a "72%" pill (`cavConfLine(c,{pill:1})`, the
   same Why? panel) and its action, secondary. Rows past the third wait
   behind "+N more", which expands the list **in place** (never the bell).
   The comps / voids / refunds flags and every cross-module link past the
   one the focus card leads with are rows here, with Done / Pass —
   never inside the collapsed Results. When a finding takes the focus card,
   the attention item it displaced is the first row. iOS keeps the lead deck
   card and lists items 2–n under it as one-line rows with their action.
   When nothing is flagged, the one-line all-clear row.
3. How you compare. **No freshness / data-health strip on Home** (owner,
   9/26/26): the score and one pill per source live in Account →
   Integrations → Data health (`#acct-dh-src`).
4. **The day** — last night's Daily Sales Report card (its summary, "Watch:"
   the first risk, Open report; the verdict and net are the status line's),
   then the morning brief ("Before service"; the brief's DSR "Last night"
   line is left out, `morning_brief.shown_on_home`). After 8pm local the
   close-out leads the day; the weekly receipts lead it on Monday only; a
   milestone, when one lands, is an inline card at the top of the day —
   never a modal. iOS: `HomeLastNightCard`.
5. What Cavnar AI recommends — three cards of title, why, $, confidence pill
   and one action with Done / Pass; what it rests on, "Risk if left
   alone", "Could also be…", Assign and Track under the card's Details —
   and no readiness row under them (owner's call, 9/26/26): readiness leads
   the page only when nothing is connected yet, and hides once complete; a
   module with no data is a readiness item, never a placeholder tile.
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

**iOS, the same order (parity round, 9/25/26).** The hero adds the web's
"Since your last visit: …" line (`HomeChanges.line`, the first three changes)
and the quick-action row under the pulse chips (`HomeQuickAction.unsaid`:
secondary capsules, never an action a Needs-attention row carries, never Ask
or All alerts). The one thing (`HomeOneThingCard` + `HomeFocusLead.pick`)
leads in the web's order — the cross-module finding, else the first
attention item (its action is the page's one primary and the deck drops it;
"Nothing else needs you — Cavnar AI is watching." when it was the only one),
else the top recommendation (Measure it, Done / Pass; the grid drops it) —
and nothing is promoted before the day's reads land. Cross-module links are
Needs-attention rows whose "Evidence" opens `HomeLinkEvidenceSheet`; What
connects is no longer a card. How you compare (`HomeBenchmarkStrip`) sits
under Needs attention, not in Results. The brief drops what the page already
says (`HomeBriefFilter`, the web's `hbShownKeys` + `shown_on_home`) and each
line draws its action before Ask. Results holds the value band, the signal
tiles (`HomeSignals`: rating glow line, labor bars by weekday with the target
as a dashed rule and over-target bars amber, waste bars by item) and, Tuesday
to Sunday, the receipts ("Done for you · What Cavnar AI did this week" on
both platforms). The value graph's ranges are 1M / 3M / 6M / 1Y on both.
"Publish N replies" confirms on the same card Ask renders (`ProposalCard`
from `/command/propose`), every reply in its own words; the count-only card
is the older-server fallback.

**Group Home (web, all locations).** Header with the group headline; the
total strip ("9/24/26 · $24,310 net across 3 of 4 locations · +$820 vs
budget · 2 need a look" — one night only, never averaged, vs budget only when
every counted location has one); **Needs attention · all locations** with the
sentence naming the location that needs a look and every item ("+N more" in
place); then the table — Location, Last night (verdict dot, net, vs budget),
Labor, Reviews in one cell, and Food cost / Last active only where there is
room; then How your locations compare. The header's location switcher shows
each location's status dot, what needs you there and last night's net.
iOS (`LocationSwitcherView`, from `/mobile/api/home/brief/group`): the same
three on every row — the dot (red critical, amber important / watch, green
healthy; never ember) before the name, "2 need you · $8,420 net" under it —
and, above the rows, a compact **All locations** card: the group headline in
its tone, the portfolio strip ("1 of 2 healthy · 9/24/26 $12,000 net across
2 of 2 · 1 needs a look"), the sentence naming who needs a look, and up to
five attention rows across locations whose action opens the item at that
location (switching first). The full table stays desktop-first.

**One job, one place.** A job reaches Home from four sources (the focus
card, a Needs-attention row, a brief line, a Still-open row). They share one
de-dup key (web `hbSame`: every "reply to reviews" key → `replies`, low
stock → `stock`, open issues → `issues`, everything else its own key); a
brief line or Still-open row whose job is already in the focus card or
Needs attention is left out. iOS applies the same map.

**Answer in place.** Done, Pass, Not today, Track, hand-off and publish
take the answered card off the page at once (`.hb-gone`, a 200ms fade) and
re-read only the brief (`hbSoft`), never the whole page; the focus card is
redrawn only when its lead changed. A hide or snooze gets an Undo on its
toast. A write anywhere else marks Home stale (`hbDirty`), so returning to
it re-reads.

Every block keeps its empty state; a quiet day is a short page.

## 12. Reusable components

**Reports (9/30/26).** The top-level Reports tab, beside Home, opens the
daily report panel on its list (`#dsr/list`): one `.dr-list-row` per night
(weekday and M/D/YY, the scorecard verdict and score in its tone, net, the
status pill, then a row of `.dr-st` figure chips — vs last week, vs
forecast, labor %, guests, average check, overtime, from
`dsr.access.list_stats`, only what the night measured, never the summary
paragraph), newest first, 30 at a time with "Load older reports". The list
card has no inner padding and does not clip: a hovered row becomes its own
opaque tile (surface plus `--hb-tint2`, 14px radius, the `--dr-lift` ring and
shadow) and lifts 3px at 1.025 scale over .26s, so the colour covers the
whole row past the card's 1px border; reduced motion keeps the tile and drops
the movement. The end rows carry the card's 15px inner rounding. Night,
Week and Period sit beside "All reports" in the panel's `.dr-views`; there is
no "Home" link (Home is the tab beside it). In the tab bar a 16px hairline
(`.tab-sep`) sets Home and Reports apart from the modules.

**Module pills (`.hb-chip`) catch light on their top edge**: an inset 1px
highlight along the rim, an inset 1px shade along the bottom and a sheen
fading over the upper half, all in the background and shadow stack so
nothing sits over the text. On Home's status line a figure and its unit
(`.nt`: "$6,150" + small "net") share one baseline, and the Report link sits
on its own row (`.go`), never after a divider.

**Passkey offer (9/30/26).** Right after a password sign-in, a login with no
passkey is offered one: the browser's own quiet prompt where it supports
conditional create, else one `so-modal` dialog — kicker "Faster sign-in",
"Save a passkey for this device?", Not now / Save a passkey. "Not now" is
remembered on that device; Account → Security can always add one.

**Web** (all in `templates/dashboard.html` unless noted)

| Need | Use |
|---|---|
| Button | `.cbtn` + variant (`static/css/cavnar-buttons.css`) |
| Sortable table | `table[data-sortable]` — see §8 |
| Print a section | `.cbtn[data-print="<id>"]` — see §8 *Print* |
| Bulk select (Reviews inbox) | `.rv2-sel` box on each card still waiting on a decision, `.rv2-selall` ("Select shown", the filter's visible cards) in the toolbar, Shift-click for a run, and the sticky `.rv2-selbar` (count, why some can't go in bulk, Approve N / Skip N / Clear). Approve goes through `/api/reviews/approve-all` with the ids pinned and never includes a flagged or urgent draft — those are approved on their own card; both actions confirm with the count and end in one toast |
| Card | `.ac-card` + `.ac-card-h` + `.ac-row` |
| Section label | `.hb-kicker` |
| List row with status | `.hb-row` + `.d` dot + severity class |
| Task sheets (9/30/26) | Labor → Team & rules → the **Task sheets** row (`.lb2-srow`, Open ↓ like the other rows; `#ts-sec` inside `#ts-panel`, loaded when opened): a one-line intro and a `.dr-views` toggle (The day · Consistency · Edit sheets). **The day** — owners READ, no tick controls: ‹ date › nav ("Today · M/D/YY"), `.hb-stats` (Sheets finished, Lines done, Overdue now with critical open, Late ticks `.warn`, Out of range `.bad`), `.ts-note` lines (no schedule published = amber; sign-offs), then `.ts-grid` of `.ts-card`s: title + done/total, window · who · state, a `.ts-bar` (red when missed, partial or overdue), `.ts-line` rows (status dot green done / red ring overdue, `.ts-tag` Critical ember · Overdue red · Late amber · Out of range red, "who · time · reading", a proof photo thumb). **Consistency** — 7/14/28/90 days; the managers side by side as `.hb-stat`s (completion ≥90% green, <70% red, else amber; a count, not a rate, under 3 sheets), then `.ts-tbl` by person and by job code (figures right-aligned, tabular). **Edit sheets** — `.ts-ed` list (grouped by job code; `cbtn` items, `aria-current`) + editor: job code (`datalist` of the schedule's codes), shift `ac-select`, day toggles, sign-off checkbox, then lines as `.ts-eline` with ↑ ↓ Edit × and an inline `.ts-edit` form (label, section, due minutes, proof, what to record, allowed range, Critical); "Draft lines with Cavnar AI" lists suggestions each with Add; a near-duplicate says so in `.ts-note`. The staff portal's Tasks tab: one `.tsheet` card per sheet, 26px round `.tbox` tick, a reading or note field + Save, "Add photo" (camera on the phone). iOS: Labor's checklist toolbar button opens `TaskSheetsScreen` (the same three views; lines reorder with Edit and swipe to delete; a line opens a form sheet); the staff app's Tasks tab is `StaffTaskSheetsSection` (PhotosPicker for a photo line) |
| Signal tile | `.hb-sig` / `.hb-chip`. Home's Rating, Labor and Waste cards carry no caption under the chart (owner, 9/26/26; a legend still draws); the flag is toned by status (`HB_FLAG_TONE`: watch amber, needs action / over target red, on target green), never ember |
| Table | `.hb-tbl`, `.fc2-mt` |
| Form control | `.ac-field`, `.ac-input`, `.ac-select`, `.ac-switch` |
| Day in a switch's sentence | `.rul-sw .sw-day` holding `.ac-select.day-pick` (owner 9/27/26) — the one control inside a switch's own sentence: "Cavnar AI drafts every [Thursday]" (the Studio's AI tab), "Send [Monday] orders to suppliers you've used before" (What to order this week). 34px, the sentence's own size, the ember chevron, no label of its own. The same day is a plain `.ac-select` row under its switch in Account → Automation (Draft day · Order day), and the sentences there name it (`.as-draftday-name`, `.as-publishday-name`, `.as-orderday-name`, written only by `asDraftDay` / `asOrderDay`). The draft day offers Monday–Saturday (the publish goes the day after, before the week starts); the order day, every day. iOS: `AccountKVRow` + `Picker` rows in the Automation sheet |
| Chart | `glowLine`, `bars`, `stacked` |
| Loading | `.hb-skel` (several lines sit in a `.hb-skel-stack` - a 12px gap, never touching), `.hb-load` + orb, `cbtnBusy()` (the one busy helper; Account's `busy()` is a wrapper over it). A post or send in flight is a busy button, never a full-screen overlay; a header popover opens on `.dr-pulse`, never "Loading…" |
| Collapsed section | `details.hb-results` (Home's Results) / `details.rv2-analytics` (Reviews' trends under the inbox): one hairline summary row — `.hb-kicker` + one line saying what is inside, a `›` that turns when open, `:focus-visible` ring. For proof that sits under the work, not for anything that needs a decision. Content loads on first open when it costs a request (`rvOpenAnalytics`) |
| Bell badge (9/25/26) | `#notif-badge` is a **red count pill** (`--red`, the number face, ringed in the header's colour) of the urgent rows nobody has handled (`GET /api/notifications/unread-count` → `urgent`); an 8px `--ink3` dot (`.dot`) when something is unread but nothing is urgent; nothing otherwise. Messages' `.hdr-dot` is an 8px ember dot while a teammate's message is unread. Since 9/26/26 the web panel has **no summary line** (`.notif-sum` is gone); iOS `NotificationsListView` still says its `summaryLine` and ends on "Mark all read" - the web's Mark all read is the check icon beside the title. The bell's count is `scope=group`, the list `mark=0&scope=group`; a row is read when opened |
| User menu | `#user-menu-btn` (the owner's name + chevron, `cbtn cbtn-text`) opens `.user-menu` (`role="menu"`): **What's new** (its unread dot on the button and the item, `--ink3` on the button — never ember, it is not a status) and **Sign out**. Sign out is never a bordered button in the header |
| Header popovers (9/26/26) | Notifications and Messages share one shell, designed by subtraction: `.hdr-pop` (22px radius, frosted `--hp-bg`, a 1px `--hp-ring` and a soft `--hp-shadow` - no border, no rule between rows, **no close button**: Escape, a click outside or the icon again), a Clash Display `.hp-title` and nothing else in the head (the bell adds its Mark all read check icon only while something is unread). The icons are `.hdr-ico` 36px circles (lit while open, `aria-expanded`). The card drops in (`.hdr-panel-in`, 240ms, no overshoot); rows rise in once per open (`.hp-enter`, 26ms stagger, never on a re-render); a hovered row lifts 1px onto `--hp-hover` + `--hp-lift`. Empty is one short phrase (`.hp-calm`: "All caught up" with a green check; "No teammates yet" + Invite one). One time format for both, `hpAgo()`: now / 5m / 2h / 3d, then M/D/YY - a bare server stamp is UTC. Tokens are scoped to `.hdr-pop` with a `[data-theme="dark"]` set. Reduced motion drops every animation and the lift |
| Alert inbox row (bell) | `.notif-item` > `.notif-row` (`cbtn cbtn-text`), four things: a 40px tile icon by kind (`NB_TYPE_ICON` then `NB_MOD_ICON`: star, clock, box, megaphone, compass, sparkle for Cavnar AI's own reads, lock, database, flag; ember on an ember wash while unread, `--ink3` on `--hp-tile` once read), the title (600 `--ink` unread, 500 `--ink2` read), **one short line** (`_line`: what an action just did in green, else the review's own words, else the location on a group bell - twelve words at most, one line, ellipsis; a row with none is its title alone) and the time, with a red `.nd` dot under it while it still needs someone. No day headings, no filter: unresolved urgent rows lead, the rest newest first. A handled row is dimmed to .5, not labelled. The row's one action ("Read the reply" → the full reply, then "Post this reply" / "Edit it first" - an outward send shows what goes out before the button that sends it; Undo; Approve / Deny) sits over the time on hover or focus (`.notif-act`, always shown under the row on a touch screen) |
| Conversation row (messages) | `button.tm-row` (`cbtn cbtn-text`): a 44px initials avatar (`.hp-av`, one quiet grey gradient for everyone), the name, the time (ember while unread), the last message on one line ("You: " when it was yours) and an ember count pill (`.tm-count`). A teammate never messaged is the avatar and the name alone. The thread: bubbles in runs (`.tm-msg`; yours ember with white text, theirs `--hp-bubble`; the corner nearest the next speaker tucks to 6px), the time once under a run's last bubble (each bubble's own time on hover), their avatar large when nothing has been said, one pill field (`#tm-compose-input`, ember hairline on focus, "Message Justin") and a round ember send. A send shows at once at .55 opacity, then the thread refreshes in place - never behind the loading bar; a failure puts the text back in the field |
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
| Saved-state button (9/27/26) | `acSavedState(btn, saved, doneLabel, label)` — a save that landed stays said: the button turns `cbtn-success cbtn-ghost cbtn-done`, disabled, "✓ Confirmed" / "✓ Saved", until something on its card changes (a snapshot of the card's fields: `rpSnap`, `aaSnap`). Confirm profile (and a green "Profile confirmed" toast) and Auto-approve's Save rule (green only while the rule is on; saved and off it rests disabled). A press while busy does nothing | One press, and it says it worked |
| Log modal (9/27/26) | Sign-in history and Account activity open in `#acct-log-modal` (`.cmodal.so-modal`, `.ac-log-card` 620px, the list scrolling inside) over a dimmed page; the list element moves in while open and back on close (`acctLogOpen` / `acctLogClose`) | Never an inline disclosure for a long list |
| Memory lanes and facts (9/29/26) | Account → What Cavnar AI remembers: `.ac-mem-lanes` — one 6px `.ac-meter` per kind (Rules 1/30, Background, Preferences, Aims, Follow-ups; amber when full — a full lane moves its least-used fact to "Left on its own"; the count is what this login may read), each fact an `.ac-row` with its kind, subjects, author, date, due / until date and who reads it as a quiet `.ac-mem-aud` pill ("Only owners", "Just you"); Forget only where `can_forget`. The add form (`.ac-mem-opts`) is Kind and Who reads it selects, a `type=date` Holds until (Due for a follow-up), and "About" `.chip-tog` subject chips. "Left on its own" lists the archive with its reason and date, "Put it back" and "Dismiss" (a confirm) where `can_restore`, and a "Show older" text button that pages it (re-audit R3). A fact may also carry "in Cavnar AI's words" and "said again by …" in its meta, and quiet `.ac-mem-aud` pills "Pinned" and "Added by someone who left"; an account holder sees Pin / Unpin beside Forget, and one quiet line counting teammates' private notes. Saving a note that looks like one already kept asks "Does the new one replace it?" (a `confirm()`). Goals sits under it: a teammate's proposed goal with Confirm (`cbtn-success`) / Decline, a missed goal ("Its date passed without reaching it", pill "Renew or close") with Another month (`cbtn-secondary`) / Close it, then the active goals |
| Target notes (9/29/26) | `.tg-note` under a target's label: "Your goal of 26% by 12/31/26 applies" (ember2 dot — the owner's own intent, not a status) from `targets.labor.goal` / `targets.food.goal`, and "Set by the owner on 9/12/26" (ink3) from `set_notes` |
| Just for me (9/29/26) | Account → Notifications `#as-mine-card`: Push to my phone and Send me the morning brief (`.ac-switch`), My quiet hours (two `cavTimeOptions` selects, both or neither — never `type=time`), and Mute on my phone behind a fold (`.chip-tog` of the server's `alert_types`, each saving the moment it is tapped); the `.al-nudge` "You never open 5★ alerts on your phone … — mute them just for you?" from `/notifications/engagement` `mine`. For a group, `#as-org-card` names where each setting comes from as a neutral `.ac-chip` (All locations / This location / Default) with "Use at every location", which asks in place (`.ac-org-confirm`) before it posts |
| Change history (9/29/26) | Security → Activity's third log, "Change history →", in the log modal (kicker "Settings"): one `.ac-chg` row per change in the server's sentence ("Labor target: 30 → 28, by the owner on 9/12/26", `changes[].line`) |
| Distrusted data (9/29/26) | Data health (the modal and Account's card): "Data you said you don't trust" — a warn `.cf-p-row` per source with since when and what it holds back, and `cbtn-secondary` "Re-verified", which posts `{source}` to `/api/data-health/verify` and re-reads |
| Issue routing (9/27/26) | "Issues go to" / "escalate to" list everyone Cavnar AI can text (consented alert contacts) and end with "+ Add someone to text…", which opens `.ac-route-add` under the rows: name, mobile, the consent line, "Add and send issues to them" → `POST issues/routing/contact` | An empty list with no way to fill it |
| Modal | `cModal.open(id)` / `cModal.close(id)` — Escape closes the top one (then the Ask panel, then a header popover), a click on the backdrop closes it (not the two-factor setup), focus returns to what opened it, one stacking level (`.cmodal` → `--z-modal`). Older modals are adopted by id (`CAV_MODALS`) and close through their own close function. Busy overlays (posting, upload) are not modals |
| Inline field | `cField(anchor, {label, value, placeholder, hint, inputmode, save, after, onSave(value, done)})` → `.cav-field` under the anchor's row (`.cav-field-row`, `.ac-row`, `li`, `tr`) with the current value in it; Enter saves, Escape cancels, `done(msg)` keeps it open with the line in `--red`. Never `prompt()` |
| Undo toast | `toast(msg, type, {label: 'Undo', fn})` → `.toast.has-act` with one `.toast-act` text button; `cavUndoable(msg, commit, restore)` for a delete with no server inverse (§10, tier 1) |
| Confirm card (any surface) | `cavPropCard(p, host, {onDone})` — the `.ask-prop` card on Home (`.hb-pub-confirm`), in the palette (`.cpal-card`) and in Ask, one renderer; Confirm (`[data-prop-confirm]`) posts only `fields_shown` and records the answer on `proposal_id` |
| Staffing board | `.sb` (Studio → Staffing, the Staffing review, was Labor → Every day and person until 10/2/26; from `labor.staffing_board`, 9/25/26): decision cards, not a table. An executive strip first (`.sb-tile` ×3: at stake in the window, costliest, fastest win), then one `.sb-lane` per kind toned by `--tc` (overstaffed ember, strong days run lean amber, overtime red — blue was tried on 9/26/26 and reverted the same evening) with an icon, title, one-line meaning and a count. Each `.sb-card`: a severity chip (`.sb-sev`), a measured consistency ring (`.sb-ring`, the share of that weekday's days in the window with the same pattern; "—" under two), who/when, one dollar figure in the lane tone, one sentence of what happened and what to do, fact chips (`.sb-chips`, a green `.ok` chip for a teammate with room), and actions under a hairline: Explain why (the arithmetic, in the explain modal) and Ask Cavnar AI (the card's own `ask`; Plan next week was removed 9/25/26). The worst card in a lane is `.worst` (lifted, ringed in its tone). Six cards show, the rest behind Show all. Figures drop a trailing .0 (`labor._n1`). Motion: staggered rise, hover lift, ring draw-in; none under reduced motion. Nothing here is estimated beyond hours × the blended rate. The hero's line reads "$X above target at straight time + $Y overtime premium" — two parts that add up to the total (9/25/26 audit). With no pay rates set (the assumed wage) the hero reads "—" over the server's `withheld_text` ("Set your pay rates to see dollars above target…"), and overstaffed cards read "—" · "set your pay rates to see dollars"; Where the money went carries the same line under its rows |
| Staffing board (iOS) | `StaffingBoardSection` (`Features/Labor/StaffingBoardSection.swift`, 9/25/26) draws the SAME `staffing_board` object from `/mobile/api/labor` — the phone no longer builds its own overstaffed / under-target / overtime-risk lists (and never shows "near" overtime, which the web never did). A `CavnarDropdown` "Every day and person" (closed; a `labor/overtime` link opens it) holding the strip (a full-width hero tile, at stake, in ember with the number glow; Costliest and Fastest win side by side), then one lane per kind in the web's tones and words (Overstaffed days ember, Strong days run lean amber, Overtime red: an SF Symbol in a tinted rounded square, title, one-line meaning, count capsule; the dashed "No day ran over…" row with a green check when empty). `StaffingDecisionCard`: severity capsule, `StaffingConsistencyRing` (34pt, trims in over 0.9s, static under Reduce Motion; "—" / "first seen" under two days), title and when, the dollars at 30pt in the lane tone, the server's sentence, fact chips in `AccountFlowLayout` (the teammate chip green), then under a hairline **Explain why** — the web's arithmetic opened IN PLACE on a recessed panel (no modal on the phone) — and `HomeAskLink` "Ask Cavnar AI" with `AskScreen(panel: "labor")`. The lane's worst card is lifted (stronger tint, 55% tone stroke, tone shadow); six show, the rest behind Show all. `LaborMoneyWentCard` is the web's "Where the money went": the three costliest `money_went` items as tinted rows (blue overstaffed, red overtime, amber past schedule; the costliest darker with a 3pt tone edge), "Show all N →" opening the board |
| Section rail | Removed 9/25/26 (owner's call): the row of truncated section links under the tabs read as stray orange text. A section is reached by its own heading, a `data-nav` deep link or ⌘K; a module that pairs two sections names them in its kicker (Reviews: `INBOX \| ANALYTICS`). Account keeps its own `.ac-nav` rail |
| Sticky chrome | `.tabs` sticks under `.hdr` (`top:56px`); `--cav-chrome` (header + tabs, measured) is what anything sticking or scrolling into view clears (`[data-nav]{scroll-margin-top}`, `.ac-nav`) |
| One tap | `#hb-quick` (`.hb-quick`) under Home's header — the server's `quick_actions` as `cbtn-secondary cbtn-sm` buttons (Home's one primary is the focus card's), each landing on its `nav`, less any whose action a Needs-attention row already carries (`hbQuickUnsaid`; the palette's One tap keeps them all); Ask and All alerts are left to the FAB and the bell |
| Data health (how current the data is) | A composition, no new pattern: Home's header chip "Data 71% · as of 9/22/26" (`hbDataChip`, in the sub line) and, when a source is behind, the `.hb-fresh` strip's kicker "DATA HEALTH 71% · DATA AS OF 9/22/26" are `cbtn-text cbtn-muted` buttons (`data-dh-open`) that opens the explanation modal with the Why? panel's `.cf-p` rows (one per source, a `.dh-dot` in the row's tone, reliability and "next sync" as its detail line), "Not connected" rows with the one fix, the module confidence-impact rows, and a `cbtn-primary` Sync now with the `.dr-pulse` bar while it waits. Under each module header one `.dh-badge` holding a single `.hb-fresh-it` chip (the weakest source's line) + a `cbtn-text` "Data health". Amber (`--amber`) for a stale or failing line (`.dh-line.warn`), never red. iOS: `HomeFreshnessStrip`'s kicker opens `DataHealthSheet` (built on `AccountSheetKit`: hero % + sections), `DataHealthModuleBadge` under a module's own freshness line, `ServerStatusCaption` for a server status line (Reviews' fetch line, Marketing's "Metrics synced", AI visibility's "Measured") |
| Recommendation answer row | `.rec-ans` via `recControlsHtml(key, surface, module)` (JS) or `client_api.rec_controls_html` (server-rendered insight HTML) — Done / Pass / Track as `cbtn-text cbtn-inline cbtn-sm` with `data-rec-key`; one delegated listener posts `/api/recs/event` and swaps the row for a muted `.rec-ans-done` sentence. Home's `.hb-rec-ans` answer row, inline under a module's recommendation line (`.rec-line` under a read, `.rec-cites` for the reviews an Intel recommendation cites). An answered line is not rendered again anywhere. What the answer did is the server's: a tracker that started reads "Measuring labor % until 10/21/26" (`recTrackerLine`), a refused one its `tracker_refused.reason`; Home's toasts say the same. `recNotForUsHtml(key, surface, module)` is the lone "Pass" for a line whose own button is its yes (an overtime move, a content idea, a roadmap card). The decline reads "Pass" everywhere — never "Pass" (memory round 9/29/26); the toast says what the answer does in the server's words (`message`: "won't suggest it again for a year", "hidden for you; the owner still sees it"). `opts.labels` gives a question its own two answers and `opts.noWhy` skips the reason picker (a kind hold) |
| Why not? (reason picker) | `.rsn` via `recReasonPicker(after, {onPick(code, note, picker), onCancel, skipLabel})` — ONE component behind every "Pass": module lines (the `[data-rec-key]` listener), Home's cards (`hbAskWhy`), the second ✕ on Needs attention ("Just hide it" as its skip), content ideas, roadmap cards, overtime moves, Ask's "Not now", the brief's lines, the win-back text's "Pass" and the schedule review's ✕ (both send `reason_code` + `reason` with their own routes). A recessed strip under the line it answers: `.rsn-k` "Why not?" kicker (ember2) + one-line hint, `.rsn-opts` the six `rec_ledger.REASON_CODES` as `cbtn-secondary cbtn-sm` in the owner's words (Already doing this · Doesn't fit us · Too costly · Bad timing · Don't trust the numbers · Other), `.rsn-ft` an optional note `.ac-input` (Enter with a note = Other) + Skip + Cancel. One tap on a reason answers; the caller posts `reason_code` (+ `reason`) |
| Check-in card | `.rck` via `recCheckinHtml(c, surface)` over `recCheckinCandidates(outcomes, timelineItems)` — a result that landed (`status` evaluated, a clear verdict) with no `owner_checkin`, joined to its recommendation by the timeline's `tracker_id`. Ember left rail on `--hb-tint`, "Check in · result landed 8/12/26", the title, the `result_line` in the number face, "Did you make this change?" Yes / Partly / No (one tap posts `POST /api/recs/checkin`), then — after Yes or Partly — "Did anything else change these weeks?" No / Yes, something else changed (Yes re-posts with `conditions_changed`), then the result's new `attribution_label`. "Not now" hides it in this browser. Home shows one, inside "What your changes did"; the Recommendations page shows up to five |
| Confidence | `cavConfLine(confidence, {key, surface, module, cls})` (`<script id="cav-conf">`, global, `window.cavConf`) — ONE line: a 38×6 meter (`.cf-m`; iOS `ConfidenceMeter` is the same 38×6 — the web was 46 wide and iOS 34×5 until 9/25/26 — the tone colour with a sheen and a soft glow, `cmBar` grow-in; hatched `.none` when the overall is not measurable; no meter at all for a band from an older server), "**72% confidence**" (digits in the number face), "— the weakest dimension's basis" in ink3, and a **Why?** `cbtn cbtn-text cbtn-inline cbtn-sm` that opens the explanation modal (`data-explain`, logging `evidence_viewed` for `key` on its own `data-explain-surface` / `-module`). The modal body (`.cf-p`, the modal's own dark tokens) **leads with what the figure means** — `.cf-p-mean`, the payload's `meaning`, "How well supported this is — not the chance it works." (the owner's support-score decision, 9/24/26: never present the % as a probability) — then the overall figure large with a wide meter and the reason (the cap that set it, when one did), the caution (amber), then **What it rests on**: Evidence strength NN% with "Sample: 12" (of the full sample when the server sends `n_full`) · Historical accuracy NN% — the **lift against doing nothing**: its basis is the lift sentence ("improved 4 of 6 times vs 1 of 6 when not acted on" / "vs about 5% by chance") and its detail "93% likely to beat doing nothing · improved-rate range 12–76%" (`beats_label`) — or "—" with "Not enough history yet (2 measured, needs 5)" · Data freshness NN% ("as of 9/23/26", never ISO) — each with its meter and basis — and the footer ("the weakest pulls it down most"; without a track record "it stays at 70% or below"; a record that doesn't yet beat doing nothing "it stays at 70% or below"; one that leans against it "holds it at 49% or below"; on stale data "Data under 50% fresh holds it at 49% or below"; undated data "holds it at 49% or below" — every ceiling from the payload's `caps` / `caps_applied` when sent). A FACT (reviews or drafts waiting, a failing sync, a count) carries no confidence line at all. The **caution rides on the line** (`.cf-c`, amber) as on iOS, not only behind Why? (`noCaution:1` for a place that shows it elsewhere). Only a measured object draws (`cavConf.k1(detail, legacy)`): a bare band or a model's own word draws nothing, never "Low confidence". Sample or demo data (evidence 0 with nothing counted) reads **"Confidence not yet measurable"**, never "0%". Tone everywhere: **the payload's `band` — high `.good` green, medium `.mid` ink2, low or not measurable `.warn` amber — never red, never ember**; a dimension row reads the payload's `thresholds` {high, medium} when sent, else `cavConf.AT` (held to `confidence_engine.HIGH_AT` / `MEDIUM_AT` by a test). Percentages stay (the owner's call); every one is the server's (contract K1), a missing one is a dash. On: Home cards, Needs attention rows, the focus card, the review / food / labor / marketing diagnoses, food drivers, Ask answers, daily-report actions. Historical accuracy names the group its prior was read from (9/29/26: `cohort_label`, else the rung — "prior from restaurants like yours", "Compared with pizzerias on Cavnar AI"), and while the restaurant profile is unconfirmed (`prior_unlock` "confirm_profile") the row offers "Confirm your restaurant profile to compare with restaurants like yours" — a `cbtn-text` (`data-bm-profile`) that closes the panel and opens Account's Restaurant profile |
| Claim tag | `cavConf.claimTag(kind, modelWritten)` → `.ck-tag` — tiny uppercase ink3 on the recess: "AI-written" (beats the kind), "Measured", "Computed", "Forecast", "Inferred", "Estimate"; also "checked" / "unchecked" / "few reviews" as the same quiet shape. `cavConf.claimStrip(claim_kinds, names, modelWritten)` → `.ck-strip` groups a payload's `claim_kinds` by kind under a read ("AI-written read by Cavnar AI · Measured this week, severity · Inferred what to watch · Forecast next week"). Never a status colour |
| All clear | `.hb-clear` "All clear — Cavnar AI is watching." with the green check only when every source under Home is current and `monitoring.all_clear` is not false (`hbAllClear(d)`); with a stale or undated source it is `.hb-clear.warn` (amber, no check) "Nothing flagged — but N sources are out of date or undated, so this is not a clean bill", and with no live source at all it says there is nothing live to watch. The focus card's last fallback obeys the same rule (kicker "Watching", `hbNotClearWhy`). iOS: `AllClearRow(notClearReason:)` over `OwnerCopy.allClear` |
| Older read | `aiCaveat('Older read', stale_note)` above an AI read the server served from cache because a new one failed (Labor, Marketing) — kept through the five-minute session cache. iOS: `CavnarCaveat.olderRead` under the strip / above the read |
| Trend strength | A rating or waste trend says its measured strength, "trend strength 62%" (`trend_strength_pct`) with its weeks — never "medium confidence" / "early read". An older server with no figure shows the weeks alone. iOS: `ReviewsAnalyticsSection.trendStrengthLabel` |
| Reliability bar (admin) | `.calbar` in `admin.html`'s Confidence calibration card (`confidenceCalibration(GET /admin/api/calibration)`) — a 0–100% track, the observed rate's 90% range as a gradient band (green when the shown figure's ember2 tick sits inside it, amber when not), the observed rate as a glowing dot; rows under `floor_n` dimmed and not read. Beside it the Brier score and the same table by kind and per dimension |
| Freshness strip | Off Home since 9/26/26 (owner: the pills cluttered it); the same `.hb-fresh-it` pills, one per `/api/data-health` source, sit in Account → Integrations → Data health (`#acct-dh-src`, the source's line without its repeated label), grouped by the colour they show (9/27/26): green, then amber (an amber dot or a middling %), then red (0%), in the server's order within each. `renderFreshness` is kept (tested) but no longer called. Before: `.hb-fresh` under Needs attention — "DATA AS OF 9/22/26" (`data_as_of`) then one `.hb-fresh-it` pill per `freshness[]` source: a state dot (`.current` green glow, `.aging` / `.stale` amber, `.off` "not connected", `.unknown` "age unknown", `.sample` hatched), the name, its basis ("POS synced 9/23/26") and its % in the number face. `hbLive` reads `monitoring` ("Monitoring 4 live sources · oldest data 9/20/26") and never says "just now" about data that isn't |
| How you compare | A composition, no new pattern (Benchmarking #23 / #18 / #19; `benchmark_views` over the Benchmark Engine): `section.hb-card.bm-card[data-bm-module]` on Labor, Food Cost, Reviews and Marketing, filled by `<script id="cav-bench">` from `/api/benchmarks/card` — `.hb-kicker` "How you compare", who in ink2 ("Compared to 11 other Pizza restaurants on Cavnar" / "vs your own previous 13 weeks"), as-of under it, the **comparison strength as the confidence line** (`.cf` meter + "68% comparison strength" + a Why? whose panel is the `.cf-p` drawer with four rows — Peer count, Band freshness, Your own figure, Type match — and the meaning first: how well supported the comparison is, not how well you are doing), below the minimum the fixed sentence "Not enough restaurants like yours yet — here's how you compare to your own last 13 weeks." with the engine's reason under it, then one `.hb-row` per metric (dot: `.good` green ahead, `.important` amber behind, `.watch` grey level — never red, never ember), the value in the number face, the standing in words, "vs … (middle) · N% comparison strength · as of M/D/YY", and for a metric it is behind on one `cbtn-text` Ask action (`data-ask`). Home: `#hb-bench` under the freshness strip, the same rows as `.hb-fresh-it` pills, behind first. Group Home: "How your locations compare" (`cavBench.locations`), one `.hb-row` per location per metric, "in line with your other locations" unless the gap beats both locations' own swing. Strength is always a %; a band comparison is the only kind that has one. **Density round (9/25/26, #34):** a row reads label, value and standing in words only. **No Why? drawer (owner, 9/29/26):** the web card is the one short line and the rows; the who line, the strength line and each row's basis and dates are not drawn. One slot per module: after the module's own read (Labor and Food Cost under the AI read, Reviews in Trends after the diagnosis, Marketing under the brief) |
| Diagnosis block | `.diag` via `renderDiagnosis(id, dg)` — cause, the confidence line (`.cf.diag-cf`), also fits / what would tell them apart / evidence pills. One shape for labor and marketing; Reviews (`.rv-diag`) and Food Cost (`.fc2-cfo`) put the same `.cf` line in a "How sure" row and an "AI-written" `.ck-tag` in the header, and a verified cross-check carries a "checked" tag. The per-module pills (`.rv-conf`, `.fc2-conf`, `.diag-conf`) are gone. Renders only when `dg.cause` exists |
| Model-output caveat | `aiCaveat(title, detail)` (`.ai-caveat`, prepended to the read) — one shape, four titles: "Unverified numbers" (a figure that didn't trace, `applyFigureCaveat` on `figures_verified:false`), "Unsupported cause" (`causes_verified:false` — the sentence stays, said to be a guess, the first `unsupported_causes` quoted), "Unverified" (a structured `UNVERIFIED:` note from `ai_guard.unverified_note`, said whole), "Older read" (a stale fallback, the server's `stale_note`). Never deletes the model's text |
| Forecast record line | `fc2AccLine(accuracy, what)` — one sentence for any `forecast_log.accuracy` payload (K8): "past waste forecasts here have been close (6.2% mean error over 5 weeks), running 7.5% high on average"; `withheld` says the next one is held back and why; below the scoring floor it repeats the server's reason ("1 closed month scored, needs 3") and shows no figure. Sits in a `.fc2-row alt` / `.sr-note`, numbers in `.hb-num` |
| Calibrated dollar figure | When a recommendation carries `dollars_adjusted` (rec_learning.attach_dollar_calibration), the figure shown is the adjusted one and a plain `.meta` span beside it says `calibration_note` ("adjusted from 6 measured results"); the stated figure moves to the title. `dollars_adjusted: null` → the stated figure as before. Home cards (`hbRecDollars`), the one-thing hero, DSR actions |
| Result baseline line | Recommendation record results: the attribution sentence, then one `.x` line — "Compared with <baseline phrase>; normal variation is <band_basis>" — and a `.rh-pill.partial` "Not counted" when `baseline_overlaps_trigger`. The check-in card says the `grade_phrase` before it asks |
| Recipe draft flags | `.fc2-recipe` header carries an "Estimate" `.ck-tag` (`is_estimate`) or "From your card"; a line whose unit didn't convert (`unit_ok:false`) reads as `.low` with its `unit_note` in the title; `unit_warnings` and a two-plus `confidence_levels` legend as `.fc2-recipe-note`; `needs_yield` puts `.fc2-recipe-yield` (a number `ac-input`, "Plates this batch makes") at the left of the footer and Accept sends it as `yield`; `unit_skipped` is named in the toast |
| Per-check dots | `.in2-qc` (`.on` lit ember, `.off` dim) in `.in2-qrow` rows — one line each: the question (one line, ellipsis, full text as its title), a dot per run oldest first, and an `n/asked` count. `.in2-q` is the AI query results list, a different thing; the two once shared the name and the list's column layout stretched every question row into a stack |
| AI search presence results (web) | `.in2-aiv-grid`: two recessed `.in2-aiv-card`s side by side (How often AI names you with the orbit; Listing strength with its checklist, one item per row), then one full-width card (`.in2-aiv-wide`), "What AI said, by question": each question once, with the latest check's answer and, under it, that question's history (`.hist`: a dot per check, oldest first, lit when named, and "named in n of m checks"). History is never a second list of the same questions. Each card's header is `.in2-aiv-h`: the label left, a tone pill (`i.good/.warn/.bad`) right that wraps under the label rather than running into the next card; the value is `.in2-aiv-v`, its caption `.in2-aiv-cap` under it. One column under 1000px. The body has no height cap, so the loading state and late-loading rows never clip. **Loading (9/27/26):** no card behind it (`.in2-pre#aiv-loading{background:none}`) - the Cavnar AI orb at 260px (`searching`, `aivLoadingShow` / `aivLoadingHide`, destroyed when the check ends) over the rotating line. A competitor refresh loads the same way (9/28/26): `compLoadingHtml` - the 260px `searching` orb, an ember line rotating every 2.5s and one `--ink3` subline, no card - in place of the empty card, or of the list under "Who you're up against" when its Refresh is pressed (the button only disables and reads "Refreshing…"); everything comes back and the orb is destroyed when the refresh ends or fails. The radar in an ember-tinted card is retired there. Re-run AI visibility starts the run first, then scrolls the loading state to the middle of the screen (`scrollIntoView`), so the fold of the results can't cut the scroll short. **Rating hero (9/27/26):** under the figure only its standing ("0.5 above the block", green or red) - no count of places, weighting or matching basis; the greeting sentence stays |
| Account overview (9/25/26) | The hero carries the plan chip only, then `.ac-say` — one deterministic sentence from the six health items ("**2 things need you:** your POS stopped syncing; nobody receives alerts.") — and `#ac-fix-btn`, the page's one primary: the worst item's fix (Integrations → Notifications → Subscription → Security → Profile → People). Everything else is under a quiet `details.ac-more` "More", which closes on any click outside it, on a pick, or on Escape (9/27/26; the activity pill's panel likewise). "Turn on two-factor" goes to Security and opens the setup (`#twofa-modal`); `acctGo` re-aims at 700ms and 1.5s while the sections above fill in, unless the owner scrolls. The ring (`.ac-ring`) is toned by the score: green 90+, amber 60–89, red under 60 — never ember. Account's helper text (`.ac-h p`, `.ac-card-h p`, `.ac-row .l span`) is 13.5px ink3, and a row explanation over eight words sits behind `.ac-info` ("i", `aria-expanded`). A primary button only for the open editor's save. Integrations opens on **Data health** (`#acct-dh-card`, the drawer's `overall.pct`, which also drives the health grid's Integrations item); once a POS is live the others fold into one **Switch POS** row. Rail: Overview, Restaurant, Daily report (owner only), People, Billing, Notifications (led by the Calm / Normal / Everything `.dr-views` dial), Automation, AI & memory (memory and trust live here; What you've decided left 9/27/26 - Home's Your recommendations shows it), Integrations, Security, Data & privacy, Support (one card; Help & FAQ, Report a problem and Refer an owner open from it). Billing shows "Measured, net: $X/mo · N changes measured" or "Nothing measured yet" under the amount — the measured figure only |
| Staff portal | **iPhone app only** since 9/30/26 (owner: "no web version for employees") — `Features/Staff/` (`StaffPortalView`, sign-up, PIN sign-in, requests, task sheets). The web keeps one page, `staff_login.html` at `/staff/`, `/staff/signup` and the restaurant's `/staff/r/<code>`: it says the portal is in the app, shows the restaurant's join code, and keeps create-your-account because it is the opt-in page registered for the staff verification texts (A2P). No web PIN pad, no web portal. **The frame (iOS):** a system `TabView` — Today · Tasks · Requests · Me — on `Color.cavnarChrome` like the owner tabs; Requests carries a red count for what waits on the person; the inbox is a sheet over Today from a 44pt tray button with a red count, never a fifth tab. **Today**, top to bottom: header (restaurant kicker, "Hi, Jordan") → the hero (the screen's one ember: every leg of a double, the relative line, a quiet 2×2 row of `StaffQuietButtonStyle` buttons — the secondary look at 44pt, no ember) → "How did your shift go?" (only when a finished shift is due a rating) → Waiting on you → the stat tiles → Before service → On with you → A guest named you → the week (today is a labelled, brighter row, never a green rail) → Later → "Updated 3:42pm". If the week fails to load, a red "Sign out" text button sits under the failure, so leaving never depends on Me loading. A notification that names a request or an announcement scrolls to it and rings its card in ink3 (`staffFocusRing`) — a pointer, not a state |
| Decision row | `.ac-row` + `.ac-chip` answer (`done` / `not for us` / `tracking` / `measured`) — Account → What you've decided, whose foot links to the Recommendations page |
| Brief line answers | Home's "Before service" rail (`.hb-tl .it`): a line with `rec_key` and `answerable` carries `recControlsHtml(rec_key, 'home', <the line's module>, {noTrack: 1})` on its own row under the text (`.hb-tl .it .t .rec-ans`) — Done / Pass, never Track (a brief line names a move, not a number). The reason picker opens under the whole line (`recPickerHost` knows `.hb-tl .it`). A line the server marks not answerable (the money ranking, owed replies) has none |
| What was said before (9/29/26) | `recPrevHtml(x)` → `.rec-prev` lines under a card's why (Home cards, Needs attention rows, the one thing): the owner's earlier answer in the server's words (`previous_answer.text`, "You passed on this on 3/12/26 ($120/mo then) — the figure has at least doubled since", an ink3 dot) and a teammate's (`delegate_answer.text`, "Dana passed on this: already doing it (9/28/26)", an ember2 dot). The text sits in one inline `span` so its figures stay in the sentence. A quiet kind shown again carries a neutral "Back for a re-test" meta pill (`retest`); the Quieter line names each kind's re-test day ("(back for a re-test on 11/28/26)", `quieter[].review_on`) |
| Caution line (9/29/26) | `recCautionHtml(c)` → `.rec-caution` — one amber line with a 2px amber edge (the `.ask-trunc` shape): another module's evidence against a card that stays and ranks lower ("Before cutting: 3 service complaints on Tuesday nights", `caution` from `staffing_signals.trim_guard`). Home cards, the one thing, the daily report's priorities |
| Conflict chooser (9/29/26) | `recConflictHtml(cf, surface)` → `.rec-cf`, the reason picker's recessed strip with a 2px ember2 left edge, on the weaker of two cards that pull against each other (`conflict` from `lever_conflicts`: Home cards, the one thing, brief lines, the daily report's priorities, the Marketing feed): the ember2 kicker "Pulls against other advice", the rule's `label`, the `why`, "The other card: …" (`with`), "Which should Cavnar AI keep?" and one `cbtn-secondary cbtn-sm` per `choose[]` option (its title, or Keep it / Hold it) joined by a lit ember2 "or". A pick posts `{conflict, prefer: signature}` to `route.web` (`/api/recs/conflict`); the strip turns `.done` (green edge, "Settled") with the server's `message`, and Home re-reads (`hbDirty`). Never behind Details |
| Kind hold (9/29/26) | `.hb-card.rail.hb-hold` in `.hb-holds` under the recommendation cards (`hbKindHolds`, home brief `kind_holds`) — a kind Cavnar AI stopped suggesting because its measured record here did no better than doing nothing: `.hb-h3` "A question about Cavnar AI's advice", the question ("Keep suggesting trim day?"), the why, then `.hb-hold-bar`: improved (green gradient, glow) and worse (red) as shares of what was measured, the do-nothing rate as an ember2 tick on the track, and "Improved 1 of 5 · worse 3 of 5 · doing nothing improves about 30%" under it; its answers are the ledger's Done / Pass row in the server's own words (`recControlsHtml(..., {labels: answers, noWhy: 1, noTrack: 1})`, "Keep suggesting it" / "Stop suggesting it", no reason picker). Answered, the card leaves. Drawn even when no card is left to recommend |
| Brief line extras (9/29/26) | `hbBriefExtra(l)` inside the line's text: the "today" line built from last night's report (`source` dsr) lists `predictions` as `.hb-tl-calls` — a recessed box, the ember2 kicker "The report's calls", one "→ call" per line (its confidence % is in the sentence); a line that pulls against other advice carries the conflict chooser. The report's own priority (`dsr_action`) is never drawn on Home — the report shows it. "Your note for today: …" / "Remembered: …" (`memory:constraint` / `memory:event`) are plain lines |
| Policy notice (9/29/26) | `renderPolicyNotice(d)` → `section.hb-card.hb-notice#hb-notice` at the top of Home and the group Home, for account holders, for 30 days from `policy_notice.POLICY_UPDATED_ON`: an obsidian shield tile, the ember kicker "Policy update", the server's sentence ("We updated our Privacy Policy and Terms on 10/1/26: how Cavnar AI's benchmarks use pooled, de-identified figures."), a `cbtn-text` "Read what changed →" to cavnar.ai/privacy (https only, new tab) and a ✕ that posts `dismiss.web` — gone for this login on every device. A notice, not work: no rail, no primary. iOS: the same payload on Home |
| Undo question (9/29/26) | `cavUndoWhyHtml(ask_why)` → `.rsn.cav-undo-why` — after an undo of an automatic send (the AI strip's About to happen, the bell's Undo) the cancel answers with `message` (said in the toast) and `ask_why` {route, options}: "Why did you undo it?" with the server's options as `cbtn-secondary cbtn-sm` and Skip; a pick posts `{reason_code}` to `/api` + route. Asked once per undo |
| Period scheme picker | Account → Daily report: one `.ac-select` of 13 × 4 weeks · 4-4-5 · 4-5-4 · 5-4-4 · Custom (from your accounting system). A stored list of lengths shows as Custom with the lengths in the `.ac-inline` field under it (and its own Save) — a select never silently matches nothing. "Gross sales means" is a second select (items at the price rung / everything rung). "Years that run differently" lists each listed year (`.as-dsr-y`: "Fiscal 2027 from 12/30/26 · 53 weeks · 4-4-5-…", Remove) with a date + lengths + "Add year" row |
| Sales definition | Under the Sales block's loss tiles: "Gross is … Net is …" from the night's own `definition` (`gross`, `net`) — the basis it was built under, not today's setting. The Tax collected tile's caption follows it: "in gross, not net" under everything rung, "not in gross or net" under items. iOS: the same tile and note |
| Ask confirmation card | `.ask-prop` — the one-line summary, `.stake` (dollars), `.dl` details, `.pv` the words that go out, then **every field the confirmed route receives** (NS5 C1): an `.ask-sug-k` kicker "Sent with this" over a second `.dl` built from the proposal's `fields_shown` (label / value, nothing hidden). Confirm posts only those keys (`_askShownBody`). iOS `ProposalCard` mirrors it: the same "SENT WITH THIS" kicker (ember2, tracked caps) over label/value rows, and `AskProposal.postedBody` |
| Ask suggestions and rating | `.ask-sug` under an answer — an ember left edge, `.ask-sug-k` "Suggested in this answer", one `.ask-sug-i` per `suggestions[]` item with its `recControlsHtml(rec_key, 'ask', 'ask')` row; `.ask-fb` "Was this useful?" Yes / No (text buttons, `POST /api/ask-cavnar/feedback` with `message_id`), a No then offers one optional "What was missing?" line (`.ask-fb.note`: the bubble's width, field and Send on one line) |
| Cut-off answer | `.ask-trunc` — one amber line with an amber left edge directly under an answer whose `truncated` is true: "Answer was cut short — ask a follow-up for the rest." (`_appendAskCavnarTruncated`). iOS: the amber "Answer was cut short" label under the bubble |
| Schedule Studio (web) | `#studio.ss` (owner 9/26/26) — scheduling as its own full-page application at `/schedule/studio`, over the dashboard (`position:fixed; inset:0; z-index:950`: above the header, under the Ask button, toasts and modals; moved to `<body>` on load so no panel clips it; Labor's `--hb-*` tokens are declared on `#studio` too). Labor keeps `.ss-launch`, a launcher card (status of the latest week, **Build next week**, **Open the studio**). **App bar** `.ss-top` (the Cavnar AI wordmark over "Schedule Studio"; 60px, an explicit grid row so Safari centres it; three columns with equal outer ones so the step pills never move — Publish hides by `visibility`; a step is done by what exists — Setup and Summary once there is a draft, Schedule on reaching Publish — never by the stage on screen): ← Dashboard, the Cavnar AI / Schedule Studio mark, the step pills `.ss-steps` (1 Setup · 2 Summary · 3 Schedule · 4 Publish, and History; a passed step turns green, an unavailable one is disabled), the week, Saved / Unsaved edits, Publish. One `.ss-stage` shows at a time: **Setup** — `Next week, drafted by Cavnar AI` and the settings as one card of tabs (`.ss-tabs`: Basic · Forecast · Employees · Business rules · AI · Advanced) at body size (15–15.5px) with a 52px Generate; Advanced's Redo some days is a four-column grid flush with the card, its button under Thursday; **Build** — the building-the-week animation at full size (48px blocks), unchanged; **Summary** — `Schedule complete`, six `.ss-tile`s (Shift quality with a ring, Labor % of forecast sales vs target, Coverage from Shift Quality's coverage dimension, Overtime hours, Warnings, Estimated savings as the `.hero` tile tagged "Projection · not yet earned": the forecast's sales at the recent measured labor % minus the same sales at the draft's, shown only when both are measured), then View the schedule / Optimize again / Publish; **Schedule** — two columns: the week grid, and insights (400px) with tabs (the settings live only in Setup — owner 9/26/26: shown once, never twice) (`.ss-rtabs` Overview: quality, coverage, labor, cost · Fix: warnings, opportunities, AI suggestions, apply fixes, a red count of hard breaks · Shift: the selected shift); **Publish** — two cards sized by their content: who it reaches, and Before it goes out (hard and soft rules and the score from the rows on screen; the saved week's server list folded in a red `Read before this goes out · N` dropdown that opens when a send asks for it to be read, hidden while edits are on screen), then Save & send and Back to the week at one size (240×50), centred under the cards, then `.ss-done` (the posted check, Back to the dashboard / History / Keep editing); **History** — `.ss-hrow` per week (range, summary line, quality — green for an Excellent week — Sent/Draft/Replaced pill, Open). A reopened week is re-scored against today's inputs as it opens (nothing saved) and says so when the number moved ("59 when it was built, 54 now"). In the Studio, List's pencil and + Add a shift open the same centred editor as the week, and a removed shift says so. **Shifts are objects**: hover (280ms) shows `#ss-tip` (who, when, hours this shift and this week, the rule it breaks, why they were picked); click selects (`.sel`) and opens the Shift tab (facts, why, Edit, Who else could take it — legal replacements strongest first, one tap swaps — Remove); double-click opens the `.swp` editor — a centred modal over a dimmed, blurred Studio (`.swp-scrim` closes it), every field 44px in even rows; drag moves it to another person or day in the same role (`.drop` / `.nodrop` on the cell); Enter edits, Delete removes, Esc lets go. Day headers carry `.st.short` / `.st.over` against the forecast's hours (past a tenth); names carry five rating dots (`.r`) from the operational score. **Insights panel** (Overview · Fix · Shift) reads at 15px. Overview: the quality figure with "70% confidence · Why?" (the reason behind Why?), dimensions, `Across the week` set apart, the recommendations — a coverage gap carries **Fill it** (`ssFillGap`: the role's usual times for that part of the day, the strongest teammate the rules allow who is free that day and stays under 40h, staged; the ledger hears it was taken) — and `Every shift · N` folded; Coverage (by-day bars; by role only when a role is past what its roster covers), Labor (the rows on screen), Cost (four equal cards, "Priced as last saved" while edits are on screen; the PAR banner is not shown — Labor is the one hours figure). Fix: Warnings with `Hard | Soft` chips that filter every breach, one line per person and kind (an over-40h week is one line with its shift count), red for hard, amber for soft; rows the automatic fix did not reach are one quiet line; Opportunities with the sentence above its Move / Pass; Apply fixes as two options each saying what it does. The shift chip `OK TO TRAIN` marks a quieter shift a new person may learn on. Under 1320px the panels stack; under 980px the tiles go two across and the step pills scroll |
| Schedule tab (web, 10/1/26) | `#tab-schedule`, after Reports: opens the Schedule Studio docked under the dashboard header and tabs — at the tab bar's measured bottom (`ssDock`, so a view-as banner above the header never hides the Studio's step bar; `--cav-chrome` is only the fallback), the tab active while it is open; any other tab closes it first (a capture-phase listener), and closing it gives the tab bar back to the panel underneath. `labor/schedule` and `schedule` (no id) open it; Labor's launcher card is hidden. |
| Studio Setup tabs (web, 10/1/26) | **Forecast** previews the picked week before a draft (`/api/labor/schedule-forecast`): three `.ssf-head` tiles (forecast sales and its source, PAR hours, labor budget at the target) over `.ssf-day` rows — day and date, forecast sales with the nights it rests on, an hours bar, the hours — with each date's measured events on an ember line under it; a day with no forecast says why once. **AI** holds the notes about the whole week; under them `.snr` rows read each sentence (a state stripe: green a rule in force, ember a minimum Cavnar AI can hold — with an inline confirm form, *every week* or *week of M/D/YY only* — amber not checked or about one person, plain guidance); a rule about one person links to their dated scheduling notes. **Advanced** carries trim to budget, the cut floor and dining sections (rows at 16px, their notes at 14.5px — never the 12.5px rule-card size) (`.rul-sw` rows, each saved on its own change and mirrored into Labor's rules card), then Redo some days. |
| Labor → What the record says (web, 10/2/26) | Each block's heading (`#intel-body .lb2-sub`) is ember at 17px with its note on its own line beneath at 14px ink3, so the headings read first. |
| Labor → Scheduling notes, held (web, 10/2/26) | Under each note a `.mem-hold` line with a state stripe: green **Held** — the hold in words, *Stop holding*; ember **Cavnar AI can hold this** — *Hold it*; amber **Not held.** — why. |
| Labor target (web, 10/1/26) | "Vs your target": the figure and its bar are red over target and green at or under (`.lb2-over` / `.lb2-under`, the bar's `.fill.bad` / `.good`); sample data stays neutral. The overtime premium reads on one line (`.lb2-money` is a wrapping flex row). |
| Covers (web, 10/1/26) | The card shows four full rows (16 nights, `LB2_COV_SHOWN`); past that, "Show all N nights" opens `#lb2-cov-modal` (cModal) centred over a blurred scrim with every night, its range and average. No divider above the Time off / Covers pair. |
| Issue rows in the bell (10/1/26) | "An issue was opened" carries one short line saying where (`client_api._ISSUE_WHERE`: "Labor · a no-show", "Food Cost · running low") — the area and kind, never the issue's own text. |
| Rules check | `.sr-panel` via `renderScheduleReview(d)` — `.sr-head` kicker + `.sr-chip` counts (`bad` hard / `warn` soft / `good` clean, `.sr-meta` for generation time), `.sr-line` rows (`.warn` for a ⚠ line, `.fix` for "Ana → Bob", `.bad` for what still needs a human), `.sr-soft` for a warning that is not a block, `.sr-actions` for the one fix button. Re-rendered from `POST /api/labor/schedule/violations` after every edit |
| Explanation drawer | a `tr.sched-why` under a clicked `tr.sched-row` — `.k` kicker "Why <name>" on the ai tone (`--sf-ai`), then the engine's own `why` sentence, or "No facts on file for this assignment." Never a hover tooltip: the reason is a paragraph |
| Flagged table row | `tr.needs-review` — amber wash and a 3px `--hb-warn` rail, `.rr` reason under the name. The rows the rules check names and the panel's counts must always agree |
| Inline table edit | `tr.sched-edit` replaces the row: `.ac-select` for who (legal replacements first, "· can take it"), `.ac-select.tm` for times in 15-minute steps (a stored time off the grid stays selectable), `.ac-input.rl` for role (filled from the person picked), Cancel (secondary) + Done (`cbtn-soft`); "+ Add a shift" defaults to the day and times of the row being looked at. Edits stage into `#sched-edit-bar` (`.on`) with Discard + Save; the send button stays live and saves them first (Friction #16/#44) |
| Publish gate | ONE send button in the draft's header (`#ps-send-btn`, `cbtn-primary`): "Send to N staff", "Save & send to N staff" with unsaved edits. `#ps-blockers` via `_psRenderBlockers(list, arm)` lists each blocker as `.sr-line.bad` under "Read before this goes out" (from `labor/publish-check` for the saved week, or a `409 needs_ack`), and the same button becomes `cbtn-danger` "Send with N rule warnings…" — that press is the acknowledgement; any edit disarms it. In the Studio the list sits folded in `details#ss-pub-alerts` and opens only when a send asked for it (`_psRenderBlockers(list, arm, keys, open)`), never on arrival. **Sent is a resting state** (`psIsSent()`: the server's `published_at` for this week with nothing saved since waiting to go, or this page's own send): the button is "✓ Sent" (`.ps-sent`, green, full strength, disabled) and the top bar's Publish hides; a saved change that moves someone brings "Send to N staff" back. The top bar's Publish (`ssTopPublish`) is the Send button - it opens this step and sends, stopping here with the list open when the rules ask. On success the Studio's done block says the sentence (`studioPublished`); `#ps-result` is cleared, never a second green copy. Its caption is a kicker (`.ss-done .cm-plabel`: ember, the chrome face); `.cm-cap` captions are words, so Apfel Grotezk, never Space Grotesk. A sent date is the viewer's day (`mdyAt`, a UTC stamp read as UTC), never the stamp's date part. `#ps-reach` under it says who each channel reaches ("Reaches 14 of 16 · 3 in the app, 2 by text, 9 by email") with a Fix contacts text button for the email drawer. Home's "Send now" answers a `needs_ack` in place the same way (`.hb-chk-ack`). A 403 shows the server's sentence in `--hb-bad` |
| Draft verdict | `#sched-verdict` — one `.sr-note` line in the draft's header: "Every rule kept · 412h vs 420h PAR · quality 82" (hard/soft counts when broken), digits in the number face. The grid opens with the draft; what changed, PAR, the economics and Shift Quality sit under `details.co-more#sched-details` "Details". Download CSV is a `cbtn-text` in the table foot (Friction #19) |
| Waiting on you | A composition, no new pattern (Friction #18): `section.hb-card#lb2-wait` at the top of Labor, `.hb-kicker` then `.ac-row`s — a drafted week (Open it / Send to N staff), each pending time off and shift request (Deny as `cbtn-text`, Approve as `cbtn-primary`), and, after an approval that touches the open draft, "Redo these days". Hidden when empty; `data-nav="labor/requests"` |
| Module Today line | `.mod-today` under a module's h1 — the ember `.k` kicker "Today", then one deterministic sentence built from counts and stored reads the page already holds (Labor: waiting count, the costliest day to trim, who is in overtime; Food Cost: the top CFO driver, what is running out; Reviews: what to answer, the top complaint; Marketing: what goes out next; Intel: ways to improve open; the counts-only view: last counted, deliveries to receive). Never a model call; hidden rather than guessed when its source has not loaded (density round #48) | Every module page |
| Neutral chip | `.hb-chip.neutral` — no dot, no glow. For a constant (a target, a balance, a count of places tracked): a green dot beside a number that cannot be good or bad makes the real green dots mean less (density round #31) | Labor's target, Food Cost's inventory value, Intel's tracked, Marketing's chips until they carry a direction |
| How this works | `details.how-this` — one muted summary line and a `›`; the helper paragraph opens under it. Helper copy is one line at most; anything longer goes here (density round #45) | Labor's Heads up, the AI-visibility footer, the receipts help, the counts-only instructions |
| Where the money went | **Removed 9/26/26** (owner), web and iOS (`LaborMoneyWentCard` deleted): the full lists stay in the Staffing review (`#lb2-money-all`, the Studio's Staffing stage since 10/2/26) and the Staffing board. Labor's own "Schedules you've built" went the same day - the Studio's History is the one list, with Open, the CSV (`ssHistCsv`) and Delete (`ssHistDelete`) |
| Team & rules | `details.hb-results.lb2-team` — the Studio's **Team** stage since 10/2/26 (was Labor; open, its summary hidden under the stage's `.ss-kick` + h1, `.ss-side`; every opener goes through `ssOpenTeam()`, and `cavNavSection` opens the Studio for a section inside a stage) — the setup rows as one quiet list (`.lb2-srow`: a hairline between rows, no per-row ember). Availability lives inside Roster & settings. The schedule container is the plain card surface; the ember is spent on Generate | Labor (density round #28) |
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
| Learned pattern | `.lrn-row` — the engine's own sentence in `.tx`, under it `.sfw2-ev` its evidence ("2 of 3 weeks · 61% confidence"; "confidence —" under two weeks it could have happened in) and, for a dismissal made through support, "Dismissed through Cavnar AI support — not counted" with Count as mine (`POST labor/learned-patterns/adopt`); `.n` state (`in use` green / `seen once` / `not often enough` / `not counted` / `not used`), one text button "Stop using this" / "Use again" → `POST /api/labor/learned-patterns`; `.off` strikes a dismissed one through. Support's saved edits wait in a `.ts-note.warn` banner above the list ("N edits made through Cavnar AI support on M weeks don't teach the draft yet — count them as yours?", `POST labor/schedule/adopt-admin-saves`). A standing row (`.mem-lrn .it`) adds its `.mem-ln` evidence — "You said always", "Left out of the next draft to check you still want it (since 10/1/26)" in amber with Keep it / Let it go (`{key, keep}`), N of M weeks and its confidence, "last confirmed by hand M/D/YY", why it was retired — and Make it a rule for a person's habit, more of a role on a slot (a role floor) or a moved start or end (a role time rule), the account owner's |
| Scheduling setup (schedule fix round, 10/3/26) | The owner's scheduling setup said back and set where they work (`<script id="cav-sfw2">` + `#cav-sfw2-css`, pure `sfw2*` helpers tested under node). **Runs the floor** — a tri-state in the person's open roster row: three `.chip-tog .ct` chips used as one choice (Yes / No / Automatic, one `.on`, `aria-pressed`) over an ink3 `.why` line ("Automatic: counts — their role is Owner."); the account owner's only, read-only chips and the line for anyone else (`POST labor/managers`). **Dated facts** in the same row — Stands in as the manager (date ranges), Always works (weekday, start and end from `cavTimeOptions` selects, optional role and from/until), In training (role, trainer from the roster, until, optional from; "End training") — are `.rul-ov` chips with ✕ that save on add or remove, a "+ …" `cbtn-text` opening an `.sfw2-add` row of `type=date` boxes and selects, the server's 400 in red under it (`.sfw2-err`); Closes for is a `.chip-tog` that saves on each tap; Reliability is one line, "Missed 3 of 20 watched shifts · called out 1 time · late to 3 of 12 clocked shifts · late rate 25%" ("—" under the floor). The name cell carries neutral `.mem-pill`s (Training, Runs the floor) and "Also: Host" under the role; a person not worked in six weeks gets a muted row under theirs (`tr.sfw2-dorm`, recessed, ink rail) with the server's "Not worked since 8/14/26 — deactivate?" and Deactivate / Still here; a maximum past the ceiling reads "Overtime allowed up to 45h" in amber under its box. **Ratings by role** (Operational Score): under a person who works two roles or more, `.sfw2-rr` rows "As Bartender" with 1–5 `.chip-tog` chips (tap the lit one to clear); "Rated 6/1/26 — still right?" in amber with Still right; the chip counts "N to check again". **Strength targets** say each target per person under its box (`.sfw2-crew`: "8 across your largest bartender crew of 2 — about 4 a person", moving as a figure is typed). **The rules screen** opens on Who runs the floor (`.sfw2-mgr`: the "Managers: …" line, an `.sfw2-row` per manager and per one left out with its why and the owner's one-tap change, who stands in, and the ask for standing shifts in a `.ts-note.warn` with Set standing shifts), then the warnings it needs as `.ts-note.warn` or `.sr-line.warn` (no close time for a trading day — with Set the hours; floors over the section count; two stays-after-close values for one role), Roles and job codes (`.sfw2-fam`: each code and the role it is, Save roles / Back to the suggestion), Suggest from history above the floors (fills the boxes, "Not saved yet — Save rules keeps them"), Stays after close (one `.rul-row` per role), the salaried cap sentence with each salaried person's cap under it (`.sfw2-caps`, the owner's only) and Labor standards (`.sfw2-std`: a role family, the measured figure and yours, a lunch and a dinner box with Set / Use measured, each family saved on its own). A blank rule box keeps the default shown in it (left out of the save). **Closers** — a Team & rules row (`data-nav="labor/closers"`): the >30% warning, where the closer rule holds and why, the roles that close as a `.chip-tog`, one `.sfw2-cl-role` card per role with its closers, support-marked closers with Count them as mine, then the punches' suggestions as `.sfw2-row.sfw2-sug` (a checkbox for each change, a Keep / Unmark / Add pill — amber unmark, ember add, green keep — the reason under the name) and one primary Apply. **A staffing rule's check** (Account → memory) is `.sfw2-chk` under the fact: green rail "Checked on every draft as: …", amber rail "Not checked by the schedule." **A call-off's gaps** on Home sit under the issue row (`.hb-gaps`): each person, their status (missing red, arrived / covered by Lu green) and one secondary button per open gap — "Ask Lu to stay on for Ana's 5:00pm" / "Ask Pat to cover Bo's 5:00pm" — the same list the /i/ page shows. The Forecast tab's third tile is **Hourly budget** with the budget's own sentence (never "$X at 35%"), the owner's salary line, an assumed wage's caveat as an `.sw-flag` with Set pay rates, each date's demand ("+30% against a typical Friday — Homecoming", `.ssf-dm`) and the sales freshness line |
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
| Forecast effects and the night's actual weather (9/29/26) | The Sales block's forecast note adds the typical night before the measured effects ("Before tonight's measured effects, a typical night here was $4,100", `baselines.forecast.base_net`) and one `.dr-chips.dr-eff` chip per effect ("Rain −12% · 5 nights", green up, red down, figures in the number face; a game's own effect — `game_event_id` — is its name alone, because the game's item says its one figure, event re-audit 2), or "No forecast to compare with: …" (`baselines.forecast.reason`). Tomorrow's forecast card carries the same chips and "Before those effects: $X on a typical Tuesday". The Intel block adds a "Weather (actual)" tile (`detail.weather.observed`: the high, the summary, the station basis) beside the forecast tiles. A night whose target is the owner's goal says "Your goal of $9,000 a night" and "vs your goal", never "Budget"; a goal KPI target reads "Your goal by 12/1/26: 30%" |
| Left out, with its reason (9/29/26) | `.dr-left` under the verification footer: a priority the rest of the product argued out (a live campaign filling the night a cut names — `dropped[]` with a `key` and a `why`) — the line in ink2, "Left out: …" in ink3, a hairline left edge; counted apart in the footer ("N left out for what the rest of Cavnar AI knows") and never as a failed check (`failed_check`) |
| Progress checklist | `.dr-prog` card with `.dr-pulse.wide` (the sliding ember pulse) over `.dr-step` rows — `.done` the green check disc (`.cv-ok.cv-md`), `.now` breathing ember dot, `.gap` the amber disc (`.cv-warn.cv-md`) with the block's reason, `.todo` dim — each with the server's own local time (`.at`, "10:14pm"). Polled every 5s, skipped while `document.hidden`, caught up on `visibilitychange` |
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
| Recommendation answer row | `RecAnswerRow` (`DesignSystem/RecAnswerRow.swift`) — Done / Pass / Track as small text buttons, POSTs `/mobile/api/recs/event`, then a muted confirmation line (the server's `message`) and, when a tracker started or could not, a `RecTrackerLine` under it. Track only where `RecAnswer.trackableModules` (reviews, food, labor — never marketing or intel). The same row under every recommendation the phone shows: module reads (Reviews, Food Cost, Marketing, Intel, Labor), diagnoses, the Daily Report's Tomorrow actions (surface `dsr`), Home's one thing, a cross-module link's Evidence sheet, loss flags and the brief's answerable lines (surface `home`; the brief's without Track), content-calendar ideas, the AI-visibility roadmap, Ask's suggestions |
| Why not (reason picker) | `.recReasonDialog(isPresented:title:message:skipLabel:onSkip:onPick:)` (`RecReasonDialog`) — a confirmation dialog of the six `RecReason`s in owner wording ("Already doing this", "Doesn't fit us", "Too costly", "Bad timing", "Don't trust the numbers", "Other"), sent as `reason_code`. Every Pass asks it — the win-back text's and the schedule review's ✕ too (skip "Skip", `reason_code` on their own routes); a second hide asks it with "Just hide it for two weeks" as the skip; Ask's "Not now" with "Just not now". Never a text field on the floor. Hold the target in its own `@State`, apart from the dialog's flag |
| Tracker line | `RecTrackerLine(text:)` — gauge glyph in ember2 + "Measuring labor % until 10/21/26" (the server's `label_text`) or the refusal's own `reason`, mixed text. `RecTrackerNote.extraLine` drops it when the message already says it |
| Answer pill | `RecAnswerPillStyle(selected:)` — capsule, ember hairline on a faint ember wash, 13.5 bold ember2; the chosen one fills. For one-tap answers to a question Cavnar asks (Yes / Partly / No, Was this useful? Yes / No), never for navigation |
| Ask rating note (iOS) | `AskFeedbackRow` (`Features/AskCavnar/AskCavnarView.swift`) — after a No on "Was this useful?", "What was missing?" (12.5 semibold ink3) over one Paper2 field ("Optional", 500 characters, focused) and a Send answer pill on one line; posts the same `/ask-cavnar/feedback` rating with `note`, Send on an empty field just closes it. The web `.ask-fb.note` |
| Voice Ask (iOS) | `AskMicButton` + `AskVoiceStatus` over `AskVoiceInput` (`Features/AskCavnar/AskVoiceInput.swift`) — a 38pt circle (44pt hit) between Ask's field and Send: Paper2 with an ink2 `mic` when idle, the Send button's ember gradient with a white `stop.fill` and ember shadow while listening. Speech (on-device when the recognizer supports it) is written live into the question field — appended to what was typed — and is never sent on its own; the owner sends. While live the header orb runs `CavnarOrb` `.listening` and a line above the field reads "Listening. Tap stop when you're done, then send." (ember `waveform`, ink3). Stops on tap, 2s of quiet after words, 8s with none, 55s at most, on Send, on leaving the tab. A denied permission is an amber line with a Settings text button; nothing heard is an amber "Didn't catch anything". Haptics: medium on start, light on stop, warning on a refusal |
| Value, net (iOS) | `HomeValueBand` / `ValueChartCard` — "MEASURED RESULTS · PER MONTH"; with nothing measured the band reads "Nothing measured yet" in ink (no green, no glow) and draws no curve at all (the hard-coded rising curve it fell back to was a synthetic series with no label); `ValueChartCard`'s example curve is captioned "Illustration only — not your data or any restaurant's". Nav title and VoiceOver say "Measured results". Once `value.worsened.count > 0` the kicker reads "MEASURED RESULTS · NET PER MONTH", the figure is `net_monthly` (red "−$X" below zero, never floored) and a breakdown line ("$X improved, less $Y from N that got worse", N = `priced_count`) replaces the delta; the chart gains the caption "The line is the improvements measured each day; the figure above is net of what got worse." The worth card lists each unpriced win as a green row ("…, improved — measured, no dollar figure") and shows the card for them alone. An older payload reads as before |
| Result tone (iOS) | `RecOutcome.standing` → `tone`: `counts == false` is neutral whatever the verdict; the server's `counts` rules, the verdict only when it is absent. Home results, the record and the month card |
| Check-in | `RecCheckInCard(outcome:surface:onAnswered:)` — "CHECK IN" kicker, the result line, "Did you make this change?" as three answer pills and a "Something else changed these weeks too" check row; one tap posts `/recs/checkin` with the result's `tracker_id` (web and iOS both: the answer lands on the result the card shows, not the key's newest episode) and the result reloads (its attribution changes; web re-reads it by id, `/api/outcomes?ids=`). Shown when `RecCheckIn.isDue`: evaluated, clear verdict, no `owner_checkin`, not informational, a recommendation's key. Home shows at most two under What your changes did; the rest wait in the record |
| Measured alongside your changes | `WhatWorkedCard(whatWorked:)` — `HomeSectionHeader("Your record", "Measured alongside your changes")` over the server's sentences, verbatim, green dots, and the `CavnarCaveat` "Before and after, not proof" always under them, `.cavnarCard()`. Renders nothing unless `enough` and a sentence exists |
| Recommendation record | `RecommendationHistoryView` (`Features/Recommendations/`) — the Account identity-card kit: `AccountHero` (the web's subtitle: "What Cavnar AI suggested, what you did with it, and what was measured afterwards. Anything left unanswered counts as not acted on.") → 30/90/180 `CavnarSegmentedControl` → the web's order (parity round, 9/25/26): **Check in** ("Did you make these changes?", up to five `RecCheckInCard`s joined to their timeline item — the timeline row then doesn't ask again) → **Your record** in three `AccountStatTile`s (Acted on / Measured better / Open now, `RecSummaryFormat.tiles` from `totals`: "—" below a rate's floor with the count that would fill it, Measured better green at half or more) with each tile's basis under them → `WhatWorkedCard` ("Measured alongside your changes", 90 days at least) → "What you followed" (per module: shown · followed · said no · ignored, the rate in the number face with its likely range only when `enough`, "Not enough yet" otherwise) → "Most effective for you" → the timeline (title, module · shown date, answer chip + `AccountPill("Validated")`, the why, then its tracker: `RecTrackerLine`, an amber `PARTIAL` capsule before the interim reading, "Stop measuring" in red behind a confirmation dialog; or the result line, attribution sentence, other changes those weeks, the re-check, and a `RecCheckInCard`), paged with "Show older ones". Opened from Account → Recommendations and Home's "What you followed →" |
| Confidence (iOS) | `ConfidenceLine(confidence:recKey:surface:module:)` (`DesignSystem/ConfidenceLine.swift`) over `TrustConfidence` (`Models/TrustConfidence.swift` — decodes the K1 object, a bare band string, or an older `{band, label, reason}`; every field optional; `thresholds` and `caps` when sent; `TrustConfidence.measured(detail, legacy)` so a bare model band draws nothing) and the pure `ConfidenceDisplay` (tone from the payload's band, label — sample data "Confidence not yet measurable", never 0% — rows with "Sample: N" and the accuracy row's lift sentence + "N% likely to beat doing nothing" (`beatsLabel`), the `meaning` line the Why? sheet leads with, footer with every ceiling in `caps_applied` — unit-tested): the meter (`ConfidenceMeter`: tone gradient, glow, grow-in), "72% confidence" in the number face, the reason in ink3, and a plain **Why?** text button that opens `ConfidenceWhySheet` — the Account kit (`AccountHero` with the overall figure and a wide meter, `AccountSection("WHAT IT RESTS ON")` with Evidence strength / Historical accuracy / Data freshness, each value, meter and basis) — and calls `RecEvidenceLog.viewed`. Same tone map as the web (`.cavnarGreen` / `.cavnarInk2` / `.cavnarAmber`; never red, never ember). Replaces the bare "MEDIUM" capsules and the strength chip |
| Freshness strip (iOS) | `HomeFreshnessStrip` (`Features/Home/`) — the web `.hb-fresh`: "DATA AS OF 9/22/26", then a capsule per `freshness[]` source with its state dot (current green, aging / stale amber, not connected / age unknown ink3, sample hatched), name, basis and % in the number face. An older server's "fresh" with no date reads "age unknown", never current |
| How you compare (iOS) | `HowYouCompareCard(module:)` (`Features/Home/HowYouCompareCard.swift`, models in `Models/BenchmarkCard.swift`, one `BenchmarkCardStore` per session) — `.cavnarCard()`, the ember2 kicker, who and its as-of, the strength line (`ConfidenceMeter` + "68% comparison strength" + Why? → `ComparisonStrengthSheet`, the Account kit with the confidence sheet's rows through the shared `ConfidenceDimensionRow`), the below-the-minimum sentence and its reason, one `HowYouCompareRow` per metric (tone dot good green / behind amber / level ink3, `HomeAskLink` for a behind metric). Under Labor's benchmark bar, Food Cost's stat strip, Reviews' period picker and Marketing's attribution card. Home: `HomeBenchmarkStrip` under Needs attention (§11b step 3), never inside Results. The locations sheet: `LocationComparisonSection` (Account-kit sections per metric). Draws nothing until the server answers or for a login that can't see the module |
| Worth split (iOS) | `HomeFollowThrough`'s worth card: "What was measured" over the measured figure, "What Cavnar surfaced / still available" over the estimate (each avoided item with its stated rate and basis), the alert total and the gap (amber, not ember) — never summed |
| Server tone (iOS) | `ServerTone.color` (`DesignSystem/ConfidenceLine.swift`) — a tone the server decided (`*_tone`: AI visibility, listing strength, market standing, holiday/benchmark words): good `.cavnarGreen`, warn `.cavnarAmber`, bad `.cavnarRed`, neutral `.cavnarInk2`; nil when none was sent and the caller's fallback applies. Never ember |
| Unverified cause (iOS) | `CavnarCaveat.unverifiedCauses(_:)` — the third caveat beside unverified figures and names, titled "Unverified cause", when a read carries `causes_verified == false` (Reviews, Marketing); quotes the first `unsupported_causes` sentence. The web twin is the "Unsupported cause" `.ai-caveat` |
| Calibrated dollar figure (iOS) | `RecDollarCalibration.figure(raw:adjusted:)` / `.note(adjusted:n:note:)` (`Models/HomeSummary.swift`) — "$X/mo · adjusted from N measured results": the adjusted figure in place of the raw one when `dollars_adjusted` is non-null, else `dollars_monthly` as before. Home recommendations, the one-thing card, nightly-report actions |
| Not counted (iOS) | An outcome row whose baseline overlaps the trigger (`baseline_overlaps_trigger`) gets an amber "Not counted — …" line under its result; the grade phrase and what it was compared with follow in ink3 (`RecommendationHistoryView`, `HomeFollowThrough`) |
| POS sync line (iOS) | `AccountConnectionsDetailView` — under each POS connection header a 6pt dot plus text from `sync_state` / `age_days` (current green, aging/stale amber, error red), dates M/D/YY; RPOWER's row uses a `GlowBadge(systemImage: "server.rack")` tile (no brand mark). Google reads "Google reviews (sampled — Places returns 5 at a time)" when `source` is `places_sampled` |
| Recipe yield (iOS) | `RecipeDraftsSheet` — a batch draft (`needs_yield`) shows a "Plates" field (`cavnarTextFieldStyle`, number pad) and Accept stays disabled until it holds a yield, sent as `yield`; `unit_skipped` lines are named after Accept |
| Forecast line tag (iOS) | A computed forecast line (Reviews and Marketing `forecast`, the brief's forecast line) carries `ClaimKindTag("forecast")` |
| Claim tag (iOS) | `ClaimKindTag` — the web `.ck-tag`: tiny uppercase ink3 on a Paper3 capsule, "AI-written" / "Measured" / "Computed" / "Forecast" / "Inferred"; also "Checked" / "Unchecked" on a scanned invoice line (`verified`) |
| Counts-only Food Cost (iOS) | `FoodCostCountsOnlyView` — for a tile with `mode: "counts"` (or a 403 `module_forbidden` on the analytics): kicker "FOOD COST · STOCK", "Counts & deliveries", the status line (last counted · deliveries to receive), Count (primary) and Log waste (secondary), then `DeliveriesSection` with no cost anywhere. The web's counts-only panel |
| Log waste (iOS) | `WasteLogForm` — kicker "LOG WASTE", an ingredient menu picker, a quantity field and a reason picker (the web's six reasons), one secondary "Log it"; the result in green under it. On the count sheet under the count, and alone as `WasteLogSheet` ("inventory/waste") |
| The one thing | `HomeOneThingCard` — `.cavnarCard(.hero)`: module names joined by `EmberThread` (two or more only), the action at 18, why, "To confirm:", up to three evidence lines, its `ClaimKindTag` and `ConfidenceLine`, $/month in the number face with what it covers under it (`dollars_basis`; a `money {low, high, label}` range stays a range), "Could also be…" (an alert; records `evidence_viewed`), an Ask link, and its `RecAnswerRow`. After Needs attention, before the recommendations (§11b) |
| Labor diagnosis | `LaborDiagnosisCard` — `.cavnarCard(.ai)`: "WHY LABOR RAN OVER", the `ConfidenceLine`, summary, most likely cause, it could also be, "Check this" (the action) with its `RecAnswerRow`, cross-checked against. Nothing when there is no cause |
| Evidence viewed | `RecEvidenceLog.viewed(key:surface:module:)` — call when the owner opens a keyed recommendation's reasoning ("Could also be…", "Why this matters"); once per key per launch, fire and forget |
| Field | `AccountField`, `CavnarFloatingField`, `CavnarDropdown` |
| Switch with its record | `AccountSwitchRow(label:detail:isOn:busy:)` — the `detail` is the owner's own record behind the switch (Automation & trust) |
| Score movement | `ScoreDeltaChip(delta:)` — "+3" green / "−2" red capsule in the number face (`ShiftQualityPanel.swift`) |
| Decide-in-place row | `TimeOffSection` row: name + dates, Deny (secondary) / Approve (primary) side by side, status text once answered |
| Needs attention (iOS) | `HomeActionDeck` — the lead `ActionDeckCard` (primary CTA, secondary link, ⋯ Not today / Hide) and every other item as an `ActionDeckRow` under it in one Paper2 card: title and detail on one line each, the CTA as an ember2 text button with a chevron, long-press for Not today / Hide. The lead plus three rows show (`shownByDefault = 4`, the server's `HOME_ATTENTION_SHOWN`); "+N more" (number face, 44pt) opens the rest in place. No swipe deck, no dots. Directly under the header strip (§11b). A card's tap follows its `nav` (the filter, section or item), never just the module |
| Bell and location (iOS) | `CavnarBellButton` (`Core/AppChrome.swift`) — the one bell, on Home and every module screen's trailing toolbar, with **the web's badge rule** (`CavnarAlertBadge`): a red count pill in the number face ("9+" past nine) of the urgent rows nobody has handled (`/mobile/api/notifications/unread-count` → `urgent`), a 7pt Ink3 dot when something is unread but nothing is urgent, nothing otherwise — it was one ember dot for anything unread. The sheet opens at once on its skeleton. The Home tab's system `.badge` carries the **urgent** count (a system badge is red, so an unread-only count there would say "urgent" when the bell says a grey dot). `CavnarScreenTitle` (drawn by `cavnarTitleToolbar`) adds the location's name under a module screen's title — ember2, 11.5, a chevron-down — for an owner with more than one location; tapping it opens `LocationSwitcherView`. A switch resets the Modules stack and never replays the landing intro |
| Queued send (iOS) | `PendingActionSheet` — medium detent, Account-kit chrome "Queued send": "GOING OUT ON ITS OWN" kicker, the action's label (Clash 20), "Goes out at 11:00am" on the restaurant's clock (M/D/YY when not today), Review (secondary → the schedule or order it sends) and Undo (primary, acts at once — it only changes a row nothing has run yet). Opened by an `action/<id>` nav path: the "goes out at" push, its notification row, a card |
| Review queue (iOS) | `ReviewDetailView` pins Skip / Approve to the bottom (`.safeAreaInset`, Paper at 94% over a hairline). With another drafted reply after this one in the list's order the primary reads "Approve & next": a 0.45s check capsule (green check + "Posted to Google"), then the next reply in place; the full `CavnarPostedCheck` plays only on the last. The inbox opens on "To approve" when replies are waiting; a trailing swipe "Approve" (green) exists only for a reply that may go out unread (drafted, not flagged, not urgent — the bulk-publish bar) |
| Undo toast (iOS) | The web's `cavUndoable` on the phone (§10, tier 1): the thing leaves the list at once and a bottom capsule — `Paper2` fill, 1px `Paper3` stroke, "Stopped tracking <name>" (14.5/600 ink) and an **Undo** text button (ember2, 44pt) — stays 7 seconds; only then is the request sent. Leaving the screen inside the window keeps the thing. First use: stopping a competitor (`IntelView.stopTracking`) |
| Staff app: Requests, Me, Inbox (iOS, employee audit I3) | Content views for the staff tabs (`StaffRequestsView`, `StaffInboxView`, `StaffMeView`; they sit in the tab's scroll view and do not scroll themselves). **Requests**: Waiting on you (swap asks, offers, open shifts — hidden when empty) → Your requests (time off and shift changes, a `TonePill` status, the manager's note) → the one primary, Ask for time off. Accepting a swap or an offer and picking up a shift open a `.confirmationDialog` that names what moves; Withdraw and Call it off are tier 1: the row is replaced in place by the Undo capsule above (`StaffUndoCapsule`, the same Paper2 capsule, because these views can't reach the screen's foot) and the request goes after 7 seconds. A row's answer shows `CavnarInlinePosted` at the top of Waiting on you; a sheet's send is `.cavnarPostedOverlay`. A failure is one red sentence under the control (`StaffUI.errorLine`); a section that didn't load is `StaffUI.failedCard` with Try again — never an empty list, never an editable form. A busy text button reads its verb with the shimmer (`StaffShimmerLabel`, plain text under Reduce Motion), never "…". A choice from a small set is `StaffChoiceChip` (44pt, ember outline and 18% tint when on — the iOS twin of `.lb2-days`); a time of day is a folded `CavnarDropdown` of half-hour chips with No limit, never a wheel. An open shift or an offer that would take the person past 40 hours shows the server's overtime sentence in amber, and its confirm repeats it. An announcement translated into the reader's language has a "Show original" / "Show translation" text button. **Me** is `AccountSection` rows: My availability and What I'd like open sheets; reminders and schedule texts are `AccountSwitchRow`s (texts hidden when `sms_available` is false); Sign out is red text at the foot |
| What changed (Intel) | Under Nearby competitors on both platforms (`#in2-moves` / `IntelView.movementSection`), from `/api/intel/movement`: the kicker "What changed", one quiet line ("Since your check on 9/14/26: new places that opened near you, places Google marks closed, and ratings that really moved", or that nothing did, or — before two weekly checks exist — that it needs two to compare), then rows: the name with an ember "Newly opened" (never tracked before, at most 60 reviews: it opened) / "Closed" (Google's business status, checked for each place that dropped out of the search) tag — never a place merely dropping in or out of that week's search (owner, 9/29/26), and up to three rating moves past the noise floor as "4.2 → 4.5★ · +38 reviews" |
| Flagged reply confirm (iOS) | A drafted reply the reply guard flagged shows the amber "Read this one before you post it" banner (following every regenerate and save); Approve then asks "Post this reply anyway?" in a `confirmationDialog` naming the reason, like the web's `_revFlagOk`. The server refuses a flagged reply without that confirm, so a swipe or a lock-screen action says to open it |
| Notification row actions (iOS) | `NotificationsListView` rows: the tap opens the row's `nav`; an actionable row carries its one action as an ember2/red text button at the right (44pt) and the same as a trailing swipe — Undo on a "going out" row when exactly one such send is pending, Approve on a reply that may go out unread — then says what happened ("Undone", "Posted") in green. Errors are one red line under the row |
| Lock-screen actions (iOS) | Push categories (`PushManager`): `CAVNAR_REVIEW_DRAFTED` Approve & post · Edit, `CAVNAR_UNDOABLE` Undo (destructive) · Review, `CAVNAR_REQUEST` Approve · Deny (destructive), `CAVNAR_BRIEF` Ask about this (sends), `CAVNAR_REVIEW` Reply. Approve / Undo / Deny run without opening the app, behind the phone's unlock; a failure comes back as a notification that opens the item |
| Tap targets (iOS) | 44pt minimum: `cavnarToolbarIconGlass` keeps its 34pt disc inside a 44pt hit area; review chips, deck buttons, row actions and the daily report's night stepper grow their frame, not their look |

| Waiting on you (Labor) | `LaborWaitingOnYou` — a `.cavnarCard()` directly under the Labor hero, ember2 kicker "WAITING ON YOU" + count in the number face, one decide-in-place row per pending time-off / shift request (both buttons secondary here: the screen's one primary is Send), the name opens the person sheet, a drop carries the text link "Approve opens it for anyone to claim · Name who covers". Hidden when nothing waits |
| Pinned send bar | `LaborSendBar` in `.safeAreaInset(edge: .bottom)` once a saved draft exists: "Review N" (secondary, scrolls to the rules check) + "Send to staff" (the screen's one primary, disabled with "Save your changes before sending" until saved) on a Paper wash with a hairline top rule. The inline Send under the table is gone for a saved draft |
| Action row | `FoodCostActionRow` under Food Cost's sub-tab control, on both sub-tabs: three equal Paper2 tiles (icon over a 12.5 bold label, 44pt minimum) — Scan invoice · Count · Order — and a 44pt "…" menu (Recipes, Menu margins, Invoice from a photo). Secondary weight: the one primary stays the form's own |
| Document scan | `InvoiceScanSheet`: "Scan with camera" (primary, VisionKit `DocumentCameraView`, full-screen cover) over "Choose a photo instead" (secondary); only the picker when the device has no camera. More pages than the server reads says so in amber, never drops them silently |
| Command sheet | `CommandSheet` (`Features/Command/`), a `.large` sheet with `accountSheetChrome("Find or ask")` and the field pinned at the bottom (`safeAreaInset`, Paper2, ember hairline when focused). Empty: "One tap" capsule chips (44pt) — Home's own `quick_actions` (`HomeQuickActionsStore`, as the web palette's One tap; All alerts left out, the bell is there), the built-in places only until Home has loaded — "Waiting on you" rows (severity dot, mixed-text title, detail; pending sends carry "Goes out in …" + Undo; requests carry Deny / Approve, both secondary), "Locations" chips with a ✓ on the current one. Typing: "Go to or do" rows (arrow for a place, bolt for an action — an action opens Ask's `ProposalCard` in place), "Found" rows with the store named when it isn't this one, and always last "Ask Cavnar AI: “…”" in ember2. Return opens or asks, never confirms. Reached from Modules' toolbar magnifier, `cavnarai://command`, the widget and the "Find in Cavnar AI" shortcut |
| Person sheet | `PersonSheet(target:)` (`Features/People/`) — the Account identity-card kit: name in Clash, role + Active chips, `AccountSection`s "Scheduling" (hours, availability, rating, certifications — "Not set" / "Not rated", never 0), "Login" (PIN set), "Contact and pay" (role, phone, email, POS id, pay rate fields; one primary Save that posts only what changed). Opened from a roster person, Account → Staff accounts (menu "Person record"), a name in Waiting on you and a person in the command sheet |
| Who runs the floor (iOS) | Schedule fix UI wave, 10/3/26. Person sheet (`RosterDetailSheet`, `PersonScheduleFacts.swift`): **Runs the floor** — three `SetupChoiceChip`s, Yes (ember) / No / Automatic, and the why in 13.5 ("Automatic: counts as a manager — their role is Manager FOH."), the account holder's alone (`can_edit_owner_facts`; others read it with "Only the account owner sets who runs the floor."); **Stands in as the manager** — date ranges as rows with a red ✕ chip, From/until `CavnarDateChip`s and an optional note, each add or remove one save; **Always works** — weekday chips, start/end `CavnarTimeChip`s, an optional role menu and dates, "Add Mondays"; **In training** — role and trainer menus, until (required) and from, a muted "Training" chip and "TRAINING" on the roster row; **Closes for** — role chips (none = their own role); **Attendance** — Missed "2 of 24 · called out 1 time", Late "3 of 18 clocked shifts (17%)", the rate "—" under six clocked shifts, amber past the engine's line; a dormant person gets an amber-wash notice "Not worked since 8/14/26 — deactivate?" with Deactivate / Still here (both secondary). Rules sheet: `RulesManagersSection` — the "Managers: …" line, each name with a Change menu (Not a manager / Counts as a manager / Back to automatic), NOT COUNTED below, who stands in, and an amber-wash "Which days and hours do … work?" with a link per name to their sheet |
| Closers cleanup (iOS) | `CloserCleanupSheet` (Roster & rules → Closers): Account-kit chrome, the hero "N of M marked to close", the closer definition in plain words, the >30% warning as a `CavnarCaveat`, closers by role as muted chips, the roles that close as toggle chips ("Go by my punches instead" clears them), "Marked through Cavnar AI support" with Count them as mine, then the keep / unmark / add suggestions as checkbox rows (unmark and add start picked) and ONE primary "Apply N changes". Roles and job codes (`RoleFamiliesSheet`): one `AccountSection` per role with its codes and a Move menu, one primary Save |
| Coverage gaps (iOS Home) | `CoverageGapList` in HomeDay's open issues: a role's issue (`meta.people` > 1) lists each gap — a 5pt dot (red open, green covered, ink3 otherwise) and "Ana Bell — 5:00pm, missing / arrived / covered by Lu" — and under each gap still open one ember2 text button for its own cover: "Ask Lu to stay on for Ana's 5:00pm" (kind stay, with its `how` under it) or "Ask Pat to cover Bo's 5:00pm" |
| PAR hours check (iOS) | `ParHoursCheck` under a draft and a past week: ember2 kicker, "Hourly budget 312h ($8,100) for the week", "298h hourly of 312h budget · 110h salaried", On budget / over / under judged on the HOURLY hours, then the budget basis (12.5 ink3) and the assumed-wage caveat (amber) |
| Post to all connected | Marketing's social publish: one Toggle per connected channel (ember tint; Instagram disabled with "Needs a photo" until there is one; "Posted" in green once up), then ONE primary "Post to Instagram and Facebook" behind a `.confirmationDialog` naming the channels — it goes outside the restaurant |
| What Cavnar AI remembers (iOS, memory round 9/29/26) | Memory the owner can correct sits where the thing it is about lives, in the kit that screen already uses — no new surface. **Team memory:** `TeamMemorySection` (`Features/Labor/TeamMemorySection.swift`), a `CavnarDropdown` "Notes & who's who" in Scheduling setup (badge = what waits on the owner, `.warning` tone), with three blocks titled like Roster's (14.5 bold ink + a 13 ink3 helper): *Same person?* (an amber-wash 10pt-radius box per question, "Same person" / "Different people" as two secondary buttons, owner only), *Scheduling notes*, *Guests naming your team* ("It's them" / "Not them"). `TeamMemoryNudge` is one `.cavnarCard()` row under Waiting on you (amber glyph, "Your team's notes", the counts in `HomeMixedText`, "Review" in ember2) that opens the setup sheet on it — the action queue's `staff_note:stale` / `people:identity` land there. |
| Dated memory line (iOS) | A remembered constraint says when it was said and when it ends, in M/D/YY: "noted 9/2/26 · ends 10/1/26" (12.5 ink3, `HomeMixedText`) under the constraint (14 ink2) with a 6pt dot — green live, amber asking, ink3 ended. An ended one stays on screen, struck through in ink3. One over the stale window asks **"Still true?"** in amber bold with *Still true* (secondary) and *It's over* (ink3 text); each answer saves that one constraint at once (owner edits never vanish). The same line on a standing pattern: "learned 9/7/26 · last kept 9/21/26 · taught by Dana", its status word (in use ember2 / now a rule green / retired ink3), and "Make it a rule" as an ember2 text button. |
| What Cavnar AI knows (Person sheet) | `PersonSheet`'s `AccountSection("What Cavnar AI knows")`: Attendance ("Missed 1 of 24 watched shifts · late 2", amber when unreliable — **"Not watched yet"** when nobody watched, never a clean record), Covers ("Took 3 of 4 covers asked · 180 days"), Roles held ("Bartender · since 9/1/26 · their role on the roster", Remove as ink3 text), What guests said (confirmed mentions only, amber when a complaint); then "Add a role" with a promotion checkbox. Each fact says its window. |
| Asked of this week (iOS) | `ScheduleWeekNotes`: the soft requirements a draft read (the reviews diagnosis, a nightly report) in an ember-wash 12pt box — the requirement in 13.5 semibold, an `AccountChip` Applied (green) / Not applied (amber) read from the rows, and its source · what would confirm it · until when under it; editor conflicts as amber notices ("left out of this draft; settle it in the roster"); "Labor target: your goal of 26% by 12/31/26." as a caption beside the revenue basis. |
| Schedule draft notices (iOS, schedule fix round 10/3/26) | `ScheduleNotice` (`Features/Labor/ScheduleFixViews.swift`) — one box for what a draft or the review needs said: a 10pt-radius box washed 8% in its tone (amber a warning, red a hard rule or a manager gap) with a 30% hairline, a tone glyph, the sentence in 14 semibold ink (`HomeMixedText`), detail lines in 13 ink3, and its actions under it (a secondary button or ember2 text buttons, 44pt). `DraftNotices` leads the drafted week with them: days not written ("Saturday 10/10/26 and Sunday 10/11/26 weren't written — <why>. The rest of the week is here." + **Redo these days**, which opens the redo sheet with them ticked), days nobody can work (+ Availability · Closures), a plan that failed, the week's manager shortfall; a first week with no history wears a neutral "STARTING POINT" tag beside the review's own sentence. The generate screen's `GenerateWeekNotes` under the week picker: the sales freshness line (12.5 ink3), a refused week as a red notice with Generate dimmed and off, the budget caveat in amber, and one "Anything for this week? (optional)" field (Paper2, control radius, 500 characters, sent as `instruction`). |
| Schedule row tag (iOS) | `ScheduleRowTag(text:tone:symbol:)` — the house CHANGED / REVIEW capsule (9pt bold uppercase, 0.5 tracking, the tone at 15% behind it) with an optional 8pt glyph. **"Manager plan"** (ember2, `pin.fill`) marks a row the manager plan placed (`_pinned == "manager_plan"`) — no pass moves it; a tap shows `_pin_reason` under the row. Ember2 because the plan is Cavnar AI's own placement, not a status. Also: "Fixed" (blue, a row a fix line names by `row_id`), "+1h clock change" (amber, `dst_hours`, with its sentence under the row), the shift's section (ink2), "Not written" (amber) on an empty day's header, "Late night" in the requirements. |
| A day's manager notes (iOS) | `DayManagerNotes` under each day header in the drafted week: "Manager on 11:00am–11:00pm" (12.5 semibold ink2, ember2 shield glyph), the day-level breaches in red (never on a person's row, E-13), each stretch with no manager as a red `ScheduleNotice` — why per manager, "Make <name> acting manager that day" per `could_act` (reads their existing acting dates first and keeps them) and "Change availability" — and a standing shift the plan could not use (12.5 ink3). |
| The managers' days (iOS) | `ManagerQuestionCard` — `.cavnarCard(.ai)`, ember2 kicker "THE MANAGERS' DAYS", the plan's question in Clash 19, why it asks (13.5 ink3), then per name a row that opens a weekday · start – end editor (menus on a 35% Paper3 ground, half-hour times), "Add a day", and **Save their days** (secondary) saving their standing shifts; the server's answer under the name in green or red. Shown while `manager_plan.question` is set. |
| Redo some days (iOS) | `RedoDaysSheet` — Account-kit chrome: the ticked days (M/D/YY), "What's wrong with them?" as `RecAnswerPillStyle` chips in an `AccountFlowLayout` (the server's REDO_REASONS, one at a time, sent as `reason_chip`), "What's wrong? (optional)" (Paper2, 300 characters with a live count, `reason_text`), one primary **Redo these days**. "Redo selected days" under the table opens it. |
| Why this change? (iOS) | `EditWhySheet` — opens after a save that returns `why_questions`: "Why this change?" in Clash 22, each question in a card with its options as `RecAnswerPillStyle` pills; an answer replaces them with "Saved" (green) or "Saved for the owner to confirm" (amber, view-as). Dismissable unanswered. |
| Look for a better arrangement (iOS) | A secondary button at the top of "Why this schedule?" in `ShiftQualityPanel` re-posts the rescore with `what_if: true`; the on-demand `reason` is its hint (12.5 ink3), and "Checked against availability and hours only." in amber when `checked_with` says so. Cavnar AI's changes carry their labor dollars ("+$84", number face, ink3, sensitive) beside the points, a `trade` reads "Swap within the day:", and the week's "Labor $X → $Y" sits under the list. |
| What the schedule has learned / Measured ratings (iOS) | `ScheduleMemoryScreen` and `MeasuredRatingsScreen` (`Features/Labor/ScheduleMemoryScreen.swift`), opened from rows under Labor's Why group (measured ratings for the account holder only) and the `labor/ratings` link: Account-kit sections per fact class, each fact's text, its % in the number face ("—" below the floor), status / "Held by Cavnar AI's checks" / where it already acts as `ScheduleRowTag`s, "N of M times · last confirmed by hand M/D/YY", and Keep · Let it go · Make it a rule as ember2 text buttons (ink3 for Let it go). A server per row: "Now 3", "Suggested 4 — <why>", Confirm (answer pill) or a 1–5 picker. |
| Games you removed (iOS) | `DemandSignalsSection` under the dates list, only when there are any: "GAMES YOU REMOVED" (ember2 kicker) in a 10pt box on a 35% paper3 wash — each game in `HomeMixedText` (14 semibold ink) over "Removed 10/1/26 by Will" (12.5 ink3), and a `CavnarSecondaryButtonStyle` **Put back** on the right ("Putting back…" while it goes; the other rows' buttons wait). A catalog game swiped off the list joins it; Put back shows the server's `message` in the green outcome line, and the dates list reloads, once more 2 seconds later while the server says `refreshing`. The web's list, in the iOS kit; no new pattern. |
| Calendars followed for you (iOS) | `DemandSignalsSection` (event re-audit 2, 10/1/26), when the location has any calendar: "CALENDARS FOLLOWED FOR YOU" (ember2 kicker) in the same 10pt box on a 35% paper3 wash as "GAMES YOU REMOVED" — each calendar's name (14 semibold, ink; ink3 when not followed) over its line in `HomeMixedText` (12.5 ink3: "N mi away · Next: …", or "Not followed — its games aren't planned for") and, when followed, the season (12.5 medium ink2) — with a `CavnarSecondaryButtonStyle` **Stop following** / **Follow** on the right ("Stopping…" / "Following…" while it saves; the other rows wait). A button, not a native Toggle, because it saves over the network. The server's `message` is the green outcome line, and the dates list reloads as for Put back. A catalog row in the dates list reads "from a calendar you follow", as on the web. The web's `.ev-follows`, in the iOS kit; no new pattern. |
| What your nights have taught (iOS) | `DemandSignalsSection`: "WHAT YOUR NIGHTS HAVE TAUGHT" (ember2 kicker) in an ember-wash 10pt box above the list — each recurring effect's server sentence (13.5, M/D/YY, "before and after, not proof") with a 6pt dot, ember when it is past the sample floor and the forecast applies it, ink3 otherwise — and under each listed event its own measured line (12.5, ember2 when it applies). |
| Fix tags (iOS) | `ReviewRetagSheet` — Account-kit chrome "Fix tags", one `AccountSection` per field (topics up to three, how it reads, how serious, dishes) with choice capsules in an `AccountFlowLayout` (selected: ember2 bold on an ember 16% wash with an ember hairline; else ink2 on white 4%), one primary "Save tags" that reads "Nothing changed" until something did. Opened from an ember2 "Fix tags" text button (tag glyph) under the review's read; the corrected tags then show as "Tagged: …" in 13 ink3. The vocabulary is the analyser's (`ReviewTagVocabulary`, pinned to `analyser.CATEGORY_LABELS` / `SEVERITY_LABELS`). |
| Held back and suggested (Food Cost, iOS) | A reprice a live link guards shows the guard's words in a `CavnarCaveat("Fix the plate before the price")` and its one-tap steps down from primary to secondary ("Set anyway: $X"); the owner's usual price is an ember2 text button beside it. **Pars to raise** repeats Prices to revisit's row exactly (amber rail, name, "6 → 9" in the number face, why, Raise par as secondary, `RecAnswerRow` Pass). An order line the owner's habit adjusted and an invoice line matched their way say so in one line under the item (ember2 / green). |
| Over time (Intel, iOS) | `IntelHistorySection` under What changed: "OVER TIME" kicker (ember), "Your Google rating: 4.3★ the week of 3/2/26 → 4.6★ now (+0.3)", `OwnRatingTrace` — a 92pt `CavnarAnimatedCanvas` ember glow line traced once (motion 08) with a fading ember fill, the latest week a hot dot, labels only for values the line reaches — then "What the market did" (name, what, M/D/YY; arrived or climbing amber, else ink3; five, then "Show all N"). |
| Link memory (iOS) | A cross-module link says how long it has stood — the server's `memory.label` ("Found 3 weeks running, since 9/7/26") in 13 ink3 under the headline in the Evidence sheet, beside an amber `AccountChip` "Recurring" / "Came back"; the Needs-attention row adds it after the modules. |
| Widget | `CavnarWaitingWidget` (`CavnarWidgets/`) — families `systemSmall`, `systemMedium`, `accessoryRectangular`, `accessoryInline`, `accessoryCircular`. Small: "CAVNAR AI" ember2 kicker, the waiting line in Clash 17, then "LAST NIGHT · M/D/YY", the net in the number face and its change (green/red) with its basis ("vs last Friday", else "vs yesterday"; none shown when unmeasured). Medium puts the restaurant's name as the kicker and the waiting line (Clash 18) beside last night. Lock Screen rectangular / inline / circular carry the same two facts. Money is `.privacySensitive()`; a snapshot older than 36h says "Open Cavnar AI to refresh" |
| Countdown with Undo | `PendingSendLiveActivity` — Lock Screen: ember2 kicker ("SCHEDULE GOES OUT" / "SUPPLIER ORDER GOES OUT"), the title, "in 12:04" as the system timer in the number face, an "Undo" capsule with an ember hairline; after Undo "Stopped. Nothing went out." in green, a refusal in amber. Dynamic Island: calendar / box icon + the timer compact, the Undo button expanded |
| Last night widget (iOS) | `CavnarLastNightWidget` (`CavnarWidgets/`, same `WidgetSnapshot` and provider as the waiting widget) — systemSmall: the store name (multi-location only) as the ember2 kicker, the verdict's tone dot + "LAST NIGHT" + the M/D/YY date, the net in the number face at 28, "net sales", then "+8% vs last Friday" (green / red) and "+$525 vs budget" (green / amber, only for a login allowed the budget) — figures in Space Grotesk, the basis in ink3. Lock Screen rectangular and inline carry the same facts. Money is `.privacySensitive()`; past 36h, or with no measured net, it says "No report for last night yet". Tapping opens that night's report |
| "How was last night?" (iOS) | `LastNightSummaryIntent` (`Core/CavnarAppIntents.swift`) — Siri / Shortcuts answer in one spoken sentence from the widget snapshot without opening the app: "Last night, 9/24/26: $4,210 in net sales, +8% vs last Friday, +$525 vs budget. Good day, 82 out of 100." Needs an unlocked device; never calls the API. No current night: "There's no report for last night yet." "Open replies waiting in Cavnar AI" is a phrase on the existing reply-queue shortcut |
| Cached data notice (iOS) | `CachedDataNotice(text:)` (`Core/CachedDataNotice.swift`) — Home's staleness line for every screen painted from the device cache (`ResponseCache`: Reviews' first inbox page per chip, Intel, Marketing and its Analytics, Food Cost Analytics, the Daily Report list): "Showing data from 12m ago" / "… 3h ago", 12.5 semibold amber, figures in the number face, centred; nothing under 5 minutes or once a live load lands |
| Queued offline answer (iOS) | A write parked in `PendingWriteQueue` says so where it was made: a rec answer row's Done / Pass becomes a clock icon + "Will send when you're back online" in its muted confirmation style; the count sheet reads "N recounts kept on this phone. Will send when you're back online." The app-wide connectivity banner counts them. Only Done / Pass and count sheets queue — never Measure it, time-off or shift decisions (they message staff), or anything outward |
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
| Haptics | `Haptic` (`DesignSystem/Haptics.swift`) — intent-named (`light` / `medium` / `heavy`, `selection`, `success` / `warning` / `error`), retained generators; a button style uses `.sensoryFeedback` instead. Web has none |
| Passcode entry | `CavnarPasscodePad` — six ember dots over a glass keypad; each digit lands with one thin ripple, a wrong code turns the row red and shakes it once. Used by `LockedView` (unlock) and `AppPasscodeSheet` (set / change / remove) |
| Staff PIN entry | `StaffPinField` (`Features/Staff/StaffLoginView.swift`) - the staff app's 4-8 digit PIN, the same control at sign-in, signup, Forgot PIN, Change PIN and the location switch: `StaffPinDots` (at least four, one more per digit past four; an empty dot is a 1.5pt ink3 ring, a filled one ember; a wrong PIN turns them red and shakes the row once, colour only under Reduce Motion; VoiceOver reads "PIN, 3 digits entered") over `StaffPinPad` (58pt keys, Clear and a labelled Delete). Never submitted by the pad: a primary Sign in / Next. A new PIN is typed twice ("Type it again"; a mismatch shakes and says "Those didn't match - try again."). A button busy state is `StaffBusyLabel`: the label stays and the 3pt ember pulse runs under it |
| Split button | `CavnarSplitButton` — see §5 |
| Tone pill and bar | `TonePill(text:tone:)` — a capsule in a `CavnarTone` (good / bad / warning / neutral = ink, never ember); `StatProgressBar(progress:tone:)` — a 6pt value-against-target bar in the same tone. Web: `.hb-chip` + tone, `.hb-goal .bar` |
| Hero cards | `cavnarGlassCard(tint:)`, `cavnarGlossyCard()` — see §9 *Cards (iOS)* |
| Forecast ribbon | `cavnarHeroForecastRibbon` + `CavnarForecastPanel` (`HeroForecastRibbon.swift`) — a "FORECAST" pill straddling a hero card's bottom edge that opens the upcoming-events panel in place; tone follows the hero's |
| Keyboard bar | `KeyboardNavToolbar` — ↑ ↓ between fields and a checkmark for Done on every keyboard, the system AutoFill bar's shape |
| Word-by-word reveal | `TypewriterText` — the web's `typewriterEffect()`: ~1.4s spread over the words (16–55ms a word), cancelled by new text; for an AI insight arriving, never for a figure |
| Ask's formatting | `CavnarMarkdown` / `CavnarRichText` — the iOS twin of web `_askMd`: headings, numbered and bulleted lists, bold, every number in the number face |
| Quiet hours mark | `CavnarQuietMark` — the do-not-disturb moon in ink, never ember (quiet is the opposite of "needs attention") |
| Seal | `CavnarSealMark(ringColor:emberWarmth:)` — the ring plus its ember, animatable warmth (motion 01) |
| Working orb | `CavnarWorkingOrb(state:label:)` — an inline orb in one of the nine states with its line of text, for a model genuinely working; `CavnarLoadingOrb` is the full-screen form |
| AI consultant strip | `AIConsultantView` / `AIConsultantEmbeddedStrip` — sparkle + the insight's first line + chevron, breathing while the first insight loads; opens the full read |
| Motion | The numbered `CavnarMotion.swift` structs — see §11 — plus `Animation.cavnarEase(_:)`, the one easing |
| Campaign Studio (web) | Marketing → Campaigns (`.cp-*`, 9/28/26): one prompt field (`.cp-prompt`, the orb, one primary Create →), the channel chips it drafts for under it (`.cp-pick`: Text, Email and the connected social accounts, each with its reach), then idea chips. Create drafts every channel at once into a plan strip (Goal · Audience chips carrying text and email counts and a measured "N% came back" · one shared Photo) over one card per channel (`.cp-ch`): the text in a phone, the email as the server renders it (a sandboxed iframe, `allow-same-origin` only; Desktop / Phone width), the post as a feed card. Edit is progressive (`<details>` "Edit the email"); a drafting card dims under a moving ember hairline, a failed one says why with "Draft it again". The email takes the wide column with text and social stacked beside it; one channel centres at 780px; under 900px it is one column, email first. One send bar under the cards: a check per channel (a warn disc where it cannot go and why), the mailing-address field when the law needs it, and one two-press primary naming what goes ("Text 31 · Email 18 · Post to Instagram →" → "Tap again to text 31 guests, email 18 guests, post to Instagram", 6s; the text count is who a text reaches NOW, the three-day spacing out, fix round 9/28/26); a channel that cannot go is left out of the label, not blocked. The first press snapshots every request; the second sends that snapshot, and any change in between disarms. While a send is in flight nothing re-labels or re-arms the button. After a send, only a channel whose content changed is offered again, with a warn check when the same audience got that channel in the last 30 minutes ("You texted this audience 4 minutes ago"). Outside 8am-9pm the text reads "Text 31 at 8:00 AM" and is queued for then. The send status is a `role="status"` live region. Every entry point (a new goal, an idea, the feed, the win-back, Use again, the weekly email) starts from a clean draft, and "Picked by Cavnar AI" tags the audience only once a plan arrived. Forecasts are the picked audience's own measured rate times its reach, or nothing; "came back" is always "within 14 days", never "because of"; an email's opens are "recorded" (Apple Mail auto-opens included), never "at least"; there is no dark preview because the email is light-only. The text's phone preview shows the number guests see it from (a grey contact disc, never the restaurant's name or initials) and the bubble opens with "{Name}: ", as every campaign text does; the counter counts that name, the real tracked link (~41 characters) and the STOP line, and a non-GSM character (an em dash, a curly quote, an emoji) at 70/67 characters a part. A text campaign's history card says where it stands — "Sending · 12 of 48" or "N texts wait until 8:00 AM" with a two-press "Stop sending", "Stopped" — and a red "Failed" row when any failed; the month line reads "accepted by carrier", never "delivered". Five KPI tiles run 5 across, 3+2 under 1100px and 2+2+1 on a phone; never an orphan tile |
| Marketing states (web) | 9/28/26 (AUX-10/11/14, MB-17): every Marketing loader — the h1 status, Scheduled, Drafts, the window tiles, "What posts did to sales", performance, recently generated — calls `loadFailed(el, d)` on a refusal or a dropped connection, and its empty state is one `.hb-empty` sentence (never an italic `.no-data`, never "Nothing queued" for a failed load); the h1 never stays "Reading your posts…". An unmeasured figure is "—" (reach from `reach_posts`, engagement from `measured_posts`), a platform with none says "reach not measured". Analytics reads the stored sync; its header carries one `cbtn-text` "Refresh from Meta" (a `role="status"` line under it says refreshed / partial / throttled). The top post is an `.hb-card` with an `.hb-kicker`, or an `.hb-empty` "A top post is named once 6 posts are measured — N so far". Recently generated chips are `.mkt-topic-chip` (`--hb-tint` / `--green-bg` on tokens, figures in words, no emoji); calendar channel chips are one neutral `.cal-platform` (`--ink2` on `--hb-tint`; brand colours stay on the brand's own buttons). Keyboard: the sub-tabs are a `role="tablist"` whose buttons carry `aria-selected`; content-type cards are `role="radio"` in a `radiogroup`, `tabindex="0"`, Enter or Space picks one (`mktCtKey`); `#mktopic` has an `.sr-only` label and `#mkoutput` is a labelled `role="textbox"` |
| Opportunity Feed (web) | Marketing, above the sub-tabs (`#mkt-opps`, `data-nav="marketing/opportunities"`, 9/28/26): "Cavnar AI found / Opportunities this week" over Home's recommendation cards — laid out horizontally since 10/2/26: one full-width card per row (`.hb-recs.mkt-opps-list` one column; `.hb-rec.mkt-opp` a two-column grid: what was found on the left, the confidence / Done · Pass / Draft it column on the right; stacked under 760px) (`.hb-card.hb-rec` in `.hb-recs`, three across, the rest behind "Show N more"). Each card: a kind label in ember caps (Slow night, Holiday, Sales dip, Dish, Guest favorite, Your list, Posting) with "Tomorrow / In N days", a verb-first title, the one-line why, the measured gap as the `.usd` pill ("$3,500 a Tuesday night under a typical day" - a gap, never an expected return) and its facts as pills, then ONE `cavConfLine` (none on a fact card: a list or a posting gap), `recControlsHtml` Done / Pass (no Measure it), and "Draft it ->" - primary on the first card only, `aria-label` "Draft it: <title>" - which opens the Campaign Studio with the goal typed and only the card's channels that can reach someone on (a posting card with no account says so and drafts nothing). While it drafts it is a `cbtnBusy` button and every other Draft it waits; over a draft in progress for another goal its first tap reads "Replace your draft? Tap again" (the Studio send button's two-press pattern, 6s) instead of replacing it silently. Loading: the section opens on a `.dr-pulse.wide`, not hidden until the fetch lands. "Show N more" fetches the rest (`?show=all`) and the list stays open after an answer; a failed reload clears it. Empty: a `.cv-ok` line naming what was checked, what had nothing to read yet (and why) and what couldn't be read (`.cv-warn` then); with cards, a failed read is one muted `.mkt-opps-note` line. A link to one card (`marketing/opportunities?card=<key>`, the morning brief's slow night) brings it into view with `cavFlash`. No model call on load |
| Marketing sub-tabs | Marketing Studio's sub-header leads the page (10/2/26): the Schedule Studio's own bar (`header.ss-top.mkt-top`) — the Cavnar AI wordmark over "Marketing Studio" (`.ss-brand`, no back button and no rule before it: it is a tab, not a page over the dashboard), Content · Campaigns · Scheduled · Analytics as the step bar in the middle — `nav.ss-steps.mkt-steps`, a recessed pill, the open tab `.on` (surface + `--elev-card`), no step numbers (they are not a sequence); scrolls sideways on a phone | Marketing |
| Replied elsewhere | A review answered outside Cavnar AI: a text "Replied on Google" button beside Approve/Edit/Skip (and on Skipped and No-reply-yet cards); once marked, the draft box becomes `.rv2-elsewhere` — label "Replied on Google", their reply when Google gave it (never Cavnar AI's unused draft), `.rv2-status.ok` "✓ Answered outside Cavnar AI", "Read from Google" / "Marked by your team" · date, Undo. iOS: a text button under Skip / Approve; the banner "Answered on Google" with Undo | Reviews |
| Website analytics | Account → Connections: an `.ac-conn` card (bar-chart glyph; green dot once read, red with the refusal) and its `#wa-card` setup — numbered `.wa-step`s (copy the read-only address, add it in GA4 as Viewer, in Search Console as Restricted, then the two `.wa-fields` and Save and check; each part's `.wa-check` result). Marketing → Analytics: `section.wa-site` "Your website" — four `.hb-stat` tiles vs the 28 days before (green up / red down), the visits `glowLine`, two `.wa-cols` (where visits came from as ember share bars `.wa-bar`, clicks out with their family; top Google searches `.wa-q`), then "What moved" `.wa-move` rows (green rail up, red down) with "Same day:" chips and the basis line | Account, Marketing |
| Opportunity Feed (web) | Marketing, above the sub-tabs (`#mkt-opps`, `data-nav="marketing/opportunities"`, 9/28/26): "Cavnar AI found / Opportunities this week" over Home's recommendation cards — laid out horizontally since 10/2/26: one full-width card per row (`.hb-recs.mkt-opps-list` one column; `.hb-rec.mkt-opp` a two-column grid: what was found on the left, the confidence / Done · Pass / Draft it column on the right; stacked under 760px) (`.hb-card.hb-rec` in `.hb-recs`, three across, the rest behind "Show N more"). Each card: a kind label in ember caps (Slow night, Holiday, Sales dip, Dish, Guest favorite, Your list, Posting) with "Tomorrow / In N days", a verb-first title, the one-line why, the measured gap as the `.usd` pill ("$3,500 a Tuesday night under a typical day" - a gap, never an expected return) and its facts as pills, then ONE `cavConfLine` (none on a fact card: a list or a posting gap), `recControlsHtml` Done / Pass (no Measure it), and "Draft it ->" - primary on the first card only, `aria-label` "Draft it: <title>" - which opens the Campaign Studio with the goal typed and only the card's channels that can reach someone on (a posting card with no account says so and drafts nothing). While it drafts it is a `cbtnBusy` button and every other Draft it waits; over a draft in progress for another goal its first tap reads "Replace your draft? Tap again" (the Studio send button's two-press pattern, 6s) instead of replacing it silently. Loading: the section opens on a `.dr-pulse.wide`, not hidden until the fetch lands. "Show N more" fetches the rest (`?show=all`) and the list stays open after an answer; a failed reload clears it. Empty: a `.cv-ok` line naming what was checked, what had nothing to read yet (and why) and what couldn't be read (`.cv-warn` then); with cards, a failed read is one muted `.mkt-opps-note` line. A link to one card (`marketing/opportunities?card=<key>`, the morning brief's slow night) brings it into view with `cavFlash`. No model call on load |
| Account banner (web) | A full-width strip directly under the header, above the tabs, for a state of the whole ACCOUNT the owner must not miss while the dashboard keeps working (fix round, 9/29/26): flex row, text left and at most one `.cbtn cbtn-sm` right, wrapping on a phone (the past-due banner's side padding drops to 16px under 640px), `role="alert"` (a problem) or `role="status"` (information). Colours from variables only: **past due** `.pastdue-banner` — `var(--red-bg)` ground, a `var(--red)` bottom rule and a red `<strong>` lead ("Your last payment didn't go through."), then what keeps working; the account holder (`is_principal`) gets **Fix payment** (`pastDueFix`: asks `/api/billing-info` at click time for `fix_url` — the open invoice, else the Stripe portal, because portal links expire — and a toast if it cannot), anyone else is told who can fix it. **View-as** `#view-as-banner` — `var(--amber)` ground, `var(--paper)` text, 12px/600: whose login this is and the acting admin, "Anything you change is recorded under your name." or "Read-only: nothing can be changed.", when it closes (2 hours), and **Back to admin** as a POST form (a GET only asks). One banner per state; never a second colour for the same state |

---

### The nightly report's order (SCORE FIRST, 9/25/26 owner decision)

One order on web (`#panel-dsr`), iPhone (`DailyReportView`) and email (`emails.dsr_email`); this paragraph is the only statement of it. It answers the 3-30-300 rule: health, the biggest risk and the biggest opportunity in 3 seconds, the why and the top actions in 30, the drill-down at 300.

**Labels name the night (owner, 9/29/26).** The report is read the morning after the night it covers, so no label says "Today", "Tomorrow" or "tonight": the server's `scorecard.labels` give "Monday's score", "Monday's wins", "Monday's risks", "Monday's shift" and "Tuesday's priorities" (web, iOS and email read them; without them the weekday comes from the business date), and the next-day card is "The day after · Tuesday 9/29/26". Below, "Today's …" names the section. **Wins and risks list each subject once**: a narrative line about something a measured line already says (overtime, replies waiting, labor against target…) is left out however it is worded (`dsr.scorecard._same_subject`).

**Owner:** **Today's score** (the hero) → the **executive summary** → **Today's wins** and **Today's risks**, three each (`scorecard.SHOWN_ITEMS`; the web keeps the rest behind a quiet `details.dr-more` "2 more") → **Tomorrow's priorities**, three visible and the rest behind "N more priorities" (the email prints three and "N more in the full report", and presents only those three to `rec_ledger`) → **Key numbers** (`.kx`, the payload's `kpis_big`, 10/1/26 — below) → **Tomorrow** (with **the day after's labor** and **overtime this week**, `.dr-tlab`) → **The night in detail** (`.dr-detail`, below) → **All KPIs** (`details.dr-sec.dr-allk`, closed: every KPI not among the six, the score's four included, with its direction; a payload without `kpis_big` keeps the older `kpis_headline` four) → **AI insights**, at most two (`access.INSIGHTS_MAX`), never a win, risk or priority said again and never the Food block's money at stake (its own "At stake · opportunity" tile says it) → the blocks, closed → **How did yesterday turn out?** (web; the email says it as one line, "Yesterday's predictions: 1 of 2 correct · 3 of 4 right so far") → **How the night was built** (closed; it holds the verification count, "8 of 9 lines kept", and "each block carries its source").

**Manager:** the **Operations summary** (the hero; there is no score) → **Today's shift** under its one verdict line (`kpis.shift_verdict`: "Labor 27.5%, on target · 1 no-show", toned like Today's score's labor — over a starting target amber, never red) → **Key numbers** (`kpis_big`, the hero labor against target; a payload without them keeps Top KPIs and Operations) → **Went well** / **Needs attention** → Tomorrow's priorities → Tomorrow → **The night in detail** → **All KPIs** → AI insights → the blocks → the same closing sections. The manager's Top KPIs never repeat a tile Operations already shows.

**Today's score** (`.hb-card.hero.dr-score`, iOS `DSRScorecardCard`, email `emails.dsr_scorecard_sections`): on the web the card carries the brand (10/1/26): an ember glow from the top-left corner, a warm halo at the far corner, an ember hairline along the top, and frosted component tiles; a verdict with a status dot (Excellent / Good = good, Mixed = warn, Tough = bad) in Clash at 24–32px; the night's **net** at 44–62px in the number face (`.net`, the page's largest figure) with the Sales component's own comparison under it in its tone (`.vs`: "+$420 vs budget" — the component's basis, budget else forecast else last week, never re-derived); the overall score as `78/100` on the right; then the other three components (Labor, Food cost, Guest experience) as stat tiles in their tone, an unmeasured one saying why in muted text, never a dash pretending to be zero; the guest tile is whole stars in ember (rounded down unless within a quarter). When Sales wasn't measured it is a tile like the others and there is no net. The **executive summary** under it is an ordinary card (`.hb-card.dr-read`, `.lead` at 19–23px): a paragraph is never the hero when there is a score. Status is a dot, a check or a triangle glyph — not emoji (email subjects stay emoji-free).

**Blocks** are closed by default; each summary line carries the block's number (Sales net, Labor % of sales, est. food cost, reviews new · rating, posts · reach, the night's high · events, who filed the close-out). Sales opens on its own only when there is no score to read.

**Key numbers** (`.kx`, owner 10/1/26: one hero, never six equal tiles; sits above the day after). The payload's `kpis_big`, each tile ranked by `dsr.kpis.big`: the **hero** (`.kx-hero`, two columns by two rows on a laptop, full width on a tablet or phone) is the first tile in the view's order with a picture to carry it — the owner's week to date (budget bar), else prime cost, guests…; the manager's labor against target. It has a 46px icon tile, the label as an ember kicker, a status pill (On track / Off track / Watch, from its tone, with a pinging dot), the figure at 52–82px counting up through `cavCount`, a large trend chip (↑ ↓ → with what it is against), the large picture — an 18px bar with a sheen and a "Budget to date" tick, a half-ring gauge with its target, or a 150px trend line over faint grid lines with a breathing endpoint — then its lines and note. It is the ONLY card that keeps moving: a slow ember glow drifting in its corner (6s), the sheen, the endpoint and the ping; everything else moves once, on entrance. **Secondary** cards (`.kx-card`, up to four): a 36px icon tile, the label (two lines at most), the figure at 28–36px with a small picture beside it, the trend chip, one muted line. **Tertiary** (`.kx-chip`): a pill with the icon, label, figure and trend. Depth: a soft layered shadow, a 1px inner highlight (`--hb-sheen`), a faint ember wash; hover lifts 4px with an ember glow and turns the icon tile solid ember. Trend chips are green, red or amber on their tone's background, never colour alone (the arrow says it too). While the night loads, `kxSkeleton()` holds the same shapes under a slow sheen. Reduced motion: every card at rest, no loop, no lift. Which six (`dsr.kpis.big`): the owner's are the week to date, prime cost, guests, spend per guest, sales per labor hour and the day after's labor % — never what Today's score states; the manager's lead with labor against target; a KPI with no measurement gives its place to the next candidate. Nothing here adds a figure: the same six, ranked.

**The night in detail** (`.dr-detail`, 9/30/26): one column of roomy cards (26–28px padding, 22px apart) — **What happened that night** (`.hx`, owner 10/1/26; the report's showpiece chart and one hybrid picture rather than bars alone: a story line first, one or two sentences the figures prove ("The rush began at 5pm and peaked at 6pm with $1,420, 11% above a usual Tuesday. Staffing ran ahead of demand at 3pm."); then net sales as rounded ember-gradient bars that grow in, the peak bar brighter with a breathing glow and a floating **Peak hour** card above it (hour, net, against usual); people on the clock (labor hours per clock hour) as ONE smooth ink line with a soft glow that draws in, on its own right-hand scale; a usual same weekday as a dashed line once two finished reports exist; a heat strip under the hours (ember, by the hour's share of the peak — never a full-height column wash behind the bars, which read as a hover that had not cleared, 10/1/26); only the hovered or keyboard-focused column is tinted, and leaving the chart clears both; up to four numbered markers, explained below as **What stood out** — the dinner rush (the climb INTO the peak, never an earlier lunch bump), an hour well above usual, sales that dipped despite full staffing or missed revenue against usual, labor ahead of demand (sales per labor hour under 60% of the night's), staffing that matched demand. Every callout is arithmetic over figures the report holds; a quiet night says nothing it cannot prove. Hovering, tapping or tabbing to an hour shows a card with its net, usual, labor hours and sales per labor hour. Dashed faint gridlines, `$1.5k` axis labels in the number face. No hourly sales: an empty state of ghost bars and one sentence; loading: the same ghost bars under a sheen (`hxSkeleton`). A phone scrolls the chart sideways at 560px, the peak card inside the scroll area's top padding) → **Where the money came from** (meal periods and rooms as ember bars with share, guests, checks and spend a guest) beside **Where the labor went** (`.dr-dep`: a stacked bar and a row per department — hours, dollars, % of sales; the owner's adds Salaried; a container query moves hours and share under the name when the card is narrow) → **Servers and bartenders** (`.dr-stbl`, people past 8 checks, spend per guest toned against the floor) → **Given away** and **Punch edits** side by side (loss needs the comps-and-voids permission; punch edits are the owner's) → **Cash and cards** (10/5/26, the `.dr-loss` card's rows: the night's total taken, cards and cash, a row per tender with its tips, then every payout and pay-in rung on the POS with who approved it — petty cash and check requests — or one sentence saying they appear once rung as payouts). Two-up cards stack below 1020px. The class names `.sw`, `.dr-hours` and `.dr-tbl` were already taken — the new ones are `.dr-sw`, `.dr-hourly` and `.dr-stbl`.

**Compared to your last home game** (`.dr-game`, Event Intelligence phase 2, 10/1/26): on a night with a followed game the Intel block ends with two cards side by side (one column under 860px) — **Tonight** and **Last home game** (or road game) — each an uppercase kicker, the game in one line (who, when, TV), the night's net at 30px in the number face, its lift against its OWN usual weekday as a green (or red) figure with "vs a usual Monday · $5,351" beside it, and a muted line of guests, people on the clock and labor % (the last two only for the Labor view). Comparing two nights on different weekdays by dollars alone misleads, so the lift against each night's own usual weekday is the comparison; the dollars sit above it. With no earlier game of that side the second card is dimmed: "The first one measured here." With more than one game tonight, the other games are one `.dr-note` under the cards, "Also tonight: …" (`detail.game.also`; event re-audit 2). No new colour: the card is `--surface` with the report's `--hb-glow` wash and `--hb-line2` border; positive lift is `--hb-good`.

**Calendars followed for you** (`.ev-follows`, Labor → Events & reservations, 10/1/26): above the dates, one row a calendar — its name, distance in miles in the number face, and its next game, or "Not followed — its games aren't planned for" dimmed — with one switch button on the right (`cbtn-text` "Stop following", `cbtn-secondary` "Follow"). The toast is the server's `message` — what changed for the calendar and when ("its games leave your calendar in a moment", or "with the 5am event sync" when the re-sync could not be queued) — and the list reloads, then once more about 2.5 seconds later while the server says `refreshing` (the calendar re-syncs in the background, event re-audit 2); a catalog row in the dates list reads "from a calendar you follow", never a source key. A followed row adds the season as one more line (`.ev-follow-s`, "Bears games so far: 6 played, 1 measured here — +$5,680 over a usual same weekday"), its basis on hover. **Games you removed** (event re-audit, 10/1/26) reuses the same list — an `.ev-follows` block titled "Games you removed", one dimmed `.ev-follow.off` row a game: the game on the restaurant's clock, "Removed by Will on 10/1/26 — not planned for", and `cbtn-secondary cbtn-sm` **Put back** ("Putting back…" while it goes; the toast is the server's `message`, "the game is on your calendar in a moment", and the list reloads as for a follow). It appears only when something was removed, and the toast for removing a catalog game points to it. No new pattern.

**Game this week** (`.ev-week`, Food Cost → Send to suppliers, phase 3): above the order, a card with a faint ember wash — an orange kicker, the game in one line, the sentence ("Order about 12 lb Chicken wings more than a usual week…", or the last game's items said as one game), the ingredients as pills (`+12 lb Chicken wings`, the figure in the number face), and the basis in small muted type. It never edits the order below it. In Campaign Studio a game's goal arrives typed in, with `#cp-send-hint` under the prompt ("Best sent **Sunday around 9am, 3 hours before kickoff** — a starting rule from kickoff, not yet measured here."), cleared by any new draft.

**The day after's labor** (`.dr-tlab`, under the forecast): the schedule's labor % at 42px, toned against the target (the owner's all-in, a manager's hourly), hours and dollars over the forecast, overtime hours on it; beside it **Overtime this week** — who goes past 40 if the schedule holds, the extra half on top of their rate, and a same-role teammate with room in green, or "No one goes past 40 hours this week if the schedule holds."

**A KPI tile** (`.dr-kpi` / `DSRKPIGrid`) is the value in the number face with a 14-night trend line (ink stroke, the latest point ember), then its direction — "↓ 1.4 pts vs last Saturday" in good/bad tone by the metric's better side — a streak ("Best Saturday in 5 weeks"), the target and, only when fair, "Restaurants like yours" (otherwise "no fair comparison yet" in muted text). **Tomorrow** lists prep items with an amber triangle (a plain dot when informational), a pointer to the staffing priority ("Staffing for tomorrow: see priority #2 above" — never the action said a second time), Cavnar's forecast and the **AI confidence** as a percentage with "Based on …" — the forecast's measured record (`confidence.track`), and "—" with that line when there is no record yet (`pct` null); the weather, schedule and events it lists are `watch`, things to keep an eye on, never "confidence". **How did yesterday turn out?** marks each prediction ✓ correct (good) / ✗ incorrect (bad) / • not graded, with the measured figure, and the prediction accuracy as a percentage once five are graded — the count before that. Labor over a target the owner never set (Cavnar's starting target, `detail.target_source` "default") is amber, never red — the Labor tile, the email's Labor stat, the shift verdict and Today's score agree. An estimate (food cost, prime cost) says so everywhere: `est.` on the tile, "(est.)" in the email.

**The email** follows the same order: the score (verdict, `78/100`, the net at 38px with the sales comparison in its tone), the other components, the summary, three wins, three risks, the priorities (`report_action`, the one loud block), Tomorrow, four KPIs (`kpis_headline`), at most two insights, yesterday as one line, what's missing, the CTA.

### Week and period summary (9/25/26, ID1-22)

Above the grid, one `.hb-card.dr-wsum`: four tiles from the grid's own totals row — "Week to date · net" (with "N of 7 nights measured"), **vs budget** (owner only; "—" with the grid's reason when a measured night has no budget), **vs last year**, **Labor %** (only when the view reads labor) — then `.dr-wbars`, one bar per night (a period: per week) of net, green when it made its budget, red when under, a dashed ink mark at the budget (owner only; a manager's bars are plain ember and the legend says "Net sales"), a hairline for a night not measured; then `.dr-story`, the one sentence `dsr.rollup.story` writes from the view's redacted grid ("Thursday carried the week ($8,420 net, 48% of it); Friday missed budget by $610." — a view without the budget reads the miss against last year). Nothing on it is summed in the browser. Once any night in the week has a last-year figure, the import card folds to a "Import more last-year nights" text link (`.dr-imp-link`) that opens it.

### What Cavnar AI remembers (memory round, UI wave B, 9/29/26)

The memory round put what Cavnar AI remembers into the payloads; these are the
shapes that show it on the web. All of it lives in `<script id="cav-mem-wb">`
(global `mem*` helpers, one per sentence, run under node by
`tests/test_mem_ui_wb.py`) and `<style id="cav-mem-wb-css">` at the end of
`dashboard.html`. Every sentence is the payload's own words or figures, every
date M/D/YY, every number in the number face (`memNum`).

This section covers the module screens (UI wave B). The memory on a piece of
advice — what was said before, a caution, a conflict, a kind hold, the brief
line extras, the policy notice — is drawn by the §12 rows of those names
(`recPrevHtml`, `recCautionHtml`, `recConflictHtml`, `hbKindHolds`,
`hbBriefExtra`, `renderPolicyNotice`), and Account's memory, targets, "Just for
me", change history and distrusted data by the §12 rows dated 9/29/26 above
them. iOS draws the same fields: the advice in "What a card remembers — iOS"
(next), the module screens in the iOS row "What Cavnar AI remembers".

| Need | Use |
|---|---|
| Memory line | `.mem-ln` — one 13px `--ink3` line under the thing it dates or sources: "noted 9/2/26 · ends 10/1/26", "Found 3 weeks running, since 9/7/26", "learned 8/3/26 · last kept 9/21/26 · used in 5 drafts", "Matched as you did last time", "Adjusted to how you order (your last 5 orders) · the formula said 12". `.mem-ln.warn` (amber) for a line that argues against the action beside it (the reprice guard, a value complaint). Never a card of its own |
| Memory pill | `.mem-pill` — a small uppercase pill with a glowing dot for a remembered state that carries a verdict: amber by default ("Recurring", "Needs you"), `.good` green for one that landed ("A rule"), `.neutral` ink with no dot for no verdict ("Standing", "Retired", "Older read"). Never ember |
| Waiting on you: who is who | Rows in `#lb2-wait-people` (`data-nav="labor/people"`, the identity queue item's landing): "Is Kim T. the same person as Kim Tran?" with the reason and where each record came from, Different people (text) / Same person (secondary) — owner only, else "The account owner answers this"; "A guest praised Ana B. on 9/25/26 — is this them?" with the review's words, Not them / Yes, Ana. The card shows while only these wait (`memWaitCount`) |
| Scheduling notes | Team & rules → Scheduling notes (`data-nav="labor/notes"`, the stale-notes queue item's landing): one recessed `.mem-note` per person, one `.mem-part` per constraint with its memory line; a part over 90 days old carries an amber inset rail and "Still true?" · Yes, still true / It ended; an ended part is struck through at .55; "Set an end" opens an inline date + Save the end; ✕ removes with Undo (§10 tier 1). The add row is Who (roster picker) · The constraint · Ends (optional date) · Add note |
| Person sheet memory | Under the sheet's own sections, orange kickers like every kicker: Roles (`.mem-role` pills with "since M/D/YY" and ✕, an inline add row that saves on each add or remove), Covers ("Took 3 covers, turned down 1 in the last 180 days"), Attendance ("Not watched yet" when nobody watched — never a clean record), Guests who named them, and for the owner Rename (`cField`) and "The same person as…" + Merge, which asks once in place (`.mem-confirm`: both names, what moves, Not yet / Merge them) |
| What the draft keeps | `.mem-lrn` under "What the draft has learned": the editor clashes first ("Ana B. on Tuesday night: taken off in some weeks and put on in others", Needs you, Keep them off / Keep them on — the other side is set aside), then each standing pattern with its pill and memory line and, when it can be one, Make it a rule |
| What the draft was asked to do | The Studio's Overview section `data-sw="asked"` (`#sw-asked`): each soft requirement as an `.sr-line` with `.cv-ok` (applied) or `.cv-warn` (not) and "Applied · 3 on (usually 2) · from your reviews · until 10/4/26"; pattern clashes left out of the draft; "Details trimmed after 30 days…" on a thinned stored week |
| What your nights have taught | `.mem-teach` at the top of Events & reservations — the ai surface (`--sf-ai`), an orange kicker, one line per measured label with an ember2 glowing dot, dimmed below its sample floor. A listed event carries its label's record as a memory line in the table |
| Your rating over time | `.mem-rating` on Intel's What changed: the latest rating in the number face at card size, the change in green or red since the first week on file, a `glowLine` of the weekly readings (hover names each week, M/D/YY), then "Over the months" — the market's arrivals, departures and rating moves as `.in2-comp` rows, six shown and Show all N |
| Pars to raise | `.mem-pars` inside What to order this week (`data-nav="inventory/pars"`): "Raise the par on Mozzarella to 12", the 86s it rests on as a memory line, Raise par to 12 (secondary) and Pass |
| Fix tags | A `cbtn-text` Fix tags in each review card's pill row opens `.rv2-retag` under it: the analyser's topics as toggle buttons (one to three), How it reads, How serious ("—" while unknown), the dishes it names; Save the tags sends only what changed |

Links that land here: `labor/notes`, `labor/people` (a `labor` head handler that
returns false for every other section) and `inventory/pars` (a plain
`data-nav`). The decline on this wave's surfaces reads "Pass" (owner, 10/1/26): the
schedule review's ✕, the reprice table, the win-back card.

### What a card remembers — iOS (memory round, 9/29/26)

The memory round put what Cavnar AI remembers about each piece of advice
into the payloads; iOS draws it with one set of views
(`DesignSystem/RecMemoryViews.swift`), the same on Home's cards, Needs
attention, the one-thing hero, the brief's lines and the nightly report's
priorities. The web draws the same fields on its own cards (`dashboard.html`):
§12's "What was said before", "Caution line", "Conflict chooser", "Kind
hold", "Brief line extras" and "Policy notice" rows; the module screens' memory
(people, notes, patterns, pars, retags, rating history) is the web's "What
Cavnar AI remembers" section and, on iOS, the "What Cavnar AI remembers (iOS)"
row.

| Piece | iOS | Reads | Look |
|---|---|---|---|
| History lines | `RecMemoryNote` | `previous_answer.text`, `delegate_answer.text`, `retest` | The server's sentence ("You passed on this on 3/12/26 ($120/mo then)", "Dana passed on this: already doing it (9/28/26)"), caption size in ink3, one small glyph per kind of memory (clock-arrow, person, arrow). The re-test line is the one the client writes. On the row, never behind Details — it changes how the owner answers |
| Caution | `RecCautionLine` | `caution` (M3's trim guard) | Amber triangle and amber caption text. The card stands, ranked lower; never red |
| Conflict | `RecConflictPanel` | `conflict {id, with, why, choose[]}` → POST `/recs/conflict {conflict, prefer}` | An inset on the card: amber 7% wash, amber 30% hairline, control radius. An orange kicker "Pulls against: <the other card>", the why in ink2, then one ember text button per way to settle it ("Keep “Trim Tuesday staffing”", "Hold it"). A 3pt ember pulse while it saves; the server's sentence replaces the buttons. A decision, so never in a collapsed section |
| Kind hold | `HomeKindHolds` | `kind_holds[]` | "Keep suggesting trim day?" under the recommendations, in an `.ai` card: the why, then two bars on one scale — "Improved here" (amber gradient, soft glow) against "By doing nothing" (ink3) — figures in the number face, grown in once without bounce; answered with `RecAnswerRow` in the server's words ("Keep suggesting it" / "Stop suggesting it"), no reason picker |
| The report's calls | `HomeReportCalls` | brief `today` line: `predictions`, `confidence_pct` | Under the brief's "today" line: an orange kicker "The report's calls", the confidence meter and "range held 72%", then each call on an ember dot |
| Policy notice | `HomePolicyNoticeCard` | `policy_notice` | A card under the header: an ember-tinted glyph tile, the notice's sentence, "Read what changed →" in ember2, and the ✕ action chip; it leaves only once the dismissal is saved |
| Memory sheet | `AccountMemoryView` | `/account/memory` | The identity-card kit: hero, the lanes as meters ("Rules · 3 of 30", amber when full), the facts by kind with who / audience / modules / dates in ink3, the archive with Restore, the add form. Dates are one-tap choices ("A week", "A month") shown back as M/D/YY — never a system date field |

"Pass" is the decline everywhere on web and iOS (owner, 10/1/26; it read "Not for us" from 9/29/26), and every
answer shows the server's `message` — what the answer does — in place of
the buttons.

### Schedule fix round (web, 10/4/26 — UI wave W1)

The Studio's generate screen, the draft, the review, the publish check,
Shift Quality and the learning rows, built on the schedule fix round's
payloads (`dashboard.html` `<style id="cav-sfw1-css">` and
`<script id="cav-sfw1">`: pure `sfw1*` helpers turn a payload into words,
the IIFE draws them). New patterns, each the smallest step from one that
existed:

| Piece | Class / host | Reads | Look |
|---|---|---|---|
| Week notice | `.sfw1-notes` > `.sfw1-n` (`.warn` / `.bad` / `.ai`) in `#sfw1-week` (under the week's header) and `#sfw1-sum` (the Summary) | `unwritten_dates`, `unstaffable_dates`, `manager_plan`, `manager_coverage.left` / `.shortfall`, `min_hours.left`, `starting_point`, `demand_data_through`, `review.setup` | The `.snr` state stripe as a recessed notice: a 3px inset rail (ink3 / amber / red; ember on the ai tone), the sentence at 14.5px, its actions at the end (`cbtn-sm`). One per thing; the setup's quiet items fold into one "How this week was set up" list |
| Owner question | `.sfw1-q` (ai tone, `--glow-ember`) | `manager_plan.question`, `unknown_pattern` | Orange kicker "A question for you", the question as a Clash 21px title, the why, then per name a seven-day grid (`.sfw1-wk .d`: a day toggle and two `cavTimeOptions` selects; a ticked day takes the ember outline) and one soft Save → standing shifts (`POST labor/staff-settings`) |
| Manager plan chip | `.swg-chip .sfw1-pin` | a row's `_pinned == "manager_plan"`, `_pin_reason` | A 10px uppercase pill in ember2 on the recess, inside the week chip; the reason is its title and the shift pane's flag. `.sfw1-dst` is the same pill in ink3 for `dst_hours` |
| Day-header lines | `.swg-d .sfw1-mw` / `.sfw1-hd` / `.sfw1-nw` | `manager_plan.windows`, `review.hard_days`, `unwritten_dates` | "Manager on 11:00am–11:00pm" in ink3 11px; a day-level breach as a small red-tinted note on the day (never on a person's chip); "Not written" as the header's own `.st` pill in amber |
| Sheet | `#sfw1-modal.so-modal` + `.so-card` (`cModal`) | Redo some days; "Why this change?" after a save | The supplier confirm's frame at 520px. Redo: reason chips (`.chip-tog .ct`, one lit), "What's wrong? (optional)" 300 characters with a count, Not now / Redo N days. Why: the change in a sentence, one secondary button per answer, Skip |
| Review groups | `.sfw1-rv` inside `#sched-review` | `review.stage_failures`, `review.unmet`, `review.budget_conflict`, `review.cap_floor_conflicts`, `review.unmatched_names` | `.sr-k` + `.sr-line`s. "What this week doesn't meet" groups by day ("Fri 10/16/26", "This week" last): `.hard` (ember dot) for manager / floor / rule / close, ember2 dot for coverage / leadership / strength / station / ask, plain for minimum hours / budget, "check it yourself" on a rule no code checks; the what in bold over the why |
| Measure bars | `.sfw1-dims .sfw1-dim` in an expanded shift | `shifts[].dimensions[]` (`score`, `floor`, `facts`) | Every counting measure as a 6px bar, its floor a 2px ink3 tick; facts lines under it at 12.5px; a leadership miss offers "Add a …" / "Swap in a …" |
| Points | `.sfw1-pts` | `recommendation_items[].points` | "up to +6 points" in green, Space Grotesk, after the suggestion; the list sorts most valuable first |
| Memory row | `.sfw1-mem .r` | `GET labor/schedule-memory`, `GET labor/ratings/suggested` | The learned-pattern row grown: sentence, `.mem-pill` status, a caption ("72% sure · 2 of 3 · last confirmed by hand 9/28/26"), actions at the end (Keep / Let it go / Make it a rule; Confirm N + a 1–5 `.chip-tog`) |

## 12c. The admin console (internal, `templates/admin.html`)

Rebuilt 9/28/26 as five places, each answering what needs attention, what
changed, who needs help, what is broken and what is growing. Dense with data,
never with text: the rebuild cut the visible text across its screens by 69%
(51% leaving out the three long logs), measured on the same database.

- **The five areas** — Overview, Operations (Jobs, Email & SMS, Push & alerts,
  AI, Integrations, Event calendar — every followed series' games with an
  inline correction row: only the changed fields are sent, a corrected game
  carries a "corrected" chip and a Revert, and an if-necessary game whose
  date has passed with no result a "needs a result" chip — the existing
  `chip warn`, its title saying enter the result or cancel it — with the
  series' count in amber beside its game count), Customers (Restaurants, Billing, Onboarding, Support
  queue, and the client page), Engineering (Overview, Status page, Audit
  trail, Team & access, Experiments), Analytics (Overview, Recommendations,
  Intelligence) — plus Field tools (audits, cheat
  sheet, status page) at the foot of the rail. Every older hash (`#clients`,
  `#jobs`, `#emails`, `#recommendations/<id>`, …) lands in the area and tab it
  moved to (`ALIAS`); keep that map when a page moves again.
- **Palette** — the web app's dark staircase (`--bg` canvas < `--rail` <
  `--card` < `--raised`), Clash Display titles, Space Grotesk for every figure,
  Apfel Grotezk for the rest; ember for the one primary action per screen and
  the kickers; green / amber / red only for state.
- **Area header** — kicker, title, and one sentence written by rules from the
  page's own payload ("1 job is failing · 14 jobs are overdue.") — no model on
  load.
- **Rank, don't list** — hero figures (`.kc`, 40px) for the few that matter,
  then a figure strip (`.strip`, one card split into cells, each a link), then
  the lists.
- **Operational list** (`.lst` / `.li` / `.lx`) in place of a dense table: one
  row per thing with its state pill, one action, and the rest on expand
  (`toggleRow`). A secondary action shows on hover (`.reveal`); a failing
  row's action is always shown.
- **Tables** stay for detail lists only, `limit` rows at a time with "Show
  all", and raw logs sit behind a `<details class="card">`.
- **Filters** are a segmented control (`.seg`) of the five or six that matter,
  the rest under a More filters menu.
- **Actions menu** (`.menu`, `menuToggle`) — a client's 25 actions in one
  searchable menu, grouped, the danger zone red and last; every action still
  confirms.
- **Honest reads (fix round, 9/29/26).** A figure whose read failed is the
  dimmed word "unknown" (`unknownV`, `figOr(d, tables, html)` — the failure
  names the table), never 0 or "all clear"; a payload with failed reads gets
  the amber line (`errBanner`) and every fleet header its age (`asOf`). A
  platform system whose read failed is a grey ring segment, never green; the
  others are green / amber / red by their own thresholds. The rail polls the
  slim badges read; when a poll fails it keeps the last counts, dimmed
  (`.rail.stale`), and the heartbeat says "unknown" with when it last read.
- **Writes say what really happened.** Every write control carries `.w`
  (hidden for a support login). Its confirm names the client or the
  recipient; the toast is the server's own sentence (`say(d)`) — its `error`
  on any non-2xx, never "done" for a 409 or a 502. An action that starts a
  background job follows it to its end (`pollJob`), and a value handed over
  once (a new password) opens in the Shown-once box. A write that acts
  outside Cavnar AI — cancels a Stripe subscription, voids a DocuSign
  envelope, revokes every connection — says so in its confirm in plain
  words, with what cannot be undone; a confirm that states a lifetime
  (the welcome's set-password link: once, for 3 days) is pinned to the
  server's constant by a test (`tests/test_fix_ui_ui1.py` reads
  `models.SET_PASSWORD_LINK_HOURS`). A link to a page support is
  refused (the legacy settings and data pages) carries `.w` like a write —
  every one, held source-wide by `tests/test_fix_integration_lead.py`.
- **One stored field edited in place** (the client's menu notes, UI-3)
  sends only that field, with the version and the value it loaded
  (`expected_version`, `base`) — the settings contract. A 409 refills the
  box with what is stored now and keeps the admin's own text under it
  ("Your text, not saved") to put back; no answer at all says the outcome
  is unknown and asks for a reload, never "saved". A counter shows the
  length against the server's own cap and "not saved" while the text
  differs from what was loaded.
- **The server's guards are answered in one place** (`api()`): 401 → sign
  in again; 403 `two_factor_required` → the enrolment page; 403
  `reauth_required` → the password asked once, however many calls asked
  at once, then the call sent again (Cancel returns the refusal); 503
  `busy` → wait its `Retry-After` once (10 seconds at most); 429 → say so,
  retry nothing. The HTTP status rides on the answer as `_status`, so a
  caller can tell a refusal (409) from a send that failed (502).
- **A form an action needs** opens in one modal (`formOpen(spec)`: fields,
  the go button, `submit` returning the server's answer); a refusal keeps it
  open with the server's sentence under the buttons.
- **A tab's extra reads load after first paint** (`lazy(id, fn)`): the card
  shows skeleton lines, then its content, or its own amber line if its read
  failed — one slow or broken panel never blanks the page.
- **Server-paged lists** end in a pager row (`.pager`: "N rows · page X of
  Y", Previous / Next); filters and search go to the server, counts come
  from the full set.
- **Spend against a ceiling** is a `.meter` tile: spend of budget, a bar
  (green, amber past the warning line, red when stopped) and when it resets
  (M/D/YY).
- **A system's state is the server's verdict** (Operations, fix round UI-2).
  Each system takes its tone from what the server already decided — a job's
  own state, the heartbeat's `stale` / `wedged` / `loop_stalled`, a platform
  issue's severity, the messaging problems, the AI health status — never a
  threshold the page makes up (`opsState`). Red where the server pages or
  raised a critical, amber for its warnings, grey "Unknown" when the read
  behind it failed. The area's sentence names every system that is failing,
  degraded or unread, and says "Everything ran" only when all were read
  (`opsSentence`). The subsystem cards (`subCard`, six to a row) carry the
  state as the icon's tone, a red border when failing, and the first word of
  the line under the trend.
- **A job's runs are squares in their own state** (`.sq`): ok green, partial
  amber (`.p`), failed red (`.x`), running ember (`.r`), no run yet empty
  (`.e`), newest on the right. Partial and running are never green; neither
  is an admin task that failed.
- **Lines over time** (`lineChart(points, series, opts)`): one axis, each
  series its own colour (a dashed line for the secondary one), a point with
  no value is a gap in its line — never a zero — and `mark` puts a red tick
  on the axis (an hour when every thread was busy). Used for latency, with
  customer and console traffic as separate lines, and for the business day
  by day from the snapshots.
- **The learning data (memory round UI wave, 9/29/26).** What the platform
  learned is drawn in the patterns above, extended three ways:
  - A **learning curve** (the client's Messages & AI → AI tab,
    `learningCurveHtml`) is one row per curve: a small line over the
    months (`curveSpark`) where a month under that curve's own sample
    floor is a gap, never a zero; the line takes the server's flag on
    the newest month — red worsening, amber flat, ember otherwise; then
    the newest figure with its n and which way is better. Fatigue is one
    sentence that says what it cuts.
  - **One square a period** (`.sq`, a job's runs) also draws an
    experiment's stored weekly verdict and a pattern's weekly record, each
    square in its state (green / amber / red / empty). An ISO week reads
    "week of 9/21/26" (`isoWeekDay`, `weekLabel`) and a month "Aug 2026"
    (`monLabel`), never "2026-W39" or "2026-08".
  - **A rate with its 90% range** reuses `.calbar` without the prediction
    tick (the ranking's taken rate by weight bucket), dimmed under the
    floor; a bar with rows written later from raw history shows that
    share in grey under the ember (`featureWeekBars`).
- **Reads go three at a time** (`apiAll(paths, 3)`) — four request threads
  serve the whole platform — and an area's reads are shared across its tabs
  for a minute; Refresh reads them again with `?fresh=1`.
- **A cursor-paged log** (the audit trail, refused attempts) ends in an
  "Older" row (`.more`) that appends the next page by `before_id`; filters go
  to the server. A numbered `.pager` is for lists the server pages by number.
- **Admin-only reads carry `.w` too.** A control that opens a prompt and a
  model's output (the call trace) is hidden from a support login like a
  write.
- **Cmd-K / `/`** opens one palette: every page and action, and the fleet
  search.
- **A page's reads belong to the visit that asked** (`api()` and `_seq`): a
  read that returns after the admin has moved on is dropped, never drawn
  over the newer page.
- Dates read M/D/YY here too (`mdy`). No `?.` or `??` (older iPad Safari at an
  on-site audit, `tests/test_edge_client_web_admin.py`).

## 13. Checklist before shipping a screen

1. Tokens only — no literal colours (`scripts/check_colors.py`).
2. Every number in Space Grotesk; headings in Clash; chrome in Apfel.
3. One primary action; every web button carries `.cbtn`.
4. Loading uses the pulse/skeleton/orb, never a spinner.
5. Empty states say what would fill them; unknown is never zero.
6. Text sizes match the rest of the product.
7. Reduced motion has a still fallback; inline JS is ES5.
8. The same feature reads the same on web and iOS — same words, same order,
   same figures. One name per concept, the plain owner-facing one (parity
   round 9/25/26): "How your replies were approved" (not "Response Rings"),
   "Reviews · per week" (not "Sentiment River"), "What guests talk about"
   (not "Topic Heat Grid"), "Listing strength" (not "GBP completeness"),
   "Gaps in your public record" (not "Your AI visibility roadmap").

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

Pick by what the email *is*, not by which is nearest:

1. **`report_shell(kicker, title, subtitle, sections, cta_label, cta_url)`** —
   anything an owner reads for information. The digest, the monthly review,
   the closing summary, the nightly DSR (`emails.dsr_email`). Compose the body
   from `report_eyebrow`, `report_paragraph`, `report_stats`, `report_lines`,
   `report_bullets`, `report_quote`, `report_action`, `report_rule`. **New
   reporting email starts here.** `report_bullets(items, accent)` is
   `report_lines` without a label — a list whose eyebrow already names it
   (the DSR's *Went well* on a `good` rule, *Needs attention* on `warn`,
   *Not in this report* on the quiet border).
   `report_stats` (9/30/26): a figure never wraps (`nowrap`); a row whose
   longest figure passes 6 characters steps 25px → 22px → 19px; each
   column is its figure's or label's width plus an equal share of the rest
   (balanced for a 330px phone column), so the gap after every figure
   matches; columns line up across rows. A rating is toned by where it
   stands (4.0+ `good`, 3.0+ `warn`, else `bad`), as everywhere.
   `report_metric_lines(metric_parts(review, lines))` sets a review's
   comparison as one block per metric: its name as a 15px `ember` heading,
   the sentence under it in `body`, 18px between blocks — never one run-on
   paragraph.
   `report_confidence(conf)` is the one line under a recommendation that
   carries a measured confidence — "72% confidence · data through 9/23/26"
   (`rec_trust.outbound_label`), 12px, `muted` — never a band word; "" for
   a fact, which carries none.
2. **`_branded_email(inner_html)`** — short transactional mail: a code, a
   confirmation, a link. Wordmark, one white card, seal footer.
3. **Bespoke** — legacy. 14 emails still are. Not a starting point; migrate
   onto `report_shell` when you touch one.
4. **`guest_newsletter_email(restaurant_name, body_html, headline, image_url,
   button_label, button_url, footer_html)`** — a restaurant's newsletter to
   ITS guests (Campaign Studio, 9/28/26). Not Cavnar AI's mail, so no
   wordmark or seal: the restaurant's name as the masthead, an optional
   full-width photo from its media library, a Georgia headline (a letter,
   not a report), the letter at 16px in `body`, ONE `ember` button (only
   with an http(s) link), the CAN-SPAM footer (why, unsubscribe, address).
   560px like `report_shell`.
5. **`_billing_frame(kicker, title, body_html)`** — the billing emails
   (dunning, card update, receipt, pay reminder, the signed welcome; the
   section at the end of `emails.py`, fix round H 9/29/26): the wordmark
   with the kicker set right in `ember` caps, ONE white card with a 3px
   `ember` top rule and a 20px `strong` title, the seal footer. 560px. Body
   parts: `_billing_p` (14.5px `body`, or 13px `muted`), `_billing_button`
   (the one `ember` button), `_billing_link` (a secondary `ember` link with
   an arrow), `_billing_money` (the amount in `_NUM`, `strong`),
   `_billing_hi` (the owner's first name, or "Hi —"). Every billing sender
   returns a `SendResult` and passes `restaurant_id`, so it appears in the
   client's email history.

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
- **An unsubscribe and a postal address, if it is marketing — the CAN-SPAM
  footer.** Applied centrally in `emails.deliver` for `_MARKETING_TYPES`
  (`emails._add_unsubscribe`): one centred 11px `muted` line under the card —
  why the reader gets it ("You get this because you use Cavnar AI." / "…
  because a Cavnar AI client introduced us."), an underlined opt-out link
  ("Unsubscribe from product emails" / "Don't email me about Cavnar AI
  again"), for an owner "Account and security emails are sent regardless.",
  and "Cavnar AI · <postal address>" — plus `List-Unsubscribe` and
  `List-Unsubscribe-Post`, which is what gets Gmail to show its own
  affordance instead of the spam button. An owner's opt-out is signed over
  their restaurant; a prospect's over their address (a marketing-only
  suppression). **No address, no send**: without `CAVNAR_POSTAL_ADDRESS` the
  marketing email is not sent and the operator is told once a day. A
  restaurant's guest newsletter carries its own restaurant's version.
  Security and operational mail must never carry one.

### Never

- A figure a model produced that was not verified against its input
  (`ai_guard.unsupported_figures` / `verify_figures`).
- A raw `resend.Emails.send` for client mail — `emails.deliver()` owns retry,
  suppression, the flood guard and `email_log`.
- A restaurant's name as the display name on Will's address.
