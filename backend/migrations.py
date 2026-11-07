"""Transactional upgrade from one execution per item to an append-only attempt log."""

import sqlite3


def migrate_attempts(conn: sqlite3.Connection, schema: str) -> None:
    indexes = conn.execute("PRAGMA index_list(evaluation_executions)").fetchall()
    unique_item = any(
        row["unique"]
        and not row["partial"]
        and [
            r["name"] for r in conn.execute("SELECT name FROM pragma_index_info(?)", (row["name"],))
        ]
        == ["item_id"]
        for row in indexes
    )
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(evaluation_executions)")]
    if not unique_item and "provider_not_called" in columns:
        return
    # Use the current table definition rather than maintaining a second schema.
    definition = next(
        statement[statement.index("CREATE TABLE") :]
        for statement in schema.split(";")
        if "CREATE TABLE IF NOT EXISTS evaluation_executions(" in statement
    ).replace("IF NOT EXISTS evaluation_executions(", "evaluation_executions_upgrade(", 1)
    dependents = [
        r[0]
        for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='evaluation_executions' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL"
        )
    ]
    conn.execute(definition)
    # Names come only from SQLite's own table metadata, quoted as identifiers.
    names = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
    conn.execute(
        f"INSERT INTO evaluation_executions_upgrade ({names}) "
        f"SELECT {names} FROM evaluation_executions"
    )
    conn.execute("DROP TABLE evaluation_executions")
    conn.execute("ALTER TABLE evaluation_executions_upgrade RENAME TO evaluation_executions")
    for sql in dependents:
        conn.execute(sql)
