"""Deterministic single-page rendering. Overflow is an error, never silent cropping."""
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, __version__ as pillow_version

from server.render import font
from .db import digest, dumps
from .document import RecipeError, parse_markdown

VERSION = "erecipe-pillow-v3"
WIDTH, HEIGHT = 1872, 1404
BODY_SIZE = 32
MARGIN = 48
INGREDIENT_WIDTH = 580
COLUMN_GAP = 56


def profile():
    selected = font(BODY_SIZE)
    path = getattr(selected, "path", None)
    font_hash = digest(Path(path).read_bytes()) if isinstance(path, str) and Path(path).is_file() else digest(selected.font.__class__.__name__.encode())
    return {"width": WIDTH, "height": HEIGHT, "encoding": "png-1bit", "layout": "ingredients-method",
            "font_sha256": font_hash, "body_size": BODY_SIZE, "line_gap": 5,
            "item_gap": 8, "margin": MARGIN, "ingredient_width": INGREDIENT_WIDTH,
            "column_gap": COLUMN_GAP, "group_heading": "reverse-bar",
            "header": "title-with-right-aligned-servings",
            "pillow_version": pillow_version}


def cache_key(revision, display_profile, preferences=None):
    return digest(dumps({"revision_id": revision["id"], "markdown_sha256": revision["markdown_sha256"],
                         "renderer": VERSION, "profile": display_profile, "preferences": preferences or {}}).encode())


def render_pages(markdown):
    document = parse_markdown(markdown)
    image = Image.new("L", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(image)

    def wrapped(text, width, size):
        face, lines, line = font(size), [], ""
        for word in text.split():
            candidate = (line + " " + word).strip()
            if draw.textlength(candidate, font=face) > width:
                if line:
                    lines.append(line)
                    line = ""
                if draw.textlength(word, font=face) > width:
                    raise RecipeError("render_overflow", "A word or value is too wide to render. Edit the recipe before sending.")
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
        return lines

    def paragraph(text, x, y, width, size=BODY_SIZE, bottom=HEIGHT-MARGIN, bold=False):
        for line in wrapped(text, width, size):
            if y + size + 5 > bottom:
                raise RecipeError("render_overflow", "This recipe does not fit one readable page. Edit it or wait for multi-page rendering; no content was cropped.")
            draw.text((x, y), line, font=font(size), fill=0, anchor="lt", stroke_width=1 if bold else 0)
            y += size + 5
        return y

    meta = document["metadata"]
    content_width = WIDTH - 2 * MARGIN
    # Reserve notes/footer before laying out either column. They can never overlap it.
    notes = []
    for note in document["notes"]:
        notes.extend(wrapped("• " + note, content_width - 110, 26))
    footer = wrapped(meta["footer"], content_width, 26) if meta["footer"] else []
    end_height = len(notes) * 31 + len(footer) * 31 + (10 if notes and footer else 0)
    body_bottom = HEIGHT - MARGIN - end_height - (24 if end_height else 0)

    title_width = content_width
    servings_bottom = 24
    if meta["servings"]:
        servings = "Serves: " + meta["servings"]
        servings_width = min(int(draw.textlength(servings, font=font(26))) + 1, content_width // 3)
        title_width -= servings_width + 40
        servings_bottom = 35
        for line in wrapped(servings, servings_width, 26):
            if servings_bottom + 31 > min(240, body_bottom):
                raise RecipeError("render_overflow", "Servings metadata exceeds the available header height.")
            draw.text((WIDTH - MARGIN, servings_bottom), line, font=font(26), fill=0, anchor="rt")
            servings_bottom += 31
    y = max(servings_bottom, paragraph(meta["title"], MARGIN, 24, title_width, size=48, bottom=min(240, body_bottom)))
    details = []
    for label, key in (("Prep", "prep_time"), ("Cook", "cook_time")):
        if meta[key]:
            details.append(label + ": " + meta[key])
    if details:
        y = paragraph("   |   ".join(details), MARGIN, y + 8, content_width, size=26, bottom=min(300, body_bottom))
    y += 22
    method_x = MARGIN + INGREDIENT_WIDTH + COLUMN_GAP
    columns = [("INGREDIENTS", document["ingredients"], MARGIN, INGREDIENT_WIDTH),
               ("METHOD", document["method"], method_x, WIDTH - MARGIN - method_x)]
    for name, groups, x, width in columns:
        row = paragraph(name, x, y, width, size=24, bottom=body_bottom, bold=True) + 10
        total_items = sum(len(group["items"]) for group in groups)
        indent = max(34, int(draw.textlength(f"{total_items}.", font=font(BODY_SIZE))) + 12) if name == "METHOD" else 26
        number = 1
        for group_index, group in enumerate(groups):
            if group_index:
                row += 8
            if group["name"]:
                heading_lines = wrapped(group["name"], width - 24, 27)
                heading_height = 10 + len(heading_lines) * 32
                if row + heading_height > body_bottom:
                    raise RecipeError("render_overflow", "Recipe group headings exceed the available page height.")
                draw.rectangle((x, row, x + width, row + heading_height - 1), fill=0)
                for index, line in enumerate(heading_lines):
                    draw.text((x + 12, row + 5 + index * 32), line, font=font(27), fill=255, anchor="lt", stroke_width=1, stroke_fill=255)
                row += heading_height + 8
            for item in group["items"]:
                prefix = f"{number}." if name == "METHOD" else "•"
                draw.text((x, row + (9 if name == "INGREDIENTS" else 0)), prefix, font=font(BODY_SIZE), fill=0, anchor="lt")
                row = paragraph(item, x + indent, row, width - indent, bottom=body_bottom) + 8
                number += 1
    if end_height:
        row = HEIGHT - MARGIN - end_height
        draw.line((MARGIN, row - 12, WIDTH-MARGIN, row - 12), fill=0, width=2)
        if notes:
            draw.text((MARGIN, row), "NOTES", font=font(23), fill=0, anchor="lt", stroke_width=1)
            for line in notes:
                draw.text((MARGIN + 110, row), line, font=font(26), fill=0, anchor="lt")
                row += 31
        if footer:
            row += 10 if notes else 0
            for line in footer:
                draw.text((MARGIN, row), line, font=font(26), fill=0, anchor="lt")
                row += 31
    output = BytesIO()
    image.point(lambda p: 255 if p >= 160 else 0).convert("1").save(output, format="PNG", optimize=True)
    return [output.getvalue()]
