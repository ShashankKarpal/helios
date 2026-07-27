# Custom SF Symbol: helios.meridian

## What is here

Three weight drawings on a 100 x 100 field, sized so the app can interpolate between them:

| File | Disc radius | Rule thickness |
|---|---|---|
| `helios.meridian.ultralight.svg` | 17 | 3 |
| `helios.meridian.regular.svg` | 21 | 6 |
| `helios.meridian.black.svg` | 27 | 12 |

Only the disc radius and the rule thickness change across weights. The stub length and
the gap stay fixed, which is what keeps the mark recognisable as it gets heavier.

## Why this is not a finished .svg symbol file

A custom symbol has to be exported from Apple's own template, which carries the guides,
the margin metadata, and the exact layer names SF Symbols expects. A hand-authored file
that only looks right will be rejected on import, and a rejected file wastes more of your
time than this note does.

## Steps, about ten minutes

1. Open SF Symbols.app (free from Apple). Find any existing symbol, for example
   `circle.and.line.horizontal`.
2. File > Export Template, choose Static, and save it as `helios.meridian.svg`.
3. Open that file in your editor. It contains a `Symbols` group with layers named
   `Ultralight-S`, `Regular-S`, `Black-S` and so on.
4. Paste the path data from each weight file above into the matching layer, keeping the
   template's own transform on that layer. Scale to the template's cap-height guides.
5. Save, then drag the file back onto SF Symbols.app to validate. It will tell you plainly
   if a layer is missing.
6. Drop the validated file into `Assets.xcassets`. It becomes available as
   `Image(systemName:)` style usage via `Image("helios.meridian")`.

## What this one file powers

The Control Center control, the Lock Screen widget glyph, Siri, and App Shortcuts. All four
read from the symbol, so this is the highest-leverage asset in the set after the app icon.

If you send me the exported blank template, I will inject the paths and hand it back ready
to validate.
