#!/bin/sh
# Test step 3 (postgres:19-alpine, as root): start the upgraded cluster through the image's
# own entrypoint, the way production will, and check it against what the seed step recorded.
set -eu
B=/var/lib/postgresql
T=$B/test
export PGDATA=$B/$PG_MAJOR/docker
fail=0
check() { if [ "$1" = ok ]; then echo "  ok    $2"; else echo "  FAIL  $2"; fail=1; fi; }
q() { gosu postgres psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" "$@"; }

docker-entrypoint.sh postgres -c listen_addresses='' > "$T/verify-server.log" 2>&1 &
for _ in $(seq 60); do pg_isready -q -U "$POSTGRES_USER" -d postgres && break; sleep 1; done

echo "[verify] $(q -d postgres -c 'SELECT version()')"
[ "$(q -d postgres -c 'SHOW server_version_num' | cut -c1-2)" = "$PG_MAJOR" ] && r=ok || r=no
check $r "server is version $PG_MAJOR"
[ "$(q -d postgres -c 'SHOW data_checksums')" = on ] && r=ok || r=no
check $r "data checksums still on"

: > "$T/counts-new.txt"
while read -r db; do
  q -d "$db" -f "$T/count-$db.sql" >> "$T/counts-new.txt"
done < "$T/databases.txt"
sort -o "$T/counts-new.txt" "$T/counts-new.txt"
cmp -s "$T/counts-18.txt" "$T/counts-new.txt" && r=ok || r=no
check $r "row counts of all $(wc -l < "$T/counts-18.txt") tables match the version 18 copy"
[ $r = ok ] || diff "$T/counts-18.txt" "$T/counts-new.txt" | head -20

q -d postgres -c "SELECT rolname || ' ' || md5(rolpassword) FROM pg_authid WHERE rolname !~ '^pg_' ORDER BY 1" > "$T/roles-new.txt"
cmp -s "$T/roles-18.txt" "$T/roles-new.txt" && r=ok || r=no
check $r "all $(wc -l < "$T/roles-new.txt") roles kept their SCRAM password hashes"

dbargs=""
while read -r db; do
  q -d "$db" -c "CREATE EXTENSION IF NOT EXISTS amcheck" > /dev/null
  dbargs="$dbargs --database=$db"
  echo "  info  $db: $(q -d "$db" -c "SELECT string_agg(extname || ' ' || extversion, ', ' ORDER BY extname) FROM pg_extension WHERE extname <> 'amcheck'")"
done < "$T/databases.txt"
# shellcheck disable=SC2086 # one --database flag per word
gosu postgres pg_amcheck -U "$POSTGRES_USER" $dbargs --heapallindexed > "$T/amcheck.log" 2>&1 && r=ok || r=no
check $r "pg_amcheck: every table and index, all databases (heap + index entries)"
[ $r = ok ] || tail -20 "$T/amcheck.log"

[ "$(q -d homebox -c "SELECT unaccent('Šalčininkų žąsys')")" = "Salcininku zasys" ] && r=ok || r=no
check $r "unaccent works (homebox)"
[ "$(q -d umami -c "SELECT encode(digest('x', 'sha256'), 'hex')")" = "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881" ] && r=ok || r=no
check $r "pgcrypto works (umami)"

# the same statements the databases sync Job runs on every Argo sync
q -d postgres -v name=homebox <<'SQL' > /dev/null && r=ok || r=no
SELECT format('CREATE ROLE %I LOGIN', :'name') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'name')\gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'name', :'name') WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'name')\gexec
SQL
check $r "databases Job statements run"
q -d questlog -c "CREATE TABLE upgrade_test (id int PRIMARY KEY, t text); INSERT INTO upgrade_test VALUES (1, 'x'); DROP TABLE upgrade_test" > /dev/null && r=ok || r=no
check $r "write test (create, insert, drop)"

echo "  info  server log warnings/errors: $(grep -c -E "WARNING|ERROR|FATAL" "$T/verify-server.log" || true)"
grep -E "WARNING|ERROR|FATAL" "$T/verify-server.log" | head -5 || true
echo "  info  disk: $(du -sh "$B/18" | cut -f1) old cluster (kept), $(du -sh "$B/$PG_MAJOR" | cut -f1) new cluster"

gosu postgres pg_ctl -D "$PGDATA" -m fast -w stop > /dev/null
if [ $fail = 0 ]; then echo "[verify] PASS"; else echo "[verify] FAIL"; exit 1; fi
