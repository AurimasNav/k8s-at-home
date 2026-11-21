#!/bin/sh
# Test step 1 (postgres:18-alpine, as root): build a version 18 cluster exactly as the image
# does on production's first start, then load a read-only copy of production into it.
set -eu
B=/var/lib/postgresql
T=$B/test
export PGDATA=$B/18/docker
PROD=postgresql.postgresql.svc.cluster.local

mkdir -p "$T"
chown postgres:postgres "$T"
chmod 700 "$T"

# the image's own first-start initdb (POSTGRES_USER/POSTGRES_PASSWORD/POSTGRES_DB from env)
docker-ensure-initdb.sh

gosu postgres pg_ctl -D "$PGDATA" -l "$T/seed-server.log" -w -o "-c listen_addresses=''" start

echo "[seed] copying production (read-only pg_dumpall from $PROD)"
start=$(date +%s)
PGPASSWORD=$PROD_PASSWORD PGAPPNAME=postgresql-upgrade-test \
  gosu postgres pg_dumpall -h "$PROD" -U "$POSTGRES_USER" -f "$T/prod.sql"
echo "[seed] dump: $(wc -c < "$T/prod.sql") bytes in $(( $(date +%s) - start )) s"
gosu postgres psql -X -q -U "$POSTGRES_USER" -d postgres -f "$T/prod.sql" > "$T/restore.log" 2>&1 || true
# the only expected errors: the dump re-creates the superuser and the database that the image's
# first start already made (as on production)
if grep "ERROR" "$T/restore.log" | grep -v -E "(role|database) \"$POSTGRES_USER\" already exists"; then
  echo "[seed] FAIL: restore errors above"; exit 1
fi

# what the upgraded cluster must still hold: row count of every table, and every role's hash
: > "$T/counts-18.txt"
for db in $(gosu postgres psql -X -At -U "$POSTGRES_USER" -d postgres -c \
    "SELECT datname FROM pg_database WHERE datallowconn AND datname NOT IN ('postgres','template1') ORDER BY 1"); do
  gosu postgres psql -X -At -U "$POSTGRES_USER" -d "$db" -c "SELECT coalesce(string_agg(format(
      'SELECT %L || count(*) FROM %I.%I', '$db.' || n.nspname || '.' || c.relname || '=', n.nspname, c.relname), ' UNION ALL '), 'SELECT NULL WHERE false')
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema')" > "$T/count-$db.sql"
  gosu postgres psql -X -At -U "$POSTGRES_USER" -d "$db" -f "$T/count-$db.sql" >> "$T/counts-18.txt"
  echo "$db" >> "$T/databases.txt"
done
sort -o "$T/counts-18.txt" "$T/counts-18.txt"
gosu postgres psql -X -At -U "$POSTGRES_USER" -d postgres -c \
  "SELECT rolname || ' ' || md5(rolpassword) FROM pg_authid WHERE rolname !~ '^pg_' ORDER BY 1" > "$T/roles-18.txt"
echo "[seed] databases: $(tr '\n' ' ' < "$T/databases.txt"); tables: $(wc -l < "$T/counts-18.txt"); roles: $(wc -l < "$T/roles-18.txt")"

# Kill it the hard way, as if the production pod had been SIGKILLed: no shutdown checkpoint and
# a stale postmaster.pid that names PID 1 (postgres is PID 1 in the production container). This
# covers the upgrade script's crash-recovery step; a normal pod stop leaves a clean cluster.
kill -KILL "$(head -1 "$PGDATA/postmaster.pid")"
for _ in $(seq 30); do pg_isready -q -U "$POSTGRES_USER" -d postgres || break; sleep 1; done
sleep 2
sed -i '1s/.*/1/' "$PGDATA/postmaster.pid"
rm -f "$T/prod.sql"
chown -R postgres:postgres "$T"
echo "[seed] done"
