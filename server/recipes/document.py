"""Validated extraction schema and the deliberately small erecipe/v1 Markdown dialect."""
import re
from typing import Literal

from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict, Field, field_validator
import yaml

from .db import Problem


class RecipeError(Problem):
    def __init__(self, code, message, *, retryable=False, details=None):
        super().__init__(message, 422)
        self.code, self.retryable, self.details = code, retryable, details or {}

    def as_dict(self):
        return {"code": self.code, "message": str(self), "retryable": self.retryable, "details": self.details}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Group(StrictModel):
    name: str | None
    items: list[str]

    @field_validator("name")
    @classmethod
    def group_name(cls, value):
        if value is not None and (not value.strip() or len(value) > 200 or "\n" in value):
            raise ValueError("Group names must be nonempty single lines of at most 200 characters")
        return value

    @field_validator("items")
    @classmethod
    def group_items(cls, items):
        if not items or len(items) > 200 or any(not x.strip() or len(x) > 8000 for x in items):
            raise ValueError("Groups need 1–200 nonempty items, at most 8000 characters each")
        return items


class ExtractedRecipe(StrictModel):
    is_recipe: bool
    title: str
    servings: str | None
    prep_time: str | None
    cook_time: str | None
    total_time: str | None
    other_time: str | None
    ingredients: list[Group]
    method: list[Group]
    notes: list[str]
    footer: str | None
    warnings: list[str]

    @field_validator("title")
    @classmethod
    def title_length(cls, value):
        if not value.strip() or len(value) > 300 or "\n" in value:
            raise ValueError("A title must be a nonempty single line of at most 300 characters")
        return value


