"""Reader PDF text extraction in a bounded child process; never fetches links."""
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 50
MAX_TEXT_BYTES = 400_000
MAX_STREAM_BYTES = 8 * 1024 * 1024


class PDFError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def extract_text(data):
    from pypdf import PdfReader, Configuration, overwrite_configuration

    if len(data) > MAX_PDF_BYTES:
        raise PDFError('source_too_large', 'Share a PDF of at most 20 MiB.')
    if not data.startswith(b'%PDF-'):
        raise PDFError('invalid_pdf', 'The shared file is not a readable PDF. Export it again from Safari.')
    config = Configuration(zlib_maximum_output_length=MAX_STREAM_BYTES,
                           lzw_maximum_output_length=MAX_STREAM_BYTES,
                           run_length_maximum_output_length=MAX_STREAM_BYTES,
                           array_based_stream_maximum_output_length=MAX_STREAM_BYTES)
    overwrite_configuration(config)
    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted:
        raise PDFError('unsupported_source', 'Share an unencrypted PDF, such as Safari’s Reader PDF.')
    if not 1 <= len(reader.pages) <= MAX_PAGES:
        raise PDFError('source_too_large', 'Share a recipe PDF with 1–50 pages.')
    pages = []
    total = 0
    for index, page in enumerate(reader.pages, 1):
        stream = page.get_contents()
        if stream is not None and len(stream.get_data()) > MAX_STREAM_BYTES:
            raise PDFError('source_too_large', 'This PDF page is too complex. Share a simpler Reader PDF or recipe photos.')
        text = page.extract_text().strip()
        # Do not silently omit an image-only recipe page in a mixed document.
        if not text:
            raise PDFError('unsupported_source', 'A PDF page has no readable text. Share Safari’s Reader PDF or recipe photos instead.')
        text = f'Page {index}:\n{text}'
        total += len(text.encode('utf-8')) + 2
        if total > MAX_TEXT_BYTES:
            raise PDFError('source_too_large', 'The PDF contains too much text. Share only the recipe pages.')
        pages.append(text)
    return '\n\n'.join(pages)


def pdf_evidence(data):
    from .document import RecipeError

    try:
        result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve())], input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        raise RecipeError('source_too_large', 'PDF processing exceeded its time limit. Share a simpler Reader PDF or recipe photos.')
    if result.returncode != 0:
        raise RecipeError('invalid_pdf', 'The PDF could not be processed within its resource limits. Export a new Reader PDF or share photos.')
    payload = json.loads(result.stdout)
    if 'error' in payload:
        raise RecipeError(payload['error']['code'], payload['error']['message'])
    return payload['text']


if __name__ == '__main__':
    # Apply limits in the child, never in the multithreaded API/worker process.
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    if sys.platform == 'linux':
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    try:
        payload = {'text': extract_text(sys.stdin.buffer.read(MAX_PDF_BYTES + 1))}
    except PDFError as error:
        payload = {'error': {'code': error.code, 'message': str(error)}}
    except Exception:
        payload = {'error': {'code': 'invalid_pdf', 'message': 'The PDF could not be read. Export a new Reader PDF or share recipe photos.'}}
    print(json.dumps(payload, ensure_ascii=True))
