"""Generate a Mario-style pixel art penny icon for Penny app dialogs.

16x16 base grid matching the Super Mario Bros coin aesthetic:
- Bright yellow gold
- Black outline
- Oval/3D shape with highlight on left, shadow on right
- Bold "P" in dark gold

Scaled to 64x64 with nearest-neighbor.
Output: penny/resources/icon.png
"""

from PIL import Image

# Palette — Mario coin colors
T = (0, 0, 0, 0)           # transparent
K = (0, 0, 0, 255)         # black outline
Y = (255, 255, 0, 255)     # bright yellow (main)
G = (228, 184, 0, 255)     # gold/darker yellow (shadow/detail)
W = (255, 255, 180, 255)   # white-yellow (highlight)

S = 16
img = Image.new("RGBA", (S, S), T)

# Step 1: Draw the coin shape (oval, taller than wide)
# Black outline first, then fill with yellow, then add shading
coin = [
    #        columns where black outline exists
    # row: (outline_pixels, fill_color_map)
]

# Build coin pixel-by-pixel using the Mario coin shape
# The coin is an oval roughly 10 wide x 14 tall, centered
COIN_SHAPE = [
    "......KKKK......",  # 0
    "....KKyyyyKK....",  # 1
    "...KyyyyyyyyyyK...",  # 2  -- wait, this is 16 cols not 17
]

# Easier: just define the full grid explicitly.
# First: plain Mario coin (no letter), then stamp P on top.

# Plain coin base
rows = [
    "......KKKK......",
    "....KKYYYYKK....",
    "...KYYYYYYYYK...",
    "..KWYYYYYYYYY K.",  # highlight left
    "..KWYYYYYYYYYK..",
    ".KWYYYYYYYYYYY K",  # -- wait, I need exactly 16 chars per row
]

# Let me just use the grid approach directly with lists.
# Start with a blank coin, then paint the P.

# Row 0-15, exactly 16 pixels each
def make_coin():
    """Build base Mario-style coin without letter."""
    grid = [[T]*16 for _ in range(16)]

    # Define coin oval outline (black pixels)
    outline = {
        0: range(6, 10),
        1: [4, 5, 10, 11],
        2: [3, 12],
        3: [2, 13],
        4: [2, 13],
        5: [1, 14],
        6: [1, 14],
        7: [1, 14],
        8: [1, 14],
        9: [1, 14],
        10: [1, 14],
        11: [2, 13],
        12: [2, 13],
        13: [3, 12],
        14: [4, 5, 10, 11],
        15: range(6, 10),
    }
    for row, cols in outline.items():
        for c in cols:
            grid[row][c] = K

    # Fill interior with yellow
    # Interior = pixels between outline on each row
    interior_ranges = {
        1: (6, 10),
        2: (4, 12),
        3: (3, 13),
        4: (3, 13),
        5: (2, 14),
        6: (2, 14),
        7: (2, 14),
        8: (2, 14),
        9: (2, 14),
        10: (2, 14),
        11: (3, 13),
        12: (3, 13),
        13: (4, 12),
        14: (6, 10),
    }
    for row, (lo, hi) in interior_ranges.items():
        for c in range(lo, hi):
            if grid[row][c] == T:
                grid[row][c] = Y

    # Add highlight column (left edge of interior) = W
    for row in range(3, 11):
        lo = interior_ranges[row][0]
        grid[row][lo] = W

    # Add shadow column (right edge of interior) = G
    for row in range(3, 11):
        hi = interior_ranges[row][1] - 1
        grid[row][hi] = G

    return grid


def stamp_letter_P(grid):
    """Stamp a bold "P" onto the coin using dark gold (G) pixels.

    P shape (6 wide x 8 tall), positioned at x=5, y=3:

      X X X X .      row 3: top bar
      X . . . X      row 4: stem + bowl right
      X . . . X      row 5: stem + bowl right
      X X X X .      row 6: middle bar (closes bowl)
      X . . . .      row 7: stem only
      X . . . .      row 8: stem only
      X . . . .      row 9: stem only
      X . . . .      row 10: stem only
    """
    P = [
        # (x, y) positions for the P
        # Vertical stem: x=5, y=3..10
        (5, 3), (5, 4), (5, 5), (5, 6), (5, 7), (5, 8), (5, 9), (5, 10),
        # Top bar: x=6..8, y=3
        (6, 3), (7, 3), (8, 3),
        # Bowl right side: x=9, y=4..5
        (9, 4), (9, 5),
        # Middle bar (closes bowl): x=6..8, y=6
        (6, 6), (7, 6), (8, 6),
    ]
    for x, y in P:
        grid[y][x] = G

    return grid


grid = make_coin()
grid = stamp_letter_P(grid)

# Write to image
for y in range(S):
    for x in range(S):
        img.putpixel((x, y), grid[y][x])

# Visual verification
NAMES = {T: '.', K: 'K', Y: 'Y', G: 'G', W: 'W'}
print("16x16 grid:")
for y in range(16):
    print(" ".join(NAMES.get(grid[y][x], '?') for x in range(16)))

# Verify P shape: below row 6, only column 5 should be G (stem only)
print("\nP verification:")
for row in range(7, 11):
    dark_cols = [x for x in range(16) if grid[row][x] == G]
    # Should only have col 5 (stem) and possibly col 13 (right shadow)
    letter_cols = [x for x in dark_cols if x < 12]
    assert letter_cols == [5], f"Row {row}: expected only stem at col 5, got {letter_cols}"
    print(f"  Row {row}: stem only at col {letter_cols} ✓")

# Verify bowl closes: row 6 has middle bar
mid_bar = [x for x in range(5, 10) if grid[6][x] == G]
assert len(mid_bar) >= 4, f"Row 6 middle bar too short: {mid_bar}"
print(f"  Row 6: bowl closes with bar at {mid_bar} ✓")

# Verify NO horizontal bar below row 6 (would make it E/F)
for row in range(7, 13):
    bar_count = sum(1 for x in range(5, 10) if grid[row][x] == G)
    assert bar_count <= 1, f"Row {row} has {bar_count} G pixels in letter zone — looks like E/F!"
print("  No extra bars below bowl ✓ — this is a P, not E/F")

# Save
icon = img.resize((64, 64), Image.NEAREST)
icon.save("penny/resources/icon.png", "PNG")
print("\nSaved icon.png (64x64)")

icon_128 = img.resize((128, 128), Image.NEAREST)
icon_128.save("penny/resources/icon_128.png", "PNG")
print("Saved icon_128.png (128x128)")
