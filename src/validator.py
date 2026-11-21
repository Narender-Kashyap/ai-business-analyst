"""SQL validator: checks AI-generated SQL in CODE before it is executed.

Rules: one statement, read-only SELECT, only tables the role may read,
only known columns, no other regions for region-restricted roles,
warn on dates outside the data, forced LIMIT.
"""
import re
from dataclasses import dataclass, field

import sqlglot
import yaml
from sqlglot import exp

import db

SEMANTIC = yaml.safe_load(open("config/semantic_layer.yaml", encoding="utf-8"))
ROLES = yaml.safe_load(open("config/roles.yaml", encoding="utf-8"))["roles"]
PROFILE = db.data_profile()                      # facts read from the data, not hardcoded
REGIONS = set(PROFILE["regions"])
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}")   # starts with YYYY-MM-DD
MAX_ROWS = 1000

# Statement types that change data or the database itself
FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
             exp.Alter, exp.Command, exp.Pragma, exp.Attach, exp.Detach)


@dataclass
class ValidationResult:
    ok: bool
    sql: str                                   # the (possibly rewritten) safe SQL
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def validate(sql, role):
    errors, warnings = [], []
    allowed_tables = set(ROLES[role]["allowed_tables"])
    role_region = ROLES[role]["region"]

    # 1. Parse: must be exactly one valid statement
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except sqlglot.errors.ParseError as e:
        return ValidationResult(False, sql, [f"SQL could not be parsed: {e}"])
    if len(statements) != 1:
        return ValidationResult(False, sql, ["Exactly one SQL statement is allowed."])
    tree = statements[0]

    # 2. Read-only: the statement must be a query, with no write/DDL anywhere inside
    if not isinstance(tree, exp.Query):
        errors.append(f"Only SELECT queries are allowed (got {tree.key.upper()}).")
    for node in tree.find_all(*FORBIDDEN):
        errors.append(f"Forbidden operation: {node.key.upper()}.")

    # 3. Tables: every table must be allowed for this role (CTE names are fine)
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if name in cte_names:
            continue
        if name not in allowed_tables:
            if name in SEMANTIC["tables"]:               # a real table this role may not read: final
                errors.append(f"Access denied: role '{role}' cannot read table '{name}'.")
            else:                                        # not part of the model at all, e.g. the raw log
                errors.append(f"Unknown table '{name}'. Use only: {', '.join(sorted(allowed_tables))}.")

    # 4. Columns: must exist in the semantic layer (catches hallucinated columns)
    known_columns = {c for t in SEMANTIC["tables"].values() for c in t["columns"]}
    aliases = {a.alias.lower() for a in tree.find_all(exp.Alias)}
    aliases |= {c.lower() for cte in tree.find_all(exp.CTE) for c in cte.alias_column_names}
    for col in tree.find_all(exp.Column):
        name = col.name.lower()
        if name and name not in known_columns and name not in aliases:
            errors.append(f"Unknown column '{name}'.")

    # 5. Region scope: a north-only role may not ask about other regions
    if role_region:
        for lit in tree.find_all(exp.Literal):
            value = str(lit.this).lower()
            if lit.is_string and value in REGIONS and value != role_region:
                errors.append(f"Access denied: role '{role}' can only see region "
                              f"'{role_region}', but the query asks for '{value}'.")

    if errors:
        return ValidationResult(False, sql, sorted(set(errors)), warnings)

    # 6. Dates outside the data's coverage -> warning (results would be empty or partial)
    for lit in tree.find_all(exp.Literal):
        value = str(lit.this)
        if lit.is_string and DATE_PATTERN.match(value):
            day = value[:10]
            if day < PROFILE["start_date"] or day > PROFILE["end_date"]:
                warnings.append(f"Date {day} is outside the data coverage "
                                f"({PROFILE['start_date']} to {PROFILE['end_date']}); results may be empty or partial.")

    # 7. Force a row limit on the outer query
    limit = tree.args.get("limit")
    if limit is None:
        tree = tree.limit(MAX_ROWS)
        warnings.append(f"No LIMIT given; added LIMIT {MAX_ROWS}.")
    else:
        value = limit.expression
        if not (isinstance(value, exp.Literal) and value.is_int and int(value.this) <= MAX_ROWS):
            tree = tree.limit(MAX_ROWS)
            warnings.append(f"LIMIT capped at {MAX_ROWS}.")

    return ValidationResult(True, tree.sql(dialect="sqlite"), [], warnings)
