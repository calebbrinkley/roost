"""Attachment text: extraction from uploads, validation of what a send carries, and the prompt layout.

Files are never stored. Only the extracted text travels with the message."""

import asyncio
import io
import json
import math
import re
import sys
import time
from pathlib import Path, PurePosixPath

MAX_UPLOAD = 20 * 1024 * 1024
MAX_PAGES = 500
EXTRACT_SECONDS = 20
MAX_EXTRACTED_CHARS = 1_000_000   # one file's text; the per-message cap below is far smaller
MAX_COUNT = 10
MAX_TOKENS = 30000                # all attachments of one message, at chars/4
MIN_PDF_CHARS = 20                # fewer non-space characters than this means a scan
NAME_LIMIT = 200
PDF_WORKERS = asyncio.Semaphore(2)

TEXT_EXTENSIONS = frozenset((
    'txt md markdown csv tsv json jsonl yaml yml toml xml html htm log ini cfg conf env rst tex srt '
    'py js mjs cjs ts tsx jsx css scss sass less sh bash zsh fish ps1 bat sql graphql proto '
    'c h cc cpp cxx hpp hh cs java kt kts swift m mm go rs rb php pl lua r scala dart zig nim ex exs erl hs ml clj '
    'vue svelte gradle make cmake dockerfile gitignore diff patch').split())
IMAGE_EXTENSIONS = frozenset('png jpg jpeg gif webp bmp tif tiff heic heif avif ico svg'.split())
IMAGE_MAGIC = (b'\x89PNG', b'\xff\xd8\xff', b'GIF8')


