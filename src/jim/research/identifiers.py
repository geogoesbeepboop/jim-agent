"""Identifier canonicalization — the engine's front door for untrusted input.

An identifier travels a long way after `run_research()` accepts it: into EDGAR
URL paths, into a Graph subgraph query, into peer query params, into cache and
store keys. Every one of those is a place where a hostile string — path
traversal (``../..``), a URL (``http://evil``), query-string smuggling
(``?x=1``), control bytes — becomes an SSRF or path-traversal primitive. So the
engine canonicalizes *before any source fetch*: strip, normalize case where the
product is case-insensitive, and match a strict per-product allowlist. Anything
outside it is rejected with a ``ValueError`` — nothing is escaped, quoted, or
shelled out, because the only safe transformation for an identifier we don't
recognize is refusal. The per-source checks (EDGAR's ticker map, ``resolve()``'s
chain/token registry) stay in place as defense-in-depth; this layer just
guarantees they only ever see boring strings.
"""

from __future__ import annotations

import re

# Per-product allowlists. Deliberately tight: widen a pattern only when a real
# identifier fails, never speculatively.
_FUNDAMENTALS = re.compile(r"^[A-Z0-9.\-]{1,10}$")  # tickers: AAPL, BRK.B, RDS-A
_TOKEN = re.compile(r"^(0x[0-9a-fA-F]{40}|[A-Za-z0-9.\-]{1,20})(:[a-z0-9\-]{1,20})?$")
_MACRO = re.compile(r"^[A-Z]{2,8}$")  # region codes: US
_GENERIC = re.compile(r"^[A-Za-z0-9.\-:]{1,40}$")  # unknown products: least privilege

_MAX_LENGTH = 80


def canonicalize(identifier: str, product: str) -> str:
    """Normalize ``identifier`` for ``product`` or raise ``ValueError``.

    Deterministic and offline. Common rules first (non-empty after strip, at
    most 80 chars, no control characters or interior whitespace), then the
    product's allowlist:

      - ``fundamentals``: uppercased ticker, ``A-Z 0-9 . -`` up to 10 chars.
      - ``token``: symbol or ``0x`` address with an optional ``:chain`` suffix.
        The chain suffix is lowercased; the symbol/address part is left as-is
        (``GraphSource.resolve`` is case-aware and lowercases addresses itself).
      - ``macro``: uppercased 2–8 letter region code.
      - anything else: a conservative generic alphanumeric pattern.
    """
    cleaned = identifier.strip()
    if not cleaned:
        raise ValueError("Identifier is empty.")
    if len(cleaned) > _MAX_LENGTH:
        raise ValueError(f"Identifier is too long ({len(cleaned)} chars; max {_MAX_LENGTH}).")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError("Identifier contains whitespace or control characters.")

    if product == "fundamentals":
        candidate = cleaned.upper()
        if not _FUNDAMENTALS.match(candidate):
            raise ValueError(
                f"{cleaned!r} is not a valid ticker: expected 1-10 characters "
                "from A-Z, 0-9, '.', '-' (e.g. AAPL, BRK.B, RDS-A)."
            )
        return candidate

    if product == "token":
        token_part, sep, chain_part = cleaned.partition(":")
        candidate = f"{token_part}:{chain_part.lower()}" if sep else cleaned
        if not _TOKEN.match(candidate):
            raise ValueError(
                f"{cleaned!r} is not a valid token identifier: expected a symbol "
                "or 0x address, optionally chain-qualified (e.g. WETH, WETH:base, 0x…:arbitrum)."
            )
        return candidate

    if product == "macro":
        candidate = cleaned.upper()
        if not _MACRO.match(candidate):
            raise ValueError(
                f"{cleaned!r} is not a valid macro identifier: expected a 2-8 "
                "letter region code (e.g. US)."
            )
        return candidate

    if not _GENERIC.match(cleaned):
        raise ValueError(
            f"{cleaned!r} is not a valid identifier: expected 1-40 characters "
            "from A-Za-z, 0-9, '.', '-', ':'."
        )
    return cleaned
