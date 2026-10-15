"""Typography selection, complete content preservation, and page boundary checks."""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import re

from PIL import Image, ImageDraw
import pytest

from server.recipes import renderer
from server.recipes.document import ExtractedRecipe, RecipeError, parse_markdown, to_markdown
from server.render import font

RECIPE = dict(is_recipe=True, title="Cucumber salad", servings="2", prep_time=None, cook_time=None, total_time=None, other_time=None,
              ingredients=[dict(name="Salad", items=["1 cucumber", "1 tablespoon lemon juice"])],
              method=[dict(name="Prepare", items=["Slice cucumber and toss with lemon juice."])],
              notes=[], footer=None, warnings=[])


def markdown(recipe):
    return to_markdown(ExtractedRecipe(**recipe))


def section_text(plan, document, section):
    layout = next(l for l in renderer.LAYOUTS if l.name == plan.layout)
    if section == "ingredients":
        x = layout.margin + 24
        size = layout.body
    elif section == "method":
        count = sum(len(g['items']) for g in document['method'])
        indent = max(34, int(ImageDraw.Draw(Image.new('L', (1, 1))).textlength(f'{count}.', font=font(layout.body)))+12)
        x = layout.margin+layout.ingredient_width+layout.column_gap+indent
        size = layout.body
    else:
        x, size = layout.margin+24, layout.secondary
    return ' '.join(command['text'] for page in plan.pages for command in page
                    if command['kind']=='text' and command['x']==x and command['size']==size and not command['bold'])


def assert_content(plan, document):
    for section in ['ingredients', 'method']:
        expected = ' '.join(' '.join(item.split()) for group in document[section] for item in group['items'])
        assert section_text(plan, document, section) == expected
    assert section_text(plan, document, 'notes') == ' '.join(' '.join(note.split()) for note in document['notes'])


def assert_bounds(plan):
    margin = next(l.margin for l in renderer.LAYOUTS if l.name == plan.layout)
    draw = ImageDraw.Draw(Image.new('L', (1, 1)))
    for page in plan.pages:
        for command in page:
            if command['kind'] == 'rect':
                left, top, right, bottom = command['box']
            else:
                left, top, right, bottom = draw.textbbox((command['x'], command['y']), command['text'],
                    font=font(command['size']), anchor='lt', stroke_width=1 if command['bold'] else 0)
            assert left >= margin-1 and top >= margin-1
            assert right <= renderer.WIDTH-margin+1 and bottom <= renderer.HEIGHT-margin+1


def test_black_bean_recipe_fits_condensed_without_text_changes():
    md = Path('server/fixtures/black-bean-burgers.formatted.md').read_text()
    plan = renderer.layout_document(md)
    assert plan.layout == 'condensed' and len(plan.pages) == 1
    assert_content(plan, parse_markdown(md))
    assert_bounds(plan)
    pages = renderer.render_pages(md, plan=plan)
    with Image.open(BytesIO(pages[0])) as image:
        assert image.size == (1872, 1404) and image.mode == '1'
    assert renderer.render_pages(md) == pages


def test_selects_largest_fitting_static_layout():
    assert renderer.layout_document(markdown(RECIPE)).layout == 'large-print'
    recipe = deepcopy(RECIPE)
    recipe['method'][0]['items'] = [f'Step {i}: Stir the vegetables gently for two minutes, then remove the pan from the heat.' for i in range(18)]
    md = markdown(recipe)
    plan = renderer.layout_document(md)
    assert plan.layout == 'normal' and len(plan.pages) == 1
    assert_content(plan, parse_markdown(md))
    assert_bounds(plan)


def test_single_group_labels_are_hidden_but_multiple_groups_keep_their_names():
    recipe = deepcopy(RECIPE)
    plan = renderer.layout_document(markdown(recipe))
    labels = [c.get('text') for page in plan.pages for c in page]
    assert 'INGREDIENTS' in labels and 'METHOD' in labels
    assert 'Salad' not in labels and 'Prepare' not in labels
    recipe['ingredients'].append(dict(name='Dressing', items=['1 teaspoon olive oil']))
    recipe['method'].append(dict(name='Serve', items=['Serve immediately.']))
    plan = renderer.layout_document(markdown(recipe))
    labels = [c.get('text') for page in plan.pages for c in page]
    assert all(label in labels for label in ['Salad', 'Dressing', 'Prepare', 'Serve'])


