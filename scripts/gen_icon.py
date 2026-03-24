"""Generate an 8-bit pixel art US penny icon for Penny app dialogs.

16x16 base grid. Clean copper coin with bold "1".
Scaled to 256x256 with nearest-neighbor.

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

# Bold "1" — centered
one = [
    (7, 3), (8, 3),          # top serif
    (8, 4),
    (8, 5),
    (8, 6),
    (8, 7),
    (8, 8),
    (8, 9),
    (7, 10), (8, 10), (9, 10),  # base serif
]
for x, y in one:
    if img.getpixel((x, y))[3] > 0:
        img.putpixel((x, y), DARK + (255,))

# Scale to 256x256
icon = img.resize((256, 256), Image.NEAREST)
icon.save("penny/resources/icon.png", "PNG")
print("Saved icon.png (256x256)")

icon_128 = img.resize((128, 128), Image.NEAREST)
icon_128.save("penny/resources/icon_128.png", "PNG")
print("Saved icon_128.png (128x128)")
