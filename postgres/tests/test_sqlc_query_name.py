# (C) Datadog, Inc. 2026-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from types import SimpleNamespace
from unittest import mock

import pytest

from datadog_checks.postgres.obfuscation_lookup import ObfuscationLookup
from datadog_checks.postgres.sqlc_query_name import (
    prepend_sqlc_query_name,
    sqlc_query_name,
    strip_sqlc_header,
    strip_sqlc_query_name,
)
from datadog_checks.postgres.statement_samples import PostgresStatementSamples, StatementTruncationState
from datadog_checks.postgres.statements import PostgresStatementMetrics

pytestmark = pytest.mark.unit

SQLC_HEADER = '-- name: GetWidgets :many'
OBFUSCATED_QUERY = 'SELECT id FROM widgets WHERE id = ?'
PREFIXED_QUERY = '/* GetWidgets */ {}'.format(OBFUSCATED_QUERY)
METADATA = {
    'tables': ['widgets'],
    'commands': ['SELECT'],
    'comments': ['-- keep this comment', SQLC_HEADER],
}


def obfuscation_result(comments: list[str] | None = None) -> dict[str, object]:
    """Build an obfuscator response for collector unit tests."""
    metadata = dict(METADATA)
    if comments is not None:
        metadata['comments'] = comments
    return {'query': OBFUSCATED_QUERY, 'metadata': metadata}


def test_sqlc_query_name_reads_valid_header() -> None:
    assert sqlc_query_name(['-- ordinary comment', SQLC_HEADER]) == 'GetWidgets'


@pytest.mark.parametrize(
    'comments',
    [
        None,
        [],
        ['-- ordinary comment'],
        ['-- name: Get Widgets :one'],
        ['-- name: \u0186etWidgets :one'],
        ['-- name: {} :one'.format('A' * 129)],
    ],
)
def test_sqlc_query_name_ignores_missing_or_invalid_headers(comments: list[str] | None) -> None:
    assert sqlc_query_name(comments) is None


def test_prefix_and_strip_sqlc_query_name() -> None:
    assert prepend_sqlc_query_name(OBFUSCATED_QUERY, METADATA['comments']) == PREFIXED_QUERY
    assert strip_sqlc_query_name(PREFIXED_QUERY) == OBFUSCATED_QUERY


def test_prefix_leaves_query_without_sqlc_header_unchanged() -> None:
    assert prepend_sqlc_query_name(OBFUSCATED_QUERY, ['-- ordinary comment']) == OBFUSCATED_QUERY
    assert prepend_sqlc_query_name('', [SQLC_HEADER]) == ''


def test_strip_sqlc_header_only_removes_a_leading_sqlc_line() -> None:
    query = '{}\n{}'.format(SQLC_HEADER, OBFUSCATED_QUERY)
    ordinary_comment = '-- ordinary comment\n{}'.format(query)

    assert strip_sqlc_header(query) == OBFUSCATED_QUERY
    assert strip_sqlc_header(ordinary_comment) == ordinary_comment
    assert strip_sqlc_header(OBFUSCATED_QUERY) == OBFUSCATED_QUERY


def test_legacy_metrics_prefixes_query_without_changing_signature_or_metadata() -> None:
    collector = object.__new__(PostgresStatementMetrics)
    collector._obfuscate_options = '{}'
    collector._config = SimpleNamespace(log_unobfuscated_queries=False)
    collector._log = mock.Mock()

    with (
        mock.patch(
            'datadog_checks.postgres.statements.obfuscate_sql_with_metadata',
            return_value=obfuscation_result(),
        ),
        mock.patch(
            'datadog_checks.postgres.statements.compute_sql_signature',
            side_effect=lambda query: 'signature:{}'.format(query),
        ) as compute_signature,
    ):
        rows = collector._normalize_queries([{'query': 'SELECT id FROM widgets WHERE id = 7'}])

    assert rows[0]['query'] == PREFIXED_QUERY
    assert rows[0]['query_signature'] == 'signature:{}'.format(OBFUSCATED_QUERY)
    assert rows[0]['dd_comments'] == METADATA['comments']
    compute_signature.assert_called_once_with(OBFUSCATED_QUERY)


def test_v2_lookup_prefixes_query_without_changing_signature_or_metadata() -> None:
    lookup = ObfuscationLookup(maxsize=10, obfuscate_options='{}')

    with (
        mock.patch(
            'datadog_checks.postgres.obfuscation_lookup.obfuscate_sql_with_metadata',
            return_value=obfuscation_result(),
        ),
        mock.patch(
            'datadog_checks.postgres.obfuscation_lookup.compute_sql_signature',
            side_effect=lambda query: 'signature:{}'.format(query),
        ) as compute_signature,
    ):
        result = lookup._obfuscate_single('SELECT id FROM widgets WHERE id = 7')

    assert result is not None
    assert result.obfuscated_query == PREFIXED_QUERY
    assert result.query_signature == 'signature:{}'.format(OBFUSCATED_QUERY)
    assert result.comments == METADATA['comments']
    compute_signature.assert_called_once_with(OBFUSCATED_QUERY)


