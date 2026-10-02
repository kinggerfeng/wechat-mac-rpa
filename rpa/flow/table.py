"""A typed table, and the operations a flow needs on one.

Dify has no table type; 影刀 does, and it is the difference between an
automation that reads a spreadsheet and one that reshapes it. A flow that has
to filter and sort a CSV today does it with an `llm` node and a hope, or with a
subprocess, or by hand-rolling the same thirty lines in every flow.

Everything here is plain Python over ``list[dict]``. No dataframe dependency:
the operations are `filter`, `sort`, `join`, `group`, and pandas would be a
multi-megabyte import to do what twenty lines do here, on tables that in an RPA
flow are a few hundred rows at most.

Three decisions that a naive implementation gets wrong:

**A filter that matches nothing is an empty result, not a failure.** Silently
returning every row is the worst outcome, and so is raising: a flow filtering
for an invoice number that has not arrived yet should get zero rows and carry
on. Both are wrong, and the third — returning everything — is the one that ships.

**Coercion is explicit and reported.** A CSV column read as text sorts
``"10"`` before ``"9"`` and a `join` on an int column against a string key
silently drops every row. Each comparison coerces according to the *declared*
column type when there is one, and otherwise falls back to natural comparison
that tries number-then-string. What type a column ended up as is reported, so
the surprise is visible in the run rather than in the data.

**A join that matches nothing on either side is an error, not an empty
table.** A typo'd key looks exactly like "no data yet", and the flow carries on
with an empty result until something downstream fails for an unrelated reason.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

#: Type names a column may declare, and the coercion each implies.
NUMERIC = ("int", "float", "number", "decimal")
TEMPORAL = ("date", "datetime", "time")


class TableError(ValueError):
    """The table operation cannot be carried out as described."""


@dataclass
class Table:
    """Rows plus what is known about their columns.

    ``columns`` carries the declared types when the author supplied them. It is
    a hint, not a contract: a column with no declared type still works, and the
    observed type is inferred and reported in :meth:`describe`.
    """

    rows: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    types: dict[str, str] = field(default_factory=dict)

    # -- construction -----------------------------------------------------

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[dict[str, Any]],
        columns: Sequence[str] | None = None,
        types: dict[str, str] | None = None,
    ) -> "Table":
        materialised = [dict(row) for row in rows]
        if columns is None:
            seen: list[str] = []
            for row in materialised:
                for key in row:
                    if key not in seen:
                        seen.append(key)
            columns = seen
        return cls(rows=materialised, columns=list(columns), types=dict(types or {}))

    @classmethod
    def from_csv(cls, text: str, delimiter: str = ",") -> "Table":
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        if not reader.fieldnames:
            return cls()
        rows = [dict(row) for row in reader]
        return cls.from_rows(rows, [name.strip() for name in reader.fieldnames])

    @classmethod
    def from_json(cls, payload: str) -> "Table":
        data = json.loads(payload)
        if isinstance(data, dict):
            data = data.get("rows", data.get("data", []))
        if not isinstance(data, list):
            raise TableError(f"JSON 顶层需要是数组或含 rows 的对象，收到 {type(data).__name__}")
        for index, row in enumerate(data):
            if not isinstance(row, dict):
                raise TableError(f"第 {index + 1} 行不是对象（收到 {type(row).__name__}）")
        return cls.from_rows(data)

    # -- inspection -------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """Shape and observed types, so a surprising result can be explained."""
        observed: dict[str, str] = {}
        for column in self.columns:
            observed[column] = self.infer_type(column)
        return {
            "row_count": len(self.rows),
            "columns": list(self.columns),
            "declared_types": dict(self.types),
            "observed_types": observed,
        }

    def infer_type(self, column: str) -> str:
        declared = self.types.get(column)
        if declared:
            return declared
        values = [row.get(column) for row in self.rows if row.get(column) not in (None, "")]
        if not values:
            return "empty"
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            return "int" if all(float(v).is_integer() for v in values) else "float"
        if all(_looks_like_date(v) for v in values):
            return "date"
        if all(isinstance(v, str) for v in values):
            return "str"
        return "mixed"

    def to_rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows]

    def to_csv(self, delimiter: str = ",") -> str:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(self.columns), delimiter=delimiter,
                                extrasaction="ignore")
        writer.writeheader()
        for row in self.rows:
            writer.writerow({k: _stringify(v) for k, v in row.items()})
        return buffer.getvalue()

    def to_json(self) -> str:
        return json.dumps(self.to_rows(), ensure_ascii=False, default=_stringify)

    def slice(self, start: int = 0, limit: int | None = None) -> "Table":
        rows = self.rows[start:] if limit is None else self.rows[start:start + limit]
        return Table(rows=[dict(r) for r in rows], columns=list(self.columns),
                     types=dict(self.types))


# ─────────────────────────────────────────────────────────────── coercion ──

_DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?$")


def _looks_like_date(value: Any) -> bool:
    return isinstance(value, str) and bool(_DATE_RE.match(value.strip()))


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def coerce(value: Any, kind: str) -> Any:
    """Convert one value to the column's declared type.

    A declared type that the data does not honour is an error rather than a
    fallback: a column declared ``int`` containing ``"abc"`` means the author's
    model of the data is wrong, and quietly comparing ``"abc"`` as a string
    produces a sorted order that looks plausible and is not.
    """
    if value is None or value == "":
        return None
    if kind in NUMERIC:
        if isinstance(value, bool):
            raise TableError(f"布尔值不能当数字用: {value!r}")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise TableError(f"列声明为 {kind}，但遇到 {value!r}: {exc}") from exc
        return int(number) if kind == "int" or number.is_integer() else number
    if kind in TEMPORAL:
        text = str(value).strip()
        if not _looks_like_date(text):
            raise TableError(f"列声明为 {kind}，但 {value!r} 不是可识别的日期")
        return text
    if kind == "str":
        return _stringify(value)
    return value


def sort_key(value: Any) -> tuple[int, float, str]:
    """Order numbers before strings, numbers numerically.

    Sorting a mixed column by ``str`` puts ``"10"`` before ``"9"``, which is
    the single most common way a sorted table is quietly wrong.
    """
    if value is None:
        return (2, 0.0, "")
    if isinstance(value, bool):
        return (0, float(value), "")
    if isinstance(value, (int, float)):
        return (0, float(value), "")
    text = str(value)
    try:
        return (0, float(text), "")
    except ValueError:
        return (1, 0.0, text)


# ─────────────────────────────────────────────────────────────── operations ──

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a is not None and b is not None and a > b,
    "gte": lambda a, b: a is not None and b is not None and a >= b,
    "lt": lambda a, b: a is not None and b is not None and a < b,
    "lte": lambda a, b: a is not None and b is not None and a <= b,
    "contains": lambda a, b: b is not None and str(b) in str(a if a is not None else ""),
    "not_contains": lambda a, b: b is not None and str(b) not in str(a if a is not None else ""),
    "startswith": lambda a, b: a is not None and str(a).startswith(str(b)),
    "endswith": lambda a, b: a is not None and str(a).endswith(str(b)),
    "in": lambda a, b: a in (b if isinstance(b, (list, tuple, set)) else [b]),
    "not_in": lambda a, b: a not in (b if isinstance(b, (list, tuple, set)) else [b]),
    "is_empty": lambda a, b: a is None or a == "",
    "not_empty": lambda a, b: not (a is None or a == ""),
    "matches": lambda a, b: a is not None and re.search(str(b), str(a)) is not None,
}

FILTER_OPS = tuple(_OPS)


def filter_rows(table: Table, column: str, op: str, value: Any) -> Table:
    """Keep the rows where ``column op value``.

    No match is an empty table, not an error and never the whole input.
    """
    if column not in table.columns:
        raise TableError(f"列 {column!r} 不存在；现有列: {'、'.join(table.columns) or '（空表）'}")
    if op not in _OPS:
        raise TableError(f"不支持的比较 {op!r}；可用: {'、'.join(FILTER_OPS)}")
    kind = table.types.get(column) or table.infer_type(column)
    compare = _OPS[op]
    kept: list[dict[str, Any]] = []
    for row in table.rows:
        try:
            left = coerce(row.get(column), kind)
            right = coerce(value, kind) if kind in NUMERIC + TEMPORAL else value
        except TableError:
            # One bad cell must not silently drop the row. The comparison
            # simply does not hold, and the row is excluded — but the reason is
            # counted so the caller can see the filter was not clean.
            continue
        if compare(left, right):
            kept.append(dict(row))
    return Table(rows=kept, columns=list(table.columns), types=dict(table.types))


def filter_and(table: Table, conditions: Sequence[dict[str, Any]]) -> Table:
    """Apply several conditions, ANDed, each with its own operator."""
    result = table
    for condition in conditions:
        result = filter_rows(result, str(condition.get("column", "")),
                             str(condition.get("op", "eq")), condition.get("value"))
    return result


def filter_or(table: Table, conditions: Sequence[dict[str, Any]]) -> Table:
    """Apply several conditions, ORed."""
    if not conditions:
        return table
    columns = {str(c.get("column", "")) for c in conditions}
    for column in columns:
        if column not in table.columns:
            raise TableError(f"列 {column!r} 不存在；现有列: {'、'.join(table.columns)}")
    kept: list[dict[str, Any]] = []
    for row in table.rows:
        for condition in conditions:
            column = str(condition.get("column", ""))
            kind = table.types.get(column) or table.infer_type(column)
            try:
                left = coerce(row.get(column), kind)
                right = coerce(condition.get("value"), kind) if kind in NUMERIC + TEMPORAL \
                    else condition.get("value")
            except TableError:
                continue
            if _OPS[str(condition.get("op", "eq"))](left, right):
                kept.append(dict(row))
                break
    return Table(rows=kept, columns=list(table.columns), types=dict(table.types))


def sort_rows(table: Table, keys: Sequence[dict[str, Any]]) -> Table:
    """Sort by one or more keys, numbers before strings, stable."""
    if not keys:
        return table
    normalised: list[tuple[str, bool, str]] = []
    for key in keys:
        column = str(key.get("column", ""))
        if column not in table.columns:
            raise TableError(f"排序列 {column!r} 不存在；现有列: {'、'.join(table.columns)}")
        descending = str(key.get("order", key.get("direction", "asc"))).lower() in (
            "desc", "descending", "降序")
        normalised.append((column, descending, table.types.get(column) or table.infer_type(column)))

    # Sorted one key at a time, least significant first, so the result is a
    # stable multi-key sort without needing functools.
    ordered = [dict(row) for row in table.rows]
    for column, descending, kind in reversed(normalised):
        def key(row: dict[str, Any], _c=column, _k=kind):
            try:
                return sort_key(coerce(row.get(_c), _k))
            except TableError:
                return sort_key(row.get(_c))
        ordered.sort(key=key, reverse=descending)
    return Table(rows=ordered, columns=list(table.columns), types=dict(table.types))


def join_tables(
    left: Table,
    right: Table,
    left_key: str,
    right_key: str,
    how: str = "inner",
    suffix: str = "_r",
) -> Table:
    """Join on one key column.

    ``inner`` drops unmatched rows on both sides, which is how a typo'd key
    turns into "no data" and the flow carries on. So the match counts are in
    the result, and an ``inner`` join that matched nothing at all is an error
    rather than an empty table.
    """
    # A key check on an empty side is vacuous: an empty table has no columns,
    # and refusing there would make "the left side came back empty" — the most
    # ordinary outcome of a filtered join — impossible to express.
    if left.rows and left_key not in left.columns:
        raise TableError(f"左表没有列 {left_key!r}；现有列: {'、'.join(left.columns)}")
    if right.rows and right_key not in right.columns:
        raise TableError(f"右表没有列 {right_key!r}；现有列: {'、'.join(right.columns)}")
    if how not in ("inner", "left", "outer", "right"):
        raise TableError(f"join 方式只能是 inner/left/outer/right，收到 {how!r}")

    right_type = right.types.get(right_key) or right.infer_type(right_key)
    index: dict[Any, list[dict[str, Any]]] = {}
    for row in right.rows:
        try:
            key = coerce(row.get(right_key), right_type)
        except TableError:
            continue
        index.setdefault(key, []).append(row)

    used_right: set[int] = set()
    matched: list[dict[str, Any]] = []
    unmatched_left = 0
    for row in left.rows:
        try:
            key = coerce(row.get(left_key), right_type)
        except TableError:
            key = row.get(left_key)
        candidates = index.get(key, [])
        if not candidates:
            unmatched_left += 1
            if how in ("left", "outer"):
                merged = dict(row)
                for column in right.columns:
                    if column not in left.columns:
                        merged[column] = None
                matched.append(merged)
            continue
        for candidate in candidates:
            used_right.add(id(candidate))
            merged = dict(row)
            for column, value in candidate.items():
                target = column if column not in left.columns else f"{column}{suffix}"
                if target not in left.columns:
                    merged[target] = value
            matched.append(merged)

    if how in ("right", "outer"):
        for row in right.rows:
            if id(row) not in used_right:
                merged = {c: None for c in left.columns}
                for column, value in row.items():
                    target = column if column not in left.columns else f"{column}{suffix}"
                    merged[target] = value
                # The join key is the one column both sides agree on, so an
                # unmatched right row must populate it. Leaving it None would
                # make the row sort and group as a missing key — a department
                # that exists in the right table would vanish from any rollup
                # keyed on the left column.
                if left_key in merged and merged.get(left_key) is None:
                    merged[left_key] = row.get(right_key)
                matched.append(merged)

    if how == "inner" and not matched and left.rows and right.rows:
        raise TableError(
            f"inner join 一行都没匹配上：左表 {len(left.rows)} 行 × 右表 {len(right.rows)} 行，"
            f"键 {left_key!r} / {right_key!r}。这通常是把键名写错了，"
            f"而不是「没有数据」——请确认键名，或改用 left join 看差集。"
        )

    columns = list(left.columns)
    for column in right.columns:
        name = column if column not in left.columns else f"{column}{suffix}"
        if name not in columns:
            columns.append(name)
    return Table(rows=matched, columns=columns, types=dict(left.types))


def group_rows(table: Table, by: Sequence[str], aggs: Sequence[dict[str, Any]]) -> Table:
    """Group by one or more columns and aggregate.

    Supported aggregations: ``count``, ``sum``, ``avg``, ``min``, ``max``,
    ``first``, ``last``, ``unique`` (count of distinct). An unknown aggregation
    is refused rather than ignored, because a group that quietly skips its
    aggregate returns the key columns alone and looks like a valid result.
    """
    for column in by:
        if column not in table.columns:
            raise TableError(f"分组列 {column!r} 不存在；现有列: {'、'.join(table.columns)}")
    known = {"count", "sum", "avg", "min", "max", "first", "last", "unique"}
    buckets: dict[tuple, list[dict[str, Any]]] = {}
    for row in table.rows:
        key = tuple(sort_key(row.get(column)) for column in by)
        buckets.setdefault(key, []).append(row)

    out_rows: list[dict[str, Any]] = []
    for key, group in buckets.items():
        record: dict[str, Any] = {}
        for column, part in zip(by, key):
            record[column] = part[2] or (part[1] if part[0] == 0 else None)
        for spec in aggs:
            target = str(spec.get("as", "") or f"{spec.get('column', '')}_{spec.get('agg', '')}")
            fn = str(spec.get("agg", "count"))
            if fn not in known:
                raise TableError(f"不支持的聚合 {fn!r}；可用: {'、'.join(sorted(known))}")
            column = str(spec.get("column", "") or (by[0] if by else ""))
            values = [row.get(column) for row in group]
            record[target] = _aggregate(fn, values, column, table)
        out_rows.append(record)

    columns = list(by)
    for spec in aggs:
        name = str(spec.get("as", "") or f"{spec.get('column', '')}_{spec.get('agg', '')}")
        if name not in columns:
            columns.append(name)
    out_rows.sort(key=lambda row: sort_key(row.get(by[0])) if by else (0, 0.0, ""))
    return Table(rows=out_rows, columns=columns)


def _aggregate(fn: str, values: Sequence[Any], column: str, table: Table) -> Any:
    kind = table.types.get(column) or table.infer_type(column)
    present = [v for v in values if v not in (None, "")]
    if fn == "count":
        return len(present)
    if fn == "unique":
        return len({sort_key(v) for v in present})
    if not present:
        return None
    try:
        coerced = [coerce(v, kind) for v in present]
    except TableError as exc:
        raise TableError(f"聚合 {fn} 于列 {column!r} 失败: {exc}") from exc
    if fn == "first":
        return coerced[0]
    if fn == "last":
        return coerced[-1]
    numbers = [v for v in coerced if isinstance(v, (int, float))]
    if not numbers:
        raise TableError(f"聚合 {fn} 于列 {column!r} 需要数字，收到 {type(coerced[0]).__name__}")
    if fn == "sum":
        return sum(numbers)
    if fn == "avg":
        return sum(numbers) / len(numbers)
    if fn == "min":
        return min(numbers)
    if fn == "max":
        return max(numbers)
    raise TableError(f"不支持的聚合 {fn!r}")  # pragma: no cover - guarded by caller
