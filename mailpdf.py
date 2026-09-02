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


def render(mail, attachments=()):
    """PDF bytes for one email. `attachments` is a list of (filename, raw)."""
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
        'Note':  ParagraphStyle('N', parent=base['Normal'], fontSize=8.5, leading=12,
                                textColor='#8a6d3b'),
    }

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=_safe(mail.get('subject') or 'Email'),
        author=_safe(mail.get('from_addr') or ''),
    )

    story = [Paragraph(_escape(mail.get('subject') or '(no subject)'), styles['Title'])]

    meta = []
    for label, key in (('From', 'from_addr'), ('To', 'to_addr'),
                       ('Received', 'received_at'), ('Message-ID', 'message_id')):
        value = mail.get(key)
        if value:
            meta.append(f'<b>{label}:</b> {_escape(value)}')
    if meta:
        story.append(Paragraph('<br/>'.join(meta), styles['Meta']))
    story.append(Spacer(1, 14))

    story += _body_flowables(mail.get('body'), styles)

    names = [n for n, _ in attachments]
    if names:
        story.append(Spacer(1, 10))
        story.append(Paragraph(
            '<b>Attachments on the original email:</b> ' + _escape(', '.join(names)),
            styles['Meta']))

    doc.build(story)
    pdf = buf.getvalue()

    merged = _append_pdfs(pdf, attachments)
    return merged or pdf


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


def filename_for(mail, ticker=None):
    """A readable name for the stored object, matching the bucket's conventions."""
    day = (mail.get('received_at') or datetime.now().isoformat())[:10]
    subject = re.sub(r'[^\w\s-]', '', _safe(mail.get('subject') or 'email')).strip()
    subject = re.sub(r'\s+', ' ', subject)[:60] or 'email'
    label = ' '.join(p for p in ((ticker or '').upper(), day) if p)
    return f'{label} {subject}.pdf'.strip()