def test_samples_prefix_query_without_changing_signature_or_metadata() -> None:
    collector = object.__new__(PostgresStatementSamples)
    collector._obfuscate_options = '{}'
    collector._config = SimpleNamespace(log_unobfuscated_queries=False)
    collector._log = mock.Mock()

    with (
        mock.patch(
            'datadog_checks.postgres.statement_samples.obfuscate_sql_with_metadata',
            return_value=obfuscation_result(),
        ),
        mock.patch(
            'datadog_checks.postgres.statement_samples.compute_sql_signature',
            side_effect=lambda query: 'signature:{}'.format(query),
        ) as compute_signature,
    ):
        row = collector._normalize_row({'query': 'SELECT id FROM widgets WHERE id = 7'})

    assert row['statement'] == PREFIXED_QUERY
    assert row['query_signature'] == 'signature:{}'.format(OBFUSCATED_QUERY)
    assert row['dd_comments'] == METADATA['comments']
    compute_signature.assert_called_once_with(OBFUSCATED_QUERY)


def test_v2_lookup_leaves_query_without_sqlc_header_unchanged() -> None:
    lookup = ObfuscationLookup(maxsize=10, obfuscate_options='{}')
    response = obfuscation_result(comments=['-- ordinary comment'])

    with mock.patch('datadog_checks.postgres.obfuscation_lookup.obfuscate_sql_with_metadata', return_value=response):
        result = lookup._obfuscate_single('SELECT id FROM widgets WHERE id = 7')

    assert result is not None
    assert result.obfuscated_query == OBFUSCATED_QUERY


def test_explain_strips_prefix_before_trimming_leading_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('DD_DISABLE_TRACKED_METHOD', 'true')
    collector = object.__new__(PostgresStatementSamples)
    collector._can_explain_statement = mock.Mock(return_value=True)
    collector._get_track_activity_query_size = mock.Mock(return_value=4096)
    collector._get_truncation_state = mock.Mock(return_value=StatementTruncationState.not_truncated)
    collector._get_db_explain_setup_state_cached = mock.Mock(return_value=(None, None))
    collector._explain_errors_cache = {}
    collector._explain_parameterized_queries = SimpleNamespace(_is_parameterized_query=mock.Mock(return_value=False))
    collector._run_explain = mock.Mock(return_value={'Plan': {}})

    raw_query = '{}\nSET LOCAL statement_timeout = 1000; SELECT id FROM widgets'.format(SQLC_HEADER)
    obfuscated_query = '/* GetWidgets */ SET LOCAL statement_timeout = ?; SELECT id FROM widgets'

    plan, error, message = collector._run_explain_safe('widgets', raw_query, obfuscated_query, 'signature')

    assert plan == {'Plan': {}}
    assert error is None
    assert message is None
    collector._can_explain_statement.assert_called_once_with('SELECT id FROM widgets')
    collector._run_explain.assert_called_once_with(
        'widgets',
        'SELECT id FROM widgets',
        'SELECT id FROM widgets',
    )
    collector._get_truncation_state.assert_called_once_with(4096, raw_query, 'signature')


def test_parameterized_explain_receives_unprefixed_obfuscated_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('DD_DISABLE_TRACKED_METHOD', 'true')
    collector = object.__new__(PostgresStatementSamples)
    collector._config = SimpleNamespace(query_samples=SimpleNamespace(explain_parameterized_queries=True))
    collector._can_explain_statement = mock.Mock(return_value=True)
    collector._get_track_activity_query_size = mock.Mock(return_value=4096)
    collector._get_truncation_state = mock.Mock(return_value=StatementTruncationState.not_truncated)
    collector._get_db_explain_setup_state_cached = mock.Mock(return_value=(None, None))
    collector._explain_errors_cache = {}
    collector._run_explain = mock.Mock()
    explain_statement = mock.Mock(return_value=({'Plan': {}}, None, None))
    collector._explain_parameterized_queries = SimpleNamespace(
        _is_parameterized_query=mock.Mock(return_value=True),
        explain_statement=explain_statement,
    )

    raw_query = '{}\nSELECT id FROM widgets WHERE id = $1'.format(SQLC_HEADER)

    plan, error, message = collector._run_explain_safe('widgets', raw_query, PREFIXED_QUERY, 'signature')

    assert (plan, error, message) == ({'Plan': {}}, None, None)
    collector._run_explain.assert_not_called()
    explain_statement.assert_called_once_with('widgets', raw_query, OBFUSCATED_QUERY, 'signature')
    assert '/* GetWidgets */ ' not in explain_statement.call_args.args[2]
    assert collector._explain_errors_cache == {}
