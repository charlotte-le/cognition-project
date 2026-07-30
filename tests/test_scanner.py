"""Tests for finding identity.

The snippets here are copied verbatim out of the live ledger, so they carry
Bandit's real `code` formatting: one "<line_no> <source line>" per line of the
finding's range.
"""

from cognition.verification import scanner


# superset/db_engine_specs/clickhouse.py:408, as Bandit emitted it.
CLICKHOUSE = (
    '407             names = database.get_df(\n'
    '408                 "SELECT name FROM system.functions UNION ALL "  # noqa: S608\n'
    '409                 + "SELECT name FROM system.table_functions LIMIT 10000"\n'
    '410             )["name"].tolist()\n'
)

# The same code after an edit above it pushed the finding down four lines.
CLICKHOUSE_MOVED = (
    '411             names = database.get_df(\n'
    '412                 "SELECT name FROM system.functions UNION ALL "  # noqa: S608\n'
    '413                 + "SELECT name FROM system.table_functions LIMIT 10000"\n'
    '414             )["name"].tolist()\n'
)


class TestNormalizeCode:
    def test_strips_bandit_line_numbers(self):
        assert scanner.normalize_code('408                 "SELECT 1"') == '"SELECT 1"'

    def test_preserves_relative_indentation(self):
        """Indentation is real structure; only the line number prefix goes."""
        normalized = scanner.normalize_code(CLICKHOUSE)
        lines = normalized.split("\n")
        assert lines[0] == "names = database.get_df("
        assert lines[1].startswith('                "SELECT name')

    def test_moving_a_finding_does_not_change_its_normalized_form(self):
        assert scanner.normalize_code(CLICKHOUSE) == scanner.normalize_code(
            CLICKHOUSE_MOVED
        )

    def test_digits_in_the_source_survive(self):
        """Only the prefix is a line number - a literal 8000 is not."""
        assert scanner.normalize_code("94         port = 8000") == "port = 8000"

    def test_empty_code_is_handled(self):
        assert scanner.normalize_code("") == ""


class TestFindingKey:
    def test_key_is_stable_when_the_finding_moves(self):
        """This is the whole point: position must not be part of identity."""
        assert scanner.finding_key(
            "B608", "superset/db_engine_specs/clickhouse.py", CLICKHOUSE
        ) == scanner.finding_key(
            "B608", "superset/db_engine_specs/clickhouse.py", CLICKHOUSE_MOVED
        )

    def test_key_changes_when_the_code_changes(self):
        assert scanner.finding_key(
            "B608", "superset/a.py", '10         f"SELECT {x}"'
        ) != scanner.finding_key(
            "B608", "superset/a.py", '10         "SELECT ?"'
        )

    def test_key_separates_rule_and_path(self):
        same_code = '10         f"SELECT {x}"'
        assert scanner.finding_key("B608", "superset/a.py", same_code) != scanner.finding_key(
            "B608", "superset/b.py", same_code
        )
        assert scanner.finding_key("B608", "superset/a.py", same_code) != scanner.finding_key(
            "B610", "superset/a.py", same_code
        )

    def test_windows_separators_normalize(self):
        code = '10         f"SELECT {x}"'
        assert scanner.finding_key(
            "B608", "superset\\a.py", code
        ) == scanner.finding_key("B608", "superset/a.py", code)


class TestFingerprintUnchanged:
    """The ledger's primary key must keep its existing values.

    Fingerprints are already embedded in filed GitHub issues and branch names,
    so redefining them would orphan every open task.
    """

    def test_known_fingerprint_still_hashes_the_same(self):
        # scan:5e8fe6c1, as filed in the live ledger.
        assert (
            scanner.fingerprint(
                "B608", "superset/db_engine_specs/clickhouse.py", CLICKHOUSE
            )
            == "scan:5e8fe6c1"
        )
