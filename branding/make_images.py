"""Draw the profile pictures for each clip channel.

Run:  python branding/make_images.py
Writes one 1024x1024 PNG per account set into branding/.
Everything is drawn from text and simple shapes: no game logos or artwork.
Key content stays inside the centre circle, so it still reads when a site
crops the picture to a small circle.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent
SIZE = 1024
FONTS = Path("C:/Windows/Fonts")
BIG = str(FONTS / "impact.ttf")
SMALL = str(FONTS / "segoeuib.ttf")

CHANNELS = [
    # name, big text, subtitle, bg inner, bg outer, text colour, accent colour, accent kind
    ("irl-en", "IRL", "STREAM CLIPS", "#ff5a4e", "#7a1029", "#ffffff", "#ffe14d", "live"),
    ("irl-es", "IRL", "CLIPS ESPAÑOL", "#ffb21e", "#b3261e", "#ffffff", "#2b0f0a", "live"),
    ("irl-pt", "IRL", "CLIPES BRASIL", "#2fbf5a", "#0b4d2a", "#ffe14d", "#ffffff", "live"),
    ("cs2", "CS2", "CLIPS DAILY", "#2a3f5f", "#0b1220", "#ffffff", "#ff9f1c", "crosshair"),
    ("fortnite", "FORTNITE", "CLIPS DAILY", "#4fb3ff", "#3a1c9c", "#ffffff", "#ffe14d", "bolt"),
    ("minecraft", "MC", "CLIPS DAILY", "#5fb83f", "#2b4a1a", "#ffffff", "#2e7d32", "pixels"),
    ("gta6-1", "GTA 6", "CLIPS", "#1f8a8a", "#08202a", "#ffffff", "#c6ff3d", "badge:1"),
    ("gta6-2", "GTA 6", "CLIPS", "#2c6bd6", "#0a1638", "#ffffff", "#c6ff3d", "badge:2"),
    ("gta6-3", "GTA 6", "CLIPS", "#7a3fd1", "#1a0b38", "#ffffff", "#c6ff3d", "badge:3"),
    ("league", "LEAGUE", "CLIPS DAILY", "#1aa6a0", "#062a33", "#ffffff", "#f2f2f2", "diamond"),
    # Added 2026-11-22
    ("gta6-4", "GTA 6", "CLIPS", "#d6452c", "#3a0d08", "#ffffff", "#c6ff3d", "badge:4"),
    ("gta6-5", "GTA 6", "CLIPS", "#1f9e5a", "#06281a", "#ffffff", "#c6ff3d", "badge:5"),
    ("gta6-6", "GTA 6", "CLIPS", "#d18a1f", "#3a2006", "#ffffff", "#c6ff3d", "badge:6"),
    # Streamer accounts: the name as text only, never the streamer's face, logo or emotes.
    ("darkviperau", "DARKVIPERAU", "CLIPS", "#c0392b", "#1a0505", "#ffffff", "#ffe14d", "live"),
    ("jynxzi", "JYNXZI", "CLIPS", "#3a7bd5", "#0a1a3a", "#ffffff", "#ffe14d", "live"),
    ("kaicenat", "KAI CENAT", "CLIPS", "#ff3d7f", "#3a0a22", "#ffffff", "#ffe14d", "live"),
    ("loltyler1", "TYLER1", "CLIPS", "#f2a900", "#3a2600", "#ffffff", "#1a1a1a", "live"),
    ("ludwig", "LUDWIG", "CLIPS", "#8e44ad", "#1e0a2e", "#ffffff", "#ffe14d", "live"),
    ("lacy", "LACY", "CLIPS", "#16a085", "#04241d", "#ffffff", "#ffe14d", "live"),
    ("couriway", "COURIWAY", "CLIPS", "#5fb83f", "#1f3a12", "#ffffff", "#2b2b2b", "live"),
]

# 5x5 pixel letters for the Minecraft-style picture (drawn as squares).
PIXEL_FONT = {
    "M": ["10001", "11011", "10101", "10001", "10001"],
    "C": ["01111", "10000", "10000", "10000", "01111"],
}


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def radial(inner, outer):
    """Radial gradient, inner colour in the middle fading to outer at the corners."""
    small = 256
    a, b = hex_rgb(inner), hex_rgb(outer)
    img = Image.new("RGB", (small, small))
    px = img.load()
    c = (small - 1) / 2
    maxd = (2 * c * c) ** 0.5
    for y in range(small):
        for x in range(small):
            t = min(1.0, ((x - c) ** 2 + (y - c) ** 2) ** 0.5 / maxd * 1.25)
            px[x, y] = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return img.resize((SIZE, SIZE), Image.BICUBIC)


def fit_font(path, text, max_w, max_size, draw):
    size = max_size
    while size > 20:
        font = ImageFont.truetype(path, size)
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        if r - l <= max_w:
            return font
        size -= 4
    return ImageFont.truetype(path, size)


def centered_text(draw, text, font, cy, fill, shadow=True, stroke=0, stroke_fill=None):
    l, t, r, b = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    x = (SIZE - (r - l)) / 2 - l
    y = cy - (b - t) / 2 - t
    if shadow:
        draw.text((x + 8, y + 10), text, font=font, fill=(0, 0, 0, 110),
                  stroke_width=stroke, stroke_fill=(0, 0, 0, 110))
    draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke, stroke_fill=stroke_fill)
    return y + (b - t) + t


def accent_layer(kind, color, text_color):
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    col = hex_rgb(color)
    c = SIZE // 2
    if kind == "live":
        # "on air" pill above the main text
        w, h, top = 250, 84, 250
        d.rounded_rectangle((c - w // 2, top, c + w // 2, top + h), radius=h // 2, fill=col + (255,))
        d.ellipse((c - w // 2 + 26, top + 26, c - w // 2 + 58, top + 58), fill=(230, 30, 40, 255))
        f = ImageFont.truetype(SMALL, 50)
        dot_col = (20, 20, 20, 255) if sum(col) > 400 else (255, 255, 255, 255)
        d.text((c - w // 2 + 78, top + h // 2), "LIVE", font=f, fill=dot_col, anchor="lm")
    elif kind == "crosshair":
        r, gap, wdt = 330, 120, 16
        a = col + (200,)
        d.ellipse((c - r, c - r, c + r, c + r), outline=a, width=wdt)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            x1, y1 = c + dx * gap, c + dy * gap
            x2, y2 = c + dx * (r + 60), c + dy * (r + 60)
            d.line((x1, y1, x2, y2), fill=a, width=wdt)
    elif kind == "bolt":
        pts = [(560, 120), (330, 560), (500, 560), (430, 900), (720, 420), (545, 420), (650, 120)]
        d.polygon(pts, fill=col + (235,))
    elif kind == "diamond":
        r = 360
        d.polygon([(c, c - r), (c + r, c), (c, c + r), (c - r, c)], outline=col + (120,), width=14)
        r2 = 300
        d.polygon([(c, c - r2), (c + r2, c), (c, c + r2), (c - r2, c)], outline=col + (70,), width=6)
    elif kind.startswith("badge:"):
        n = kind.split(":")[1]
        r = 96
        bx, by = c, 790
        d.ellipse((bx - r, by - r, bx + r, by + r), fill=col + (255,))
        f = ImageFont.truetype(BIG, 140)
        d.text((bx, by + 4), n, font=f, fill=(15, 15, 15, 255), anchor="mm")
    return layer


def pixel_letters(img, text, color, cy):
    d = ImageDraw.Draw(img)
    cell, gap = 62, 40
    w = len(text) * 5 * cell + (len(text) - 1) * gap
    x0 = (SIZE - w) // 2
    y0 = cy - 5 * cell // 2
    shade = tuple(max(0, v - 60) for v in hex_rgb(color))
    for i, ch in enumerate(text):
        rows = PIXEL_FONT[ch]
        lx = x0 + i * (5 * cell + gap)
        for ry, row in enumerate(rows):
            for rx, bit in enumerate(row):
                if bit == "1":
                    x, y = lx + rx * cell, y0 + ry * cell
                    d.rectangle((x + 10, y + 12, x + cell + 10, y + cell + 12), fill=(0, 0, 0, 110))
            for rx, bit in enumerate(row):
                if bit == "1":
                    x, y = lx + rx * cell, y0 + ry * cell
                    d.rectangle((x, y, x + cell - 1, y + cell - 1), fill=hex_rgb(color))


def make(name, big, sub, inner, outer, text_col, accent, kind):
    img = radial(inner, outer).convert("RGBA")
    if kind == "pixels":
        # faint grass/dirt block grid in the background
        grid = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        g = ImageDraw.Draw(grid)
        for i in range(0, SIZE, 64):
            g.line((i, 0, i, SIZE), fill=(0, 0, 0, 28), width=3)
            g.line((0, i, SIZE, i), fill=(0, 0, 0, 28), width=3)
        img = Image.alpha_composite(img, grid)
    acc = accent_layer(kind, accent, text_col)
    if kind in ("bolt",):
        acc = acc.filter(ImageFilter.GaussianBlur(1))
    img = Image.alpha_composite(img, acc)

    txt = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(txt)
    if kind == "pixels":
        pixel_letters(txt, big, text_col, 430)
        bottom = 430 + 5 * 62 // 2
    else:
        cy = {"live": 500, "badge": 430}.get(kind.split(":")[0], 470)
        max_w = 640 if len(big) > 4 else 600
        font = fit_font(BIG, big, max_w, 340, d)
        bottom = centered_text(d, big, font, cy, text_col, stroke=6, stroke_fill=(0, 0, 0, 255))
    sfont = fit_font(SMALL, sub, 560, 84, d)
    sy = bottom + 70
    if kind.startswith("badge"):
        sy = bottom + 55
    centered_text(d, sub, sfont, sy, text_col, shadow=True)
    img = Image.alpha_composite(img, txt)
    path = OUT / f"{name}.png"
    img.convert("RGB").save(path, optimize=True)
    return path


def preview(paths):
    """Contact sheet with each picture cropped to a circle at full and tiny size."""
    cols, tile = 5, 300
    rows = (len(paths) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile, rows * (tile + 70)), "#ffffff")
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, SIZE, SIZE), fill=255)
    f = ImageFont.truetype(SMALL, 22)
    d = ImageDraw.Draw(sheet)
    for i, p in enumerate(paths):
        im = Image.open(p).convert("RGBA")
        im.putalpha(mask)
        x, y = (i % cols) * tile, (i // cols) * (tile + 70)
        big = im.resize((220, 220), Image.LANCZOS)
        small = im.resize((48, 48), Image.LANCZOS)
        sheet.paste(big, (x + 10, y + 10), big)
        sheet.paste(small, (x + 240, y + 10), small)
        d.text((x + 10, y + 240), p.stem, font=f, fill="#222")
    out = OUT / "preview_circles.png"
    sheet.save(out)
    return out


if __name__ == "__main__":
    made = [make(*c) for c in CHANNELS]
    print("\n".join(str(p) for p in made))
    print(preview(made))
