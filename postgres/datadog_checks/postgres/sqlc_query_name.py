# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import annotations

import re
from collections.abc import Iterable

SQLC_NAME_RE = re.compile(r'^\s*--\s*name:\s*(\w{1,128})\s*:', re.ASCII)
LEADING_SQLC_HEADER_RE = re.compile(r'^\s*--\s*name:\s*\w{1,128}\s*:[^\r\n]*(?:\r?\n|$)', re.ASCII)
LEADING_SQLC_NAME_RE = re.compile(r'^/\* \w{1,128} \*/ ', re.ASCII)


def sqlc_query_name(comments: Iterable[str] | None) -> str | None:
    """Return the query name from a sqlc header comment."""
    for comment in comments or ():
        match = SQLC_NAME_RE.match(comment)
        if match:
            return match.group(1)
    return None


def prepend_sqlc_query_name(obfuscated_query: str, comments: Iterable[str] | None) -> str:
    """Prefix an obfuscated query with its sqlc query name."""
    name = sqlc_query_name(comments)
    if not name or not obfuscated_query:
        return obfuscated_query
    return '/* {} */ {}'.format(name, obfuscated_query)


def strip_sqlc_query_name(obfuscated_query: str) -> str:
    """Remove a sqlc query name prefix from an obfuscated query."""
    return LEADING_SQLC_NAME_RE.sub('', obfuscated_query, count=1)


def strip_sqlc_header(query: str) -> str:
    """Remove a leading sqlc header from a query."""
    return LEADING_SQLC_HEADER_RE.sub('', query, count=1)
