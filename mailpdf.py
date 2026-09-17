"""
Turning a received email into a PDF to file against an idea.

The point is a self-contained artefact: months later the idea should carry the
email that produced it, without depending on the mailbox still existing or the
inbox row still being there.

Any PDFs that came attached to the email are appended as further pages, so one
file holds the whole thing. Attachments in other formats can't be merged, so
they are listed on the cover page by name rather than silently dropped.
"""
import io
import logging
import re
from datetime import datetime

log = logging.getLogger(__name__)

# reportlab's built-in fonts encode WinAnsi (cp1252), which covers smart quotes,
# dashes and accented Latin. Anything beyond that — CJK, emoji — has no glyph,
# so it is transliterated where there's an obvious equivalent and dropped where
# there isn't. Embedding a Unicode TTF would fix it at the cost of shipping a
# font; not worth it for what is almost always English prose.
_SUBSTITUTIONS = {
    '‘': "'", '’': "'", '‚': ',', '“': '"', '”': '"',
    '–': '-', '—': '-', '…': '...', ' ': ' ',
    '•': '-', '→': '->', '≥': '>=', '≤': '<=',
}

# Guard against one enormous attachment turning an idea into a 200 MB download.
MAX_MERGED_MB = 20


def _safe(text):
    """Text reportlab's core fonts can actually render."""
    out = str(text or '')
    for bad, good in _SUBSTITUTIONS.items():
        out = out.replace(bad, good)
    return out.encode('cp1252', 'replace').decode('cp1252')


def _escape(text):
    """Paragraph markup is XML-ish, so the three specials have to be escaped."""
    return (_safe(text).replace('&', '&amp;')
                       .replace('<', '&lt;')
                       .replace('>', '&gt;'))


def _body_flowables(body, styles):
    """Body text as paragraphs, keeping the blank-line structure of the mail."""
    from reportlab.platypus import Paragraph, Spacer

    out = []
    # Collapse runs of blank lines, then treat each block as a paragraph. Single
    # newlines inside a block become <br/> so quoted text and lists survive.
    for block in re.split(r'\n\s*\n', (body or '').strip()):
        block = block.strip()
        if not block:
            continue
        out.append(Paragraph(_escape(block).replace('\n', '<br/>'), styles['Body']))
        out.append(Spacer(1, 6))
    if not out:
        out.append(Paragraph('<i>(no body text)</i>', styles['Body']))
    return out


def _build(title, meta, body, footer=None, author=''):
    """Cover document: a title, a label/value block, then the body text."""
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    base = getSampleStyleSheet()
    styles = {
        'Title': ParagraphStyle('T', parent=base['Heading1'], fontSize=15, leading=19,
                                spaceAfter=4, alignment=TA_LEFT),
        'Meta':  ParagraphStyle('M', parent=base['Normal'], fontSize=8.5, leading=12,
                                textColor='#666666'),
        'Body':  ParagraphStyle('B', parent=base['Normal'], fontSize=10, leading=14.5),
    }

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=_safe(title or 'Document'), author=_safe(author),
    )

    story = [Paragraph(_escape(title or '(untitled)'), styles['Title'])]
    lines = [f'<b>{label}:</b> {_escape(value)}' for label, value in meta if value]
    if lines:
        story.append(Paragraph('<br/>'.join(lines), styles['Meta']))
    story.append(Spacer(1, 14))
    story += _body_flowables(body, styles)
    if footer:
        story.append(Spacer(1, 10))
        story.append(Paragraph(footer, styles['Meta']))

    doc.build(story)
    return buf.getvalue()


def render(mail, attachments=()):
    """PDF bytes for one email. `attachments` is a list of (filename, raw)."""
    names = [n for n, _ in attachments]
    footer = ('<b>Attachments on the original email:</b> ' + _escape(', '.join(names))
              if names else None)
    pdf = _build(
        mail.get('subject') or '(no subject)',
        (('From', mail.get('from_addr')), ('To', mail.get('to_addr')),
         ('Received', mail.get('received_at')), ('Message-ID', mail.get('message_id'))),
        mail.get('body'), footer=footer, author=mail.get('from_addr') or '',
    )
    return _append_pdfs(pdf, attachments) or pdf


def render_page(title, url, captured_at, text):
    """A text snapshot of a web page, with the address and capture time on top.

    This keeps the words, not the look: no images, styling or layout. That is
    the part worth having if the page later disappears, and it avoids running a
    headless browser on the server.
    """
    return _build(
        title or url,
        (('URL', url), ('Captured', captured_at)),
        text,
        footer=('Text captured from the page at the time the idea was saved. Images, '
                'charts and layout are not included; the link above is the original.'),
    )


def _append_pdfs(cover, attachments):
    """Append any attached PDFs after the cover. Returns None if nothing merged."""
    pdfs = [(n, raw) for n, raw in attachments
            if raw and (n.lower().endswith('.pdf') or raw[:5] == b'%PDF-')]
    if not pdfs:
        return None

    total = len(cover) + sum(len(r) for _, r in pdfs)
    if total > MAX_MERGED_MB * 1024 * 1024:
        log.info('Not merging attachments into the email PDF: %.1f MB is over the limit',
                 total / 1024 / 1024)
        return None

    try:
        from pypdf import PdfReader, PdfWriter
        logging.getLogger('pypdf').setLevel(logging.ERROR)

        writer = PdfWriter()
        for page in PdfReader(io.BytesIO(cover)).pages:
            writer.add_page(page)
        for name, raw in pdfs:
            try:
                for page in PdfReader(io.BytesIO(raw)).pages:
                    writer.add_page(page)
            except Exception:
                # An encrypted or malformed attachment shouldn't cost us the
                # cover page; it's still named on it either way.
                log.info('Could not merge attachment %s', name)
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception:
        log.exception('Merging attachments failed; filing the cover page alone')
        return None


def page_filename(title, ticker=None, day=None):
    """Snapshot name in the same shape as the email PDFs."""
    day = (day or datetime.now().isoformat())[:10]
    name = re.sub(r'[^\w\s-]', '', _safe(title or 'web page')).strip()
    name = re.sub(r'\s+', ' ', name)[:60] or 'web page'
    label = ' '.join(p for p in ((ticker or '').upper(), day) if p)
    return f'{label} {name}.pdf'.strip()


def filename_for(mail, ticker=None):
    """A readable name for the stored object, matching the bucket's conventions."""
    day = (mail.get('received_at') or datetime.now().isoformat())[:10]
    subject = re.sub(r'[^\w\s-]', '', _safe(mail.get('subject') or 'email')).strip()
    subject = re.sub(r'\s+', ' ', subject)[:60] or 'email'
    label = ' '.join(p for p in ((ticker or '').upper(), day) if p)
    return f'{label} {subject}.pdf'.strip()
