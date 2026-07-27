# Icon Composer layers (Xcode 26, macOS 26)

Icon Composer builds the light, dark, clear, and tinted appearances from stacked layers.
Import these in order, bottom to top:

| Layer | File | Notes |
|---|---|---|
| 1 background | `layer-1-background-dark.svg` | solid `#0B0D0C`, fills the canvas |
| 2 rule | `layer-2-rule.svg` | the two stubs in `#9AA49E` |
| 3 disc | `layer-3-disc.svg` | the disc in `#7EE0B1`, top layer |

For the light appearance swap in `layer-1-background-light.svg`, `layer-2-rule-light.svg`,
and `layer-3-disc-light.svg`.

`layer-mono.svg` is the single flat white silhouette. Use it for the clear and tinted
appearances, where Icon Composer applies its own material and tint.

Two rules that matter here:
- Do not add a shadow, gloss, or specular layer. macOS 26 applies its own material to
  layered icons and a hand-painted highlight fights it.
- Keep the disc on its own layer. It is the only element that should ever pick up the
  system's specular treatment.
