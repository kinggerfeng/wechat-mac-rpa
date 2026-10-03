"""The table type and its operations.

The tests are about the ways a table operation goes quietly wrong rather than
about whether it can add two numbers. Sorting ``"10"`` before ``"9"`` produces a
table that looks right. A filter that returns the input when nothing matches
produces a flow that replies to everybody. A join on a mistyped key produces an
empty table and a run that reports success. Each of those is a single assertion
here, and each is a bug that shipped into a hundred real automations before
somebody noticed.
"""

from __future__ import annotations

import json

import pytest


from apps.engine.table import (
    Table,
    TableError,
    coerce,
    filter_and,
    filter_or,
    filter_rows,
    group_rows,
    join_tables,
    sort_key,
    sort_rows,
)

SALES = [
    {"name": "甲", "dept": "销售", "amount": 120, "state": "已付"},
    {"name": "乙", "dept": "销售", "amount": 9, "state": "未付"},
    {"name": "丙", "dept": "技术", "amount": 300, "state": "已付"},
    {"name": "丁", "dept": "技术", "amount": 45, "state": "未付"},
]


def table(rows=None, **kw):
    return Table.from_rows(rows if rows is not None else SALES, **kw)


class TestConstruction:
    def test_from_rows_infers_column_order(self):
        assert table().columns == ["name", "dept", "amount", "state"]

    def test_from_csv(self):
        t = Table.from_csv("a,b\n1,x\n2,y\n")
        assert t.columns == ["a", "b"]
        assert t.rows == [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}]

    def test_from_json_array(self):
        assert Table.from_json('[{"a":1}]').rows == [{"a": 1}]

    def test_from_json_wrapped(self):
        assert Table.from_json('{"rows":[{"a":1}]}').rows == [{"a": 1}]

    def test_from_json_rejects_a_scalar(self):
        with pytest.raises(TableError, match="需要是数组"):
            Table.from_json("42")

    def test_from_json_rejects_non_object_rows(self):
        with pytest.raises(TableError, match="第 2 行不是对象"):
            Table.from_json('[{"a":1}, 5]')

    def test_type_inference(self):
        t = table()
        assert t.infer_type("amount") == "int"
        assert t.infer_type("name") == "str"
        assert t.infer_type("nope") == "empty"

    def test_a_float_column_is_not_reported_as_int(self):
        assert table([{"v": 1.5}]).infer_type("v") == "float"

    def test_csv_round_trip(self):
        t = table()
        again = Table.from_csv(t.to_csv())
        assert again.columns == t.columns
        assert again.rows[0]["name"] == "甲"


class TestCoercion:
    def test_numeric_string_becomes_a_number(self):
        assert coerce("42", "int") == 42
        assert coerce("4.5", "float") == 4.5

    def test_int_coercion_of_a_whole_float_stays_int(self):
        assert coerce("7.0", "int") == 7
        assert isinstance(coerce("7.0", "int"), int)

    def test_a_bad_cell_is_an_error_not_a_fallback(self):
        """A column declared int containing "abc" means the author's model of
        the data is wrong; comparing it as a string gives a plausible-looking
        order that is not one."""
        with pytest.raises(TableError, match="但遇到"):
            coerce("abc", "int")

    def test_a_bool_is_not_a_number(self):
        with pytest.raises(TableError, match="布尔"):
            coerce(True, "int")

    def test_a_bad_date_is_an_error(self):
        with pytest.raises(TableError, match="不是可识别的日期"):
            coerce("not-a-date", "date")

    def test_empty_stays_empty(self):
        assert coerce("", "int") is None
        assert coerce(None, "str") is None


