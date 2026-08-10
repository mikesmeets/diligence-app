"""
Claude-backed drafting for project write-ups.

Credentials: the ANTHROPIC_API_KEY environment variable wins if set, and the
settings table is the fallback. That order is deliberate — a key provisioned by
the host can't be overridden from the admin page, and on Railway you can set the
variable and never store a key in the database at all.

Every generation returns a summary (what the project page shows) and a detail
(the full write-up on its own page), enforced with structured outputs rather
than parsed out of prose.
"""
import json
import logging
import os

import db

DEFAULT_MODEL = 'claude-opus-5'

MODELS = [
    ('claude-opus-5',   'Claude Opus 5 — most capable'),
    ('claude-sonnet-5', 'Claude Sonnet 5 — faster, cheaper'),
    ('claude-haiku-4-5', 'Claude Haiku 4.5 — fastest, least capable'),
]

FIELDS = {
    'business_description': 'Business Description',
    'bull_case':            'Bull Case',
    'bear_case':            'Bear Case',
}

# The summary/detail contract lives here rather than in the editable prompts, so
# rewriting a prompt in the admin page can't break the response shape.
SYSTEM_PROMPT = """You are assisting a professional investor with their own primary research. \
They are building a view, not asking you for one.

Return two things:

- summary: two to four sentences carrying the single most important idea. This is \
what shows on the project page at a glance, so lead with the conclusion rather than \
building up to it.
- detail: the full write-up in Markdown. This is the page the user opens when they \
want the whole argument. Use headings and short paragraphs; prefer specifics over \
adjectives. Headings, lists, bold, blockquotes and GFM pipe tables all render — \
put figures in a table when you are showing several of them side by side (scenarios, \
a build-up, year-on-year), and keep prose in prose. Tables render with numbers \
right-aligned automatically, so alignment markers are optional.

Ground what you can in the reference data supplied. Where you are drawing on your own \
knowledge rather than that data, say so. Where you don't know, say that plainly instead \
of guessing — an acknowledged gap is far more useful here than a confident wrong number, \
because the user will act on this.

You are not a licensed financial adviser and this is not investment advice. You are \
helping the user develop and stress-test their own thinking."""

DEFAULT_PROMPTS = {
    'business_description': """\
Write a business description of {name} ({ticker}) for a professional investor who knows \
the market but not this specific company.

Cover:
- What the company actually sells, and to whom.
- How revenue splits across segments, geographies, and customer types.
- The unit economics — what a marginal dollar of revenue costs to serve, and what drives \
gross and operating margin.
- Where it sits in its value chain, and who its real competitors are (not just the \
obvious listed peers).
- The two or three variables that most determine whether it earns its cost of capital.

Be concrete. Use figures where you're confident in them and flag where you aren't. Don't \
editorialise about whether the stock is attractive — that's not the job here.

Reference data:
{context}""",

    'bull_case': """\
Build the strongest honest bull case for {name} ({ticker}).

Argue it as its most credible advocate would, not as a promoter. Set out what has to go \
right, why that's plausible rather than merely possible, and what it's worth if it \
happens. Be explicit about the mechanism: which line item moves, by how much, over what \
period.

Anchor to numbers where you can — a revenue and margin trajectory, an exit multiple, and \
the resulting value with the arithmetic shown. State your assumptions plainly so the user \
can argue with them.

Close by naming the single most important thing that would have to be true for this case \
to work, and what would confirm it early.

Reference data:
{context}""",

    'bear_case': """\
Build the strongest honest bear case for {name} ({ticker}).

Argue it as a thoughtful short seller would, not as a doomsayer. Set out what breaks, why \
it's more likely than the market is assuming, and where the stock trades if it happens. \
Distinguish clearly between a thesis-killing structural problem and an ordinary cyclical \
setback — they justify very different position sizes.

Anchor to numbers where you can — the earnings or cash flow a downside scenario implies, \
the multiple that would then apply, and the resulting downside with the arithmetic shown. \
State your assumptions plainly.

Close by naming the single most important thing that would have to be true for this case \
to work, and what evidence would tell the user early that it's happening.

Reference data:
{context}""",
}

