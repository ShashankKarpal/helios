# Menu bar template

`HeliosTemplate.png`, `@2x`, `@3x` are pure black with alpha. macOS inverts a template
image automatically, so it renders dark on a light menu bar and light on a dark one.
Never tint it yourself.

The drawing is not a scaled-down app icon. It is redrawn at 18pt with its own minimums:
disc radius 3.8px, stubs 3.6 x 2.4px, 1px clear gap. That is why it survives.

## Swift, AppKit status item

```swift
let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
if let image = NSImage(named: "HeliosTemplate") {
    image.isTemplate = true          // required, or the OS will not invert it
    image.size = NSSize(width: 18, height: 18)
    item.button?.image = image
}
```

The filename suffix `Template` makes `isTemplate` default to true when loaded from an
asset catalog. Setting it explicitly costs nothing and removes the ambiguity.

`HeliosTemplate.pdf` is the vector original. Prefer it in an asset catalog with
"Preserve Vector Data" enabled if you ever need a size other than 18, 36, or 54.