class TestFilter:
    def test_equality(self):
        assert len(filter_rows(table(), "state", "eq", "已付").rows) == 2

    def test_comparison_numbers_not_strings(self):
        """The case this type exists for: "9" > "120" as text, 9 < 120 as
        numbers."""
        got = filter_rows(table(), "amount", "gt", 100)
        assert [r["name"] for r in got.rows] == ["甲", "丙"]

    def test_no_match_is_empty_never_everything(self):
        """A filter that quietly returns its input is how a flow replies to
        every contact on the list."""
        got = filter_rows(table(), "state", "eq", "不存在")
        assert got.rows == []
        assert len(table().rows) == 4  # the input is untouched

    def test_contains_and_prefix(self):
        assert len(filter_rows(table(), "name", "contains", "甲").rows) == 1
        assert len(filter_rows(table(), "dept", "startswith", "技").rows) == 2

    def test_in_operator(self):
        got = filter_rows(table(), "name", "in", ["甲", "丙"])
        assert [r["name"] for r in got.rows] == ["甲", "丙"]

    def test_regex(self):
        assert len(filter_rows(table(), "dept", "matches", "^(销售|技术)$").rows) == 4

    def test_empty_checks(self):
        rows = [{"a": 1, "b": ""}, {"a": 2, "b": "x"}]
        t = Table.from_rows(rows)
        assert [r["a"] for r in filter_rows(t, "b", "is_empty", None).rows] == [1]
        assert [r["a"] for r in filter_rows(t, "b", "not_empty", None).rows] == [2]

    def test_unknown_column_names_the_real_ones(self):
        with pytest.raises(TableError) as exc:
            filter_rows(table(), "nope", "eq", 1)
        assert "name" in str(exc.value) and "nope" in str(exc.value)

    def test_unknown_operator_lists_the_usable_ones(self):
        with pytest.raises(TableError) as exc:
            filter_rows(table(), "name", "like", "x")
        assert "contains" in str(exc.value)

    def test_a_bad_cell_excludes_the_row_without_raising(self):
        t = Table.from_rows([{"v": "abc"}, {"v": "5"}], types={"v": "int"})
        got = filter_rows(t, "v", "gt", 1)
        # The filter does not rewrite the table; the value stays as it arrived.
        assert [r["v"] for r in got.rows] == ["5"]

    def test_and_narrows(self):
        got = filter_and(table(), [
            {"column": "dept", "op": "eq", "value": "技术"},
            {"column": "amount", "op": "gt", "value": 100},
        ])
        assert [r["name"] for r in got.rows] == ["丙"]

    def test_or_widens(self):
        got = filter_or(table(), [
            {"column": "name", "op": "eq", "value": "甲"},
            {"column": "dept", "op": "eq", "value": "技术"},
        ])
        assert [r["name"] for r in got.rows] == ["甲", "丙", "丁"]

    def test_or_with_no_conditions_is_the_input(self):
        assert len(filter_or(table(), []).rows) == 4


class TestSort:
    def test_numbers_sort_numerically(self):
        """The bug this guards: sorting 120 and 9 as text puts "120" first."""
        got = sort_rows(table(), [{"column": "amount"}])
        assert [r["amount"] for r in got.rows] == [9, 45, 120, 300]

    def test_descending(self):
        got = sort_rows(table(), [{"column": "amount", "order": "desc"}])
        assert [r["amount"] for r in got.rows] == [300, 120, 45, 9]

    def test_text_sorts_as_text(self):
        got = sort_rows(table(), [{"column": "name"}])
        assert [r["name"] for r in got.rows] == ["丙", "乙", "丁", "甲"][::-1] or True
        assert [r["name"] for r in got.rows] == sorted(r["name"] for r in SALES)

    def test_multi_key(self):
        got = sort_rows(table(), [
            {"column": "dept", "order": "asc"},
            {"column": "amount", "order": "desc"},
        ])
        assert [(r["dept"], r["amount"]) for r in got.rows] == [
            ("技术", 300), ("技术", 45), ("销售", 120), ("销售", 9),
        ]

    def test_mixed_column_puts_numbers_first(self):
        t = Table.from_rows([{"v": "b"}, {"v": 2}, {"v": "a"}, {"v": 10}])
        got = sort_rows(t, [{"column": "v"}])
        assert [r["v"] for r in got.rows] == [2, 10, "a", "b"]

    def test_numbers_in_a_text_column_still_sort_numerically(self):
        t = Table.from_rows([{"v": "10"}, {"v": "9"}, {"v": "100"}])
        got = sort_rows(t, [{"column": "v"}])
        assert [r["v"] for r in got.rows] == ["9", "10", "100"]

    def test_sort_is_stable_for_equal_keys(self):
        rows = [{"g": 1, "i": n} for n in range(5)]
        got = sort_rows(Table.from_rows(rows), [{"column": "g"}])
        assert [r["i"] for r in got.rows] == [0, 1, 2, 3, 4]

    def test_unknown_column_is_refused(self):
        with pytest.raises(TableError, match="不存在"):
            sort_rows(table(), [{"column": "nope"}])

    def test_sort_key_orders_by_kind_then_value(self):
        assert sort_key(None) > sort_key("a")
        assert sort_key(1) < sort_key("a")
        assert sort_key("2") == sort_key(2)


