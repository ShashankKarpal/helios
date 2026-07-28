# Helios brand guide

Concept 02, The Meridian. Everything on this page is enforceable.

Converted from `brand/BRAND-GUIDE.html` on 2026-07-28. Markdown so GitHub renders it inline; the HTML version showed as raw source.

## 1. The mark

A solid point of light, and the single line it is measured against, interrupted by it.

- The disc is the value Helios decided to believe.
- The rule is the user's own baseline.
- The gap exists because the value does not sit on the baseline politely. It **is** the reading, so it breaks the line.

Second reading, arriving a beat later: the sun crossing the meridian at solar noon, the moment a shadow is shortest and a measurement is most honest.

| Variant | File | Use |
|---|---|---|
| Dark, native | `brand/symbol-dark.svg` | The default. Product, README, dark surfaces. |
| Light and print | `brand/symbol-light.svg` | White and near-white grounds. |
| One flat colour | `brand/symbol-mono-black.svg`, `brand/symbol-mono-white.svg` | Menu bar, print, tinted icon, single-colour contexts. |
| Bridge sibling, muted | `brand/symbol-bare-dark.svg` | The Bridge companion app, deliberately quieter. |

## 2. Geometry

Drawn on a 4pt grid, delivered on a 1024 master.

| Element | Value at 1024 |
|---|---|
| Disc radius | 184 |
| Rule height | 44 |
| Stub length | 184 each side |
| Gap, disc to stub | 32 |
| Stub corner radius | 8 |
| Artwork width | 800, centred |

**The gap is the single most important number on this page.** It is what separates the mark from a hyphenated dot. Do not close it, do not double it.

## 3. Clear space

One disc radius on all four sides of the artwork bounding box, expressed as a ratio so it scales. If the disc is 40px across, keep 20px clear.

Minimum sizes:

| Context | Minimum |
|---|---|
| Full lockup | 120px wide |
| Symbol | 24px wide |
| Menu bar glyph | 18px |
| Favicon | 16px |
| Lockup in print | 22mm wide |

At 18px and below, use the optical template drawing at `brand/menubar/HeliosTemplate.svg`, not a scaled-down icon. The template is redrawn with its own minimums: disc radius 3.8px, stubs 3.6 by 2.4px, 1px clear gap. A mechanical downscale loses the stubs and leaves a dot.

## 4. Colour

Source of truth is `../design/tokens.json`, which mirrors `web/src/index.css` plus the two print tokens. **The mark uses two colours and never a third.**

| Token | Hex | Role |
|---|---|---|
| `bg` | `#0B0D0C` | Canvas |
| `surface` | `#151917` | Bridge icon field |
| `hairline` | `#232826` | Dividers |
| `text` | `#E8ECE9` | Body |
| `muted` | `#9AA49E` | The rule |
| `mint` | `#7EE0B1` | The disc, the only accent |
| `mintPrint` | `#1B7A55` | The disc on white, 5.3:1 |
| `mutedPrint` | `#5F6B65` | The rule on white, 5.6:1 |

`caution #FBBF24` and `alert #F87171` carry state meaning in the product: a marker outside its band, and a dead sync. They are **off limits to the identity** in every context.

## 5. Misuse

- Do not stretch or condense.
- Do not recolour, and never use caution amber or alert red.
- Do not add a glow, gradient, or shadow.
- Do not place it on a photograph or a busy field.
- Do not crowd it. Keep one disc radius clear.
- Do not rotate it. The rule is a horizon.

## 6. The lockups

| Lockup | File |
|---|---|
| Primary, horizontal | `brand/lockup-horizontal-dark.svg`, `brand/lockup-horizontal-light.svg` |
| Secondary, stacked | `brand/lockup-stacked-dark.svg`, `brand/lockup-stacked-light.svg` |
| Integrated | `brand/lockup-integrated-dark.svg`, `brand/lockup-integrated-light.svg` |
| Mono | `brand/lockup-horizontal-mono-black.svg`, `brand/lockup-horizontal-mono-white.svg` |

The disc sits 124 units across against a wordmark x-height of 100, aligned near the x-height centre rather than the block centre, which is what stops the symbol floating.

## 7. Typography

**The wordmark is an original geometric construction, not a typeface.** A monoline lowercase built from annular sectors and rectangles on the same grid as the mark, weight 17 against an x-height of 100, with the disc reused as the tittle of the `i`. It is deliberately lighter than the mark so the disc remains the only heavy element. It ships as outlined paths, never as live text.

Because it is original, an MIT repository carries no font licence obligation and no attribution requirement.

**Montserrat remains the product typeface**, for the PWA, the clinician report, and every document. That division is correct: the wordmark is drawn once and frozen, the interface is set in a licensed face that has to handle every string in the app. Montserrat is SIL OFL 1.1, so it is safe to ship and to embed.

Numerals in the product stay tabular, via the existing `.tnum` utility. A health interface where digits shift width as values change is a health interface that looks like it is guessing.

## 8. The rule behind the rules

The mark must never look like it holds an opinion about the person using it. No arc that reads as a percentage complete, no upward direction implying improvement, no tick, no face.

There is a specific trap in the palette: **mint is green, and in every health app green means good.** Mint is therefore spent only on shapes with no valence: a disc, a tick, a baseline. Never on a direction, an arrow, a rising line, or a closing arc.

That single constraint shaped this mark more than anything else in the brief, and it is the one to defend if the identity is ever extended.

## Asset index

| Folder | Contents |
|---|---|
| `brand/` | Symbol, wordmark, and lockup SVGs in every variant |
| `brand/app-icons/` | macOS `.icns` and `.iconset`, iOS, watchOS, Icon Composer layers |
| `brand/menubar/` | Optical template glyph, 1x 2x 3x, plus PDF |
| `brand/widgets/` | Lock Screen circular and rectangular, widget corner glyph |
| `brand/sf-symbol/` | Three weights: ultralight, regular, black |
| `brand/print/` | Letterhead, light and mono |
| `github/` | `readme-banner-{light,dark}-1400x400.png`, `social-preview-1280x640.png`, `avatar-400x400.png` |
| `web/` | `og-1200x630.png`, favicon set, touch and maskable icons |
| `tokens.json` | Single source of truth for colour and geometry |
