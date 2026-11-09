"""PostgreSQL in Colab: install, bootstrap roles/databases, backup to Drive, restore.

Roles (least privilege; grants on business tables are added with the schema in Phase 6):
  va_owner : owns database `voiceagent`, runs migrations
  va_app   : login used by n8n workflows for business data (DML only, no DDL)
  n8n_sys  : owns database `n8n` (n8n's internal tables)
Network: listens on 127.0.0.1 only; scram-sha-256 for TCP, peer auth on the local socket.

  python -m ops.pg backup            # one backup now
  python -m ops.pg backup-loop 600   # every 10 minutes (started as a service by the notebook)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from .env import DRIVE_ROOT, RUNTIME
from .services import Service

PG_MAJOR = os.environ.get("PG_MAJOR", "16")
BIN = Path(f"/usr/lib/postgresql/{PG_MAJOR}/bin")
DATA = RUNTIME / "pgdata"
PORT = 5432
DATABASES = {"voiceagent": "va_owner", "n8n": "n8n_sys"}
KEEP_BACKUPS = 48


def sh(cmd: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed ({r.returncode}): {cmd}\n{r.stderr[-1500:]}")
    if not quiet and r.stdout.strip():
        print(r.stdout.strip()[-1500:])
    return r


def install() -> None:
    if (BIN / "postgres").exists():
        print(f"PostgreSQL {PG_MAJOR} already installed")
        return
    codename = sh("lsb_release -cs", quiet=True).stdout.strip()
    print(f"Installing PostgreSQL {PG_MAJOR} from apt.postgresql.org for Ubuntu {codename} ...")
    sh("install -d /usr/share/postgresql-common/pgdg && "
       "curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc "
       "https://www.postgresql.org/media/keys/ACCC4CF8.asc")
    sh(f"echo 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] "
       f"https://apt.postgresql.org/pub/repos/apt {codename}-pgdg main' > /etc/apt/sources.list.d/pgdg.list")
    sh("apt-get update -qq")
    from . import apt                                  # .deb files cached on Drive after the first install
    apt.install([f"postgresql-{PG_MAJOR}", f"postgresql-client-{PG_MAJOR}"], cache_name="apt-postgresql")
    sh("service postgresql stop || true", check=False)   # we run our own cluster in /content/runtime/pgdata
    if not (BIN / "postgres").exists():
        raise RuntimeError(f"PostgreSQL {PG_MAJOR} binaries missing after install")


def service() -> Service:
    return Service("postgres", [str(BIN / "postgres"), "-D", str(DATA), "-p", str(PORT)], user="postgres",
                   health=lambda: sh(f"{BIN}/pg_isready -h 127.0.0.1 -p {PORT}", check=False, quiet=True).returncode == 0)


def init_cluster() -> bool:
    """Create the cluster if needed. Returns True when a fresh cluster was created."""
    if (DATA / "PG_VERSION").exists():
        return False
    DATA.mkdir(parents=True, exist_ok=True)
    sh(f"chown -R postgres:postgres {DATA} && chmod 700 {DATA}")
    pwfile = RUNTIME / "tmp" / "pgsu.pw"
    pwfile.write_text(os.environ["PG_SUPERUSER_PASSWORD"])
    sh(f"chown postgres {pwfile} && chmod 600 {pwfile}")
    try:
        sh(f"runuser -u postgres -- {BIN}/initdb -D {DATA} -U postgres --auth-local=peer "
           f"--auth-host=scram-sha-256 --pwfile={pwfile} -E UTF8 --locale=C.UTF-8", quiet=True)
    finally:
        pwfile.unlink(missing_ok=True)
    with open(DATA / "postgresql.conf", "a") as fh:
        fh.write("\n# voice-agent overrides\nlisten_addresses = '127.0.0.1'\npassword_encryption = 'scram-sha-256'\n"
                 "shared_buffers = 256MB\nmax_connections = 50\nlog_min_duration_statement = 500\n"
                 "log_connections = on\n")
    (DATA / "pg_hba.conf").write_text(
        "local all postgres peer\n"
        "local all all scram-sha-256\n"
        "host all all 127.0.0.1/32 scram-sha-256\n")
    sh(f"chown postgres:postgres {DATA}/pg_hba.conf {DATA}/postgresql.conf")
    print("Initialised new cluster at", DATA)
    return True


def psql(sql: str, db: str = "postgres") -> str:
    """Run SQL as the postgres superuser over the local socket (peer auth)."""
    r = subprocess.run(["runuser", "-u", "postgres", "--", str(BIN / "psql"), "-p", str(PORT), "-d", db,
                        "-v", "ON_ERROR_STOP=1", "-qAtX"], input=sql, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"psql failed on {db}: {r.stderr.strip()[-800:]}")
    return r.stdout.strip()


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def bootstrap_roles() -> None:
    """Idempotent: create or update roles (passwords from secrets) and databases."""
    roles = {"va_owner": "PG_OWNER_PASSWORD", "va_app": "PG_APP_PASSWORD", "n8n_sys": "PG_N8N_PASSWORD"}
    for role, env in roles.items():
        pw = _lit(os.environ[env])
        psql(f"DO $$BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{role}') THEN "
             f"CREATE ROLE {role} LOGIN PASSWORD {pw}; ELSE ALTER ROLE {role} LOGIN PASSWORD {pw}; END IF; END$$;")
    for db, owner in DATABASES.items():
        if psql(f"SELECT 1 FROM pg_database WHERE datname='{db}'") != "1":
            psql(f"CREATE DATABASE {db} OWNER {owner} ENCODING 'UTF8' TEMPLATE template0")
    psql("REVOKE ALL ON DATABASE voiceagent FROM PUBLIC; GRANT CONNECT ON DATABASE voiceagent TO va_app;"
         "REVOKE ALL ON DATABASE n8n FROM PUBLIC;")
    psql("REVOKE CREATE ON SCHEMA public FROM PUBLIC;", db="voiceagent")
    print("roles and databases ready:", ", ".join(f"{d} (owner {o})" for d, o in DATABASES.items()))


def backup_dir(db: str) -> Path:
    d = DRIVE_ROOT / "backups" / "pg" / db
    d.mkdir(parents=True, exist_ok=True)
    return d


def backup() -> list[Path]:
    """pg_dump custom format to Drive; atomic rename; retention KEEP_BACKUPS per db."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = []
    for db in DATABASES:
        tmp = RUNTIME / "tmp" / f"{db}_{ts}.dump"
        # pg_dump runs as `postgres`, which cannot write into root-owned /content/runtime/tmp,
        # so it streams to stdout and this (root) process writes the file.
        with open(tmp, "wb") as fh:
            r = subprocess.run(["runuser", "-u", "postgres", "--", str(BIN / "pg_dump"), "-p", str(PORT), "-Fc", db],
                               stdout=fh, stderr=subprocess.PIPE)
        if r.returncode != 0 or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"pg_dump of {db} failed (exit {r.returncode}): "
                               f"{r.stderr.decode(errors='replace').strip()[-500:]}")
        dest = backup_dir(db) / tmp.name
        partial = dest.with_suffix(".partial")
        partial.write_bytes(tmp.read_bytes())
        partial.rename(dest)
        tmp.unlink()
        dumps = sorted(backup_dir(db).glob(f"{db}_*.dump"))
        for old in dumps[:-KEEP_BACKUPS]:
            old.unlink()
        out.append(dest)
    print("backups:", ", ".join(f"{p.name} ({p.stat().st_size // 1024} KiB)" for p in out))
    return out