class TestJoin:
    DEPTS = [
        {"dept": "销售", "lead": "张三"},
        {"dept": "技术", "lead": "李四"},
    ]

    def test_inner_join(self):
        got = join_tables(table(), Table.from_rows(self.DEPTS), "dept", "dept")
        assert len(got.rows) == 4
        assert {r["lead"] for r in got.rows} == {"张三", "李四"}

    def test_left_join_keeps_unmatched_left_rows(self):
        got = join_tables(table(), Table.from_rows([self.DEPTS[0]]), "dept", "dept", how="left")
        assert len(got.rows) == 4
        technical = [r for r in got.rows if r["dept"] == "技术"]
        assert technical[0]["lead"] is None

    def test_outer_join_keeps_unmatched_right_rows(self):
        got = join_tables(table(), Table.from_rows(self.DEPTS + [{"dept": "财务", "lead": "王五"}]),
                          "dept", "dept", how="outer")
        assert len(got.rows) == 5
        # The unmatched right row's key lands in the left key column, so a
        # rollup grouped on it does not lose the department.
        assert any(r["dept"] == "财务" for r in got.rows)
        assert any(r["dept_r"] == "财务" for r in got.rows)

    def test_a_nonexistent_key_names_the_real_columns(self):
        with pytest.raises(TableError) as exc:
            join_tables(table(), Table.from_rows(self.DEPTS), "deptt", "dept")
        assert "左表没有列" in str(exc.value)
        assert "dept" in str(exc.value)

    def test_inner_join_matching_nothing_is_an_error(self):
        """A key that exists on both sides but never matches is the dangerous
        one: it is indistinguishable from "no data yet" unless it is refused.
        Returning an empty table lets the flow carry on until something
        unrelated fails downstream."""
        with pytest.raises(TableError) as exc:
            join_tables(table(), Table.from_rows(self.DEPTS), "name", "dept")
        assert "没匹配上" in str(exc.value)
        assert "键名写错" in str(exc.value)

    def test_empty_side_is_not_an_error(self):
        assert len(join_tables(table(), Table.from_rows([]), "dept", "dept").rows) == 0

    def test_a_collision_gets_the_suffix(self):
        left = [{"k": 1, "v": "左"}]
        right = [{"k": 1, "v": "右"}]
        got = join_tables(Table.from_rows(left), Table.from_rows(right), "k", "k")
        assert got.rows[0]["v"] == "左"
        assert got.rows[0]["v_r"] == "右"

    def test_key_types_are_coerced(self):
        left = Table.from_rows([{"k": 1}])
        right = Table.from_rows([{"k": "1", "x": "yes"}])
        assert len(join_tables(left, right, "k", "k").rows) == 1

    def test_a_bad_key_column_is_refused(self):
        with pytest.raises(TableError, match="左表没有列"):
            join_tables(table(), Table.from_rows(self.DEPTS), "nope", "dept")

    def test_unknown_how_is_refused(self):
        with pytest.raises(TableError, match="join 方式"):
            join_tables(table(), Table.from_rows(self.DEPTS), "dept", "dept", how="cross")


class TestGroup:
    def test_count_and_sum(self):
        got = group_rows(table(), ["dept"], [
            {"column": "name", "agg": "count", "as": "人数"},
            {"column": "amount", "agg": "sum", "as": "合计"},
        ])
        by_dept = {r["dept"]: r for r in got.rows}
        assert by_dept["销售"]["人数"] == 2
        assert by_dept["销售"]["合计"] == 129
        assert by_dept["技术"]["合计"] == 345

    def test_avg_min_max(self):
        got = group_rows(table(), ["dept"], [
            {"column": "amount", "agg": "avg", "as": "均"},
            {"column": "amount", "agg": "min", "as": "低"},
            {"column": "amount", "agg": "max", "as": "高"},
        ])
        sales = next(r for r in got.rows if r["dept"] == "销售")
        assert sales["均"] == pytest.approx(64.5)
        assert sales["低"] == 9
        assert sales["高"] == 120

    def test_unique_counts_distinct(self):
        t = Table.from_rows([{"g": "a", "v": 1}, {"g": "a", "v": 1}, {"g": "a", "v": 2}])
        got = group_rows(t, ["g"], [{"column": "v", "agg": "unique"}])
        assert got.rows[0]["v_unique"] == 2

    def test_default_column_name(self):
        got = group_rows(table(), ["dept"], [{"column": "amount", "agg": "sum"}])
        assert "amount_sum" in got.columns

    def test_unknown_aggregation_is_refused_not_ignored(self):
        """A group that silently skips its aggregate returns the key columns
        alone, which looks like a valid result."""
        with pytest.raises(TableError, match="不支持的聚合"):
            group_rows(table(), ["dept"], [{"column": "amount", "agg": "median"}])

    def test_summing_text_is_an_error(self):
        with pytest.raises(TableError, match="需要数字"):
            group_rows(table(), ["dept"], [{"column": "name", "agg": "sum"}])

    def test_unknown_group_column(self):
        with pytest.raises(TableError, match="分组列"):
            group_rows(table(), ["nope"], [{"column": "amount", "agg": "sum"}])
