"""Generate an 8-bit pixel art US penny icon for Penny app dialogs.

16x16 base grid. Clean copper coin with centered "P".
Scaled to 128x128 with nearest-neighbor (50% smaller than before).

Output: penny/resources/icon.png
"""

from PIL import Image, ImageDraw

BG = (0, 0, 0, 0)
RIM = (145, 80, 35)
FACE = (200, 130, 70)
DARK = (100, 50, 15)

S = 16
img = Image.new("RGBA", (S, S), BG)
draw = ImageDraw.Draw(img)

# Coin: rim + face
draw.ellipse([1, 1, 14, 14], fill=RIM)
draw.ellipse([2, 2, 13, 13], fill=FACE)

# Centered "P" — 5 wide x 7 tall, starting at (5,4)
p = [
    # Vertical stroke
    (6, 4), (6, 5), (6, 6), (6, 7), (6, 8), (6, 9), (6, 10),
    # Top bar
    (7, 4), (8, 4), (9, 4),
    # Bump right side
    (10, 5), (10, 6),
    # Middle bar (closes the P bowl)
    (7, 7), (8, 7), (9, 7),
]
for x, y in p:
    if img.getpixel((x, y))[3] > 0:
        img.putpixel((x, y), DARK + (255,))

# Scale to 128x128
icon = img.resize((128, 128), Image.NEAREST)
icon.save("penny/resources/icon.png", "PNG")
print("Saved icon.png (128x128)")

icon_128 = img.resize((128, 128), Image.NEAREST)
icon_128.save("penny/resources/icon_128.png", "PNG")
print("Saved icon_128.png (128x128)")
