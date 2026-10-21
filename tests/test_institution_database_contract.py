"""Check relational DDL semantics that SQL syntax parsing cannot establish."""

from pathlib import Path

import pytest

parser = pytest.importorskip("pglast")
from pglast import ast
from pglast.enums import AlterTableType, ConstrType, ObjectType

ROOT = Path(__file__).resolve().parents[1]


def relational_catalog(sources):
    columns, unique, primary, constraints = {}, {}, {}, set()
    foreign_count = 0

    def constraint(table, node, column=None):
        nonlocal foreign_count
        if node.conname:
            identity = (table, node.conname[:63])
            assert identity not in constraints, f"Duplicate constraint: {identity}"
            constraints.add(identity)
        if node.contype in {ConstrType.CONSTR_PRIMARY, ConstrType.CONSTR_UNIQUE}:
            keys = tuple(item.sval for item in node.keys) if node.keys else (column,)
            unique[table].append(set(keys))
            if node.contype == ConstrType.CONSTR_PRIMARY:
                primary[table] = keys
        elif node.contype == ConstrType.CONSTR_FOREIGN:
            parent = node.pktable.relname
            source = (
                tuple(item.sval for item in node.fk_attrs)
                if node.fk_attrs
                else (column,)
            )
            target = (
                tuple(item.sval for item in node.pk_attrs)
                if node.pk_attrs
                else primary[parent]
            )
            assert parent in columns, f"Missing parent table: {parent}"
            assert set(source) <= columns[table].keys(), (
                f"Missing FK columns: {table}{source}"
            )
            assert set(target) <= columns[parent].keys(), (
                f"Missing referenced columns: {parent}{target}"
            )
            assert set(target) in unique[parent], (
                f"Referenced key is not unique: {parent}{target}"
            )
            assert len(source) == len(target)
            for left, right in zip(source, target):
                assert columns[table][left] == columns[parent][right], (
                    f"FK type mismatch: {table}.{left} -> {parent}.{right}"
                )
            foreign_count += 1

    for name, sql in sources:
        for item in parser.parse_sql(sql):
            node = item.stmt
            if isinstance(node, ast.CreateStmt):
                table = node.relation.relname
                assert table not in columns, f"Duplicate table: {table} in {name}"
                definitions = [
                    field
                    for field in node.tableElts
                    if isinstance(field, ast.ColumnDef)
                ]
                columns[table] = {
                    field.colname: field.typeName.names[-1].sval
                    for field in definitions
                }
                unique[table] = []
                for field in definitions:
                    for rule in field.constraints or ():
                        constraint(table, rule, field.colname)
                for rule in node.tableElts:
                    if isinstance(rule, ast.Constraint):
                        constraint(table, rule)
            elif isinstance(node, ast.AlterTableStmt):
                table = node.relation.relname
                assert table in columns, f"Missing ALTER target: {table} in {name}"
                for command in node.cmds:
                    if command.subtype == AlterTableType.AT_AddColumn:
                        field = command.def_
                        if command.missing_ok and field.colname in columns[table]:
                            continue
                        assert field.colname not in columns[table], (
                            f"Duplicate column: {table}.{field.colname}"
                        )
                        columns[table][field.colname] = field.typeName.names[-1].sval
                        for rule in field.constraints or ():
                            constraint(table, rule, field.colname)
                    elif command.subtype == AlterTableType.AT_AddConstraint:
                        constraint(table, command.def_)
                    elif command.subtype == AlterTableType.AT_DropConstraint:
                        constraints.discard((table, command.name[:63]))
            elif (
                isinstance(node, ast.IndexStmt)
                and node.unique
                and node.whereClause is None
            ):
                if all(field.name for field in node.indexParams):
                    unique[node.relation.relname].append(
                        {field.name for field in node.indexParams}
                    )
            elif (
                isinstance(node, ast.DropStmt)
                and node.removeType == ObjectType.OBJECT_TABLE
            ):
                for name in node.objects:
                    table = name[-1].sval
                    columns.pop(table, None)
                    unique.pop(table, None)
                    primary.pop(table, None)
    return columns, foreign_count


def test_all_migration_foreign_keys_reference_existing_typed_unique_keys():
    from scripts.institution.migrate import bundles

    sources = [("farmer schema", (ROOT / "schema.sql").read_text(encoding="utf-8"))]
    sources.extend(
        (path.name, path.read_text(encoding="utf-8"))
        for _, paths, _ in bundles()
        for path in paths
    )
    columns, count = relational_catalog(sources)
    assert "institution_id" not in columns["users"]
    assert "demo_checkpoint_registry" not in columns
    assert "demo_login_sessions" not in columns
    assert count > 200


def test_relational_check_rejects_global_user_tenant_reference():
    sql = "CREATE TABLE users(id uuid PRIMARY KEY); CREATE TABLE child(id uuid PRIMARY KEY,institution_id uuid,user_id uuid); ALTER TABLE child ADD FOREIGN KEY(institution_id,user_id) REFERENCES users(institution_id,id);"
    with pytest.raises(AssertionError, match="Missing referenced columns"):
        relational_catalog([("invalid scoped user reference", sql)])


def test_relational_check_rejects_nonunique_parent_reference():
    sql = "CREATE TABLE parent(id uuid PRIMARY KEY,other_id uuid); CREATE TABLE child(id uuid PRIMARY KEY,parent_id uuid REFERENCES parent(other_id));"
    with pytest.raises(AssertionError, match="not unique"):
        relational_catalog([("invalid nonunique reference", sql)])
