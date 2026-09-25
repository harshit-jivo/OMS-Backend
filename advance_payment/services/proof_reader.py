"""Turn an uploaded payment proof into ROWS of text.

A proof is a bank statement (many transactions) or one payment's advice or
screenshot, as a PDF, an Excel / CSV export, or a scan or photo. Whatever it
is, `read()` returns it as rows: one string per visual line of the document,
with a statement's columns (date, narration, amount...) kept on the row they
belong to, so a UTR can be tied to its own amount rather than to the line
printed next to it.

Where the text comes from:
  * a PDF page that carries text      -> read here with PyMuPDF (exact, instant)
  * .xlsx / .xls / .csv, and the HTML tables some banks save as ".xls"
                                       -> read here, cell by cell
  * a photo, or a scanned PDF page    -> the OCR service (`OCR_SERVICE_URL`,
    `ocr-service/` on the OMS server), which only these need

So a net-banking PDF or Excel export never waits on OCR, and reads the same on
a developer's machine as on the server.
"""

import csv
import io
import logging
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import PurePath

import openpyxl
import pymupdf
import requests
import xlrd
from django.conf import settings

logger = logging.getLogger(__name__)

MAX_BYTES = 15 * 1024 * 1024
MAX_PAGES = 30
#: A PDF page with less text than this is a scan, and goes to OCR...
MIN_PAGE_TEXT = 30
#: ...as does one mostly covered by an image (a printed e-mail around a
#: pasted statement), whose text layer is only the frame around it.
IMAGE_COVERAGE_FOR_OCR = 0.25
PDF_DPI = 200

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff', '.gif'}


class ProofUnreadable(ValueError):
    """The file is not something this can read (type, size, damage)."""


class OcrUnavailable(Exception):
    """The file needs OCR, and the OCR service is not configured or not answering."""


# ---------------------------------------------------------------------------
# Rows from positioned words (PDF text, and OCR boxes)
# ---------------------------------------------------------------------------
def group_rows(items):
    """`items` are `(x0, y0, x1, y1, text)`; returns one string per visual row.

    Words whose vertical centres are within ~half a line of each other are one
    row, read left to right. A wide horizontal gap (a column boundary) is kept
    as three spaces, so "date   narration   amount" still reads as columns.
    """
    items = [i for i in items if str(i[4]).strip()]
    if not items:
        return []
    heights = sorted(max(i[3] - i[1], 1.0) for i in items)
    line = heights[len(heights) // 2]
    items.sort(key=lambda i: ((i[1] + i[3]) / 2, i[0]))

    rows, current, centre = [], [], 0.0
    for item in items:
        mid = (item[1] + item[3]) / 2
        if current and mid - centre > line * 0.6:
            rows.append(current)
            current = []
        current.append(item)
        centre = sum((i[1] + i[3]) / 2 for i in current) / len(current)
    rows.append(current)

    out = []
    for row in rows:
        row.sort(key=lambda i: i[0])
        text, last_x1 = '', None
        for x0, _y0, x1, _y1, word in row:
            if last_x1 is not None:
                text += '   ' if x0 - last_x1 > line * 1.5 else ' '
            text += str(word).strip()
            last_x1 = x1
        out.append(text)
    return out


# ---------------------------------------------------------------------------
# OCR (photos, scanned pages)
# ---------------------------------------------------------------------------
def _ocr(content, filename, content_type):
    """Rows of one image, from the OCR service."""
    base = str(getattr(settings, 'OCR_SERVICE_URL', '') or '').rstrip('/')
    if not base:
        raise OcrUnavailable(
            'This file needs OCR (a photo or a scanned page), and OCR is not set up '
            'here (OCR_SERVICE_URL). Upload the bank\'s PDF or Excel export instead, '
            'or read it on the server.')
    try:
        response = requests.post(
            f'{base}/ocr',
            files={'file': (filename, content, content_type)},
            timeout=getattr(settings, 'OCR_SERVICE_TIMEOUT', 180))
    except requests.RequestException as exc:
        logger.warning('advance_payment: OCR service unreachable (%s): %s', base, exc)
        raise OcrUnavailable('The OCR service could not be reached.') from exc
    if response.status_code != 200:
        logger.warning('advance_payment: OCR service %s: %s',
                       response.status_code, response.text[:300])
        raise OcrUnavailable(f'The OCR service answered {response.status_code}.')

    rows = []
    for page in response.json().get('pages', []):
        # `rows` where the service groups by position; its plain lines otherwise.
        rows.extend(page.get('rows') or (page.get('text') or '').splitlines())
    return rows


# ---------------------------------------------------------------------------
# The formats
# ---------------------------------------------------------------------------
def _image_coverage(page):
    area = page.rect.width * page.rect.height
    if not area:
        return 0.0
    covered = 0.0
    for info in page.get_image_info():
        box = pymupdf.Rect(info['bbox']) & page.rect
        if not box.is_empty:
            covered += box.width * box.height
    return min(covered / area, 1.0)


def _read_pdf(data, filename):
    try:
        doc = pymupdf.open(stream=data, filetype='pdf')
    except Exception as exc:
        raise ProofUnreadable('The PDF could not be opened.') from exc
    rows, ocr_pages = [], 0
    with doc:
        if doc.needs_pass:
            raise ProofUnreadable(
                'The PDF is password-protected. Bank statements often are: '
                'open it, save a copy without the password, and upload that.')
        if doc.page_count > MAX_PAGES:
            raise ProofUnreadable(f'The PDF has {doc.page_count} pages; the limit is {MAX_PAGES}.')
        for page in doc:
            words = page.get_text('words')
            text_len = sum(len(w[4]) for w in words)
            if text_len >= MIN_PAGE_TEXT and _image_coverage(page) < IMAGE_COVERAGE_FOR_OCR:
                rows.extend(group_rows([w[:5] for w in words]))
                continue
            png = page.get_pixmap(dpi=PDF_DPI).tobytes('png')
            rows.extend(_ocr(png, f'{PurePath(filename).stem}-p{page.number + 1}.png', 'image/png'))
            ocr_pages += 1
        pages = doc.page_count
    source = 'pdf-text' if not ocr_pages else ('ocr' if ocr_pages == pages else 'pdf-text+ocr')
    return {'source': source, 'rows': rows, 'pages': pages, 'ocr_pages': ocr_pages}


def _cell(value):
    """A spreadsheet cell as statement text.

    Whole numbers of 8+ digits are account numbers or references, which Excel
    stores as numbers: written as integers, never "5.02E+13" or "...744.00".
    Other numbers are money, written with paise so they match as amounts.
    """
    if value is None:
        return ''
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, datetime):
        return value.strftime('%d/%m/%Y')
    if isinstance(value, date):
        return value.strftime('%d/%m/%Y')
    if isinstance(value, (int, float)):
        if float(value).is_integer() and abs(value) >= 1e7:
            return str(int(value))
        return f'{float(value):.2f}'
    return str(value).strip()


