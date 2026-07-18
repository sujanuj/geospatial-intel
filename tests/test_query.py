"""Tests for query.py — the filter DSL used by /api/objects?q= and the
WebSocket "filter" message.
"""

import pytest

from query import QueryError, evaluate, filter_objects, parse_query


SHIP = {
    "id": "s1", "name": "MV Ocean Pioneer", "type": "ship", "status": "threat",
    "country": "Iran", "speed": 16.3, "altitude": 0, "heading": 90,
    "lat": 25.0, "lon": 55.0,
}
AIRCRAFT = {
    "id": "a1", "name": "UAV Alpha", "type": "aircraft", "status": "active",
    "country": "USA", "speed": 500.0, "altitude": 35000, "heading": 180,
    "lat": 40.0, "lon": -74.0,
}
VEHICLE = {
    "id": "v1", "name": "Unit Bravo", "type": "vehicle", "status": "warning",
    "country": "Iraq", "speed": 45.0, "altitude": 0, "heading": 0,
    "lat": -33.0, "lon": 44.0,
}
ALL = [SHIP, AIRCRAFT, VEHICLE]


# ---------------------------------------------------------------------------
# String field matching (: contains, = exact, != not-equal)
# ---------------------------------------------------------------------------

class TestStringFieldMatching:
    def test_colon_is_case_insensitive_substring(self):
        assert filter_objects(ALL, "name:pioneer") == [SHIP]
        assert filter_objects(ALL, "name:PIONEER") == [SHIP]
        assert filter_objects(ALL, "name:ocean") == [SHIP]

    def test_equals_is_case_insensitive_exact_match(self):
        assert filter_objects(ALL, "type=ship") == [SHIP]
        assert filter_objects(ALL, "type=SHIP") == [SHIP]
        # exact match means a substring of the value should NOT match
        assert filter_objects(ALL, "type=shi") == []

    def test_not_equal(self):
        result = filter_objects(ALL, "status!=active")
        assert AIRCRAFT not in result
        assert SHIP in result and VEHICLE in result

    def test_quoted_value_with_spaces(self):
        assert filter_objects(ALL, 'name:"Ocean Pioneer"') == [SHIP]

    def test_comparison_operators_rejected_on_string_fields(self):
        for op in [">", "<", ">=", "<="]:
            with pytest.raises(QueryError, match="not valid on string field"):
                parse_query(f"type{op}ship")


# ---------------------------------------------------------------------------
# Numeric field matching
# ---------------------------------------------------------------------------

class TestNumericFieldMatching:
    def test_greater_than(self):
        assert filter_objects(ALL, "speed>400") == [AIRCRAFT]

    def test_less_than(self):
        assert filter_objects(ALL, "speed<20") == [SHIP]

    def test_greater_than_or_equal(self):
        assert filter_objects(ALL, "speed>=45") == [AIRCRAFT, VEHICLE]

    def test_less_than_or_equal(self):
        assert filter_objects(ALL, "speed<=45") == [SHIP, VEHICLE]

    def test_equality_via_colon_and_equals(self):
        assert filter_objects(ALL, "altitude:0") == [SHIP, VEHICLE]
        assert filter_objects(ALL, "altitude=0") == [SHIP, VEHICLE]

    def test_not_equal_numeric(self):
        assert filter_objects(ALL, "altitude!=0") == [AIRCRAFT]

    def test_negative_number_value(self):
        assert filter_objects(ALL, "lat<-30") == [VEHICLE]
        assert filter_objects(ALL, "lon<0") == [AIRCRAFT]

    def test_non_numeric_value_on_numeric_field_raises(self):
        with pytest.raises(QueryError, match="is numeric but"):
            parse_query("speed>fast")


# ---------------------------------------------------------------------------
# Boolean combinators and precedence
# ---------------------------------------------------------------------------

class TestBooleanLogic:
    def test_and(self):
        assert filter_objects(ALL, "type:ship AND status:threat") == [SHIP]
        assert filter_objects(ALL, "type:ship AND status:active") == []

    def test_or(self):
        result = filter_objects(ALL, "country:Iran OR country:Iraq")
        assert set(o["id"] for o in result) == {"s1", "v1"}

    def test_not(self):
        result = filter_objects(ALL, "NOT status:active")
        assert AIRCRAFT not in result

    def test_and_or_lowercase_and_mixed_case(self):
        assert filter_objects(ALL, "type:ship and status:threat") == [SHIP]
        assert filter_objects(ALL, "type:ship And status:threat") == [SHIP]

    def test_not_binds_tighter_than_and(self):
        # NOT status:active AND altitude>30000
        # should parse as (NOT status:active) AND (altitude>30000),
        # not NOT (status:active AND altitude>30000)
        result = filter_objects(ALL, "NOT status:active AND altitude>30000")
        assert result == []  # no object is both non-active AND high-altitude

    def test_and_binds_tighter_than_or(self):
        # type:ship OR type:aircraft AND status:warning
        # should parse as type:ship OR (type:aircraft AND status:warning)
        result = filter_objects(ALL, "type:ship OR type:aircraft AND status:warning")
        assert result == [SHIP]  # aircraft is active, not warning, so excluded

    def test_parentheses_override_precedence(self):
        # (type:ship OR type:aircraft) AND status:warning
        # should match nothing, since neither ship nor aircraft is warning
        result = filter_objects(ALL, "(type:ship OR type:aircraft) AND status:warning")
        assert result == []

    def test_nested_parentheses(self):
        result = filter_objects(
            ALL, "type:ship AND (status:threat OR (status:warning AND speed>100))"
        )
        assert result == [SHIP]

    def test_double_not(self):
        result = filter_objects(ALL, "NOT NOT status:active")
        assert result == [AIRCRAFT]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    @pytest.mark.parametrize("query,expected_fragment", [
        ("", "empty query"),
        ("   ", "empty query"),
        ("bogus_field:value", "unknown field"),
        ("speed<threat", "is numeric but"),
        ("type>ship", "not valid on string field"),
        ("type:", "expected a value"),
        (":value", "expected a field name"),
        ("type:ship AND", "expected a field name"),
        ("type:ship extra:tokens:here", "expected EOF"),
        ('name:"unterminated', "unterminated quoted string"),
        ("(type:ship", "expected RPAREN"),
        ("type:ship)", "expected EOF"),
    ])
    def test_malformed_queries_raise_query_error(self, query, expected_fragment):
        with pytest.raises(QueryError, match=expected_fragment):
            parse_query(query)

    def test_query_error_is_a_value_error(self):
        # so callers that only catch ValueError still work
        assert issubclass(QueryError, ValueError)

    def test_evaluate_raises_on_missing_field(self):
        ast = parse_query("type:ship")
        with pytest.raises(QueryError, match="missing expected field"):
            evaluate(ast, {"status": "active"})  # no "type" key


# ---------------------------------------------------------------------------
# filter_objects() integration
# ---------------------------------------------------------------------------

class TestFilterObjects:
    def test_empty_result_when_nothing_matches(self):
        assert filter_objects(ALL, "type:submarine") == []

    def test_all_match_a_trivially_true_query(self):
        result = filter_objects(ALL, "speed>=0")
        assert len(result) == 3

    def test_does_not_mutate_input_list(self):
        original = list(ALL)
        filter_objects(ALL, "type:ship")
        assert ALL == original

    def test_realistic_fde_style_query(self):
        # the kind of query this DSL is meant to support
        result = filter_objects(
            ALL, 'type:ship AND status:threat AND country:Iran'
        )
        assert result == [SHIP]
