"""
Working out what quarter a transcript is, and when the call happened.

Filenames are the unreliable source here — plenty of them are just
"Transcript.pdf", and even the descriptive ones often carry the quarter but no
date. The text almost always opens with both, in some variation of:

    Copart, Inc. (CPRT) Q3 2025 Earnings Call Transcript
    May 22, 2025 11:00 AM ET

So the filename is read first because it's free, and anything still missing is
filled from the opening pages of the document itself.
"""
import io
import logging
import re
import zipfile

MONTHS = {m: i for i, m in enumerate(
    ['january', 'february', 'march', 'april', 'may', 'june', 'july',
     'august', 'september', 'october', 'november', 'december'], start=1)}
for _abbr, _n in list(MONTHS.items()):
    MONTHS[_abbr[:3]] = _n

ORDINALS = {'first': 1, 'second': 2, 'third': 3, 'fourth': 4}

# Enough to cover a cover page and the opening operator remarks.
TEXT_BUDGET = 8000


# ── Text extraction ─────────────────────────────────────────────────────────

def _pdf_text(raw):
    try:
        from pypdf import PdfReader
    except ImportError:
        return ''
    # A file that isn't really a PDF is an expected outcome here — we fall back
    # to a plain decode — so don't let pypdf log a warning for each one.
    logging.getLogger('pypdf').setLevel(logging.ERROR)
    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception:
        return ''
    out = []
    # The header is on page one; a couple more covers title pages and
    # disclaimers that push the real heading down.
    for page in reader.pages[:3]:
        try:
            out.append(page.extract_text() or '')
        except Exception:
            continue
        if sum(len(p) for p in out) >= TEXT_BUDGET:
            break
    return '\n'.join(out)[:TEXT_BUDGET]


