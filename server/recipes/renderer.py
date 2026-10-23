"""Deterministic largest-fitting typography with condensed multi-page fallback."""
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, __version__ as pillow_version

from server.render import font
from .db import digest, dumps
from .document import RecipeError, parse_markdown

VERSION = "erecipe-pillow-v6"
WIDTH, HEIGHT = 1872, 1404


@dataclass(frozen=True)
class Layout:
    name: str
    body: int
    line_gap: int
    item_gap: int
    margin: int
    ingredient_width: int
    column_gap: int
    title: int
    secondary: int
    heading: int


# Fixed presets, in selection order. Never shrink below condensed.
LAYOUTS = (
    Layout("large-print", 44, 9, 12, 48, 760, 56, 56, 32, 32),
    Layout("normal", 30, 6, 4, 36, 700, 32, 48, 26, 27),
    Layout("condensed", 26, 5, 4, 36, 680, 32, 42, 24, 24),
)


def profile():
    selected = font(LAYOUTS[0].body)
    path = getattr(selected, "path", None)
    font_hash = digest(Path(path).read_bytes()) if isinstance(path, str) and Path(path).is_file() else digest(selected.font.__class__.__name__.encode())
    return {"width": WIDTH, "height": HEIGHT, "encoding": "png-1bit", "layout": "ingredients-method",
            "font_sha256": font_hash, "selection": "largest-single-page-else-condensed",
            "layouts": [asdict(layout) for layout in LAYOUTS], "group_heading": "reverse-bar-multiple-groups-only",
            "header": "title-with-right-aligned-servings", "pillow_version": pillow_version}


def cache_key(revision, display_profile, preferences=None):
    return digest(dumps({"revision_id": revision["id"], "markdown_sha256": revision["markdown_sha256"],
                         "renderer": VERSION, "profile": display_profile, "preferences": preferences or {}}).encode())


class DoesNotFit(Exception):
    pass


@dataclass
class Block:
    lines: list[str]
    size: int
    gap: int
    indent: int = 0
    prefix: str = ""
    heading: bool = False


@dataclass
class RenderPlan:
    layout: str
    pages: list[list[dict]]


