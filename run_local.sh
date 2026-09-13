#!/usr/bin/env bash
# Bring up a fully local stack without root privileges:
#   1. ensure a portable PostgreSQL is extracted under .pgroot and initialised
#   2. start it on 127.0.0.1:55432, create the "timing" database
#   3. start FastAPI on :8000
#   4. (separately) start the Angular dev server: (cd frontend && npm start)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PGBIN="$ROOT/.pgroot/usr/lib/postgresql/15/bin"
PGDATA="$ROOT/.pgdata"
export LD_LIBRARY_PATH="$ROOT/.pgroot/usr/lib/aarch64-linux-gnu:${LD_LIBRARY_PATH:-}"

if [ ! -x "$PGBIN/postgres" ]; then
  echo "[run_local] downloading portable PostgreSQL 15 (Debian bookworm)…"
  APT="apt-get -o Dir::Etc::sourcelist=/tmp/aptcfg/sources.list
       -o Dir::Etc::sourceparts=- -o Dir::State::Lists=/tmp/aptcfg/lists
       -o Dir::Cache=/tmp/aptcfg/archives"
  mkdir -p /tmp/aptcfg/lists/partial /tmp/aptcfg/archives/partial /tmp/pgdeb
  if [ ! -f /tmp/aptcfg/sources.list ]; then
    printf 'deb http://deb.debian.org/debian bookworm main\n' \
      > /tmp/aptcfg/sources.list
  fi
  if [ ! -f /tmp/aptcfg/lists/lock ] && [ -z "$(ls -A /tmp/aptcfg/lists 2>/dev/null)" ]; then
    $APT update
  fi
  ( cd /tmp/pgdeb && $APT download postgresql-15 libpq5 )
  mkdir -p "$ROOT/.pgroot"
  for f in /tmp/pgdeb/*.deb; do dpkg-deb -x "$f" "$ROOT/.pgroot"; done
fi

if [ ! -d "$PGDATA/base" ]; then
  mkdir -p "$PGDATA"
  "$PGBIN/initdb" -D "$PGDATA" -U timing --auth=trust --encoding=UTF8 --locale=C
fi

if ! "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
  "$PGBIN/pg_ctl" -D "$PGDATA" -l "$PGDATA/start.log" \
    -o "-p 55432 -k /tmp -h 127.0.0.1" start
fi

python3 - <<'PY'
import time, psycopg2
for _ in range(30):
    try:
        c = psycopg2.connect(host="127.0.0.1", port=55432, user="timing",
                             dbname="postgres", connect_timeout=2)
        break
    except Exception:
        time.sleep(0.5)
else:
    raise SystemExit("postgres did not become ready")
c.autocommit = True
cur = c.cursor()
cur.execute("SELECT 1 FROM pg_database WHERE datname='timing'")
if not cur.fetchone():
    cur.execute("CREATE DATABASE timing")
    print("[run_local] database 'timing' created")
c.close()
PY

export DATABASE_URL="postgresql+psycopg2://timing@127.0.0.1:55432/timing"
echo "[run_local] starting API on http://127.0.0.1:8000"
cd "$ROOT/backend"
exec python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