def test_timing_metadata_round_trips_and_renders_only_above_the_recipe_body():
    recipe = deepcopy(RECIPE)
    recipe.update(prep_time='10 minutes', cook_time='20 minutes', total_time='1 hour',
                  other_time='Rest: 30 minutes')
    recipe['method'][0]['items'] = ['Blend in a food processor, then cook for 20 minutes.',
                                  'Rest for 30 minutes. Refrigerate leftovers for up to 3 days.']
    md = markdown(recipe)
    document = parse_markdown(md)
    assert document['metadata']['total_time'] == '1 hour'
    assert document['metadata']['other_time'] == 'Rest: 30 minutes'
    plan = renderer.layout_document(md)
    body_top = min(c['y'] for c in plan.pages[0] if c.get('text') == 'INGREDIENTS')
    header = ' '.join(c['text'] for c in plan.pages[0] if c['kind'] == 'text' and c['y'] < body_top)
    assert 'Prep: 10 minutes' in header and 'Cook: 20 minutes' in header
    assert 'Total: 1 hour' in header and 'Rest: 30 minutes' in header
    assert_content(plan, document)
    assert_bounds(plan)


def test_legacy_markdown_still_parses_without_new_header_fields():
    md = Path('server/fixtures/black-bean-burgers.formatted.md').read_text()
    metadata = parse_markdown(md)['metadata']
    assert metadata['total_time'] is None and metadata['other_time'] is None


def test_extraction_schema_requires_nullable_timing_fields():
    schema = ExtractedRecipe.model_json_schema()
    assert set(schema['required']) == set(schema['properties'])
    for field in ['total_time', 'other_time']:
        assert {'type': 'null'} in schema['properties'][field]['anyOf']


@pytest.mark.parametrize('section', ['method', 'ingredients', 'notes'])
def test_long_sections_paginate_in_condensed_and_keep_every_item(section):
    recipe = deepcopy(RECIPE)
    items = [f'Item {i}: Keep all of this instruction and its precise quantity of 2 tablespoons.' for i in range(100)]
    if section == 'notes':
        recipe['notes'] = items
    else:
        recipe[section] = [dict(name='First group', items=items[:50]), dict(name='Second group', items=items[50:])]
    recipe['footer'] = 'Final storage instruction: refrigerate promptly.'
    md = markdown(recipe)
    plan = renderer.layout_document(md)
    assert plan.layout == 'condensed' and len(plan.pages) > 1
    assert_content(plan, parse_markdown(md))
    assert_bounds(plan)
    for index, page in enumerate(plan.pages, 1):
        assert any(command.get('text') == f'PAGE {index} / {len(plan.pages)}' for command in page)
        assert any(command.get('text') == RECIPE['title'] for command in page)
        # Every page has real body content, not only headings/page numbers.
        assert any(command['kind']=='text' and re.search(r'Item \d+:|Slice cucumber|Final storage', command['text']) for command in page)
    assert any(command.get('text') == recipe['footer'] for command in plan.pages[-1])


def test_oversized_step_and_note_split_across_pages_without_omission():
    recipe = deepcopy(RECIPE)
    recipe['method'][0]['items'] = [' '.join(f'word{i}' for i in range(800))]
    recipe['notes'] = [' '.join(f'note{i}' for i in range(800))]
    md = markdown(recipe)
    plan = renderer.layout_document(md)
    assert plan.layout == 'condensed' and len(plan.pages) > 2
    assert_content(plan, parse_markdown(md))
    assert_bounds(plan)
    assert any(command.get('text') == '...' for page in plan.pages for command in page)


def test_group_headings_are_not_stranded_at_page_bottom():
    recipe = deepcopy(RECIPE)
    recipe['method'] = [dict(name=f'Group {i}', items=[f'Item {i}: ' + 'Stir gently and preserve this instruction. '*9]) for i in range(25)]
    plan = renderer.layout_document(markdown(recipe))
    for page in plan.pages:
        for index, command in enumerate(page):
            if command['kind'] == 'rect':
                assert any(c['kind']=='text' and c['fill']==0 and c['y']>=command['box'][3]
                           and c['size']==26 and c['x']>command['box'][0] for c in page[index+1:])
    assert_content(plan, parse_markdown(markdown(recipe)))
    assert_bounds(plan)


def test_unbreakable_values_still_fail_explicitly():
    recipe = deepcopy(RECIPE)
    recipe['ingredients'][0]['items'] = ['W'*200]
    with pytest.raises(RecipeError) as error:
        renderer.render_pages(markdown(recipe))
    assert error.value.code == 'render_overflow'


def test_profile_and_cache_include_static_layout_policy():
    profile = renderer.profile()
    assert [l['name'] for l in profile['layouts']] == ['large-print', 'normal', 'condensed']
    revision = dict(id='revision', markdown_sha256='hash')
    key = renderer.cache_key(revision, profile)
    changed = deepcopy(profile)
    changed['layouts'][2]['body'] += 1
    assert renderer.cache_key(revision, changed) != key
