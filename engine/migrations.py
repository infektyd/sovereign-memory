"""
Sovereign Memory — Schema Migrations Runner.

Reads PRAGMA user_version from SQLite and runs pending numbered SQL scripts
from engine/migrations/*.sql in lexicographic order, in a single transaction.
Bumps user_version only on success.

Design principles:
- Idempotent: safe to call multiple times per process (module-level guard).
- Transactional: all pending migrations run as one atomic transaction.
- Additive only: migration scripts must never DROP tables or columns.
- Graceful: any failure raises, leaving user_version unchanged so the next
  start will retry from the same version.
"""

import logging
import os
import sqlite3

logger = logging.getLogger("sovereign.migrations")

# Directory containing numbered .sql migration files
_MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


def _load_migration_files():
    """
    Return sorted list of (version_int, filepath) pairs for every
    NNN_*.sql file found in the migrations directory.

    Files must be named NNN_description.sql where NNN is a zero-padded
    integer (e.g. 001_baseline.sql → version 1).
    """
    if not os.path.isdir(_MIGRATIONS_DIR):
        logger.warning("Migrations directory not found: %s", _MIGRATIONS_DIR)
        return []

    entries = []
    for fname in os.listdir(_MIGRATIONS_DIR):
        if not fname.endswith(".sql"):
            continue
        prefix = fname.split("_")[0]
        try:
            version = int(prefix)
        except ValueError:
            logger.warning("Skipping non-numeric migration file: %s", fname)
            continue
        entries.append((version, os.path.join(_MIGRATIONS_DIR, fname)))

    entries.sort(key=lambda x: x[0])
    return entries


def run_migrations(conn: sqlite3.Connection) -> None:
    """
    Run all pending migrations against *conn*.

    Reads current user_version, finds all migration scripts with a version
    number greater than current, and runs them in a single transaction.
    Bumps PRAGMA user_version to the highest applied version on success.

    Args:
        conn: An open sqlite3.Connection. The caller owns this connection;
              this function does not close it.

    Raises:
        Exception: Re-raises any error that occurs during migration, after
                   rolling back the transaction. user_version is left
                   unchanged so the next startup retries cleanly.
    """
    # Read current schema version
    row = conn.execute("PRAGMA user_version").fetchone()
    current_version = row[0] if row else 0

    migration_files = _load_migration_files()
    pending = [(v, p) for v, p in migration_files if v > current_version]

    if not pending:
        logger.debug("Schema is up-to-date (user_version=%d)", current_version)
        return

    target_version = pending[-1][0]
    logger.info(
        "Running %d migration(s): user_version %d → %d",
        len(pending), current_version, target_version,
    )

    # Run all pending scripts in one transaction
    try:
        conn.execute("BEGIN IMMEDIATE")

        for version, filepath in pending:
            logger.info("  Applying migration %03d: %s", version, os.path.basename(filepath))
            with open(filepath, "r", encoding="utf-8") as fh:
                sql = fh.read()
            # executescript auto-commits, so we use execute for each statement
            for statement in _split_statements(sql):
                if statement.strip():
                    conn.execute(statement)

        # Bump user_version — PRAGMA cannot be parameterized, must use f-string
        conn.execute(f"PRAGMA user_version = {target_version}")
        conn.commit()
        logger.info("Migrations complete. user_version=%d", target_version)

    except Exception:
        conn.rollback()
        logger.exception("Migration failed — rolled back. user_version unchanged.")
        raise


def _split_statements(sql: str):
    """
    Split a SQL script into individual statements on semicolons.

    Comment lines (starting with --) are stripped before splitting so that
    semicolons inside comments do not create spurious statements.
    Sufficient for DDL-only migration files (no string literals with semicolons).
    """
    # Strip comment lines first so embedded semicolons in comments are ignored
    clean_lines = [
        line for line in sql.splitlines()
        if not line.strip().startswith("--")
    ]
    clean_sql = "\n".join(clean_lines)

    for part in clean_sql.split(";"):
        statement = part.strip()
        if statement:
            yield statement
