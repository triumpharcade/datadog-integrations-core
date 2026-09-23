# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from __future__ import annotations

import re
import time
from typing import Any

from psycopg.rows import dict_row

from datadog_checks.base.utils.db.utils import obfuscate_sql_with_metadata

from .sqlc_query_name import sqlc_query_name, strip_sqlc_query_name

FUNCTION_CATALOG_QUERY = """
/* DDIGNORE */
SELECT current_database() AS datname, n.nspname, p.proname, p.prosrc
  FROM pg_proc AS p
  JOIN pg_namespace AS n ON n.oid = p.pronamespace
  JOIN pg_language AS l ON l.oid = p.prolang
 WHERE l.lanname = 'sql'
   AND p.prokind = 'f'
   AND p.prosqlbody IS NULL
   AND LEFT(n.nspname, 3) <> 'pg_'
   AND n.nspname <> 'information_schema'
   AND octet_length(p.prosrc) <= %s
"""

FUNCTION_CATALOG_TTL = 300
MAX_FUNCTION_BODY_BYTES = 64 * 1024
SAFE_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$', re.ASCII)


class FunctionLabelCatalog:
    """Best-effort SQL function labels keyed by normalized single-statement bodies."""

    def __init__(self, check: Any, obfuscate_options: str, log: Any) -> None:
        self._check = check
        self._obfuscate_options = obfuscate_options
        self._log = log
        self._enabled = False
        self._labels: dict[str, str] = {}
        self._database: str | None = None
        self._last_refresh: float | None = None

    def set_enabled(self, enabled: bool) -> None:
        if self._enabled and not enabled:
            self._labels = {}
            self._database = None
            self._last_refresh = None
        self._enabled = enabled

    def enrich(self, rows: list[dict]) -> bool:
        """Prefix eligible rows and return whether the catalog changed."""
        changed = self._refresh_if_needed()
        eligibility: dict[tuple, bool] = {}
        sqlc_rows: dict[tuple, dict] = {}
        for row in rows:
            key = (row.get('query_signature'), row.get('datname'), row.get('rolname'))
            eligibility[key] = eligibility.get(key, True) and row.get('toplevel') is False
            if key not in sqlc_rows and sqlc_query_name(row.get('dd_comments')):
                sqlc_rows[key] = row

        for row in rows:
            key = (row.get('query_signature'), row.get('datname'), row.get('rolname'))
            if key in sqlc_rows:
                row['query'] = sqlc_rows[key]['query']
                row['dd_comments'] = sqlc_rows[key]['dd_comments']
            if eligibility[key] and row.get('datname') == self._database and key not in sqlc_rows:
                label = self._labels.get(strip_sqlc_query_name(row['query']))
                if label:
                    row['query'] = '{} {}'.format(label, row['query'])
            row.pop('toplevel', None)
        return changed

    def _refresh_if_needed(self) -> bool:
        if not self._enabled:
            return False
        now = time.monotonic()
        if self._last_refresh is not None and now - self._last_refresh < FUNCTION_CATALOG_TTL:
            return False
        self._last_refresh = now
        previous = (self._database, self._labels)
        try:
            labels, database = self._load()
        except Exception as e:
            self._log.debug("Failed to refresh SQL function labels | err_type=%s", type(e).__name__)
            labels, database = {}, None
        self._labels = labels
        self._database = database
        return previous != (database, labels)

    def _load(self) -> tuple[dict[str, str], str | None]:
        with self._check._get_main_db() as conn:
            with conn.cursor(row_factory=dict_row) as cursor:
                cursor.execute(FUNCTION_CATALOG_QUERY, params=(MAX_FUNCTION_BODY_BYTES,), ignore_query_metric=True)
                rows = cursor.fetchall()

        candidates: dict[str, list[str]] = {}
        database = None
        for row in rows:
            database = row['datname']
            schema = row['nspname']
            function = row['proname']
            if not SAFE_IDENTIFIER.fullmatch(schema) or not SAFE_IDENTIFIER.fullmatch(function):
                continue
            body = row['prosrc'].strip()
            if body.endswith(';'):
                body = body[:-1].rstrip()
            if not body or ';' in body or len(body.encode('utf-8')) > MAX_FUNCTION_BODY_BYTES:
                continue
            normalized = obfuscate_sql_with_metadata(body, self._obfuscate_options)['query']
            candidates.setdefault(normalized, []).append('/* function: {}.{} */'.format(schema, function))

        return {body: labels[0] for body, labels in candidates.items() if len(labels) == 1}, database