_SCHEMA = {
    'type': 'object',
    'properties': {
        'summary': {
            'type': 'string',
            'description': 'Two to four sentences. Leads with the conclusion.',
        },
        'detail': {
            'type': 'string',
            'description': 'The full write-up in Markdown.',
        },
    },
    'required': ['summary', 'detail'],
    'additionalProperties': False,
}


class NotConfigured(Exception):
    """No API key available from either the environment or settings."""


class Refused(Exception):
    """Claude's safety classifiers declined the request."""


def api_key():
    return os.environ.get('ANTHROPIC_API_KEY') or db.get_setting('anthropic_api_key') or ''


def key_source():
    """Where the active key comes from — surfaced in the admin page, never the key."""
    if os.environ.get('ANTHROPIC_API_KEY'):
        return 'env'
    if db.get_setting('anthropic_api_key'):
        return 'settings'
    return None


def enabled():
    return bool(api_key())


def model():
    return db.get_setting('ai_model') or DEFAULT_MODEL


# Effort trades depth against latency. Opus 5 is unusually strong at the lower
# levels, so dropping to medium is the first thing to try if drafts time out.
EFFORTS = [
    ('high',   'High — most thorough (slowest)'),
    ('medium', 'Medium — good quality, noticeably faster'),
    ('low',    'Low — quick first pass'),
]


def effort():
    value = db.get_setting('ai_effort')
    return value if value in dict(EFFORTS) else 'high'


def prompt_for(field):
    return db.get_setting(f'prompt_{field}') or DEFAULT_PROMPTS[field]


def _render(template, project, context):
    """Substitute the handful of tokens a prompt may use, tolerating unknown braces."""
    out = template
    for token, value in (
        ('{ticker}',    project.get('ticker') or '—'),
        ('{name}',      project.get('name') or ''),
        ('{direction}', project.get('direction') or 'undecided'),
        ('{context}',   context),
    ):
        out = out.replace(token, str(value))
    return out


def generate(field, project, context):
    """Draft one write-up. Returns {'summary': ..., 'detail': ...}."""
    if field not in FIELDS:
        raise ValueError(f'unknown field: {field}')

    key = api_key()
    if not key:
        raise NotConfigured(
            'No Anthropic API key. Set the ANTHROPIC_API_KEY environment variable, '
            'or add a key on the Admin page.'
        )

    import anthropic  # imported lazily so the app still boots without the package

    client = anthropic.Anthropic(api_key=key)
    request = {
        'model':      model(),
        'max_tokens': 16000,
        'system':     SYSTEM_PROMPT,
        'messages':   [{'role': 'user', 'content': _render(prompt_for(field), project, context)}],
        'output_config': {
            'format': {'type': 'json_schema', 'schema': _SCHEMA},
            'effort': effort(),
        },
    }

    message = _send(client, request)

    if getattr(message, 'stop_reason', None) == 'refusal':
        raise Refused(
            "Claude declined to answer this one. That's usually a false positive on "
            'benign financial work — rephrasing the prompt on the Admin page normally clears it.'
        )

    text = next((b.text for b in message.content if b.type == 'text'), '')
    if not text:
        raise RuntimeError('Claude returned no text.')

    data = json.loads(text)
    return {'summary': (data.get('summary') or '').strip(),
            'detail':  (data.get('detail')  or '').strip()}


# Whether this account/SDK accepts the server-side fallback beta. Set False on
# the first rejection so we stop paying a wasted 400 round-trip per generation.
_fallbacks_supported = True


def _send(client, request):
    """Stream the request, preferring server-side fallbacks where they're accepted.

    Streaming avoids HTTP timeouts on the large max_tokens these write-ups need —
    on Opus 5 that budget covers thinking as well as the response. Fallbacks re-run
    a safety-declined request on another model server-side; not every account or
    SDK build accepts the parameter, so we degrade to a plain call rather than fail.
    """
    global _fallbacks_supported

    if _fallbacks_supported:
        try:
            with client.beta.messages.stream(
                betas=['server-side-fallback-2026-07-01'],
                fallbacks='default',
                **request,
            ) as stream:
                return stream.get_final_message()
        except Exception as exc:
            if not _is_unsupported_param(exc):
                raise
            _fallbacks_supported = False
            logging.info(
                'Server-side fallbacks rejected (%s) — using plain requests from now on.',
                type(exc).__name__,
            )

    with client.messages.stream(**request) as stream:
        return stream.get_final_message()


