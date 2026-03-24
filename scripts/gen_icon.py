"""Generate a Mario-style pixel art penny icon for Penny app dialogs.

16x16 base grid with black outlines and copper palette, inspired by
the Super Mario Bros coin. Scaled to 64x64 with nearest-neighbor.

Output: penny/resources/icon.png
"""

from PIL import Image

# Palette — Mario coin style with copper tones
T = (0, 0, 0, 0)           # transparent
K = (0, 0, 0, 255)         # black outline
H = (235, 190, 100, 255)   # highlight copper (light gold)
F = (210, 150, 60, 255)    # face copper (main)
S = (165, 105, 35, 255)    # shadow copper (dark)
D = (100, 55, 15, 255)     # dark (P letter + inner detail)

# 16x16 pixel grid — Mario-style coin with "P"
# T=transparent, K=black, H=highlight, F=face, S=shadow, D=dark
GRID = [
    [T, T, T, T, T, K, K, K, K, K, K, T, T, T, T, T],
    [T, T, T, K, K, H, H, H, H, F, F, K, K, T, T, T],
    [T, T, K, H, H, H, H, H, F, F, F, F, S, K, T, T],
    [T, K, H, H, H, D, D, D, D, F, F, F, S, S, K, T],
    [T, K, H, H, D, D, F, F, D, D, F, F, S, S, K, T],
    [K, H, H, H, D, D, F, F, F, F, F, F, S, S, S, K],
    [K, H, H, H, D, D, F, F, F, F, F, F, S, S, S, K],
    [K, H, H, H, D, D, D, D, D, F, F, F, S, S, S, K],
    [K, H, H, H, D, D, F, F, F, F, F, F, S, S, S, K],
    [K, H, H, H, D, D, F, F, F, F, F, F, S, S, S, K],
    [K, H, H, H, D, D, F, F, F, F, F, F, S, S, S, K],
    [T, K, H, H, D, D, F, F, F, F, F, F, S, S, K, T],
    [T, K, H, H, H, F, F, F, F, F, F, S, S, S, K, T],
    [T, T, K, H, H, F, F, F, F, F, S, S, S, K, T, T],
    [T, T, T, K, K, F, F, F, S, S, S, K, K, T, T, T],
    [T, T, T, T, T, K, K, K, K, K, K, T, T, T, T, T],
]

img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
for y, row in enumerate(GRID):
    for x, c in enumerate(row):
        img.putpixel((x, y), c)

# Scale to 64x64
icon = img.resize((64, 64), Image.NEAREST)
icon.save("penny/resources/icon.png", "PNG")
print("Saved icon.png (64x64)")

icon_128 = img.resize((128, 128), Image.NEAREST)
icon_128.save("penny/resources/icon_128.png", "PNG")
print("Saved icon_128.png (128x128)")
