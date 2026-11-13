from io import BytesIO
from pathlib import Path

from PIL import Image
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from werkzeug.utils import secure_filename


MAX_STAFF_FILES = 10


def read_document(upload, max_bytes, *, verify_contents=False):
    if not upload or not upload.filename:
        raise ValueError('Choose a PDF, JPG or PNG file.')
    filename = secure_filename(upload.filename)[:180]
    data = upload.read(max_bytes + 1)
    if not data or len(data) > max_bytes:
        raise ValueError(f'Choose a non-empty document no larger than {max_bytes // (1024*1024)} MB.')
    formats = {
        '.pdf': ('application/pdf', b'%PDF-'),
        '.png': ('image/png', b'\x89PNG\r\n\x1a\n'),
        '.jpg': ('image/jpeg', b'\xff\xd8\xff'),
        '.jpeg': ('image/jpeg', b'\xff\xd8\xff'),
    }
    mime, signature = formats.get(Path(filename).suffix.lower(), (None, None))
    if not signature or not data.startswith(signature):
        raise ValueError('Upload a valid PDF, JPG or PNG file.')
    if verify_contents:
        try:
            if mime == 'application/pdf':
                pdf = PdfReader(BytesIO(data))
                if '/Root' not in pdf.trailer:
                    raise ValueError()
            else:
                with Image.open(BytesIO(data)) as image:
                    image.verify()
        except (ValueError, OSError, EOFError, SyntaxError, PyPdfError, Image.DecompressionBombError) as error:
            raise ValueError('This file could not be read. Choose a complete PDF, JPG or PNG.') from error
    return filename, mime, data
