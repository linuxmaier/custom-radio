# PWA Icon Generation Prompts

Generate two images from Gemini, then derive the rest mechanically by resizing and cropping.

Brand colours for reference:
- Background: `#0f1117` (near-black navy)
- Accent: `#6c63ff` (soft purple)

---

## Prompt 1 — Master icon (generate at 1024×1024)

A square app icon for a family internet radio station. A bold, stylised radio tower
centred on a solid deep navy background (#0f1117), emitting three curved signal arcs
in soft purple (#6c63ff) that radiate outward to the right. The tower itself is white.
The entire design — tower and arcs — sits within the centre 60% of the canvas, leaving
at least 20% clear margin on every side. Clean and modern — flat design, no gradients,
no text, no drop shadows. Square canvas, 1:1 aspect ratio.

Derive the following from this image by resizing:
- icon-512.png → resize to 512×512
- icon-512-maskable.png → resize to 512×512 (the safe-zone padding is already built in)
- icon-192.png → resize to 192×192
- apple-touch-icon.png → resize to 180×180

---

## Prompt 2 — Signal symbol (generate at 1024×1024, transparent background)

A minimal signal icon: a small filled white circle with two bold concentric curved arcs
radiating from it to the right, like a Wi-Fi symbol rotated 45 degrees. White on a
fully transparent background. The symbol is perfectly centred, occupies about 70% of
the canvas, and uses strokes thick enough to remain legible at 24 pixels wide.
No colour, no gradients, no background fill, no text. Square canvas, 1:1 aspect ratio.

Derive the following from this image:
- badge-96.png → resize to 96×96, keep transparent background
- favicon.png → place on a solid #6c63ff background, resize to 32×32
