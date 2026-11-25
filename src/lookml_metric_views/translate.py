"""Translate LookML explores into Unity Catalog metric view definitions.

One explore becomes one metric view: the base view is the `source`, every explore join becomes a
metric view join (nested when its `sql_on` hangs off another join), dimensions become fields and
measures become measures. Every translated or skipped item gets a status and a reason:

- exact:    same meaning, mechanical translation
- adapted:  same meaning, the value is represented differently (for example a month as a date)
- review:   translated, but results can differ from Looker; a person must check it
- manual:   not translated; needs a person (Liquid, list measures, many-to-many joins, ...)
- skipped:  deliberately not exposed (hidden in Looker)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .loader import Explore, Project

EXACT, ADAPTED, REVIEW, MANUAL, SKIPPED = "exact", "adapted", "review", "manual", "skipped"
STATUSES = (EXACT, ADAPTED, REVIEW, MANUAL, SKIPPED)

REF = re.compile(r"\$\{\s*([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))?\s*\}")
LIQUID = re.compile(r"\{%|\{\{")
CONSTANT_REF = re.compile(r"@\{\s*\w+\s*\}")
AGG_FUNC = re.compile(
    r"\b(sum|avg|average|count|min|max|median|percentile|percentile_approx|stddev|stddev_pop|"
    r"stddev_samp|variance|var_pop|var_samp|approx_count_distinct|collect_list|collect_set|"
    r"array_agg|string_agg|listagg|any_value|first|last|bool_and|bool_or|count_if|sum_if|"
    r"approx_percentile|mode)\s*\(",
    re.IGNORECASE,
)

DEFAULT_TIMEFRAMES = ["raw", "time", "date", "week", "month", "quarter", "year"]
# A subquery that refers back to the metric view's base table (`source.`) is rejected by Databricks.
CORRELATED_SOURCE = re.compile(r"\(\s*select\b(?:(?!\(\s*select\b).)*?\bsource\.", re.I | re.S)


class Untranslatable(Exception):
    """Raised inside rendering when an expression can't be translated faithfully."""


@dataclass
class Item:
    explore: str
    kind: str  # field | measure | join | source | filter | explore
    looker: str
    status: str
    reason: str = ""
    name: str = ""


@dataclass
class Result:
    model: str
    explore: str
    view_name: str
    metric_view: Optional[dict]
    items: List[Item] = field(default_factory=list)
    measure_names: List[str] = field(default_factory=list)
    field_names: List[str] = field(default_factory=list)

    @property
    def metric_view_name(self) -> str:
        base = f"{self.model}__{self.explore}" if self.model else self.explore
        return re.sub(r"[^A-Za-z0-9_]", "_", base)


# ---------------------------------------------------------------------------------------------
# Small helpers


def _yes(value) -> bool:
    return str(value).strip().lower() in ("yes", "true")


def _clean_sql(sql: Optional[str]) -> str:
    if sql is None:
        return ""
    sql = sql.strip()
    if sql.endswith(";;"):
        sql = sql[:-2]
    return sql.strip()


def _title(name: str) -> str:
    return " ".join(part.capitalize() for part in name.replace("_", " ").split())


def _has_agg(sql: str) -> bool:
    return bool(AGG_FUNC.search(sql))