def _docx_text(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml = zf.read('word/document.xml').decode('utf-8', 'replace')
    except Exception:
        return ''
    # Paragraph breaks first, then drop every remaining tag.
    xml = re.sub(r'</w:p>', '\n', xml)
    return re.sub(r'<[^>]+>', ' ', xml)[:TEXT_BUDGET]


def extract_text(raw, filename=''):
    """Opening text of a transcript, or '' if the format isn't readable."""
    if not raw:
        return ''
    low = (filename or '').lower()
    text = ''
    if low.endswith('.pdf') or raw[:5] == b'%PDF-':
        text = _pdf_text(raw)
    elif low.endswith(('.docx', '.doc')) or raw[:2] == b'PK':
        text = _docx_text(raw)
    if text.strip():
        return text
    # Fall through to a plain decode. A scanned PDF yields nothing here either,
    # but a mislabelled text file — or a .pdf that is really text — still reads,
    # and the alternative is silently giving up on the whole document.
    try:
        return raw[:TEXT_BUDGET * 2].decode('utf-8', 'replace')[:TEXT_BUDGET]
    except Exception:
        return ''


def full_text(raw, filename='', limit=400_000):
    """The whole document, for summarising rather than just identifying it.

    A long call runs perhaps 60k characters, so the limit is a runaway guard
    rather than a real constraint — Opus 5's context swallows a full transcript
    comfortably.
    """
    if not raw:
        return ''
    low = (filename or '').lower()
    if low.endswith('.pdf') or raw[:5] == b'%PDF-':
        try:
            from pypdf import PdfReader
            logging.getLogger('pypdf').setLevel(logging.ERROR)
            reader = PdfReader(io.BytesIO(raw))
            pages = []
            for page in reader.pages:
                try:
                    pages.append(page.extract_text() or '')
                except Exception:
                    continue
                if sum(len(p) for p in pages) >= limit:
                    break
            text = '\n'.join(pages)
            if text.strip():
                return text[:limit]
        except Exception:
            pass
    elif low.endswith(('.docx', '.doc')) or raw[:2] == b'PK':
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                xml = zf.read('word/document.xml').decode('utf-8', 'replace')
            xml = re.sub(r'</w:p>', '\n', xml)
            text = re.sub(r'<[^>]+>', ' ', xml)
            if text.strip():
                return text[:limit]
        except Exception:
            pass
    try:
        return raw[:limit].decode('utf-8', 'replace')
    except Exception:
        return ''


# ── Field parsing ───────────────────────────────────────────────────────────

def _find_quarter(text):
    low = text.lower()
    # F3Q25 / 3QFY25 style comes first: the bare-digit patterns below would
    # otherwise match the wrong half of it.
    if m := re.search(r'\bf?([1-4])\s*q\s*(?:fy)?\s*\d{2,4}\b', low):
        return int(m.group(1))
    if m := re.search(r'\bq\s*([1-4])\b', low):
        return int(m.group(1))
    if m := re.search(r'\b([1-4])\s*q\b', low):
        return int(m.group(1))
    if m := re.search(r'\b(first|second|third|fourth)[\s-]+quarter\b', low):
        return ORDINALS[m.group(1)]
    return None


def _find_dates(text):
    """Every date in the text, as (position, YYYY-MM-DD), in order."""
    found = []

    for m in re.finditer(r'\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b', text):
        y, mo, d = (int(g) for g in m.groups())
        if 1 <= mo <= 12 and 1 <= d <= 31:
            found.append((m.start(), f'{y:04d}-{mo:02d}-{d:02d}'))

    # "May 22, 2025" and "22 May 2025".
    for m in re.finditer(r'\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b', text):
        mo = MONTHS.get(m.group(1).lower())
        if mo and 1 <= int(m.group(2)) <= 31:
            found.append((m.start(), f'{int(m.group(3)):04d}-{mo:02d}-{int(m.group(2)):02d}'))
    for m in re.finditer(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(20\d{2})\b', text):
        mo = MONTHS.get(m.group(2).lower())
        if mo and 1 <= int(m.group(1)) <= 31:
            found.append((m.start(), f'{int(m.group(3)):04d}-{mo:02d}-{int(m.group(1)):02d}'))

    # US numeric dates last, so an unambiguous form above wins the same span.
    for m in re.finditer(r'\b(\d{1,2})/(\d{1,2})/(20\d{2})\b', text):
        mo, d, y = (int(g) for g in m.groups())
        if 1 <= mo <= 12 and 1 <= d <= 31:
            found.append((m.start(), f'{y:04d}-{mo:02d}-{d:02d}'))

    found.sort()
    seen, out = set(), []
    for pos, iso in found:
        if iso not in seen:
            seen.add(iso)
            out.append((pos, iso))
    return out


def _find_call_date(text):
    """The date of the call, preferring one sitting near a 'call' mention."""
    dates = _find_dates(text)
    if not dates:
        return None

    anchors = [m.start() for m in re.finditer(
        r'earnings call|conference call|earnings conference|call transcript',
        text, re.I)]
    if anchors:
        # A transcript header puts the date within a line or two of the title.
        best, best_gap = None, None
        for pos, iso in dates:
            gap = min(abs(pos - a) for a in anchors)
            if best_gap is None or gap < best_gap:
                best, best_gap = iso, gap
        if best_gap is not None and best_gap <= 400:
            return best
    return dates[0][1]


def _find_fiscal_year(text, call_date, quarter):
    low = text.lower()
    if m := re.search(r'\bf(?:y|iscal)\s*(?:year\s*)?\'?(\d{2,4})\b', low):
        raw = int(m.group(1))
        return raw if raw > 100 else 2000 + raw
    if quarter and (m := re.search(r'\bq\s*[1-4]\s*,?\s*(?:fy)?\s*(20\d{2})\b', low)):
        return int(m.group(1))
    # "F3Q25" / "3QFY25": no word boundary sits between the 'f' and the digit,
    # so the leading f has to be part of the pattern rather than before it.
    if m := re.search(r'\bf?([1-4])\s*q\s*(?:fy)?\s*(\d{2,4})\b', low):
        raw_year = int(m.group(2))
        return raw_year if raw_year > 100 else 2000 + raw_year
    # A bare year, but never one that is only there as part of the call date.
    masked = text.replace(call_date, ' ') if call_date else text
    if m := re.search(r'\b(20\d{2})\b', masked):
        return int(m.group(1))
    return int(call_date[:4]) if call_date else None


def parse(filename='', raw=None):
    """Quarter, fiscal year and call date — filename first, then the text.

    Each field is resolved independently: a filename that names the quarter but
    no date contributes the quarter and lets the document supply the date.
    """
    stem = re.sub(r'\.[A-Za-z0-9]{1,5}$', '', filename or '')
    from_name = stem.replace('_', ' ').replace('.', ' ')

    quarter = _find_quarter(from_name)
    call_date = _find_call_date(from_name)
    fiscal_year = _find_fiscal_year(from_name, call_date, quarter) if from_name.strip() else None
    source = 'filename' if (quarter or call_date) else None

    if raw is not None and (quarter is None or call_date is None):
        text = extract_text(raw, filename)
        if text.strip():
            if quarter is None:
                quarter = _find_quarter(text)
            if call_date is None:
                call_date = _find_call_date(text)
            # Recompute from the document when the filename gave nothing useful.
            if from_name.strip() == '' or fiscal_year is None or source is None:
                fiscal_year = _find_fiscal_year(text, call_date, quarter) or fiscal_year
            source = 'filename + document' if source else 'document'

    return {
        'fiscal_quarter': quarter,
        'fiscal_year': fiscal_year,
        'call_date': call_date,
        'source': source,
    }


def title(ticker, quarter, fiscal_year, call_date):
    """{Ticker} - {Q}{YY} - {YYYY-MM-DD}, with unknown parts left as dashes."""
    tick = (ticker or '—').upper()
    if quarter and fiscal_year:
        period = f'Q{int(quarter)}{int(fiscal_year) % 100:02d}'
    elif fiscal_year:
        period = f'FY{int(fiscal_year) % 100:02d}'
    else:
        period = '—'
    return f'{tick} - {period} - {call_date or "—"}'
