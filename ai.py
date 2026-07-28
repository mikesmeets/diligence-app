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
