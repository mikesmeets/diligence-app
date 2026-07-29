"""
Stamp and read Excel custom document properties, to pair a model file with its
project automatically.

An .xlsx/.xlsm is a zip. Custom document properties live in docProps/custom.xml
and survive ordinary Excel edit-and-save, so a file stamped on upload still
carries its identity when you send it back weeks later.

Deliberately NOT using openpyxl: it parses and rewrites the whole workbook, and
silently drops charts, pivot tables, slicers and VBA that it doesn't model. A
financial model would come back damaged. Everything here is zip-level surgery —
every part other than the three we touch is copied through byte-for-byte.
"""
import io
import zipfile
from xml.etree import ElementTree as ET

PROJECT_ID   = 'DiligenceProjectId'
PROJECT_NAME = 'DiligenceProject'
VERSION      = 'DiligenceModelVersion'

_CUSTOM_PART = 'docProps/custom.xml'
_CONTENT_TYPES = '[Content_Types].xml'
_ROOT_RELS = '_rels/.rels'

_CUSTOM_CT = 'application/vnd.openxmlformats-officedocument.custom-properties+xml'
_CUSTOM_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/custom-properties'
_VT_NS     = 'http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes'
_REL_TYPE  = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties'

# The well-known fmtid every custom document property uses.
_FMTID = '{D5CDD505-2E9C-101B-9397-08002B2CF9AE}'

SUPPORTED_EXTENSIONS = ('.xlsx', '.xlsm', '.xltx', '.xltm')


def is_supported(filename: str) -> bool:
    return str(filename or '').lower().endswith(SUPPORTED_EXTENSIONS)


def _escape(text) -> str:
    return (str(text)
            .replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def read_properties(data: bytes) -> dict:
    """Return the file's custom document properties. {} for anything unreadable."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        if _CUSTOM_PART not in zf.namelist():
            return {}
        root = ET.fromstring(zf.read(_CUSTOM_PART))
    except Exception:
        return {}   # not a zip, not an Office file, or corrupt — nothing to read

    props = {}
    for prop in root:
        name = prop.get('name')
        if not name:
            continue
        props[name] = ''.join((child.text or '') for child in prop)
    return props


def read_project_id(data: bytes):
    """The project this file was stamped for, or None."""
    raw = read_properties(data).get(PROJECT_ID)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def stamp(data: bytes, properties: dict) -> bytes:
    """Return a copy of the workbook carrying `properties`.

    Returns the input unchanged if it isn't a readable Office zip, so callers can
    stamp unconditionally without worrying about what was uploaded.
    """
    try:
        src = zipfile.ZipFile(io.BytesIO(data))
        names = src.namelist()
        if _CONTENT_TYPES not in names or _ROOT_RELS not in names:
            return data
    except Exception:
        return data

    # Merge over anything already there, so re-stamping updates rather than
    # duplicating, and properties set by other tools are preserved.
    merged = {**read_properties(data), **{k: v for k, v in properties.items() if v is not None}}

    items = ''.join(
        f'<property fmtid="{_FMTID}" pid="{i + 2}" name="{_escape(name)}">'
        f'<vt:lpwstr xmlns:vt="{_VT_NS}">{_escape(value)}</vt:lpwstr></property>'
        for i, (name, value) in enumerate(merged.items())
    )
    custom_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Properties xmlns="{_CUSTOM_NS}">{items}</Properties>'
    )

    try:
        content_types = src.read(_CONTENT_TYPES).decode('utf-8')
        root_rels     = src.read(_ROOT_RELS).decode('utf-8')
    except Exception:
        return data

    if _CUSTOM_CT not in content_types:
        content_types = content_types.replace(
            '</Types>', f'<Override PartName="/{_CUSTOM_PART}" ContentType="{_CUSTOM_CT}"/></Types>',
        )
    if _CUSTOM_PART not in root_rels:
        root_rels = root_rels.replace(
            '</Relationships>',
            f'<Relationship Id="rIdDiligenceCustomProps" Type="{_REL_TYPE}" '
            f'Target="{_CUSTOM_PART}"/></Relationships>',
        )

    out = io.BytesIO()
    rewritten = {_CONTENT_TYPES, _ROOT_RELS, _CUSTOM_PART}
    try:
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as dst:
            # Copy every other part through untouched — charts, VBA, pivot caches,
            # anything we don't understand.
            for item in src.infolist():
                if item.filename in rewritten:
                    continue
                dst.writestr(item, src.read(item.filename))
            dst.writestr(_CONTENT_TYPES, content_types)
            dst.writestr(_ROOT_RELS, root_rels)
            dst.writestr(_CUSTOM_PART, custom_xml)
    except Exception:
        return data   # never hand back a half-written workbook
    return out.getvalue()


def stamp_for_project(data: bytes, project_id, project_name, version=None) -> bytes:
    props = {PROJECT_ID: str(project_id), PROJECT_NAME: project_name}
    if version is not None:
        props[VERSION] = str(version)
    return stamp(data, props)