class AttachmentError(ValueError):
    """A file or attachment list Roost will not accept, with a message safe to show."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def tokens(chars):
    return math.ceil(chars / 4)


def clean_name(name):
    """A display name without path parts or control characters."""
    name = PurePosixPath(str(name).replace('\\', '/')).name
    name = ''.join(char for char in name if char.isprintable()).strip()
    return '' if name in ('.', '..') else name[:NAME_LIMIT]


def extension(name):
    name = name.lower()
    return name.rsplit('.', 1)[1] if '.' in name else name


def decode(data):
    if b'\0' in data:
        raise AttachmentError("This file looks like a binary file, not text. Roost can only read text and PDFs.")
    return data.decode('utf-8', errors='replace')


def pdf_text(data, deadline):
    """Runs in a disposable child; the deadline is also checked between pages."""
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise AttachmentError("This PDF is encrypted. Remove the password and try again.")
        if len(reader.pages) > MAX_PAGES:
            raise AttachmentError(f"This PDF has {len(reader.pages)} pages. The limit is {MAX_PAGES}.")
        parts, size = [], 0
        for page in reader.pages:
            if time.monotonic() > deadline:
                raise AttachmentError("Reading this PDF took too long.", 408)
            parts.append(page.extract_text() or '')
            size += len(parts[-1])
            if size > MAX_EXTRACTED_CHARS:
                raise AttachmentError("This PDF has too much text to attach.", 413)
    except AttachmentError:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, RecursionError, OSError):
        raise AttachmentError("Roost could not read this PDF.") from None
    return '\n\n'.join(parts).strip()


async def extract_pdf(data):
    if PDF_WORKERS.locked():
        raise AttachmentError("Roost is busy reading PDFs. Try again in a moment.", 503)
    async with PDF_WORKERS:
        worker = await asyncio.create_subprocess_exec(
            sys.executable, '-I', str(Path(__file__).resolve()), str(MAX_PAGES), str(MAX_EXTRACTED_CHARS),
            str(EXTRACT_SECONDS), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            try:
                stdout, _ = await asyncio.wait_for(worker.communicate(data), EXTRACT_SECONDS)
            except TimeoutError:
                raise AttachmentError("Reading this PDF took too long.", 408) from None
            if worker.returncode:
                raise AttachmentError("Roost could not read this PDF within its resource limits.")
            result = json.loads(stdout)
            if 'error' in result:
                raise AttachmentError(result['error'], result['status'])
            return result['text']
        finally:
            if worker.returncode is None:
                try:
                    worker.kill()
                except ProcessLookupError:
                    pass
            await worker.communicate()  # Drain pipes as well as reaping, even after cancellation.


def pdf_worker():
    """Isolated entry point: bound parser memory and CPU before importing pypdf."""
    global MAX_PAGES, MAX_EXTRACTED_CHARS, EXTRACT_SECONDS
    MAX_PAGES, MAX_EXTRACTED_CHARS = map(int, sys.argv[1:3])
    EXTRACT_SECONDS = float(sys.argv[3])
    try:
        import resource
    except ImportError:
        resource = None
    for name, limit in (('RLIMIT_AS', 512 * 1024 * 1024), ('RLIMIT_CPU', math.ceil(EXTRACT_SECONDS) + 1)):
        if resource is not None and hasattr(resource, name):
            try:
                kind = getattr(resource, name)
                _, hard = resource.getrlimit(kind)
                limit = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
                resource.setrlimit(kind, (limit, limit))
            except (OSError, ValueError):
                pass  # Some platforms expose limits they cannot enforce.
    try:
        result = {'text': pdf_text(sys.stdin.buffer.read(MAX_UPLOAD + 1), time.monotonic() + EXTRACT_SECONDS)}
    except AttachmentError as exc:
        result = {'error': str(exc), 'status': exc.status}
    except MemoryError:
        result = {'error': "Roost could not read this PDF within its resource limits.", 'status': 400}
    sys.stdout.write(json.dumps(result))


async def extract(name, data):
    """The text of an uploaded file as {name, kind, chars, tokens, text}, or an AttachmentError."""
    name = clean_name(name)
    if not name:
        raise AttachmentError("The file needs a name.")
    if len(data) > MAX_UPLOAD:
        raise AttachmentError(f"That file is larger than {MAX_UPLOAD // 1024 // 1024} MB.", 413)
    ext = extension(name)
    if ext in IMAGE_EXTENSIONS or data.startswith(IMAGE_MAGIC):
        raise AttachmentError("Roost can't read images yet. Attach text files or PDFs.")
    if ext == 'pdf':
        if not data.lstrip().startswith(b'%PDF'):
            raise AttachmentError("Roost could not read this PDF.")
        text = await extract_pdf(data)
        if len(re.sub(r'\s', '', text)) < MIN_PDF_CHARS:
            raise AttachmentError("This PDF has no text layer, probably a scan. Roost can't read scanned PDFs yet.")
        kind = 'pdf'
    elif ext in TEXT_EXTENSIONS:
        text, kind = decode(data), 'text'
        if not text.strip():
            raise AttachmentError("This file is empty.")
        if len(text) > MAX_EXTRACTED_CHARS:
            raise AttachmentError("This file has too much text to attach.", 413)
    else:
        raise AttachmentError("Roost can only read text files and PDFs.")
    return {'name': name, 'kind': kind, 'chars': len(text), 'tokens': tokens(len(text)), 'text': text}


def validate(items):
    """Check a send's attachment list; return the stored form [{name, kind, chars, text}]."""
    if not isinstance(items, list) or len(items) > MAX_COUNT:
        raise AttachmentError(f"Attach at most {MAX_COUNT} files.")
    stored, total = [], 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {'name', 'text'} or not isinstance(item['name'], str) \
                or not isinstance(item['text'], str):
            raise AttachmentError("Each attachment needs a name and text.")
        name = clean_name(item['name'])
        if not name or len(item['name']) > NAME_LIMIT:
            raise AttachmentError("Each attachment needs a name of at most 200 characters.")
        total += len(item['text'])
        stored.append({'name': name, 'kind': 'pdf' if extension(name) == 'pdf' else 'text',
                       'chars': len(item['text']), 'text': item['text']})
    if tokens(total) > MAX_TOKENS:
        raise AttachmentError(f"The attachments are about {tokens(total):,} tokens. The limit is {MAX_TOKENS:,} per message.")
    return stored


def render(content, attachments):
    """The user message the model sees: each attachment first, then the typed text."""
    if not attachments:
        return content
    blocks = ['<attachment name="%s">\n%s\n</attachment>' % (a['name'].replace('"', '&quot;'),
                                                              a['text'].replace('</attachment', '<\\/attachment'))
              for a in attachments]
    return '\n\n'.join(blocks) + '\n\n' + content


if __name__ == '__main__':
    pdf_worker()
