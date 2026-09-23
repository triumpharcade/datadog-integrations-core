# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import re
from unittest import mock

import pytest

from datadog_checks.base.utils.db.sql import compute_sql_signature
from datadog_checks.base.utils.db.utils import obfuscate_sql_with_metadata
from datadog_checks.postgres.function_labels import FunctionLabelCatalog
from datadog_checks.postgres.statements import PostgresStatementMetrics

pytestmark = pytest.mark.unit

OPTIONS = '{}'
FUNCTION_BODY = """
SELECT COALESCE(octet_length(user_id), 0) <= 128
   AND COALESCE(octet_length(pack_id), 0) <= 128
   AND COALESCE(pg_column_size(metadata || '{}'::jsonb), 0) <= 1024
"""


def normalize(query: str) -> str:
    return obfuscate_sql_with_metadata(query, OPTIONS)['query']


def catalog_rows(*rows: dict[str, str]) -> mock.MagicMock:
    check = mock.MagicMock()
    cursor = check._get_main_db.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = list(rows)
    return check


def function_row(body: str = FUNCTION_BODY, schema: str = 'app', function: str = 'validate_metadata') -> dict[str, str]:
    return {'datname': 'main', 'nspname': schema, 'proname': function, 'prosrc': body}


def metric_row(
    query: str = FUNCTION_BODY,
    *,
    toplevel: bool | None = False,
    datname: str = 'main',
    comments: list[str] | None = None,
) -> dict[str, object]:
    normalized = normalize(query)
    return {
        'query': normalized,
        'query_signature': compute_sql_signature(normalized),
        'datname': datname,
        'rolname': 'app',
        'dd_comments': comments,
        'toplevel': toplevel,
    }


def make_catalog(*rows: dict[str, str]) -> FunctionLabelCatalog:
    result = FunctionLabelCatalog(catalog_rows(*rows), OPTIONS, mock.MagicMock())
    result.set_enabled(True)
    return result


def test_labels_unique_nested_statement_and_preserves_signature() -> None:
    catalog = make_catalog(function_row())
    row = metric_row()
    signature = row['query_signature']

    assert catalog.enrich([row]) is True
    assert row['query'].startswith('/* function: app.validate_metadata */ SELECT')
    assert row['query_signature'] == signature
    assert 'toplevel' not in row


def test_v1_normalization_adds_label_without_changing_signature() -> None:
    catalog = make_catalog(function_row())
    metrics = PostgresStatementMetrics.__new__(PostgresStatementMetrics)
    metrics._obfuscate_options = OPTIONS
    metrics._config = mock.MagicMock(log_unobfuscated_queries=False)
    metrics._log = mock.MagicMock()
    metrics._function_labels = catalog
    metrics._full_statement_text_cache = {}

    rows = metrics._normalize_queries(
        [
            {
                'query': FUNCTION_BODY,
                'datname': 'main',
                'rolname': 'app',
                'toplevel': False,
            }
        ]
    )

    assert rows[0]['query'].startswith('/* function: app.validate_metadata */')
    assert rows[0]['query_signature'] == compute_sql_signature(normalize(FUNCTION_BODY))


@pytest.mark.parametrize(
    'rows',
    [
        [metric_row(toplevel=True)],
        [metric_row(toplevel=None)],
        [metric_row(datname='other')],
        [metric_row(), metric_row(toplevel=True)],
    ],
    ids=['top_level', 'unknown_level', 'other_database', 'mixed_group'],
)
def test_does_not_label_ineligible_rows(rows: list[dict[str, object]]) -> None:
    catalog = make_catalog(function_row())
    catalog.enrich(rows)
    assert all('function:' not in row['query'] for row in rows)
    assert all('toplevel' not in row for row in rows)


def test_sqlc_name_takes_precedence() -> None:
    catalog = make_catalog(function_row())
    row = metric_row(comments=['-- name: ValidateMetadata :one'])
    row['query'] = '/* ValidateMetadata */ {}'.format(row['query'])

    catalog.enrich([row])
    assert row['query'].startswith('/* ValidateMetadata */')
    assert 'function:' not in row['query']


def test_sqlc_name_wins_when_same_signature_rows_merge() -> None:
    catalog = make_catalog(function_row())
    unnamed = metric_row()
    named = metric_row(comments=['-- name: ValidateMetadata :one'])
    named['query'] = '/* ValidateMetadata */ {}'.format(named['query'])

    catalog.enrich([unnamed, named])
    assert unnamed['query'].startswith('/* ValidateMetadata */')
    assert 'function:' not in unnamed['query']


