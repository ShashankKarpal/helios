# Helios identity: delivery and wiring

**Concept:** 02, The Meridian. One solid point of light, and the single line it is measured against, interrupted by it.
SUPERSEDED (2026-08-19): this document describes the pre-Ink-and-Bone
palette and is kept as history only. The live system is Ink and Bone
v1.0.0; see design/BRAND.md in this repo and the canonical definition in
ShashankKarpal/shashankkarpal under design/brand/. Do not take hex values
from this file.

**Palette:** unchanged. `mint #7EE0B1` on `bg #0B0D0C`, rule in `muted #9AA49E`. Two new tokens for print, listed in section 6.
**Licence:** everything here is original geometry. No purchased asset, no licensed typeface, no font dependency. It ships MIT with the code cleanly.

---

## 1. Do these six things, in this order

Each step is independent and each one is visible the moment it lands.

| # | What | Where it shows up | Time |
|---|---|---|---|
| 1 | Fix the PWA icon set (section 2) | iPhone Home Screen, Mac Dock, browser tab | 10 min |
| 2 | Drop the Bridge app icon into Xcode (section 3) | the Bridge app on your phone and in Settings | 10 min |
| 3 | Add the README banner and social preview (section 5) | the GitHub repo page | 5 min |
| 4 | Add the two print tokens (section 6) | clinician report, weekly review | 2 min |
| 5 | Wire the menu bar template when you build the status item (section 4) | macOS menu bar | later |
| 6 | Make the custom SF Symbol from the artwork (`sf-symbol/HOW-TO.md`) | Control Center, Lock Screen, Siri, Shortcuts | 10 min, needs SF Symbols.app |

**Start with step 1.** There is a live bug in the repo it fixes: `web/index.html` currently points `apple-touch-icon` at `/icon.svg`, and iOS does not accept SVG for a touch icon. So today, when Helios is added to the iPhone Home Screen, iOS falls back to a screenshot of the page instead of an icon. A 180 x 180 PNG fixes it.

---

## 2. PWA: iPhone Home Screen, Mac Dock, browser tab

Copy from `pwa/` into `web/public/`:

| From | To |
|---|---|
| `pwa/icon.svg` | `web/public/icon.svg` (replaces the placeholder) |
| `pwa/icon-maskable.svg` | `web/public/icon-maskable.svg` |
| `pwa/favicon.svg` | `web/public/favicon.svg` |
| `pwa/favicon.ico` | `web/public/favicon.ico` |
| `pwa/apple-touch-icon.png` | `web/public/apple-touch-icon.png` |
| `pwa/icons/` | `web/public/icons/` |
| `pwa/splash/` | `web/public/splash/` |
| `pwa/manifest.webmanifest` | `web/public/manifest.webmanifest` (replaces) |

Then replace the contents of `<head>` in `web/index.html` between the `theme-color` meta and the `<title>` with `pwa/head-snippet.html`. It contains the favicon links, the PNG touch icon, and nine `apple-touch-startup-image` entries covering iPhone SE 3 through iPhone 16 Pro Max, so the PWA gets a real splash screen on `#0B0D0C` instead of a white flash.

Two things changed in the manifest and both were bugs:

- The old manifest declared one SVG with `"purpose": "any maskable"`. A single icon cannot serve both: `any` should have breathing room, `maskable` must be full-bleed with the artwork inside the inner 80 percent. They are now separate entries.
- PNG entries at 192 and 512 were missing. Chrome and Android installs need them; iOS needs the 180 PNG.

The maskable artwork sits at 86 percent scale so its widest points land 344 units from centre, inside the 409 safe radius. `proof/contact-sheet.png` shows it with the safe circle drawn.

---

## 3. Xcode: Helios Bridge, and anything native you add later

You have four ready-made asset catalogues in `xcassets/`. Each is a complete `Assets.xcassets` folder, not a fragment.

| Folder | For | Icon |
|---|---|---|
| `xcassets/HeliosBridge-iOS/` | the Bridge app you ship today | the **muted sibling** |
| `xcassets/Helios-iOS/` | a future native iOS Helios app | mint |
| `xcassets/Helios-macOS/` | a future Mac app or status-item app | mint |
| `xcassets/Helios-watchOS/` | a future Watch app | mint |

### Bridge gets a different icon on purpose

Bridge is not a destination, it is a pipe. Its whole job is to be granted HealthKit permission once and opened only when sync dies. If it wore the mint mark you would own two visually identical icons and tap the wrong one daily.

So Bridge takes the same geometry with the colour roles swapped: the disc in `muted #9AA49E` on `surface #151917`. That is semantically exact rather than an arbitrary downgrade, because `muted` is the product's provenance and corroboration colour, and Bridge is literally provenance. Mint interprets, grey carries.

