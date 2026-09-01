"""
Getting ideas in by email.

Two front doors onto one pipeline:

  webhook  a mail service (Mailgun, SendGrid, Postmark, CloudMailin) POSTs each
           message as it arrives. Instant, and the app never holds mailbox
           credentials — but it needs a domain with MX records pointed at the
           service.
  IMAP     the app polls a mailbox every few minutes. No DNS to set up, works
           the moment credentials are saved.

Both normalise to the same dict and go through the same parse-and-stage path, so
you can start on one and move to the other without anything downstream noticing.

Nothing here writes to `ideas` directly. Email is untrusted input: it is staged,
parsed, and only becomes an idea once it clears the confidence bar or a human
presses the button.
"""
import email
import email.policy
import email.utils
import imaplib
import logging
import os
import re
import secrets
from datetime import datetime

import db

log = logging.getLogger(__name__)

# Bodies get truncated well before this; the cap is a runaway guard on a mail
# with a giant HTML payload.
MAX_BODY = 200_000
MAX_ATTACHMENT_MB = 25


# ── Webhook credential ───────────────────────────────────────────────────────

def token():
    """Shared secret the webhook must present. Generated on first use."""
    value = os.environ.get('INBOX_TOKEN') or db.get_setting('inbox_token')
    if not value:
        value = secrets.token_urlsafe(32)
        db.set_setting('inbox_token', value)
    return value


def token_source():
    return 'env' if os.environ.get('INBOX_TOKEN') else 'settings'


def rotate_token():
    """Issue a new secret, invalidating the old webhook URL."""
    if os.environ.get('INBOX_TOKEN'):
        raise RuntimeError(
            'INBOX_TOKEN is set as an environment variable, so it has to be changed there.')
    value = secrets.token_urlsafe(32)
    db.set_setting('inbox_token', value)
    return value


def token_ok(supplied):
    """Constant-time compare, so a wrong token can't be found by timing it."""
    return bool(supplied) and secrets.compare_digest(str(supplied), token())


# ── Normalising what the providers send ──────────────────────────────────────

def _clean(text):
    return (text or '').strip()


