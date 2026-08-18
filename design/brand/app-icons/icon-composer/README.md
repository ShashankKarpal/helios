# Icon Composer layers (Xcode 26, macOS 26)

Ink and Bone v1.0.0. Geometry comes from `design/marks/generate_marks.py`
(the helios hub mark); these layers are exported from it, not hand-drawn.

Icon Composer builds the light, dark, clear, and tinted appearances from stacked layers.
Import these in order, bottom to top:

| Layer | File | Notes |
|---|---|---|
| 1 background | `layer-1-background-dark.svg` | solid `#0B0C0D`, fills the canvas |
| 2 hub | `layer-2-hub.svg` | hub ring and spokes in ink `#F3F1EB` |
| 3 line | `layer-3-line.svg` | the brass line `#BFB287`, top layer |

For the light appearance swap in `layer-1-background-light.svg`, `layer-2-hub-light.svg`,
and `layer-3-line-light.svg` (`#F5F5F3`, `#1A1917`, `#4D4323`).

`layer-mono.svg` is the single flat white silhouette. Use it for the clear and tinted
appearances, where Icon Composer applies its own material and tint.

Two rules that matter here:
- Do not add a shadow, gloss, or specular layer. macOS 26 applies its own material to
  layered icons and a hand-painted highlight fights it.
- Keep the brass line on its own layer. It is the only element that should ever pick up
  the system's specular treatment.