### Wiring it, XcodeGen

Copy the folder in:

```bash
cp -R xcassets/HeliosBridge-iOS/Assets.xcassets ios-bridge/
```

Then in `ios-bridge/project.yml`, add the catalogue to sources and name the icon:

```yaml
targets:
  HeliosBridge:
    sources:
      - path: Sources
      - path: Assets.xcassets          # add this line
    settings:
      base:
        PRODUCT_BUNDLE_IDENTIFIER: com.shanky.helios.bridge
        PRODUCT_NAME: Helios Bridge
        ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon        # add this line
        ASSETCATALOG_COMPILER_GLOBAL_ACCENT_COLOR_NAME: AccentColor   # optional, see below
```

Then `xcodegen generate` and build. The icon appears on the Home Screen, in Settings, in the app switcher, and in Spotlight.

The catalogue contains all three iOS 18 appearances:

- **default**, opaque `#0B0D0C`
- **dark**, transparent background so the system composites its own dark material
- **tinted**, greyscale single channel, disc at white and rule at mid grey so the hierarchy survives tinting

### Your paid Developer Program seat changes four things

You mentioned the seat is now active. Worth acting on:

1. **`DEVELOPMENT_TEAM` is now set in `project.yml`**, not in Xcode's UI. This matters: `ios-bridge/*.xcodeproj` is gitignored and generated, so a team picked in Signing and Capabilities is silently discarded by the next `xcodegen generate`. Setting it in `project.yml` makes signing reproducible. A Team ID is not a secret; it ships inside the entitlements of every signed build.
2. **Widget extension is now practical.** The roadmap lists Home Screen widgets. A widget is a separate target that shares the app icon, and `widgets/` already has the corner glyph and both Lock Screen shapes. A widget needs an App Group to read Bridge's data, which the free tier does not give you.
3. **Silent push wake becomes real.** `UIBackgroundModes` already lists `remote-notification` and `BGTaskSchedulerPermittedIdentifiers` is already declared, so the app side is done. The seat unlocks the APNs key. That is the piece that would make the "Mac was asleep, batches queued" freshness gap close on its own.
4. **TestFlight is available but not worth it.** TestFlight builds expire in 90 days, which is
   strictly worse than the one-year development profile you now have. Only reach for it to get a
   clean install onto a device you cannot cable to.

### Optional: an accent colour in the catalogue

If you add an `AccentColor` colour set with `#7EE0B1`, SwiftUI controls in Bridge tint to the brand automatically. Two minutes, and Bridge stops looking like a default-blue Xcode app.

---

## 4. macOS menu bar and the Dock

The `heliosd` status item does not exist yet, so this is ready rather than wired.

- `menubar/HeliosTemplate.png` `@2x` `@3x` plus the vector PDF. Pure black with alpha, so macOS inverts it for light and dark menu bars. `menubar/README.md` has the four lines of Swift.
- `app-icons/macos/Helios.icns` is compiled and ready for any Mac app bundle.
- `app-icons/macos/Helios.iconset/` is there if you would rather compile it yourself on your Mac: `iconutil -c icns app-icons/macos/Helios.iconset -o Helios.icns`.
- `app-icons/icon-composer/` holds the layered sources for Xcode 26's Icon Composer, which generates the light, dark, clear, and tinted macOS 26 appearances.

**The Dock icon today comes from the PWA**, not from a Mac app. Safari's Add to Dock reads the manifest, so step 2 is what fixes the Dock.

### One thing done deliberately, and you should know why

The macOS icons at 16, 32, and 64 pixels are **not** downscales of the 1024. They are redrawn with the menu bar template's proportions, because a mechanical downscale loses the rule stubs entirely below about 48 pixels and leaves you with a plain mint dot in the Finder sidebar. There is a visible weight change between 64 and 128 in the ladder. That is intentional, it is what Apple does in its own icon sets, and `proof/contact-sheet.png` shows the ladder at true pixel size so you can see it working.

The favicon is redrawn the same way for the same reason.

---

## 5. Repository and social

| File | Where |
|---|---|
| `social/github-social-1280x640.png` | GitHub, Settings > General > Social preview |
| `social/github-avatar-400.png` | your profile or an org avatar, survives the circular crop |
| `social/readme-banner-dark-1400x400.png` | top of `README.md` |
| `social/readme-banner-light-1400x400.png` | the light-theme half of the banner |
| `social/og-1200x630.png` | Open Graph, for whenever there is a landing page |

For a banner that follows GitHub's theme, put both in `docs/` and use the picture element:

```html
<picture>
  <source media="(prefers-color-scheme: dark)"  srcset="docs/readme-banner-dark-1400x400.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/readme-banner-light-1400x400.png">
  <img alt="Helios" src="docs/readme-banner-dark-1400x400.png" width="700">
</picture>
```

