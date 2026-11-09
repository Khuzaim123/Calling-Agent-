"""Apply db/migrations/*.sql in order, once each, as va_owner (one transaction per file).

  python -m ops.migrate            # in Colab (uses ops.pg settings and secrets from the environment)
Records applied versions in va.schema_migrations. A failed file rolls back completely.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"


def _psql(psql: str, conn: dict, sql: str | None = None, file: Path | None = None) -> str:
    cmd = [psql, "-h", conn["host"], "-p", str(conn["port"]), "-U", conn["user"], "-d", conn["db"],
           "-v", "ON_ERROR_STOP=1", "-qAtX"]
    if file is not None:
        cmd += ["--single-transaction", "-f", str(file)]
    env = dict(os.environ, PGPASSWORD=conn["password"], PGCLIENTENCODING="UTF8")
    r = subprocess.run(cmd, input=sql, capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        raise RuntimeError(f"psql failed ({file.name if file else 'sql'}): {r.stderr.strip()[-1500:]}")
    return r.stdout.strip()


def migrate(psql: str, conn: dict, directory: Path = MIGRATIONS) -> list[str]:
    _psql(psql, conn, "CREATE SCHEMA IF NOT EXISTS va; CREATE TABLE IF NOT EXISTS va.schema_migrations "
                      "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());")
    done = set(_psql(psql, conn, "SELECT version FROM va.schema_migrations;").splitlines())
    applied = []
    for f in sorted(directory.glob("*.sql")):
        if f.stem in done:
            continue
        _psql(psql, conn, file=f)
        _psql(psql, conn, f"INSERT INTO va.schema_migrations (version) VALUES ('{f.stem}');")
        applied.append(f.stem)
        print("applied", f.name)
    if not applied:
        print("database schema up to date")
    return applied


def colab_conn() -> tuple[str, dict]:
    from . import pg
    return str(pg.BIN / "psql"), {"host": "127.0.0.1", "port": pg.PORT, "user": "va_owner", "db": "voiceagent",
                                  "password": os.environ["PG_OWNER_PASSWORD"]}


if __name__ == "__main__":
    psql, conn = colab_conn()
    migrate(psql, conn)
    sys.exit(0)
