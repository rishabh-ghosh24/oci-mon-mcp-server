#!/usr/bin/env python3
"""Shared sanitization helpers for promoting or aggregating learnings."""

from __future__ import annotations

import re


OCID_RE = re.compile(r"ocid1\.[A-Za-z0-9_.-]+")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SECRETISH_RE = re.compile(
    r"(?:api[_-]?key|secret|password|token|bearer|authorization)",
    re.IGNORECASE,
)
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
LONG_HEX_RE = re.compile(r"\b[a-f0-9]{24,}\b", re.IGNORECASE)


def looks_sensitive(text: str) -> bool:
    return any(
        pattern.search(text)
        for pattern in (OCID_RE, IPV4_RE, EMAIL_RE, SECRETISH_RE, UUID_RE, LONG_HEX_RE)
    )



def sanitize_pattern(text: str) -> str | None:
    candidate = text.strip()
    if not candidate:
        return None
    if looks_sensitive(candidate):
        return None
    candidate = re.sub(r'"[^"]{32,}"', '"<value>"', candidate)
    candidate = re.sub(r"'[^']{32,}'", "'<value>'", candidate)
    return candidate



def sanitize_query_text(query_text: str) -> str | None:
    if not query_text:
        return None
    cleaned = OCID_RE.sub("<resource_ocid>", query_text)
    cleaned = IPV4_RE.sub("<ip_address>", cleaned)
    cleaned = EMAIL_RE.sub("<email>", cleaned)
    cleaned = UUID_RE.sub("<uuid>", cleaned)
    cleaned = LONG_HEX_RE.sub("<id>", cleaned)
    if SECRETISH_RE.search(cleaned):
        return None
    return cleaned
