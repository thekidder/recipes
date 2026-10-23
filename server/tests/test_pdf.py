"""Real PDF parsing and durable Reader PDF imports, without network access."""
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import patch

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from server.recipes.db import Library
from server.recipes.document import RecipeError
from server.recipes.pdf import pdf_evidence
from server.recipes.processing import ProcessingService
from server.tests.test_processing import worker, finish


def make_pdf(pages=('Lemon salad: 2 cans chickpeas.', 'Mix with lemon juice. Serve chilled.'), password=None):
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        escaped = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        stream.set_data(f'BT /F1 12 Tf 50 700 Td ({escaped}) Tj ET'.encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    if password:
        writer.encrypt(password)
    out = BytesIO(); writer.write(out)
    return out.getvalue()


def test_pdf_text_in_page_order():
    assert pdf_evidence(make_pdf()) == 'Page 1:\nLemon salad: 2 cans chickpeas.\n\nPage 2:\nMix with lemon juice. Serve chilled.'


@pytest.mark.parametrize('data,code', [
    (b'%PDF-1.7\nbroken', 'invalid_pdf'),
    (make_pdf(password='secret'), 'unsupported_source'),
    (make_pdf(pages=['']), 'unsupported_source'),
    (make_pdf(pages=['Readable recipe', '']), 'unsupported_source'),
    (make_pdf(pages=['x'] * 51), 'source_too_large'),
    (make_pdf(pages=['x' * 400001]), 'source_too_large'),
])
def test_unusable_pdf_fails_without_silent_truncation(data, code):
    with pytest.raises(RecipeError) as error:
        pdf_evidence(data)
    assert error.value.code == code


def test_pdf_timeout_is_actionable():
    with patch('server.recipes.pdf.subprocess.run', side_effect=subprocess.TimeoutExpired('pdf', 30)):
        with pytest.raises(RecipeError, match='time limit'):
            pdf_evidence(make_pdf())


def test_pdf_restart_reextraction_and_no_website_fetch(tmp_path):
    library = Library(tmp_path)
    source = make_pdf()
    service = ProcessingService(library)
    job = service.submit_pdf('Reader.pdf', source, key='pdf')
    assert service.submit_pdf('Reader.pdf', source, key='pdf')['id'] == job['id']
    library = Library(tmp_path)
    runner = worker(library)
    with patch.object(runner.fetcher, 'fetch', side_effect=AssertionError('PDF must not fetch a website')):
        assert runner.run_once() and runner.run_once()
        repeat = ProcessingService(library).reextract(job['recipe_id'], job['source_capture_id'], key='again')
        assert runner.run_once()
        assert library.required('jobs', repeat['id'])['status'] == 'succeeded'
    capture = library.required('source_captures', job['source_capture_id'])
    assert capture['kind'] == 'pdf' and capture['submitted_url'] is None
    asset = library.one('SELECT * FROM source_assets WHERE capture_id=?', (capture['id'],))
    assert asset['role'] == 'original_pdf' and asset['original_filename'] == 'Reader.pdf'
    assert library.asset_bytes(asset['asset_id']) == source
    call = library.one('SELECT * FROM llm_calls')
    request = json.loads(library.asset_bytes(call['request_asset_id']))
    assert 'Mix with lemon juice' in json.dumps(request)


def test_pdf_job_failure_retains_source(tmp_path):
    library = Library(tmp_path)
    job = ProcessingService(library).submit_pdf('broken.pdf', b'%PDF-broken', key='bad')
    assert worker(library).run_once()
    failed = library.required('jobs', job['id'])
    assert failed['status'] == 'failed' and json.loads(failed['error_json'])['code'] == 'invalid_pdf'
    assert library.all('SELECT * FROM llm_calls') == []
    assert library.all('SELECT * FROM source_assets')


def test_existing_database_migration_preserves_history(tmp_path):
    library = Library(tmp_path)
    finish(library)
    before = {table: library.all('SELECT * FROM ' + table) for table in ('source_captures', 'source_assets', 'recipe_revisions', 'jobs', 'render_sets')}
    # Recreate the pre-PDF constraint while retaining a complete recipe history.
    with sqlite3.connect(library.path) as c:
        schema = Path('server/recipes/schema.sql').read_text().replace("'url','pdf'", "'url'")
        definition = schema.split('CREATE TABLE IF NOT EXISTS source_captures (', 1)[1].split(';', 1)[0]
        c.execute('CREATE TABLE old_captures (' + definition)
        c.execute('INSERT INTO old_captures SELECT * FROM source_captures')
        c.execute('DROP TABLE source_captures')
        c.execute('ALTER TABLE old_captures RENAME TO source_captures')
        c.execute('DELETE FROM schema_versions WHERE version=3')
    migrated = Library(tmp_path)
    for table, rows in before.items():
        assert migrated.all('SELECT * FROM ' + table) == rows
    with migrated.connect() as c:
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    ProcessingService(migrated).submit_pdf('Reader.pdf', make_pdf(), key='pdf')
    Library(tmp_path)  # Migration is safe on subsequent startups.
