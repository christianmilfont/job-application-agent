"""Chaves de deduplicação: URL normalizada, fonte+ID e empresa+cargo+localização."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = re.compile(
    r"^(utm_.*|gh_src|gh_jid|lever-source.*|source|ref|refid|trk|trackingid|src|origin|fbclid|gclid)$", re.I)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return value.strip()


def url_key(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if not _TRACKING_PARAMS.match(k)]
    path = re.sub(r"/+$", "", parts.path) or "/"
    host = parts.netloc.lower().removeprefix("www.")
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def source_key(source: str, external_id: str | None) -> str | None:
    return f"{source}:{external_id}" if external_id else None


def content_key(company: str, title: str, location: str | None) -> str:
    raw = "|".join(normalize_text(x) for x in (company, title, location))
    return hashlib.sha256(raw.encode()).hexdigest()