SVG sources sit beside each PNG. If you want a tagline under the lockup, add it in the SVG and export on your Mac, where Montserrat is installed.

---

## 6. Two new tokens, and one deliberate exception

`tokens/tokens-additions.json` has the full spec. The short version:

| Token | Hex | Contrast on white | Use |
|---|---|---|---|
| `mintPrint` | `#1B7A55` | 5.3:1 | the disc in light and print contexts |
| `mutedPrint` | `#5F6B65` | 5.6:1 | the rule in light and print contexts |

These exist because `mint` on white is roughly 1.6:1 and fails at every size. Snippets for `web/src/index.css` and `web/tailwind.config.js` are in `tokens/`.

**The exception: the clinician report letterhead is pure black, not green.** `print/letterhead-mono.svg` and `.pdf`. That document is about someone's body and it goes to a stranger who has never heard of Helios; brand colour on it is a category error, and it also has to survive a photocopier. `mintPrint` belongs on the printed weekly review and any future light web surface, not on the report header.

---

## 7. What is in `masters/`

| File | Notes |
|---|---|
| `symbol-dark` / `-light` / `-mono-black` / `-mono-white` | the mark in a squircle |
| `symbol-bare-dark` | the mark with no background, for placing on `surface` |
| `wordmark-dark` / `-light` / `-mono-black` / `-mono-white` | outlined paths, not live text |
| `lockup-horizontal-*` | the primary lockup, symbol plus wordmark |
| `lockup-stacked-*` | the secondary lockup |
| PDFs | for anything that touches print or a vendor who wants PDF |

The wordmark is an **original geometric construction**: a monoline alphabet built from annular sectors and rectangles on the same grid as the mark, with the disc reused as the tittle of the `i`. Weight 17 against an x-height of 100, deliberately lighter than the mark so the disc stays the only heavy element on the page.

It is not Montserrat and not Montserrat-derived. Montserrat remains the product and UI typeface, which is the right division: the wordmark is drawn once and frozen, the interface is set in a licensed face that handles every string in the app.

Because it is original, there is no licence question at all for an MIT repo. Nothing here needs attribution or a font EULA check.

---

## 8. The clear space and minimum size rules

- **Clear space:** one disc radius on all four sides of the bounding box. Expressed as a ratio so it scales: if the disc is 40px across, keep 20px clear.
- **Minimum sizes:** full lockup 120px wide. Symbol alone 24px wide. Menu bar glyph 18px, using the template drawing. Favicon 16px, using the template drawing. Print lockup 22mm wide.
- **Never** stretch it, recolour it outside the tokens, put `caution` amber or `alert` red into it, add a glow or gradient, place it on a photograph, or rebuild the wordmark in live text.

`BRAND-GUIDE.html` has this as a one-page reference with the misuse examples drawn.

---

## 9. Honest list of what is not finished

1. **The SF Symbol needs Apple's template.** Artwork for all three weights is drawn and ready; the container has to come from SF Symbols.app or it will be rejected on import. `sf-symbol/HOW-TO.md`, ten minutes. Send me the exported blank template and I will inject the paths.
2. **The `s` in the wordmark is the one letter I would still refine.** Its spine is a straight slab joining two bowl arcs, which is honest geometry but slightly stiff at display size. It is correct at every size you will actually use; a round two would soften the two junctions and open the counters a little.
3. **Prior-art check not done.** A disc with an interrupted rule is a small idea. Before this goes on a landing page or an App Store listing, run a proper similarity search.
4. **watchOS and macOS icons are ready, not wired.** There is no Watch app and no Mac app target yet. They are here so that the day you add one, nothing blocks you.
5. **Tinted mode is drawn but untested on a device.** Greyscale hierarchy looks right in the render; put it on your phone in tinted mode and check the stubs still read.

---

## 10. Where every file lands, in one table

| Asset | Destination |
|---|---|
| `pwa/*` | `web/public/` |
| `pwa/head-snippet.html` | into `web/index.html` head |
| `pwa/manifest.webmanifest` | `web/public/manifest.webmanifest` |
| `xcassets/HeliosBridge-iOS/Assets.xcassets` | `ios-bridge/Assets.xcassets` |
| `menubar/*` | the status-item app bundle, when it exists |
| `app-icons/macos/Helios.icns` | any Mac app bundle |
| `social/*` | `docs/` plus GitHub repo settings |
| `masters/*` | `design/brand/` (suggested new folder) |
| `tokens/*` | merge into `web/src/index.css` and `web/tailwind.config.js` |
| `print/letterhead-mono.svg` | the clinician report template |
| `BRAND-GUIDE.html` | `design/brand/` |