def test_overloads_with_identical_bodies_abstain() -> None:
    catalog = make_catalog(
        function_row('SELECT user_id = 1', function='first'),
        function_row('SELECT user_id = 1', function='second'),
    )
    row = metric_row('SELECT user_id = 1')
    catalog.enrich([row])
    assert 'function:' not in row['query']


def test_different_literals_that_normalize_to_one_shape_abstain() -> None:
    catalog = make_catalog(
        function_row('SELECT user_id = 1', function='first'),
        function_row('SELECT user_id = 2', function='second'),
    )
    row = metric_row('SELECT user_id = 3')

    def normalize_literals(query: str, options: str) -> dict[str, object]:
        return {'query': re.sub(r'\d+', '?', query), 'metadata': {}}

    with mock.patch(
        'datadog_checks.postgres.function_labels.obfuscate_sql_with_metadata', side_effect=normalize_literals
    ):
        catalog.enrich([row])
    assert catalog._labels == {}


@pytest.mark.parametrize(
    'catalog_row',
    [
        function_row('SELECT 1; SELECT 2'),
        function_row(schema='unsafe-schema'),
        function_row(function='unsafe.name'),
        function_row(schema='unsafe*/schema'),
        function_row(function='unsafe\nname'),
    ],
    ids=['multiple_statements', 'unsafe_schema', 'unsafe_function', 'comment_injection', 'newline_injection'],
)
def test_unsupported_catalog_entries_are_skipped(catalog_row: dict[str, str]) -> None:
    catalog = make_catalog(catalog_row)
    row = metric_row(catalog_row['prosrc'].split(';')[0])
    catalog.enrich([row])
    assert 'function:' not in row['query']


def test_refresh_replaces_renamed_and_dropped_labels_and_obeys_ttl() -> None:
    catalog = make_catalog()
    catalog._load = mock.MagicMock(
        side_effect=[
            ({normalize('SELECT 1'): '/* function: app.old */'}, 'main'),
            ({normalize('SELECT 1'): '/* function: app.new */'}, 'main'),
            ({}, 'main'),
        ]
    )
    with mock.patch('datadog_checks.postgres.function_labels.time.monotonic', side_effect=[100, 200, 401, 702]):
        first = metric_row('SELECT 1')
        assert catalog.enrich([first]) is True
        assert 'app.old' in first['query']

        cached = metric_row('SELECT 1')
        assert catalog.enrich([cached]) is False
        assert 'app.old' in cached['query']

        renamed = metric_row('SELECT 1')
        assert catalog.enrich([renamed]) is True
        assert 'app.new' in renamed['query']

        dropped = metric_row('SELECT 1')
        assert catalog.enrich([dropped]) is True
        assert 'function:' not in dropped['query']


def test_refresh_failure_clears_labels_without_logging_sql() -> None:
    log = mock.MagicMock()
    catalog = FunctionLabelCatalog(catalog_rows(), OPTIONS, log)
    catalog.set_enabled(True)
    catalog._labels = {normalize('SELECT secret'): '/* function: app.secret */'}
    catalog._database = 'main'
    catalog._load = mock.MagicMock(side_effect=RuntimeError('catalog unavailable'))
    row = metric_row('SELECT secret')

    assert catalog.enrich([row]) is True
    assert 'function:' not in row['query']
    assert 'SELECT secret' not in str(log.debug.call_args)


def test_obfuscation_failure_discards_entire_refresh() -> None:
    catalog = make_catalog(function_row('SELECT 1'), function_row('SELECT 2'))
    catalog._labels = {'stale': '/* function: app.stale */'}
    catalog._database = 'main'
    row = metric_row('SELECT 1')
    with mock.patch(
        'datadog_checks.postgres.function_labels.obfuscate_sql_with_metadata', side_effect=RuntimeError('bad body')
    ):
        catalog.enrich([row])
    assert catalog._labels == {}


def test_oversized_body_is_skipped() -> None:
    catalog = make_catalog(function_row('SELECT ' + 'x' * (64 * 1024)))
    row = metric_row('SELECT 1')
    catalog.enrich([row])
    assert 'function:' not in row['query']


def test_disabled_catalog_does_not_query_database() -> None:
    check = catalog_rows(function_row())
    catalog = FunctionLabelCatalog(check, OPTIONS, mock.MagicMock())
    row = metric_row()

    assert catalog.enrich([row]) is False
    check._get_main_db.assert_not_called()
    assert 'function:' not in row['query']
