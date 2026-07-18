#!/usr/bin/env python3
"""Minimal, privacy-conscious original-photo smoke exporter."""

from __future__ import annotations

import re
import urllib.parse


CDN_HOST = "cdn-mctchildfoliocn.childfolio.net"
ORIGINAL_URL_RE = re.compile(r"原图地址::(\S+)")


class SmokeError(RuntimeError):
    """Expected smoke-check failure whose message contains no sensitive URL."""


def validate_original_url(raw: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if parsed.scheme != "https":
        return None
    if parsed.hostname != CDN_HOST or port not in (None, 443):
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    if not parsed.path.endswith(".jpeg"):
        return None
    return raw


def extract_urls(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in ORIGINAL_URL_RE.finditer(text):
        candidate = validate_original_url(match.group(1))
        if candidate is not None and candidate not in seen:
            found.append(candidate)
            seen.add(candidate)
    return found


def select_samples(urls: list[str], count: int = 3) -> list[str]:
    samples = list(dict.fromkeys(urls))[:count]
    if len(samples) < count:
        raise SmokeError("not-enough-candidates")
    return samples