def latest_backup(db: str) -> Path | None:
    dumps = sorted(backup_dir(db).glob(f"{db}_*.dump"))
    return dumps[-1] if dumps else None


def restore_latest() -> dict[str, str]:
    """Restore each database from its newest Drive backup into a fresh cluster."""
    res = {}
    for db, owner in DATABASES.items():
        b = latest_backup(db)
        if b is None:
            res[db] = "no backup (fresh database)"
            continue
        tmp = RUNTIME / "tmp" / b.name
        tmp.write_bytes(b.read_bytes())
        sh(f"chown postgres {tmp}")
        r = subprocess.run(["runuser", "-u", "postgres", "--", str(BIN / "pg_restore"), "-p", str(PORT),
                            "-d", db, "--no-owner", f"--role={owner}", "--exit-on-error", str(tmp)],
                           capture_output=True, text=True)
        tmp.unlink(missing_ok=True)
        if r.returncode != 0:
            raise RuntimeError(f"restore of {db} from {b.name} failed:\n{r.stderr[-1500:]}")
        res[db] = f"restored from {b.name}"
    return res


def backup_loop(interval_s: int) -> None:
    while True:
        try:
            backup()
        except Exception as e:  # keep looping; failures are visible in the service log
            print("backup failed:", e, file=sys.stderr, flush=True)
        time.sleep(interval_s)


if __name__ == "__main__":
    if sys.argv[1:2] == ["backup"]:
        backup()
    elif sys.argv[1:2] == ["backup-loop"]:
        backup_loop(int(sys.argv[2]) if len(sys.argv) > 2 else 600)
    else:
        raise SystemExit("usage: python -m ops.pg backup | backup-loop [seconds]")