def _is_unsupported_param(exc):
    """True when the failure looks like this SDK/account not knowing the fallback beta."""
    if isinstance(exc, TypeError):
        return True
    text = str(exc).lower()
    return 'fallback' in text or 'beta' in text or 'unexpected keyword' in text


# ── Earnings transcripts ─────────────────────────────────────────────────────
#
# Two passes. Each call is summarised on its own, then the trends pass reads
# those summaries rather than the raw transcripts: it keeps the cross-call
# request small however many quarters you hold, and it keeps the trends
# consistent with the per-call cards a reader sees underneath them.

SENTIMENTS = ['Bullish', 'Mixed', 'Bearish', 'Neutral']

CALL_SYSTEM_PROMPT = """You are assisting a professional investor reading earnings \
call transcripts to build a view of a business over time.

Summarise what management actually said and what changed versus prior quarters. Be \
specific: name products, segments, figures and guidance where the transcript gives \
them. Prefer a concrete number to an adjective. Do not speculate beyond the \
transcript, and do not give investment advice — the reader forms their own view.

Where the transcript does not support a field, leave it empty rather than guessing. \
Executive names must come from the transcript's speaker list, not from memory."""

CALL_PROMPT = """Summarise this earnings call for {name} ({ticker}).

Fiscal period: {period}
Call date: {call_date}
Share price reaction around the call: {reaction}
Move since the prior call: {between}

Write:
- headline: the quarter plus a short phrase capturing what made this call matter,
  in the style "Q3 2024 - Guidance Cut on Creator Churn". Under 70 characters.
- sentiment: one of Bullish, Mixed, Bearish, Neutral - management's tone and the
  substance of the results together, not the share price reaction.
- ceo / cfo / ir: names of the speakers holding those roles on this call, taken from
  the speaker list. Empty string if not identifiable.
- summary: one paragraph on what the quarter was about and why it landed the way it
  did. 90-140 words.
- highlights: one paragraph of the specific disclosures worth remembering - figures,
  guidance, segment detail, product news. 90-140 words.
- themes: 3-5 short tags of two or three words each, e.g. "Creator Churn",
  "Margin Expansion".

TRANSCRIPT
{transcript}"""

TRENDS_PROMPT = """Below are summaries of {count} consecutive earnings calls for \
{name} ({ticker}), oldest first, each with the share price reaction to that call.

Identify what the business looks like across the whole span - the arcs that only show \
up when the quarters are read together. Judge trends by what management said and how \
the numbers moved, and note where the two diverged.

Write:
- trends: 4-7 cards. Each has a title that makes a claim rather than naming a topic
  ("Marketplace Pivot Traded Growth For Creator Attrition", not "Marketplace"), a
  tone of good, warn or bad from the shareholder's point of view, and a body of
  70-130 words citing the specific quarters that show it.
- milestones: 6-12 entries in chronological order, each with a period (e.g.
  "Q4 2023 - February 2024"), a title, and a 40-90 word description. Cover the
  turning points: strategy changes, management changes, the largest price reactions,
  and the quarters where the trajectory visibly shifted.

CALL SUMMARIES
{summaries}"""

_CALL_SCHEMA = {
    'type': 'object',
    'properties': {
        'headline':   {'type': 'string'},
        'sentiment':  {'type': 'string', 'enum': SENTIMENTS},
        'ceo':        {'type': 'string'},
        'cfo':        {'type': 'string'},
        'ir':         {'type': 'string'},
        'summary':    {'type': 'string'},
        'highlights': {'type': 'string'},
        'themes':     {'type': 'array', 'items': {'type': 'string'}},
    },
    'required': ['headline', 'sentiment', 'summary', 'highlights', 'themes'],
    'additionalProperties': False,
}

_TRENDS_SCHEMA = {
    'type': 'object',
    'properties': {
        'trends': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string'},
                    'tone':  {'type': 'string', 'enum': ['good', 'warn', 'bad']},
                    'body':  {'type': 'string'},
                },
                'required': ['title', 'tone', 'body'],
                'additionalProperties': False,
            },
        },
        'milestones': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'period':      {'type': 'string'},
                    'title':       {'type': 'string'},
                    'description': {'type': 'string'},
                },
                'required': ['period', 'title', 'description'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['trends', 'milestones'],
    'additionalProperties': False,
}