class Typesetter:
    def __init__(self, document, layout):
        self.document, self.layout = document, layout
        self.measure = ImageDraw.Draw(Image.new("L", (1, 1)))
        self.fonts = {}

    def face(self, size):
        if size not in self.fonts:
            self.fonts[size] = font(size)
        return self.fonts[size]

    def wrap(self, text, width, size):
        face, lines, line = self.face(size), [], ""
        for word in text.split():
            if self.measure.textlength(word, font=face) > width:
                raise DoesNotFit("A word or value is too wide for the condensed layout.")
            candidate = (line + " " + word).strip()
            if line and self.measure.textlength(candidate, font=face) > width:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
        return lines

    @staticmethod
    def text(commands, text, x, y, size, *, bold=False, fill=0):
        commands.append(dict(kind="text", text=text, x=x, y=y, size=size, bold=bold, fill=fill))

    def header(self):
        l, meta = self.layout, self.document["metadata"]
        commands = []
        width = WIDTH - 2*l.margin
        title_width, servings_bottom = width, l.margin
        if meta["servings"]:
            text = "Serves: " + meta["servings"]
            sw = min(int(self.measure.textlength(text, font=self.face(l.secondary))) + 2, width//3)
            title_width -= sw + 32
            y = l.margin + 8
            for line in self.wrap(text, sw, l.secondary):
                x = WIDTH - l.margin - self.measure.textlength(line, font=self.face(l.secondary))
                self.text(commands, line, x, y, l.secondary)
                y += l.secondary + l.line_gap
            servings_bottom = y
        y = l.margin
        for line in self.wrap(meta["title"], title_width, l.title):
            self.text(commands, line, l.margin, y, l.title)
            y += l.title + l.line_gap
        y = max(y, servings_bottom)
        details = [label + ": " + meta[key] for label, key in
                   (("Prep", "prep_time"), ("Cook", "cook_time"), ("Total", "total_time")) if meta[key]]
        if meta["other_time"]:
            details.append(meta["other_time"])
        if details:
            y += 8
            for line in self.wrap("   |   ".join(details), width, l.secondary):
                self.text(commands, line, l.margin, y, l.secondary)
                y += l.secondary + l.line_gap
        y += 18
        if y > HEIGHT//3:
            raise DoesNotFit("Recipe metadata leaves too little room for readable content.")
        return commands, y

    def blocks(self, groups, width, method=False):
        l = self.layout
        count = sum(len(g["items"]) for g in groups)
        indent = max(34, int(self.measure.textlength(f"{count}.", font=self.face(l.body))) + 12) if method else 24
        blocks, number = [], 1
        for group in groups:
            if group["name"] and len(groups) > 1:
                blocks.append(Block(self.wrap(group["name"], width-24, l.heading), l.heading, l.item_gap, heading=True))
            for item in group["items"]:
                blocks.append(Block(self.wrap(item, width-indent, l.body), l.body, l.item_gap,
                                    indent=indent, prefix=f"{number}." if method else "•"))
                number += 1
        return blocks

    def notes(self, width):
        l = self.layout
        blocks = [Block(self.wrap(note, width-24, l.secondary), l.secondary, l.item_gap, indent=24, prefix="•")
                  for note in self.document["notes"]]
        footer = self.document["metadata"]["footer"]
        if footer:
            blocks.append(Block(self.wrap(footer, width, l.secondary), l.secondary, l.item_gap))
        return blocks

    def block_height(self, block):
        return len(block.lines)*(block.size+self.layout.line_gap) + (10 if block.heading else 0)

    def flow(self, blocks, x, width, top, bottom, section, *, paginate, first_top=None):
        """Measure first, then emit commands. Long items split only at line boundaries."""
        l = self.layout
        pages, rows = [[]], []
        y = top if first_top is None else first_top
        section_height = l.heading + l.line_gap + 10

        def section_label(continued=False):
            nonlocal y
            self.text(pages[-1], section + (" / CONTINUED" if continued else ""), x, y, l.heading, bold=True)
            y += section_height

        def next_page():
            nonlocal y
            if not paginate:
                raise DoesNotFit("Recipe content exceeds one page.")
            rows.append(y)
            pages.append([])
            y = top
            section_label(True)

        if not blocks:
            return pages, [y]
        # Avoid a stranded section label when notes start near the page bottom.
        first = self.block_height(blocks[0]) if blocks[0].heading else min(2, len(blocks[0].lines))*(blocks[0].size+l.line_gap)
        if y + section_height + first > bottom:
            next_page()
        else:
            section_label()
        for index, block in enumerate(blocks):
            height = self.block_height(block)
            capacity = bottom - top - section_height
            keep_next = 0
            if block.heading and index+1 < len(blocks):
                following = blocks[index+1]
                following_height = self.block_height(following)
                keep_next = block.gap + (following_height if height+block.gap+following_height <= capacity
                                        else min(2, len(following.lines))*(following.size+l.line_gap))
            if height + keep_next > capacity and block.heading:
                raise DoesNotFit("A group heading is too tall for a page.")
            # Keep ordinary items together. Only split an item taller than a full column.
            if y + height + keep_next > bottom and (height + keep_next <= capacity or block.heading):
                next_page()
            if block.heading:
                pages[-1].append(dict(kind="rect", box=(x, y, x+width, y+height-1), fill=0))
                for n, line in enumerate(block.lines):
                    self.text(pages[-1], line, x+12, y+5+n*(block.size+l.line_gap), block.size, bold=True, fill=255)
                y += height + block.gap
                continue
            offset = 0
            while offset < len(block.lines):
                available = int((bottom-y)//(block.size+l.line_gap))
                if available < min(2, len(block.lines)-offset):
                    next_page()
                    continue
                count = min(available, len(block.lines)-offset)
                if len(block.lines)-offset-count == 1 and count > 2:
                    count -= 1
                if block.prefix:
                    self.text(pages[-1], block.prefix if offset == 0 else "...", x, y, block.size)
                for line in block.lines[offset:offset+count]:
                    self.text(pages[-1], line, x+block.indent, y, block.size)
                    y += block.size+l.line_gap
                offset += count
                if offset < len(block.lines):
                    next_page()
            y += block.gap
        rows.append(y)
        return pages, rows

    def plan(self, paginate=False):
        l = self.layout
        header, top = self.header()
        bottom = HEIGHT-l.margin-(30 if paginate else 0)
        width = WIDTH-2*l.margin
        note_blocks = self.notes(width)
        note_height = (l.heading+l.line_gap+10 + sum(self.block_height(b)+b.gap for b in note_blocks)) if note_blocks else 0
        body_bottom = bottom if paginate else bottom-note_height-(18 if note_blocks else 0)
        method_x = l.margin+l.ingredient_width+l.column_gap
        ingredients, ir = self.flow(self.blocks(self.document["ingredients"], l.ingredient_width),
            l.margin, l.ingredient_width, top, body_bottom, "INGREDIENTS", paginate=paginate)
        method_width = WIDTH-l.margin-method_x
        method, mr = self.flow(self.blocks(self.document["method"], method_width, True),
            method_x, method_width, top, body_bottom, "METHOD", paginate=paginate)
        count = max(len(ingredients), len(method))
        pages = [(ingredients[n] if n < len(ingredients) else []) + (method[n] if n < len(method) else []) for n in range(count)]
        if note_blocks:
            end_y = max(ir[-1] if len(ir)==count else top, mr[-1] if len(mr)==count else top)
            note_top = end_y+18 if paginate else bottom-note_height
            notes, _ = self.flow(note_blocks, l.margin, width, top, bottom, "NOTES", paginate=paginate, first_top=note_top)
            pages[-1].extend(notes[0])
            pages.extend(notes[1:])
        for index, page in enumerate(pages):
            page[:0] = header
            if len(pages)>1:
                label = f"PAGE {index+1} / {len(pages)}"
                x = WIDTH-l.margin-self.measure.textlength(label, font=self.face(20))
                self.text(page, label, x, HEIGHT-l.margin-20, 20)
        return RenderPlan(l.name, pages)


def layout_document(markdown):
    document = parse_markdown(markdown)
    for layout in LAYOUTS:
        try:
            return Typesetter(document, layout).plan()
        except DoesNotFit:
            pass
    try:
        return Typesetter(document, LAYOUTS[-1]).plan(paginate=True)
    except DoesNotFit as error:
        raise RecipeError("render_overflow", str(error) + " Edit the recipe before sending; no content was cropped.") from error


def render_pages(markdown, *, plan=None):
    plan = plan or layout_document(markdown)
    pages, fonts = [], {}
    for commands in plan.pages:
        image = Image.new("L", (WIDTH, HEIGHT), 255)
        draw = ImageDraw.Draw(image)
        for command in commands:
            if command["kind"] == "rect":
                draw.rectangle(command["box"], fill=command["fill"])
            else:
                size = command["size"]
                if size not in fonts:
                    fonts[size] = font(size)
                draw.text((command["x"], command["y"]), command["text"], font=fonts[size],
                          fill=command["fill"], anchor="lt", stroke_width=1 if command["bold"] else 0,
                          stroke_fill=command["fill"])
        output = BytesIO()
        image.point(lambda p: 255 if p >= 160 else 0).convert("1").save(output, format="PNG", optimize=True)
        pages.append(output.getvalue())
    return pages
