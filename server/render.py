"""Deterministic, one-bit TRMNL X fixtures. No AI or browser rendering."""
from io import BytesIO
from pathlib import Path
import os
from PIL import Image, ImageDraw, ImageFont

SIZE = (1872, 1404)
RECIPES = {
    "A": ("Lemon chickpea salad", ["2 cans chickpeas, drained", "1 cucumber, diced", "1 cup cherry tomatoes, halved", "3 tbsp olive oil", "2 tbsp lemon juice", "1/2 tsp salt", "1/4 cup chopped parsley"],
          ["Rinse and drain the chickpeas well.", "Combine chickpeas, cucumber and tomatoes in a large bowl.", "Whisk olive oil, lemon juice and salt. Pour over the salad.", "Fold in parsley. Rest for 10 minutes, then taste and serve."]),
    "B": ("Tomato butter pasta", ["300 g dried pasta", "400 g canned tomatoes", "40 g butter", "2 garlic cloves, sliced", "1/2 tsp salt", "Fresh basil and grated Parmesan"],
          ["Bring a large pot of salted water to a boil.", "Melt butter in a pan. Add garlic and cook gently for 1 minute.", "Add tomatoes and salt. Simmer for 15 minutes, stirring occasionally.", "Cook pasta according to its packet. Reserve 1 cup cooking water.", "Toss pasta with sauce. Add cooking water as needed. Finish with basil and Parmesan."]),
    "LONG": ("Roasted vegetables & herbed couscous", ["2 carrots, cut into batons", "1 red onion, cut into wedges", "1 courgette, sliced", "1 red pepper, chopped", "2 tbsp olive oil", "1 tsp ground cumin", "1/2 tsp smoked paprika", "1/2 tsp salt", "200 g couscous", "250 ml hot vegetable stock", "1 lemon, zest and juice", "3 tbsp chopped parsley", "2 tbsp toasted almonds", "100 g plain yogurt"],
          ["Heat the oven to 220 C. Line a large baking tray.", "Combine carrots, onion, courgette and pepper with oil, cumin, paprika and salt.", "Spread vegetables in a single layer. Roast for 25-30 minutes, turning after 15 minutes.", "Put couscous in a heatproof bowl. Pour over hot stock and cover for 5 minutes.", "Fluff with a fork. Fold in lemon zest, half the juice and the parsley.", "Mix yogurt with the remaining lemon juice. Add a little water if needed.", "Divide couscous among bowls. Top with roasted vegetables and toasted almonds.", "Serve yogurt on the side. Refrigerate leftovers promptly and use within 2 days."])
}

def font(size):
    candidates = [os.environ.get("ERECIPE_FONT", ""), "/System/Library/Fonts/Supplemental/Arial.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for path in candidates:
        if path and Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)

def paragraph(draw, text, x, y, width, size=36, gap=12, bottom=1270):
    f = font(size)
    words, line = text.split(), ""
    lines = []
    for word in words:
        candidate = (line + " " + word).strip()
        if draw.textlength(candidate, font=f) > width:
            if not line:
                raise ValueError("Unbreakable text exceeds fixture width")
            lines.append(line)
            line = word
        else:
            line = candidate
    lines.append(line)
    for line in lines:
        if y + size + gap > bottom:
            raise ValueError("Fixture overflow: increase space or edit fixture")
        draw.text((x, y), line, font=f, fill=0)
        y += size + gap
    return y

def render(kind, version, diagnostic=None):
    im = Image.new("L", SIZE, 255)
    d = ImageDraw.Draw(im)
    dark = kind == "B"
    d.rectangle((0, 0, 1872, 250), fill=0 if dark else 255)
    d.text((65, 32), ("L" if kind == "LONG" else kind) if kind != "DIAG" else "X", font=font(160), fill=255 if dark else 0)
    d.text((400, 64), "eRecipe / TRMNL X", font=font(60), fill=255 if dark else 0)
    d.text((400, 157), "Transport & battery experiment", font=font(35), fill=255 if dark else 0)
    d.line((65, 265, 1807, 265), fill=0, width=4)
    if diagnostic:
        y = 300
        for label, value in diagnostic:
            y = paragraph(d, f"{label}: {value}", 70, y, 1720, size=35, gap=12)
            y += 12
    else:
        title, ingredients, steps = RECIPES[kind]
        d.text((70, 300), title, font=font(62), fill=0)
        d.text((70, 402), "INGREDIENTS", font=font(32), fill=0)
        d.text((800, 402), "METHOD", font=font(32), fill=0)
        y = 465
        for item in ingredients:
            y = paragraph(d, item, 70, y, 630, size=34 if kind == "LONG" else 38)
            y += 12
        y = 465
        for n, step in enumerate(steps, 1):
            y = paragraph(d, f"{n}. {step}", 800, y, 995, size=32 if kind == "LONG" else 38)
            y += 20
    d.line((65, 1300, 1807, 1300), fill=0, width=3)
    d.text((70, 1330), f"VERSION {version}   |   1872 x 1404 / 1-bit PNG", font=font(29), fill=0)
    output = BytesIO()
    im.point(lambda p: 255 if p >= 160 else 0).convert("1").save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_tap_trial(page, version):
    """Two deliberately separate screens; no general recipe pagination implied."""
    if page not in (1, 2):
        raise ValueError("Trial page must be 1 or 2")
    im = Image.new("L", SIZE, 255)
    d = ImageDraw.Draw(im)
    d.text((70, 45), f"PAGE {page} / 2", font=font(110), fill=0)
    d.text((70, 205), "Lemon chickpea salad", font=font(72), fill=0)
    d.line((70, 310, 1802, 310), fill=0, width=4)
    _, ingredients, steps = RECIPES["A"]
    d.text((70, 350), "INGREDIENTS" if page == 1 else "METHOD", font=font(44), fill=0)
    items = ingredients if page == 1 else [f"{n}. {step}" for n, step in enumerate(steps, 1)]
    y = 440
    for item in items:
        y = paragraph(d, item, 70, y, 1720, size=48, gap=16, bottom=1140) + 22
    d.line((70, 1180, 1802, 1180), fill=0, width=3)
    d.text((70, 1210), f"Tap the CENTER to go to page {3 - page}.", font=font(44), fill=0)
    d.text((70, 1280), "Wait until the screen finishes updating before tapping again.", font=font(32), fill=0)
    d.text((70, 1340), f"TWO-PAGE TRIAL  |  {version}", font=font(25), fill=0)
    output = BytesIO()
    im.point(lambda p: 255 if p >= 160 else 0).convert("1").save(output, format="PNG", optimize=True)
    return output.getvalue()