def call_prompt():
    return db.get_setting('prompt_transcript_call') or CALL_PROMPT


def trends_prompt():
    return db.get_setting('prompt_transcript_trends') or TRENDS_PROMPT


def _structured(prompt, system, schema, max_tokens=16000):
    """One structured-output call, returning the parsed object."""
    key = api_key()
    if not key:
        raise NotConfigured(
            'No Anthropic API key. Set the ANTHROPIC_API_KEY environment variable, '
            'or add a key on the Admin page.'
        )

    import anthropic  # imported lazily so the app still boots without the package

    client = anthropic.Anthropic(api_key=key)
    message = _send(client, {
        'model':      model(),
        'max_tokens': max_tokens,
        'system':     system,
        'messages':   [{'role': 'user', 'content': prompt}],
        'output_config': {
            'format': {'type': 'json_schema', 'schema': schema},
            'effort': effort(),
        },
    })

    if getattr(message, 'stop_reason', None) == 'refusal':
        raise Refused(
            "Claude declined to answer this one. That's usually a false positive on "
            'benign financial work — rephrasing the prompt on the Admin page normally clears it.'
        )
    text = next((b.text for b in message.content if b.type == 'text'), '')
    if not text:
        raise RuntimeError('Claude returned no text.')
    return json.loads(text)


def _fill(template, pairs):
    out = template
    for token, value in pairs:
        out = out.replace(token, str(value))
    return out


def summarize_call(project, meta, transcript):
    """Summarise one earnings call. `meta` carries the period and price context."""
    prompt = _fill(call_prompt(), (
        ('{name}',       project.get('name') or ''),
        ('{ticker}',     project.get('ticker') or '—'),
        ('{period}',     meta.get('period') or 'unknown'),
        ('{call_date}',  meta.get('call_date') or 'unknown'),
        ('{reaction}',   meta.get('reaction') or 'not available'),
        ('{between}',    meta.get('between') or 'not available'),
        ('{transcript}', transcript),
    ))

    data = _structured(prompt, CALL_SYSTEM_PROMPT, _CALL_SCHEMA)
    themes = [str(t).strip() for t in (data.get('themes') or []) if str(t).strip()]
    return {
        'headline':   (data.get('headline') or '').strip(),
        'sentiment':  data.get('sentiment') if data.get('sentiment') in SENTIMENTS else 'Neutral',
        'ceo':        (data.get('ceo') or '').strip(),
        'cfo':        (data.get('cfo') or '').strip(),
        'ir':         (data.get('ir') or '').strip(),
        'summary':    (data.get('summary') or '').strip(),
        'highlights': (data.get('highlights') or '').strip(),
        'themes':     themes[:6],
    }


def summarize_trends(project, calls):
    """Synthesise the arc across calls. `calls` is oldest-first summary blocks."""
    blocks = []
    for c in calls:
        blocks.append(
            f"### {c.get('period') or '?'} — call {c.get('call_date') or '?'}\n"
            f"Headline: {c.get('headline') or '—'}\n"
            f"Sentiment: {c.get('sentiment') or '—'}\n"
            f"Price reaction: {c.get('reaction') or 'n/a'}; "
            f"since prior call: {c.get('between') or 'n/a'}\n"
            f"Summary: {c.get('summary') or '—'}\n"
            f"Highlights: {c.get('highlights') or '—'}"
        )

    prompt = _fill(trends_prompt(), (
        ('{name}',      project.get('name') or ''),
        ('{ticker}',    project.get('ticker') or '—'),
        ('{count}',     len(calls)),
        ('{summaries}', '\n\n'.join(blocks)),
    ))

    data = _structured(prompt, CALL_SYSTEM_PROMPT, _TRENDS_SCHEMA, max_tokens=24000)
    return {
        'trends':     [t for t in (data.get('trends') or []) if t.get('title')],
        'milestones': [m for m in (data.get('milestones') or []) if m.get('title')],
    }