class FrontMatter(StrictModel):
    format: Literal["erecipe/v1"] = "erecipe/v1"
    title: str = Field(min_length=1, max_length=300)
    servings: str | None = None
    prep_time: str | None = None
    cook_time: str | None = None
    total_time: str | None = None
    other_time: str | None = None
    layout: Literal["ingredients-method"] = "ingredients-method"
    footer: str | None = None


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate metadata keys instead of silently choosing one."""


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("Metadata keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def escape(text):
    # Model strings are literal recipe content, never Markdown instructions.
    text = " ".join(text.split())
    return re.sub(r"([\\`*_{}\[\]<>()#!|~+\-.])", r"\\\1", text)


def to_markdown(recipe):
    if not recipe.is_recipe or not recipe.ingredients or not recipe.method:
        raise RecipeError("incomplete_recipe", "The source does not contain one complete recipe. Review the source and extraction warnings.",
                          details={"warnings": recipe.warnings})
    metadata = FrontMatter(title=recipe.title, servings=recipe.servings, prep_time=recipe.prep_time,
                           cook_time=recipe.cook_time, total_time=recipe.total_time,
                           other_time=recipe.other_time, footer=recipe.footer)
    lines = ["---", yaml.safe_dump(metadata.model_dump(exclude_none=True), allow_unicode=True, sort_keys=False).rstrip(), "---", ""]
    for section, groups in (("Ingredients", recipe.ingredients), ("Method", recipe.method)):
        lines.extend(["## " + section, ""])
        number = 1
        for group in groups:
            if group.name:
                lines.extend(["### " + escape(group.name), ""])
            for item in group.items:
                prefix = "- " if section == "Ingredients" else f"{number}. "
                lines.append(prefix + escape(item))
                number += 1
            lines.append("")
    if recipe.notes:
        lines.extend(["## Notes", ""] + ["- " + escape(note) for note in recipe.notes] + [""])
    result = "\n".join(lines)
    parse_markdown(result)  # Enforce exactly the same grammar as user edits.
    return result


def inline_text(token):
    allowed = {"text", "softbreak", "hardbreak", "em_open", "em_close", "strong_open", "strong_close"}
    children = token.children or []
    if any(child.type not in allowed for child in children):
        raise RecipeError("invalid_markdown", "Only text and emphasis are allowed inside recipe items; links, images, HTML, and code are unsupported.")
    return "".join(" " if c.type in {"softbreak", "hardbreak"} else c.content if c.type == "text" else "" for c in children).strip()


def parse_markdown(markdown):
    if not isinstance(markdown, str) or len(markdown) > 100_000:
        raise RecipeError("invalid_markdown", "Markdown must be at most 100,000 characters.")
    normalized = markdown.replace("\r\n", "\n")
    if not normalized.startswith("---\n") or "\n---\n" not in normalized[4:]:
        raise RecipeError("invalid_markdown", "Begin with YAML metadata enclosed by --- lines.")
    front, body = normalized[4:].split("\n---\n", 1)
    try:
        # Aliases are not needed for this small metadata object.
        if any(isinstance(t, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for t in yaml.scan(front)):
            raise ValueError("YAML aliases/anchors are unsupported")
        metadata = FrontMatter.model_validate(yaml.load(front, Loader=UniqueLoader))
        if not metadata.title.strip() or any("\n" in value or len(value) > 1000 for value in metadata.model_dump().values() if isinstance(value, str)):
            raise ValueError("Metadata values must be bounded single lines")
    except (ValueError, TypeError, yaml.YAMLError) as error:
        raise RecipeError("invalid_markdown", "Invalid recipe metadata; use the erecipe/v1 fields with string values.") from error

    tokens = MarkdownIt("commonmark", {"html": True}).parse(body)
    sections = {"Ingredients": [], "Method": [], "Notes": []}
    seen, section, list_kind, in_item, paragraph_count = set(), None, None, False, 0
    group = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type == "heading_open":
            if list_kind:
                raise RecipeError("invalid_markdown", "Headings cannot be nested inside recipe items.")
            heading = inline_text(tokens[index + 1])
            if token.tag == "h2" and heading in sections and heading not in seen:
                if heading == "Ingredients" and seen or heading == "Method" and "Ingredients" not in seen or heading == "Notes" and "Method" not in seen:
                    raise RecipeError("invalid_markdown", "Sections must appear in the order Ingredients, Method, Notes.")
                section, group = heading, None
                seen.add(section)
            elif token.tag == "h3" and section in {"Ingredients", "Method"}:
                group = {"name": heading, "items": []}
                sections[section].append(group)
            else:
                raise RecipeError("invalid_markdown", "Use ## Ingredients, ## Method, optional ## Notes, and ### group headings.")
            index += 3
            continue
        if token.type in {"bullet_list_open", "ordered_list_open"}:
            expected = "ordered_list_open" if section == "Method" else "bullet_list_open"
            if not section or list_kind or token.type != expected:
                raise RecipeError("invalid_markdown", "Use flat bullet lists for ingredients/notes and numbered lists for method steps.")
            list_kind = token.type
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            list_kind = None
        elif token.type == "list_item_open":
            in_item, paragraph_count = True, 0
        elif token.type == "list_item_close":
            if paragraph_count == 0:
                raise RecipeError("invalid_markdown", "Recipe list items cannot be empty.")
            in_item = False
        elif token.type == "paragraph_open":
            if not section or (not in_item and section != "Notes"):
                raise RecipeError("invalid_markdown", "Recipe content must be inside its section's list.")
            paragraph_count += 1
            if in_item and paragraph_count > 1:
                raise RecipeError("invalid_markdown", "Each list item must be one paragraph.")
        elif token.type == "inline":
            value = inline_text(token)
            if not value:
                raise RecipeError("invalid_markdown", "Recipe items cannot be empty.")
            if section == "Notes":
                sections[section].append(value)
            else:
                if group is None:
                    group = {"name": None, "items": []}
                    sections[section].append(group)
                group["items"].append(value)
        elif token.type != "paragraph_close":
            raise RecipeError("invalid_markdown", f"Unsupported Markdown construct: {token.type}.")
        index += 1
    if not sections["Ingredients"] or not sections["Method"] or any(not g["items"] for name in ("Ingredients", "Method") for g in sections[name]):
        raise RecipeError("incomplete_recipe", "Ingredients and Method must each contain at least one nonempty item per group.")
    return {"metadata": metadata.model_dump(), "ingredients": sections["Ingredients"],
            "method": sections["Method"], "notes": sections["Notes"]}