def _paren(expr: str) -> str:
    expr = expr.strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", expr) or re.fullmatch(r"`[^`]+`", expr):
        return expr
    return f"({expr})"


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# ---------------------------------------------------------------------------------------------
# Explore structure


@dataclass
class Alias:
    name: str  # alias used in LookML (explore name or join name)
    view: dict
    path: str  # metric view reference path: "source" or "orders" or "orders.users"
    relationship: str = "many_to_one"
    is_base: bool = False


class ExploreTranslator:
    def __init__(self, project: Project, explore: Explore, dialect: str = "databricks",
                 table_map: Optional[Dict[str, str]] = None, timezone_conversion: bool = False):
        self.project = project
        self.explore = explore
        self.dialect = dialect.lower()
        self.table_map = table_map or {}
        self.timezone_conversion = timezone_conversion
        self.items: List[Item] = []
        self.aliases: Dict[str, Alias] = {}
        self.measure_name_of: Dict[Tuple[str, str], str] = {}
        self.taken_names: set = set()

    # -- reporting ---------------------------------------------------------------------------
    def note(self, kind: str, looker: str, status: str, reason: str = "", name: str = "") -> None:
        self.items.append(Item(self.explore.name, kind, looker, status, reason, name))

    def unique_name(self, wanted: str) -> str:
        base = re.sub(r"[^A-Za-z0-9_]", "_", wanted).strip("_") or "field"
        name, n = base, 1
        while name in self.taken_names:
            n += 1
            name = f"{base}_{n}"
        self.taken_names.add(name)
        return name

    # -- sources -----------------------------------------------------------------------------
    def table_source(self, view: dict, kind_label: str) -> Tuple[Optional[str], str, str]:
        """Return (source, status, reason) for a view."""
        if view.get("sql_table_name"):
            table = _clean_sql(view["sql_table_name"]).strip()
            if LIQUID.search(table):
                return None, MANUAL, "sql_table_name uses Liquid; pick the Unity Catalog table by hand."
            if CONSTANT_REF.search(table):
                return None, MANUAL, ("sql_table_name uses a LookML constant (@{...}) that the "
                                      "project's manifest doesn't define.")
            plain = table.replace("`", "").replace('"', "")
            mapped = self.table_map.get(plain) or self._map_prefix(plain)
            if mapped:
                return mapped, ADAPTED, f"Table '{plain}' mapped to '{mapped}'."
            if self.dialect != "databricks":
                return plain, REVIEW, (f"Table '{plain}' comes from a {self.dialect} connection; "
                                       "point it at the Unity Catalog copy (use --table-map).")
            return plain, EXACT, ""
        derived = view.get("derived_table")
        if derived:
            if derived.get("explore_source"):
                return None, MANUAL, ("Native derived table (explore_source); rewrite it as SQL or a "
                                      "materialized view first.")
            sql = _clean_sql(derived.get("sql"))
            if not sql:
                return None, MANUAL, "Derived table without SQL."
            if LIQUID.search(sql):
                return None, MANUAL, "Derived table SQL uses Liquid templating."
            inlined: List[str] = []
            if "${" in sql:
                try:
                    sql = self.inline_table_refs(sql, inlined, depth=0)
                except Untranslatable as exc:
                    return None, MANUAL, str(exc)
            persisted = any(k in derived for k in ("datagroup_trigger", "persist_for", "sql_trigger_value",
                                                   "materialized_view", "increment_key"))
            if CONSTANT_REF.search(sql):
                return None, MANUAL, ("Derived table SQL uses a LookML constant (@{...}) that the "
                                      "project's manifest doesn't define.")
            try:
                sql = self.transpile_snippet(sql)
            except Untranslatable as exc:
                return None, MANUAL, f"Derived table SQL: {exc}"
            reason = "Derived table becomes a SQL source."
            if inlined:
                reason += (" Inlined the derived tables it builds on (" + ", ".join(inlined) + "); "
                           "consider creating them as views or materialized views.")
            if persisted:
                reason += " It was persisted in Looker (PDT); consider a Unity Catalog materialized view."
            return sql, ADAPTED, reason
        name = view.get("name", "")
        mapped = self.table_map.get(name) or self._map_prefix(name)
        if mapped:
            return mapped, ADAPTED, f"View has no sql_table_name; mapped '{name}' to '{mapped}'."
        return name, REVIEW, (f"View has no sql_table_name; Looker would query a table named '{name}'. "
                              "Check the Unity Catalog name (use --table-map).")

    def inline_table_refs(self, sql: str, inlined: List[str], depth: int) -> str:
        """Replace ${view.SQL_TABLE_NAME} with that view's table or derived SQL."""
        if depth > 6:
            raise Untranslatable("Derived tables reference each other too deeply.")

        def repl(match: re.Match) -> str:
            view_name, attr = match.group(1), match.group(2)
            if attr != "SQL_TABLE_NAME":
                raise Untranslatable(f"Derived table SQL references '${{{view_name}.{attr}}}'.")
            view = self.project.views.get(view_name)
            if view is None:
                raise Untranslatable(f"Derived table SQL references unknown view '{view_name}'.")
            if view.get("sql_table_name"):
                table = _clean_sql(view["sql_table_name"]).replace("`", "")
                return self.table_map.get(table) or self._map_prefix(table) or table
            inner = _clean_sql((view.get("derived_table") or {}).get("sql"))
            if not inner:
                raise Untranslatable(f"Derived table '{view_name}' it builds on has no SQL (native derived table).")
            if LIQUID.search(inner):
                raise Untranslatable(f"Derived table '{view_name}' it builds on uses Liquid templating.")
            inlined.append(view_name)
            return "(" + self.inline_table_refs(inner, inlined, depth + 1) + ")"

        return REF.sub(repl, sql)

    def _map_prefix(self, table: str) -> Optional[str]:
        if "*" in self.table_map and "." not in table:
            return self.table_map["*"].rstrip("*").rstrip(".") + "." + table
        for prefix, target in self.table_map.items():
            if prefix.endswith(".*") and table.startswith(prefix[:-1]):
                return target.rstrip(".*") + "." + table[len(prefix) - 1:]
        return None

    # -- dialect -----------------------------------------------------------------------------
    def transpile_snippet(self, sql: str) -> str:
        """Translate one LookML SQL snippet to Databricks SQL, keeping ${...} references intact."""
        if self.dialect in ("databricks", "spark", "spark2") or not sql.strip():
            return sql
        refs = {}

        def protect(match: re.Match) -> str:
            key = f"mvref{len(refs)}x"
            refs[key] = match.group(0)
            return key

        guarded = REF.sub(protect, sql)
        out = self.transpile(guarded)
        for key, original in refs.items():
            out = re.sub(rf"`?\b{key}\b`?", lambda _m, o=original: o, out)
        return out

    def transpile(self, expr: str, statement: bool = False) -> str:
        if self.dialect in ("databricks", "spark", "spark2"):
            return expr
        try:
            import sqlglot  # imported lazily: not needed for Databricks-dialect projects
        except ImportError as exc:  # pragma: no cover
            raise Untranslatable("sqlglot is needed to translate from another SQL dialect.") from exc
        protected = {}

        def keep(match: re.Match) -> str:
            key = f"__mv_measure_{len(protected)}__"
            protected[key] = match.group(0)
            return key

        expr_in = re.sub(r"MEASURE\(`[^`]+`\)", keep, expr)
        try:
            out = sqlglot.transpile(expr_in, read=self.dialect, write="databricks")[0]
        except Exception as exc:
            raise Untranslatable(f"Could not translate the SQL from {self.dialect}: {exc}") from exc
        for key, original in protected.items():
            out = out.replace(key, original)
        return out

    # -- field lookup ------------------------------------------------------------------------
    def find_dimension(self, view: dict, name: str) -> Tuple[Optional[dict], Optional[str], Optional[dict]]:
        """Return (dimension, timeframe, dimension_group) for a field name in a view."""
        for dim in view.get("dimensions", []) or []:
            if dim.get("name") == name:
                return dim, None, None
        for group in view.get("dimension_groups", []) or []:
            gname = group.get("name", "")
            if group.get("type", "time") == "duration":
                for interval in group.get("intervals", []) or ["day"]:
                    if name == f"{interval}s_{gname}":
                        return None, interval, group
                continue
            if name.startswith(gname + "_"):
                frame = name[len(gname) + 1:]
                frames = group.get("timeframes") or DEFAULT_TIMEFRAMES
                if frame in frames or frame == "raw":
                    return None, frame, group
        return None, None, None

    def find_measure(self, view: dict, name: str) -> Optional[dict]:
        for m in view.get("measures", []) or []:
            if m.get("name") == name:
                return m
        return None

    # -- expression rendering ----------------------------------------------------------------
    def render(self, sql: str, alias: Alias, allow_measures: bool = False, depth: int = 0) -> str:
        if depth > 25:
            raise Untranslatable("Field references go too deep (possible cycle).")
        if LIQUID.search(sql):
            raise Untranslatable("Uses Liquid templating ({% %} or {{ }}); needs metric view "
                                 "parameters or a fixed choice.")
        sql = self.transpile_snippet(sql)

        def repl(match: re.Match) -> str:
            first, second = match.group(1), match.group(2)
            if second is None:
                if first == "TABLE":
                    return alias.path
                if first == "SQL_TABLE_NAME":
                    raise Untranslatable("References ${SQL_TABLE_NAME}.")
                return self.ref(alias, first, allow_measures, depth)
            target = self.aliases.get(first)
            if target is None:
                if first == alias.view.get("name"):
                    target = alias
                else:
                    raise Untranslatable(f"References '{first}.{second}', which is not joined in this explore.")
            if second == "SQL_TABLE_NAME":
                raise Untranslatable(f"References ${{{first}.SQL_TABLE_NAME}}.")
            return self.ref(target, second, allow_measures, depth)

        return REF.sub(repl, sql)

    def ref(self, alias: Alias, name: str, allow_measures: bool, depth: int) -> str:
        dim, frame, group = self.find_dimension(alias.view, name)
        if dim is not None:
            return _paren(self.dimension_expr(alias, dim, depth + 1))
        if group is not None:
            return _paren(self.group_expr(alias, group, frame, depth + 1)[0])
        measure = self.find_measure(alias.view, name)
        if measure is not None:
            if not allow_measures:
                raise Untranslatable(f"Uses measure '{name}' inside a dimension or aggregate.")
            key = (alias.name, name)
            if key not in self.measure_name_of:
                raise Untranslatable(f"Depends on measure '{alias.name}.{name}', which was not translated.")
            return f"MEASURE(`{self.measure_name_of[key]}`)"
        if any(p.get("name") == name for p in alias.view.get("parameters", []) or []):
            raise Untranslatable(f"Uses Looker parameter '{name}'; needs a metric view parameter.")
        raise Untranslatable(f"References unknown field '{alias.name}.{name}'.")

    def dimension_expr(self, alias: Alias, dim: dict, depth: int = 0) -> str:
        sql = dim.get("sql")
        if sql is None:
            sql = "${TABLE}." + dim.get("name", "")
        return self.render(_clean_sql(sql), alias, depth=depth)

    def group_expr(self, alias: Alias, group: dict, frame: str, depth: int = 0) -> Tuple[str, str, str]:
        """Return (expr, status, reason) for one timeframe of a dimension group."""
        gtype = group.get("type", "time")
        if gtype == "duration":
            start = self.render(_clean_sql(group.get("sql_start")), alias, depth=depth)
            end = self.render(_clean_sql(group.get("sql_end")), alias, depth=depth)
            unit = {"day": "DAY", "hour": "HOUR", "minute": "MINUTE", "second": "SECOND",
                    "week": "WEEK", "month": "MONTH", "quarter": "QUARTER", "year": "YEAR"}.get(frame)
            if unit is None:
                raise Untranslatable(f"Duration interval '{frame}' is not supported.")
            return (f"TIMESTAMPDIFF({unit}, {_paren(start)}, {_paren(end)})", ADAPTED,
                    "Duration as whole units between start and end; check rounding against Looker.")
        raw = self.render(_clean_sql(group.get("sql")) or "${TABLE}." + group.get("name", ""), alias, depth=depth)
        datatype = str(group.get("datatype", "timestamp")).lower()
        if datatype == "epoch":
            raw = f"TIMESTAMP_SECONDS({_paren(raw)})"
        elif datatype == "yyyymmdd":
            raw = f"TO_DATE(CAST({_paren(raw)} AS STRING), 'yyyyMMdd')"
        e = _paren(raw)
        frames = {
            "raw": (raw, EXACT, ""),
            "time": (raw, EXACT, ""),
            "date": (f"TO_DATE({e})", EXACT, ""),
            "week": (f"TO_DATE(DATE_TRUNC('WEEK', {e}))", EXACT, "Weeks start on Monday, as in Looker's default."),
            "month": (f"TO_DATE(DATE_TRUNC('MONTH', {e}))", ADAPTED, "Looker shows 'YYYY-MM'; here it is the first day of the month."),
            "quarter": (f"TO_DATE(DATE_TRUNC('QUARTER', {e}))", ADAPTED, "Looker shows 'YYYY-Qn'; here it is the first day of the quarter."),
            "year": (f"YEAR({e})", EXACT, ""),
            "hour": (f"DATE_TRUNC('HOUR', {e})", EXACT, ""),
            "minute": (f"DATE_TRUNC('MINUTE', {e})", EXACT, ""),
            "second": (f"DATE_TRUNC('SECOND', {e})", EXACT, ""),
            "day_of_week": (f"DATE_FORMAT({e}, 'EEEE')", EXACT, ""),
            "day_of_week_index": (f"MOD(DAYOFWEEK({e}) + 5, 7)", EXACT, "Monday = 0, as in Looker."),
            "day_of_month": (f"DAYOFMONTH({e})", EXACT, ""),
            "day_of_year": (f"DAYOFYEAR({e})", EXACT, ""),
            "week_of_year": (f"WEEKOFYEAR({e})", ADAPTED, "ISO week numbers; Looker can count weeks differently."),
            "month_name": (f"DATE_FORMAT({e}, 'MMMM')", EXACT, ""),
            "month_num": (f"MONTH({e})", EXACT, ""),
            "quarter_of_year": (f"CONCAT('Q', QUARTER({e}))", EXACT, ""),
            "hour_of_day": (f"HOUR({e})", EXACT, ""),
            "minute_of_hour": (f"MINUTE({e})", EXACT, ""),
            "millisecond": (f"DATE_TRUNC('MILLISECOND', {e})", EXACT, ""),
            "microsecond": (f"DATE_TRUNC('MICROSECOND', {e})", EXACT, ""),
            "time_of_day": (f"DATE_FORMAT({e}, 'HH:mm')", EXACT, ""),
        }
        bucket = re.fullmatch(r"(minute|second)(\d+)", frame)
        if bucket:
            secs = int(bucket.group(2)) * (60 if bucket.group(1) == "minute" else 1)
            frames[frame] = (f"TIMESTAMP_SECONDS(FLOOR(UNIX_TIMESTAMP({raw}) / {secs}) * {secs})", ADAPTED,
                             f"Rounded down to {bucket.group(2)}-{bucket.group(1)} buckets in UTC seconds.")
        if frame.startswith("fiscal_"):
            raise Untranslatable(f"Fiscal timeframe '{frame}' depends on the model's fiscal_month_offset; "
                                 "add it by hand.")
        if frame not in frames:
            raise Untranslatable(f"Timeframe '{frame}' is not supported yet.")
        expr, status, reason = frames[frame]
        if datatype in ("epoch", "yyyymmdd") and status == EXACT:
            status, reason = ADAPTED, f"Converted from datatype {datatype}."
        return expr, status, reason

    # -- filters (Looker filter expressions) --------------------------------------------------
    def filter_condition(self, alias: Alias, field_ref: str, value: str) -> str:
        if "." in field_ref:
            alias_name, fname = field_ref.split(".", 1)
            target = self.aliases.get(alias_name)
            if target is None:
                raise Untranslatable(f"Filter on '{field_ref}', which is not joined in this explore.")
        else:
            target, fname = alias, field_ref
        dim, frame, group = self.find_dimension(target.view, fname)
        if dim is not None:
            col = _paren(self.dimension_expr(target, dim))
            dtype = dim.get("type", "string")
        elif group is not None:
            col = _paren(self.group_expr(target, group, frame)[0])
            dtype = "date"
        else:
            raise Untranslatable(f"Filter on unknown field '{field_ref}'.")
        return self.looker_filter(col, str(value), dtype)

    def looker_filter(self, col: str, value: str, dtype: str) -> str:
        value = value.strip()
        if dtype == "yesno":
            if value.lower() == "yes":
                return col
            if value.lower() == "no":
                return f"NOT {col}"
        if dtype in ("date", "time") or re.search(r"\b(day|days|week|weeks|month|months|year|years|ago|this|last|before|after)\b", value, re.I):
            raise Untranslatable(f"Date filter expression '{value}' needs a hand-written condition.")
        if value.upper() == "NULL":
            return f"{col} IS NULL"
        if value.upper() == "-NULL":
            return f"{col} IS NOT NULL"
        if value.upper() == "EMPTY":
            return f"({col} IS NULL OR {col} = '')"
        if value.upper() == "-EMPTY":
            return f"({col} IS NOT NULL AND {col} <> '')"
        if dtype == "number":
            m = re.fullmatch(r"(>=|<=|>|<|=|!=|<>)\s*(-?\d+(?:\.\d+)?)", value)
            if m:
                return f"{col} {m.group(1)} {m.group(2)}"
            parts = [p.strip() for p in value.split(",")]
            if all(re.fullmatch(r"-?\d+(?:\.\d+)?", p) for p in parts):
                return f"{col} IN ({', '.join(parts)})" if len(parts) > 1 else f"{col} = {parts[0]}"
            raise Untranslatable(f"Number filter '{value}' needs a hand-written condition.")
        parts = [p.strip() for p in re.split(r"(?<!\^),", value) if p.strip()]
        pos, neg = [], []
        for part in parts:
            target = neg if part.startswith("-") else pos
            part = part[1:] if part.startswith("-") else part
            part = part.replace("^,", ",")
            if "%" in part or "_" in part and part.startswith("%"):
                target.append(("like", part))
            else:
                target.append(("eq", part))
        conds = []
        eq_pos = [p for k, p in pos if k == "eq"]
        if eq_pos:
            conds.append(f"{col} IN ({', '.join(_quote_literal(v) for v in eq_pos)})" if len(eq_pos) > 1
                         else f"{col} = {_quote_literal(eq_pos[0])}")
        conds += [f"{col} LIKE {_quote_literal(p)}" for k, p in pos if k == "like"]
        condition = " OR ".join(conds) if conds else ""
        neg_conds = []
        eq_neg = [p for k, p in neg if k == "eq"]
        if eq_neg:
            neg_conds.append(f"{col} NOT IN ({', '.join(_quote_literal(v) for v in eq_neg)})")
        neg_conds += [f"{col} NOT LIKE {_quote_literal(p)}" for k, p in neg if k == "like"]
        if condition and (len(conds) > 1 and neg_conds):
            condition = f"({condition})"
        all_parts = ([condition] if condition else []) + neg_conds
        if not all_parts:
            raise Untranslatable(f"Empty filter '{value}'.")
        return " AND ".join(all_parts)

    # -- main --------------------------------------------------------------------------------
    def build(self) -> Result:
        raw = self.explore.raw
        base_name = raw.get("view_name") or raw.get("from") or raw.get("name")
        result = Result(self.explore.model, self.explore.name, base_name, None)
        base_view = self.project.views.get(base_name)
        if base_view is None:
            self.note("explore", self.explore.name, MANUAL, f"Base view '{base_name}' was not found in the project.")
            result.items = self.items
            return result
        if raw.get("extends__all") or raw.get("extends"):
            self.note("explore", self.explore.name, REVIEW, "Explore uses 'extends'; inherited joins and settings are not merged yet.")

        base_alias = Alias(raw.get("name"), base_view, "source", is_base=True)
        self.aliases[raw.get("name")] = base_alias
        if base_name != raw.get("name"):
            self.aliases.setdefault(base_name, base_alias)

        source, status, reason = self.table_source(base_view, "source")
        self.note("source", base_name, status if source else MANUAL, reason)
        if source is None:
            blocked = f"Explore's base view can't be translated. {reason}"
            for dim in base_view.get("dimensions", []) or []:
                self.note("field", f"{base_name}.{dim.get('name')}", MANUAL, blocked)
            for group in base_view.get("dimension_groups", []) or []:
                self.note("field", f"{base_name}.{group.get('name')}", MANUAL, blocked)
            for m in base_view.get("measures", []) or []:
                self.note("measure", f"{base_name}.{m.get('name')}", MANUAL, blocked)
            result.items = self.items
            return result

        mv: dict = {"version": "1.1"}
        description = raw.get("description") or base_view.get("description")
        label = raw.get("label")
        comment = description or (f"Looker explore '{label}'" if label else None)
        mv["comment"] = (comment + " " if comment else "") + f"(Converted from Looker explore '{self.explore.name}'.)"
        mv["source"] = source

        joins_out = self.build_joins(raw.get("joins", []) or [])
        if joins_out:
            mv["joins"] = joins_out

        filters = []
        for key in ("sql_always_where",):
            if raw.get(key):
                try:
                    filters.append(self.render(_clean_sql(raw[key]), base_alias))
                    self.note("filter", key, EXACT, "Becomes the metric view filter, applied to every query.")
                except Untranslatable as exc:
                    self.note("filter", key, MANUAL, str(exc))
        for key in ("always_filter", "conditionally_filter"):
            if raw.get(key):
                self.note("filter", key, MANUAL, f"'{key}' is a default filter in Looker's UI, not part of the "
                          "metric definitions. Decide whether it belongs in the metric view filter.")
        for key in ("access_filter", "access_filters", "required_access_grants"):
            if raw.get(key):
                self.note("filter", key, MANUAL, "Row-level security from Looker; recreate it with Unity Catalog "
                          "row filters or ABAC policies.")
        if raw.get("sql_always_having"):
            self.note("filter", "sql_always_having", MANUAL, "No metric view equivalent; filter after querying.")
        if filters:
            mv["filter"] = " AND ".join(_paren(f) for f in filters) if len(filters) > 1 else filters[0]

        allowed = self.allowed_fields(raw)
        fields_out, measures_out = [], []
        for alias in self.aliases.values():
            if alias.is_base and alias.name != raw.get("name"):
                continue  # registered twice under base view name
            self.add_fields(alias, fields_out, allowed)
        self.add_measures([a for a in self.aliases.values() if not (a.is_base and a.name != raw.get("name"))],
                          measures_out, allowed)

        if fields_out:
            mv["fields"] = fields_out
        if measures_out:
            mv["measures"] = measures_out
        result.metric_view = mv if (fields_out or measures_out) else None
        if result.metric_view is None:
            self.note("explore", self.explore.name, MANUAL, "Nothing could be translated for this explore.")
        result.items = self.items
        result.field_names = [f["name"] for f in fields_out]
        result.measure_names = [m["name"] for m in measures_out]
        return result

    def allowed_fields(self, raw: dict):
        """Explore-level `fields:` restriction, as a predicate on (alias, field)."""
        spec = raw.get("fields")
        if not spec:
            return lambda alias, name: True
        include, exclude = set(), set()
        all_fields = False
        for entry in spec:
            entry = entry.strip()
            neg = entry.startswith("-")
            entry = entry[1:] if neg else entry
            if entry == "ALL_FIELDS*":
                all_fields = True
                continue
            (exclude if neg else include).add(entry)

        def allowed(alias: str, name: str) -> bool:
            full = f"{alias}.{name}"
            if full in exclude or f"{alias}*" in exclude:
                return False
            if all_fields:
                return True
            return full in include or f"{alias}*" in include

        if any("." in e and e.endswith("*") and not e.endswith(".*") and e.split(".")[1] != "*" for e in include | exclude):
            self.note("explore", "fields", REVIEW, "Explore 'fields' uses sets; set members are not expanded yet.")
        return allowed

    def build_joins(self, joins: List[dict]) -> List[dict]:
        pending = []
        for join in joins:
            name = join.get("name")
            view_name = join.get("from") or join.get("view_name") or name
            view = self.project.views.get(view_name)
            if view is None:
                self.note("join", name, MANUAL, f"Joined view '{view_name}' was not found.")
                continue
            relationship = str(join.get("relationship", "many_to_one")).lower()
            pending.append((join, name, view))
            self.aliases[name] = Alias(name, view, name, relationship=relationship)

        # Decide parents from sql_on references.
        parent_of: Dict[str, Optional[str]] = {}
        base = next(a for a in self.aliases.values() if a.is_base)
        for join, name, _ in pending:
            refs = set()
            for m in REF.finditer(join.get("sql_on", "") or ""):
                if m.group(2) is not None:
                    refs.add(m.group(1))
            if join.get("foreign_key") and not join.get("sql_on"):
                refs.add(base.name)
            others = refs - {name}
            others = {o if o in self.aliases else o for o in others}
            if len(others) == 1:
                parent = others.pop()
                parent_of[name] = None if (parent == base.name or self.aliases.get(parent) is base) else parent
            elif not others:
                parent_of[name] = None
            else:
                non_base = [o for o in others if not (o == base.name or self.aliases.get(o) is base)]
                parent_of[name] = non_base[0] if len(non_base) == 1 else "__multi__"

        # Compute reference paths (nested joins use dot paths through join names).
        def path(name: str, seen=()) -> str:
            parent = parent_of.get(name)
            if parent in (None, "__multi__") or parent not in self.aliases or parent in seen:
                return name
            return path(parent, seen + (name,)) + "." + name

        for _, name, _ in pending:
            self.aliases[name].path = path(name)

        out_by_name: Dict[str, dict] = {}
        children: Dict[Optional[str], List[str]] = {}
        for join, name, view in pending:
            alias = self.aliases[name]
            parent = parent_of.get(name)
            spec: dict = {"name": name}
            source, s_status, s_reason = self.table_source(view, "join")
            if join.get("sql_table_name"):
                source, s_status, s_reason = _clean_sql(join["sql_table_name"]), EXACT, ""
            if source is None:
                self.note("join", name, MANUAL, s_reason)
                del self.aliases[name]
                continue
            spec["source"] = source
            status, reasons = EXACT, []
            if s_status != EXACT:
                status, reasons = s_status, [s_reason]
            try:
                if join.get("sql_on"):
                    on = self.render(_clean_sql(join["sql_on"]), alias)
                    # Inside an `on` clause a join is referred to by its own name, not its dot path.
                    for other in sorted(self.aliases.values(), key=lambda a: -len(a.path)):
                        if "." in other.path:
                            on = on.replace(other.path + ".", other.name + ".")
                    spec["on"] = on
                elif join.get("foreign_key"):
                    pk = next((d for d in view.get("dimensions", []) if _yes(d.get("primary_key"))), None)
                    if pk is None:
                        raise Untranslatable("Join uses foreign_key but the joined view has no primary key.")
                    fk = self.render("${" + join["foreign_key"] + "}", base)
                    spec["on"] = f"{fk} = {_paren(self.dimension_expr(alias, pk))}"
                elif "UNNEST" in (join.get("sql") or "").upper():
                    raise Untranslatable("Join unnests a nested array (BigQuery UNNEST). Metric views can't "
                                         "join an array; flatten it in a view with explode() first.")
                else:
                    raise Untranslatable("Join has neither sql_on nor foreign_key (for example sql: or a cross join).")
            except Untranslatable as exc:
                self.note("join", name, MANUAL, str(exc))
                del self.aliases[name]
                continue
            if parent == "__multi__":
                status = REVIEW
                reasons.append("sql_on references several joined views; it was placed at the top level. "
                               "Check the join path.")
                parent = None
            rel = alias.relationship
            if rel == "one_to_many":
                spec["cardinality"] = "one_to_many"
                reasons.append("one_to_many: its measures aggregate at their own grain; its dimensions can't be "
                               "fields (metric view rule).")
                status = REVIEW if status == EXACT else status
            elif rel == "many_to_many":
                self.note("join", name, MANUAL, "many_to_many joins are not supported by metric views (the first "
                          "matching row would be used). Model a bridge table.")
                del self.aliases[name]
                continue
            jtype = str(join.get("type", "left_outer")).lower()
            if jtype != "left_outer":
                status = REVIEW
                reasons.append(f"Looker join type '{jtype}'; metric views always use LEFT OUTER joins.")
            if join.get("sql_where"):
                status = REVIEW
                reasons.append("Join-level sql_where is not translated.")
            if join.get("required_access_grants"):
                reasons.append("required_access_grants: recreate with Unity Catalog permissions.")
                status = REVIEW
            if rel in ("many_to_one", "one_to_one") and status == EXACT:
                spec["rely"] = {"at_most_one_match": True} if rel == "one_to_one" else None
                if spec["rely"] is None:
                    del spec["rely"]
            self.note("join", name, status, " ".join(r for r in reasons if r), name=name)
            out_by_name[name] = spec
            children.setdefault(parent, []).append(name)

        def attach(parent: Optional[str]) -> List[dict]:
            out = []
            for child in children.get(parent, []):
                spec = out_by_name[child]
                nested = attach(child)
                if nested:
                    parent_rel = self.aliases[child].relationship
                    for n in nested:
                        if parent_rel == "one_to_many" and n.get("cardinality") != "one_to_many":
                            n["cardinality"] = "one_to_many"
                    spec["joins"] = nested
                out.append(spec)
            return out

        return attach(None)

    def _under_one_to_many(self, alias: Alias) -> bool:
        parts = alias.path.split(".")
        return any(self.aliases.get(p) and self.aliases[p].relationship == "one_to_many" for p in parts)

    def display_prefix(self, alias: Alias) -> str:
        join = next((j for j in self.explore.raw.get("joins", []) or [] if j.get("name") == alias.name), {})
        return (join.get("view_label") or alias.view.get("label") or _title(alias.name))

    def add_fields(self, alias: Alias, out: List[dict], allowed) -> None:
        prefix = "" if alias.is_base else alias.name + "_"
        view_label = self.display_prefix(alias)
        for dim in alias.view.get("dimensions", []) or []:
            name = dim.get("name")
            looker = f"{alias.name}.{name}"
            if not allowed(alias.name, name):
                continue
            if _yes(dim.get("hidden")):
                self.note("field", looker, SKIPPED, "Hidden in Looker; still used inside other definitions.")
                continue
            if self._under_one_to_many(alias):
                self.note("field", looker, MANUAL, "Dimension of a one_to_many join can't be a metric view field.")
                continue
            if dim.get("required_access_grants"):
                self.note("field", looker, MANUAL, "required_access_grants: recreate with column masks or permissions.")
                continue
            dtype = str(dim.get("type", "string")).lower()
            try:
                status, reason = EXACT, ""
                if dtype in ("string", "number", "date", "date_time", "zipcode", "int", "time", "date_raw",
                             "date_date", "date_time_of_day"):
                    expr = self.dimension_expr(alias, dim)
                elif dtype == "yesno":
                    expr = self.dimension_expr(alias, dim)
                    status, reason = ADAPTED, "Boolean field; Looker displays it as Yes/No."
                elif dtype in ("tier", "bin"):
                    expr, status, reason = self.tier_expr(alias, dim)
                elif dtype == "location":
                    raise Untranslatable("Location type needs separate latitude and longitude fields.")
                elif dtype in ("distance", "duration"):
                    raise Untranslatable(f"Dimension type '{dtype}' is not supported yet.")
                else:
                    raise Untranslatable(f"Dimension type '{dtype}' is not supported yet.")
                if dim.get("case"):
                    expr, status, reason = self.case_expr(alias, dim)
                if dim.get("html") or dim.get("link") or dim.get("links"):
                    reason = (reason + " " if reason else "") + "Looker html/links are presentation only and were dropped."
            except Untranslatable as exc:
                self.note("field", looker, MANUAL, str(exc))
                continue
            if CORRELATED_SOURCE.search(expr):
                self.note("field", looker, MANUAL, "Correlated subquery that refers to the base table: Databricks "
                          "rejects this in a metric view (tested). Move it into a view or the source query.")
                continue
            if re.search(r"\(\s*select\b", expr, re.I):
                status = REVIEW
                reason = (reason + " " if reason else "") + ("Contains a subquery. Databricks accepted this form "
                          "in testing, but check results and query cost; a view or the source query is often better.")
            mv_name = self.unique_name(prefix + name)
            entry = {"name": mv_name, "expr": expr}
            label = dim.get("label") or _title(name)
            entry["display_name"] = label if alias.is_base else f"{view_label} {label}"
            if dim.get("description"):
                entry["comment"] = dim["description"]
            out.append(entry)
            self.note("field", looker, status, reason.strip(), name=mv_name)

        for group in alias.view.get("dimension_groups", []) or []:
            gname = group.get("name")
            if _yes(group.get("hidden")):
                self.note("field", f"{alias.name}.{gname}", SKIPPED, "Hidden in Looker.")
                continue
            if self._under_one_to_many(alias):
                self.note("field", f"{alias.name}.{gname}", MANUAL, "Dimension group of a one_to_many join can't be a field.")
                continue
            gtype = group.get("type", "time")
            if gtype == "duration":
                frames = group.get("intervals") or ["day"]
                names = [(f, f"{f}s_{gname}") for f in frames]
            else:
                frames = group.get("timeframes") or DEFAULT_TIMEFRAMES
                names = [(f, f"{gname}_{f}") for f in frames]
            tz_note = ""
            if self.timezone_conversion and gtype == "time" and str(group.get("convert_tz", "yes")).lower() != "no" and \
                    str(group.get("datatype", "timestamp")).lower() in ("timestamp", "datetime", "epoch"):
                tz_note = ("Looker may convert this time to the user's timezone (convert_tz); "
                           "metric views return the stored value.")
            for frame, lname in names:
                looker = f"{alias.name}.{lname}"
                if not allowed(alias.name, lname) and not allowed(alias.name, gname):
                    continue
                try:
                    expr, status, reason = self.group_expr(alias, group, frame)
                except Untranslatable as exc:
                    self.note("field", looker, MANUAL, str(exc))
                    continue
                if tz_note and frame not in ("year",):
                    status = REVIEW if status == EXACT else status
                    reason = (reason + " " + tz_note).strip()
                mv_name = self.unique_name(("" if alias.is_base else alias.name + "_") + lname)
                label = group.get("label") or _title(gname)
                frame_label = _title(frame) if gtype != "duration" else _title(lname)
                display = f"{label} {frame_label}" if gtype != "duration" else frame_label
                entry = {"name": mv_name, "expr": expr,
                         "display_name": display if alias.is_base else f"{view_label} {display}"}
                if group.get("description"):
                    entry["comment"] = group["description"]
                out.append(entry)
                self.note("field", looker, status, reason, name=mv_name)

        for param in alias.view.get("parameters", []) or []:
            self.note("field", f"{alias.name}.{param.get('name')}", MANUAL,
                      "Looker parameter; recreate as a metric view parameter if it is needed.")
        for flt in alias.view.get("filters", []) or []:
            self.note("field", f"{alias.name}.{flt.get('name')}", MANUAL,
                      "Templated filter (filter field); usually used with Liquid. Needs a parameter or a fixed condition.")

    def tier_expr(self, alias: Alias, dim: dict) -> Tuple[str, str, str]:
        col = _paren(self.dimension_expr(alias, dim))
        tiers = [float(t) for t in (dim.get("tiers") or [])]
        style = str(dim.get("style", "classic")).lower()
        if not tiers:
            raise Untranslatable("Tier dimension without tiers.")
        def fmt(x: float) -> str:
            return str(int(x)) if x == int(x) else str(x)
        parts = [f"WHEN {col} < {fmt(tiers[0])} THEN 'Below {fmt(tiers[0])}'"]
        for low, high in zip(tiers, tiers[1:]):
            if style == "integer":
                label = f"{fmt(low)} to {fmt(high - 1)}"
            else:
                label = f"{fmt(low)} to {fmt(high)}"
            parts.append(f"WHEN {col} >= {fmt(low)} AND {col} < {fmt(high)} THEN '{label}'")
        parts.append(f"WHEN {col} >= {fmt(tiers[-1])} THEN '{fmt(tiers[-1])} or Above'")
        expr = "CASE " + " ".join(parts) + " ELSE 'Undefined' END"
        return expr, ADAPTED, "Tiers become a CASE expression; Looker's sort order of tiers is not kept."

    def case_expr(self, alias: Alias, dim: dict) -> Tuple[str, str, str]:
        case = dim["case"]
        whens = case.get("whens") or []
        if not whens:
            raise Untranslatable("Case dimension without 'when' branches.")
        parts = []
        for w in whens:
            cond = self.render(_clean_sql(w.get("sql")), alias)
            parts.append(f"WHEN {cond} THEN {_quote_literal(str(w.get('label', '')))}")
        else_label = case.get("else")
        expr = "CASE " + " ".join(parts) + (f" ELSE {_quote_literal(str(else_label))}" if else_label else "") + " END"
        return expr, ADAPTED, "Looker case becomes a CASE expression; Looker's custom sort order is not kept."

    # -- measures ------------------------------------------------------------------------------
    def add_measures(self, aliases: List[Alias], out: List[dict], allowed) -> None:
        # Measures can depend on measures of any view in the explore: resolve them in passes.
        pending = [(a, m) for a in aliases for m in (a.view.get("measures", []) or [])]
        pending.sort(key=lambda am: 1 if str(am[1].get("type", "count")).lower() == "number" else 0)
        for _ in range(6):
            if not pending:
                break
            still = [(a, m) for a, m in pending if not self.add_measure(a, m, out, allowed, final=False)]
            if len(still) == len(pending):
                break
            pending = still
        for a, m in pending:
            self.add_measure(a, m, out, allowed, final=True)

    def add_measure(self, alias: Alias, m: dict, out: List[dict], allowed, final: bool) -> bool:
        name = m.get("name")
        looker = f"{alias.name}.{name}"
        if not allowed(alias.name, name):
            return True
        mtype = str(m.get("type", "count")).lower()
        status, reasons = EXACT, []
        try:
            if m.get("required_access_grants"):
                raise Untranslatable("required_access_grants: recreate with permissions.")
            expr = self.measure_expr(alias, m, mtype, reasons)
            if reasons and reasons[0].startswith("REVIEW:"):
                status = REVIEW
                reasons[0] = reasons[0][len("REVIEW:"):].strip()
            elif reasons and reasons[0].startswith("ADAPTED:"):
                status = ADAPTED
                reasons[0] = reasons[0][len("ADAPTED:"):].strip()
        except Untranslatable as exc:
            if not final and "Depends on measure" in str(exc):
                return False
            self.note("measure", looker, MANUAL, str(exc))
            return True
        if CORRELATED_SOURCE.search(expr):
            self.note("measure", looker, MANUAL, "Correlated subquery that refers to the base table: Databricks "
                      "rejects this in a metric view (tested). Move it into a view or the source query.")
            return True
        if re.search(r"\(\s*select\b", expr, re.I):
            if status == EXACT:
                status = REVIEW
            reasons.append("Contains a subquery; check results and query cost.")
        mv_name = self.unique_name(("" if alias.is_base else alias.name + "_") + name)
        self.measure_name_of[(alias.name, name)] = mv_name
        label = m.get("label") or _title(name)
        entry = {"name": mv_name, "expr": expr,
                 "display_name": label if alias.is_base else f"{self.display_prefix(alias)} {label}"}
        if m.get("description"):
            entry["comment"] = m["description"]
        if _yes(m.get("hidden")):
            reasons.append("Hidden in Looker; kept because other measures can depend on it.")
        out.append(entry)
        self.note("measure", looker, status, " ".join(reasons).strip(), name=mv_name)
        return True

    def pk_expr(self, alias: Alias) -> Optional[str]:
        for dim in alias.view.get("dimensions", []) or []:
            if _yes(dim.get("primary_key")):
                return self.dimension_expr(alias, dim)
        return None

    def measure_filters(self, alias: Alias, m: dict) -> Optional[str]:
        raw_filters: List[Tuple[str, str]] = []

        def collect(entry) -> None:
            if isinstance(entry, list):
                for e in entry:
                    collect(e)
            elif isinstance(entry, dict):
                if "field" in entry and "value" in entry:
                    raw_filters.append((entry["field"], entry["value"]))
                else:
                    for k, v in entry.items():
                        raw_filters.append((k, v))

        collect(m.get("filters__all") or [])
        collect(m.get("filters") or [])
        if not raw_filters:
            return None
        conds = [self.filter_condition(alias, k, v) for k, v in raw_filters]
        if len(conds) == 1:
            return conds[0]
        return " AND ".join(_paren(c) for c in conds)

    def measure_expr(self, alias: Alias, m: dict, mtype: str, reasons: List[str]) -> str:
        fanout = (not alias.is_base) and alias.relationship in ("many_to_one", "one_to_one") \
            and not self._under_one_to_many(alias)
        cond = self.measure_filters(alias, m)
        sql = m.get("sql")
        body = self.render(_clean_sql(sql), alias) if sql is not None and mtype not in ("number", "string", "date", "yesno") else None

        def agg(func: str, inner: str) -> str:
            out = f"{func}({inner})"
            if cond:
                out += f" FILTER (WHERE {cond})"
            return out

        if mtype == "count":
            if alias.is_base:
                return agg("COUNT", "1")
            pk = self.pk_expr(alias)
            if pk is None:
                raise Untranslatable("Count on a joined view needs a primary key (Looker counts distinct keys).")
            return agg("COUNT", f"DISTINCT {_paren(pk)}")
        if mtype == "count_distinct":
            if body is None:
                raise Untranslatable("count_distinct without sql.")
            return agg("COUNT", f"DISTINCT {_paren(body)}")
        if mtype in ("sum", "average", "median", "percentile"):
            if body is None:
                raise Untranslatable(f"{mtype} without sql.")
            if mtype == "sum":
                expr = agg("SUM", body)
            elif mtype == "average":
                expr = agg("AVG", body)
            elif mtype == "median":
                expr = agg("MEDIAN", body)
            else:
                p = float(m.get("percentile", 50)) / 100.0
                expr = agg("PERCENTILE", f"{body}, {p}")
            if fanout:
                reasons.insert(0, "REVIEW: Looker computes this over distinct rows of the joined view "
                                  "(symmetric aggregates); here it counts once per row of the explore's base view. "
                                  "Define it in a metric view based on this view, or use a one_to_many design.")
            return expr
        if mtype in ("min", "max"):
            if body is None:
                raise Untranslatable(f"{mtype} without sql.")
            return agg(mtype.upper(), body)
        if mtype in ("sum_distinct", "average_distinct", "median_distinct", "percentile_distinct"):
            raise Untranslatable(f"{mtype} uses sql_distinct_key; needs a hand-written aggregation at the right grain.")
        if mtype == "number":
            if sql is None:
                raise Untranslatable("Measure of type number without sql.")
            expr = self.render(_clean_sql(sql), alias, allow_measures=True)
            if cond:
                raise Untranslatable("Filters on a number measure are not supported; filter its input measures.")
            if "MEASURE(" not in expr and not _has_agg(expr):
                raise Untranslatable("Number measure without an aggregate or measure reference.")
            return expr
        if mtype in ("string", "date", "yesno", "date_time", "time"):
            if sql is None:
                raise Untranslatable(f"Measure of type {mtype} without sql.")
            expr = self.render(_clean_sql(sql), alias, allow_measures=True)
            if "MEASURE(" not in expr and not _has_agg(expr):
                raise Untranslatable(f"Measure of type {mtype} must contain an aggregate function.")
            return expr
        if mtype in ("list",):
            raise Untranslatable("List measures have no metric view equivalent (use collect_set in a query).")
        if mtype in ("running_total", "percent_of_total", "percent_of_previous", "period_over_period"):
            raise Untranslatable(f"{mtype} depends on the query result; consider a metric view window measure.")
        raise Untranslatable(f"Measure type '{mtype}' is not supported yet.")


def translate_project(project: Project, dialect: str = "databricks",
                      table_map: Optional[Dict[str, str]] = None,
                      timezone_conversion: bool = False) -> List[Result]:
    results = []
    for explore in project.explores:
        translator = ExploreTranslator(project, explore, dialect, table_map, timezone_conversion)
        try:
            results.append(translator.build())
        except Exception as exc:  # never lose the whole run because of one explore
            res = Result(explore.model, explore.name, explore.raw.get("from") or explore.name, None)
            res.items = translator.items + [Item(explore.name, "explore", explore.name, MANUAL,
                                                 f"Unexpected error while translating: {exc}")]
            results.append(res)
    return results
