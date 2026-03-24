"""Generate a Mario Bros coin icon for Penny app dialogs.

Faithful recreation of the Super Mario Bros coin:
- 16x16 base grid
- Bright yellow oval with black outline
- Dark gold vertical stripe detail in center
- 3D shading: shadow on right side

Scaled to 64x64 with nearest-neighbor.
Output: penny/resources/icon.png
"""

from PIL import Image

# Palette — Mario coin
T = (0, 0, 0, 0)           # transparent
K = (0, 0, 0, 255)         # black outline / shadow
Y = (255, 255, 0, 255)     # bright yellow
G = (228, 184, 0, 255)     # dark gold (stripe + right shadow)

S = 16
img = Image.new("RGBA", (S, S), T)

# Faithfully traced from the Mario Bros coin reference.
# Oval shape, black outline, bright yellow face,
# dark gold vertical stripe in center + shadow on right.
GRID = [
    [T, T, T, T, T, T, K, K, K, K, T, T, T, T, T, T],
    [T, T, T, T, K, K, Y, Y, Y, Y, K, K, T, T, T, T],
    [T, T, T, K, Y, Y, Y, G, G, Y, Y, Y, K, T, T, T],
    [T, T, K, Y, Y, Y, Y, G, G, Y, Y, Y, Y, K, T, T],
    [T, T, K, Y, Y, Y, Y, G, G, Y, Y, Y, Y, K, T, T],
    [T, K, Y, Y, Y, Y, Y, G, G, Y, Y, Y, Y, G, K, T],
    [T, K, Y, Y, Y, Y, Y, G, G, Y, Y, Y, Y, G, K, T],
    [T, K, Y, Y, Y, Y, Y, G, G, Y, Y, Y, Y, G, K, T],
    [T, K, Y, Y, Y, Y, Y, G, G, Y, Y, Y, Y, G, K, T],
    [T, K, Y, Y, Y, Y, Y, G, G, Y, Y, Y, Y, G, K, T],
    [T, K, Y, Y, Y, Y, Y, G, G, Y, Y, Y, Y, G, K, T],
    [T, T, K, Y, Y, Y, Y, G, G, Y, Y, Y, Y, K, T, T],
    [T, T, K, Y, Y, Y, Y, G, G, Y, Y, Y, Y, K, T, T],
    [T, T, T, K, Y, Y, Y, G, G, Y, Y, Y, K, T, T, T],
    [T, T, T, T, K, K, Y, Y, Y, Y, K, K, T, T, T, T],
    [T, T, T, T, T, T, K, K, K, K, T, T, T, T, T, T],
]

for y, row in enumerate(GRID):
    for x, c in enumerate(row):
        img.putpixel((x, y), c)

# Visual check
NAMES = {T: '.', K: 'K', Y: 'Y', G: 'G'}
print("16x16 grid:")
for y in range(16):
    print(" ".join(NAMES.get(GRID[y][x], '?') for x in range(16)))

# Scale coin to 32x32 (2x), then center on a 64x64 transparent canvas.
# NSAlert renders the icon at a fixed 64x64pt — padding the canvas
# makes the coin appear at half that size within the icon slot.
coin = img.resize((32, 32), Image.NEAREST)
canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
canvas.paste(coin, (16, 16))  # centered with 16px padding on each side

canvas.save("penny/resources/icon.png", "PNG")
print("\nSaved icon.png (64x64 canvas, 32x32 coin)")

# 128x128 version: 64x64 coin centered on 128x128 canvas
coin_lg = img.resize((64, 64), Image.NEAREST)
canvas_lg = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
canvas_lg.paste(coin_lg, (32, 32))
canvas_lg.save("penny/resources/icon_128.png", "PNG")
print("Saved icon_128.png (128x128 canvas, 64x64 coin)")
