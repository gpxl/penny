"""Generate an 8-bit pixel art US penny icon for Penny app dialogs.

Draws a 32x32 pixel art penny at native resolution, then scales to 512x512
using nearest-neighbor interpolation to preserve the chunky pixel aesthetic.

Output: penny/resources/icon.png (512x512 RGBA PNG)
"""

from PIL import Image, ImageDraw

# Palette — copper penny tones
BG = (0, 0, 0, 0)          # transparent
RIM_DARK = (120, 60, 20)   # dark rim
RIM = (150, 85, 40)        # rim
COIN = (195, 120, 60)      # main copper face
COIN_LT = (215, 150, 85)   # light copper highlight
COIN_DK = (160, 90, 40)    # shadow copper
PROFILE = (120, 65, 25)    # Lincoln profile (dark)
PROFILE_DK = (100, 50, 18) # deep shadow on profile
TEXT = (140, 75, 30)        # subtle text/detail

SIZE = 32

img = Image.new("RGBA", (SIZE, SIZE), BG)
draw = ImageDraw.Draw(img)

# -- Step 1: Draw the circular coin --

# Outer rim (dark)
draw.ellipse([2, 2, 29, 29], fill=RIM_DARK)
# Inner rim
draw.ellipse([3, 3, 28, 28], fill=RIM)
# Coin face
draw.ellipse([4, 4, 27, 27], fill=COIN)

# -- Step 2: Subtle light/shadow on coin face --
# Light highlight (upper-left)
for y in range(5, 14):
    for x in range(5, 14):
        px = img.getpixel((x, y))
        if px[3] > 0 and px[:3] == COIN:
            # Slight highlight
            dist = ((x - 10) ** 2 + (y - 8) ** 2) ** 0.5
            if dist < 5:
                img.putpixel((x, y), COIN_LT + (255,))

# Shadow (lower-right)
for y in range(20, 27):
    for x in range(20, 27):
        px = img.getpixel((x, y))
        if px[3] > 0 and px[:3] == COIN:
            dist = ((x - 22) ** 2 + (y - 23) ** 2) ** 0.5
            if dist < 5:
                img.putpixel((x, y), COIN_DK + (255,))

# -- Step 3: Lincoln profile silhouette (facing right) --
# This is a simplified side profile — head, nose, chin, shoulders
# Positioned roughly center-left of the coin

profile_pixels = [
    # Head top / hair
    (13, 7), (14, 7), (15, 7),
    (12, 8), (13, 8), (14, 8), (15, 8), (16, 8),
    # Forehead
    (12, 9), (13, 9), (14, 9), (15, 9), (16, 9),
    (12, 10), (13, 10), (14, 10), (15, 10), (16, 10),
    # Brow / eyes area
    (12, 11), (13, 11), (14, 11), (15, 11), (16, 11), (17, 11),
    # Nose
    (12, 12), (13, 12), (14, 12), (15, 12), (16, 12), (17, 12),
    (13, 13), (14, 13), (15, 13), (16, 13), (17, 13), (18, 13),
    # Mouth / chin
    (13, 14), (14, 14), (15, 14), (16, 14), (17, 14),
    (13, 15), (14, 15), (15, 15), (16, 15), (17, 15),
    (14, 16), (15, 16), (16, 16), (17, 16),
    # Chin / jaw
    (14, 17), (15, 17), (16, 17),
    (15, 18), (16, 18),
    # Neck
    (14, 19), (15, 19),
    (13, 20), (14, 20), (15, 20),
    # Shoulders
    (11, 21), (12, 21), (13, 21), (14, 21), (15, 21), (16, 21),
    (10, 22), (11, 22), (12, 22), (13, 22), (14, 22), (15, 22), (16, 22), (17, 22),
    (9, 23), (10, 23), (11, 23), (12, 23), (13, 23), (14, 23), (15, 23), (16, 23), (17, 23), (18, 23),
    (8, 24), (9, 24), (10, 24), (11, 24), (12, 24), (13, 24), (14, 24), (15, 24), (16, 24), (17, 24), (18, 24), (19, 24),
]

for x, y in profile_pixels:
    if img.getpixel((x, y))[3] > 0:  # only draw on coin face
        img.putpixel((x, y), PROFILE + (255,))

# Deeper shadow on back of head
deep_shadow = [
    (12, 8), (12, 9), (12, 10), (12, 11), (12, 12),
    (13, 8), (13, 9),
    (13, 20), (14, 19),
]
for x, y in deep_shadow:
    if img.getpixel((x, y))[3] > 0:
        img.putpixel((x, y), PROFILE_DK + (255,))

# -- Step 4: Subtle rim text dots (representing "LIBERTY" and "IN GOD WE TRUST") --
# Just tiny dots around the rim to suggest text at this scale
text_dots = [
    # Top arc: "IN GOD WE TRUST"
    (10, 5), (12, 5), (14, 5), (16, 5), (18, 5), (20, 5),
    # Left arc: "L I B E R T Y"
    (6, 9), (6, 11), (6, 13), (6, 15), (6, 17), (6, 19),
    # Year at bottom
    (12, 25), (14, 25), (16, 25), (18, 25),
]
for x, y in text_dots:
    px = img.getpixel((x, y))
    if px[3] > 0:
        img.putpixel((x, y), TEXT + (255,))

# -- Step 5: Scale up to 512x512 with nearest-neighbor --
icon = img.resize((512, 512), Image.NEAREST)

# Save
out_path = "penny/resources/icon.png"
icon.save(out_path, "PNG")
print(f"Saved {out_path} ({icon.size[0]}x{icon.size[1]})")

# Also save a 128x128 version for smaller uses
icon_128 = img.resize((128, 128), Image.NEAREST)
icon_128.save("penny/resources/icon_128.png", "PNG")
print("Saved penny/resources/icon_128.png (128x128)")