def _join(cells):
    return ' | '.join(c for c in (_cell(v) for v in cells) if c)


def _read_xlsx(data):
    try:
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ProofUnreadable('The Excel file could not be opened.') from exc
    rows = []
    for sheet in book.worksheets:
        for values in sheet.iter_rows(values_only=True):
            line = _join(values)
            if line:
                rows.append(line)
    book.close()
    return {'source': 'excel', 'rows': rows, 'pages': len(book.worksheets), 'ocr_pages': 0}


def _read_xls(data):
    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception as exc:
        raise ProofUnreadable('The Excel (.xls) file could not be opened.') from exc
    rows = []
    for sheet in book.sheets():
        for r in range(sheet.nrows):
            values = []
            for cell in sheet.row(r):
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        values.append(xlrd.xldate_as_datetime(cell.value, book.datemode))
                        continue
                    except Exception:
                        pass
                values.append(cell.value)
            line = _join(values)
            if line:
                rows.append(line)
    return {'source': 'excel', 'rows': rows, 'pages': book.nsheets, 'ocr_pages': 0}


class _TableRows(HTMLParser):
    """Rows of every <table> in an HTML page."""

    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self._row = []
        elif tag in ('td', 'th') and self._row is not None:
            self._cell = []
        elif tag == 'br' and self._cell is not None:
            self._cell.append(' ')

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self._row is not None and self._cell is not None:
            self._row.append(' '.join(''.join(self._cell).split()))
            self._cell = None
        elif tag == 'tr' and self._row is not None:
            line = ' | '.join(c for c in self._row if c)
            if line:
                self.rows.append(line)
            self._row = None

    def handle_data(self, text):
        if self._cell is not None:
            self._cell.append(text)


def _decode(data):
    for encoding in ('utf-8-sig', 'cp1252'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('latin-1')


def _read_html(data):
    parser = _TableRows()
    parser.feed(_decode(data))
    return {'source': 'excel', 'rows': parser.rows, 'pages': 1, 'ocr_pages': 0}


def _read_csv(data):
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t|')
    except csv.Error:
        dialect = csv.excel
    rows = [line for line in (_join(r) for r in csv.reader(io.StringIO(text), dialect)) if line]
    return {'source': 'csv', 'rows': rows, 'pages': 1, 'ocr_pages': 0}


def read(data, filename):
    """`{source, rows, pages, ocr_pages}` for an uploaded proof.

    `source`: pdf-text | ocr | pdf-text+ocr | excel | csv. Raises
    `ProofUnreadable` (the caller's 400) or `OcrUnavailable` (503).
    """
    if not data:
        raise ProofUnreadable('The file is empty.')
    if len(data) > MAX_BYTES:
        raise ProofUnreadable(f'The file is over {MAX_BYTES // (1024 * 1024)} MB.')
    name = filename or 'upload'
    ext = PurePath(name).suffix.lower()
    head = data[:8]

    if head.startswith(b'%PDF-'):
        return _read_pdf(data, name)
    if head.startswith(b'\xd0\xcf\x11\xe0'):  # the old binary .xls
        return _read_xls(data)
    if head.startswith(b'PK') and ext in ('.xlsx', '.xlsm', ''):
        return _read_xlsx(data)
    sniff = data[:2048].lstrip().lower()
    if sniff.startswith(b'<') and b'<table' in data[:200_000].lower():
        # Several banks' "Excel" download is an HTML table named .xls.
        return _read_html(data)
    if ext in ('.csv', '.txt'):
        return _read_csv(data)
    if ext in IMAGE_EXTENSIONS or head.startswith((b'\x89PNG', b'\xff\xd8', b'GIF8', b'RIFF', b'BM', b'II*', b'MM*')):
        rows = _ocr(data, name, 'application/octet-stream')
        return {'source': 'ocr', 'rows': rows, 'pages': 1, 'ocr_pages': 1}
    if ext in ('.heic', '.heif'):
        raise ProofUnreadable(
            'iPhone HEIC photos cannot be read. Share it as a JPEG (or take a screenshot) and upload that.')
    raise ProofUnreadable('Upload a PDF, an Excel or CSV file, or a photo (JPEG / PNG).')