def _strip_html(html):
    """Plain text out of an HTML body — good enough to summarise from."""
    if not html:
        return ''
    text = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', html)
    text = re.sub(r'(?i)<br\s*/?>|</p>|</div>|</tr>', '\n', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    for entity, char in (('&nbsp;', ' '), ('&amp;', '&'), ('&lt;', '<'),
                         ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'")):
        text = text.replace(entity, char)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _message_id(raw, fallback_parts):
    """Prefer the real Message-ID; otherwise derive something stable."""
    if raw and raw.strip():
        return raw.strip()[:400]
    import hashlib
    seed = '|'.join(str(p or '') for p in fallback_parts)
    return 'derived:' + hashlib.sha256(seed.encode('utf-8', 'replace')).hexdigest()[:32]


def normalize_webhook(form, files, json_body):
    """One email dict from whichever provider shape arrived.

    Providers disagree on nearly every field name, so each known shape is read
    explicitly and a generic fallback covers the rest. Unknown keys are ignored
    rather than guessed at.
    """
    data = json_body if isinstance(json_body, dict) else {}
    f = form or {}

    def pick(*names, source=None):
        src = source if source is not None else f
        for n in names:
            if n in src and _clean(str(src.get(n))):
                return _clean(str(src.get(n)))
        return ''

    attachments = []

    if data:
        # Postmark and CloudMailin both post JSON, with different field names.
        sender  = pick('From', 'from', source=data) or pick('sender', source=data)
        to_addr = pick('To', 'to', source=data)
        subject = pick('Subject', 'subject', source=data)
        body    = (pick('TextBody', 'plain', 'text', 'body-plain', source=data)
                   or _strip_html(pick('HtmlBody', 'html', 'body-html', source=data)))
        msg_id  = pick('MessageID', 'MessageId', 'message_id', source=data)

        if isinstance(data.get('envelope'), dict):          # CloudMailin
            sender  = sender or _clean(data['envelope'].get('from'))
            to_addr = to_addr or _clean(data['envelope'].get('to'))
        if isinstance(data.get('headers'), dict):
            msg_id  = msg_id or _clean(data['headers'].get('Message-ID'))
            subject = subject or _clean(data['headers'].get('Subject'))

        import base64
        for att in (data.get('Attachments') or data.get('attachments') or []):
            if not isinstance(att, dict):
                continue
            name = _clean(att.get('Name') or att.get('file_name') or att.get('filename'))
            content = att.get('Content') or att.get('content')
            if not name or not content:
                continue
            try:
                attachments.append((name, base64.b64decode(content)))
            except Exception:
                log.info('Skipped an attachment that would not decode: %s', name)
    else:
        # Mailgun and SendGrid Inbound Parse both post multipart form fields.
        sender  = pick('sender', 'from', 'From')
        to_addr = pick('recipient', 'to', 'To')
        subject = pick('subject', 'Subject')
        body    = (pick('body-plain', 'text', 'stripped-text')
                   or _strip_html(pick('body-html', 'html')))
        msg_id  = pick('Message-Id', 'message-id', 'Message-ID')
        for key in (files or {}):
            for fh in files.getlist(key):
                if fh and fh.filename:
                    attachments.append((fh.filename, fh.read()))

    body = body[:MAX_BODY]
    return {
        'message_id':  _message_id(msg_id, (sender, subject, body[:500])),
        'origin':      'webhook',
        'from_addr':   sender[:400],
        'to_addr':     to_addr[:400],
        'subject':     subject[:800],
        'body':        body,
        'received_at': datetime.now().isoformat(),
        'attachments': attachments,
    }


# ── IMAP ─────────────────────────────────────────────────────────────────────

def imap_config():
    """Mailbox settings. Environment wins, same rule as every other credential."""
    def get(name, key, default=''):
        return os.environ.get(name) or db.get_setting(key) or default

    return {
        'host':     get('INBOX_IMAP_HOST', 'imap_host'),
        'port':     int(get('INBOX_IMAP_PORT', 'imap_port', '993') or 993),
        'user':     get('INBOX_IMAP_USER', 'imap_user'),
        'password': get('INBOX_IMAP_PASSWORD', 'imap_password'),
        'folder':   get('INBOX_IMAP_FOLDER', 'imap_folder', 'INBOX'),
    }


def imap_ready():
    c = imap_config()
    return bool(c['host'] and c['user'] and c['password'])


def imap_source():
    return 'env' if os.environ.get('INBOX_IMAP_PASSWORD') else (
        'settings' if db.get_setting('imap_password') else None)


def _body_and_attachments(msg):
    text, html, attachments = '', '', []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or '')
        ctype = part.get_content_type()
        if disposition == 'attachment':
            name = part.get_filename()
            if not name:
                continue
            try:
                payload = part.get_payload(decode=True) or b''
            except Exception:
                continue
            if len(payload) <= MAX_ATTACHMENT_MB * 1024 * 1024:
                attachments.append((name, payload))
            continue
        try:
            content = part.get_content()
        except Exception:
            continue
        if ctype == 'text/plain':
            text += content
        elif ctype == 'text/html':
            html += content
    return (text or _strip_html(html))[:MAX_BODY], attachments


def fetch_imap(limit=25, mark_seen=True):
    """Unread mail from the configured folder, normalised like the webhook.

    Marking as seen is what stops the same message being processed forever; the
    message-id index is the backstop for when it doesn't stick.
    """
    cfg = imap_config()
    if not (cfg['host'] and cfg['user'] and cfg['password']):
        raise RuntimeError('IMAP is not configured. Add the mailbox details on the Admin page.')

    out = []
    conn = imaplib.IMAP4_SSL(cfg['host'], cfg['port'])
    try:
        conn.login(cfg['user'], cfg['password'])
        conn.select(cfg['folder'])
        status, data = conn.search(None, 'UNSEEN')
        if status != 'OK':
            return out

        ids = (data[0] or b'').split()[:limit]
        for num in ids:
            status, payload = conn.fetch(num, '(RFC822)')
            if status != 'OK' or not payload or not payload[0]:
                continue
            msg = email.message_from_bytes(payload[0][1], policy=email.policy.default)
            body, attachments = _body_and_attachments(msg)

            received = msg.get('Date')
            try:
                received_at = email.utils.parsedate_to_datetime(received).isoformat()
            except Exception:
                received_at = datetime.now().isoformat()

            out.append({
                'message_id':  _message_id(msg.get('Message-ID'),
                                           (msg.get('From'), msg.get('Subject'), body[:500])),
                'origin':      'imap',
                'from_addr':   _clean(msg.get('From'))[:400],
                'to_addr':     _clean(msg.get('To'))[:400],
                'subject':     _clean(msg.get('Subject'))[:800],
                'body':        body,
                'received_at': received_at,
                'attachments': attachments,
            })
            if mark_seen:
                conn.store(num, '+FLAGS', '\\Seen')
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return out


def test_imap():
    """Prove the mailbox answers, and say how much is waiting."""
    cfg = imap_config()
    if not (cfg['host'] and cfg['user'] and cfg['password']):
        raise RuntimeError('IMAP is not configured.')
    conn = imaplib.IMAP4_SSL(cfg['host'], cfg['port'])
    try:
        conn.login(cfg['user'], cfg['password'])
        status, _ = conn.select(cfg['folder'], readonly=True)
        if status != 'OK':
            raise RuntimeError(f'Signed in, but the folder "{cfg["folder"]}" could not be opened.')
        status, data = conn.search(None, 'UNSEEN')
        unseen = len((data[0] or b'').split()) if status == 'OK' else 0
        return {'host': cfg['host'], 'user': cfg['user'],
                'folder': cfg['folder'], 'unseen': unseen}
    finally:
        try:
            conn.logout()
        except Exception:
            pass
