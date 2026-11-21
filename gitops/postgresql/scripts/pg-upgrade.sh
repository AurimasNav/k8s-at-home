#!/bin/sh
# Major-version upgrade of the PostgreSQL data directory, run once as an init container.
#
# Runs in the NEW image (postgres:19-alpine). The init container before it copies the OLD
# binaries (postgres:18-alpine /usr/local) to $PGBINOLD/.. . Both images are Alpine with the
# same musl and ICU, so text sort order, and with it every text index, stays valid. The
# Debian-based upgrade images (tianon/postgres-upgrade) would not match our Alpine data.
#
# pg_upgrade runs in copy mode: the old cluster in /var/lib/postgresql/$OLD_MAJOR/docker is
# left untouched, so a rollback is reverting the image tag. Delete the old directory once
# the new version has run for a while.
#
# Safe to re-run: it does nothing once the new cluster exists, and a run that stopped
# half-way (marker file left behind) is redone from scratch.
set -eu

OLD_MAJOR=${OLD_MAJOR:?set OLD_MAJOR}
NEW_MAJOR=${PG_MAJOR:?PG_MAJOR comes from the postgres image}
BASE=/var/lib/postgresql
OLD=$BASE/$OLD_MAJOR/docker
NEW=$BASE/$NEW_MAJOR/docker
# both live in $BASE/$NEW_MAJOR, the one directory this script owns (the volume root may not be
# writable by postgres)
MARKER=$BASE/$NEW_MAJOR/.pg-upgrade-from-$OLD_MAJOR
WORK=$BASE/$NEW_MAJOR/pg-upgrade-work
PGBINOLD=${PGBINOLD:-/opt/pg-old/bin}
PGBINNEW=/usr/local/bin
SUPERUSER=${POSTGRES_USER:-postgres}

log() { echo "[pg-upgrade] $*"; }

if [ "$OLD_MAJOR" = "$NEW_MAJOR" ]; then
  log "OLD_MAJOR equals the image's major ($NEW_MAJOR); nothing to do"
  exit 0
fi

if [ "$(id -u)" = 0 ]; then
  # same step-down as the image's entrypoint: prepare directories as root, run as postgres
  mkdir -p "$BASE/$NEW_MAJOR"
  chown postgres:postgres "$BASE/$NEW_MAJOR"
  chmod 700 "$BASE/$NEW_MAJOR"
  [ ! -d "$OLD" ] || chown -R postgres:postgres "$OLD"
  exec gosu postgres /bin/sh "$0" "$@"
fi

if [ -s "$NEW/PG_VERSION" ] && [ ! -e "$MARKER" ]; then
  log "$NEW already holds a version $(cat "$NEW/PG_VERSION") cluster; nothing to do"
  exit 0
fi
if [ ! -s "$OLD/PG_VERSION" ]; then
  log "no version $OLD_MAJOR cluster in $OLD; nothing to upgrade"
  exit 0
fi
log "upgrading $OLD (version $(cat "$OLD/PG_VERSION")) to $NEW ($("$PGBINNEW/postgres" --version))"
"$PGBINOLD/postgres" --version

if [ -e "$MARKER" ]; then
  log "an earlier upgrade attempt did not finish; removing its partial $NEW and starting over"
fi
touch "$MARKER"
rm -rf "$NEW" "$WORK"
mkdir -p "$WORK"
cd "$WORK"

# pg_upgrade needs a cleanly shut down old cluster. After a crash or SIGKILL, let the old
# server run its crash recovery once, then stop it cleanly.
state=$("$PGBINOLD/pg_controldata" "$OLD" | sed -n 's/^Database cluster state: *//p')
if [ "$state" != "shut down" ]; then
  log "old cluster state is '$state'; starting it once for crash recovery"
  "$PGBINOLD/pg_ctl" -D "$OLD" -l "$WORK/old-recovery.log" -w -t 600 \
    -o "-c listen_addresses='' -c unix_socket_directories='$WORK'" start
  "$PGBINOLD/pg_ctl" -D "$OLD" -m fast -w -t 600 stop
  cat "$WORK/old-recovery.log"
fi

# Same initdb as the image's entrypoint (LANG=en_US.utf8 from the image, data checksums on by
# default since 18), with the old cluster's superuser as the install user. pg_upgrade then
# brings over all roles and their SCRAM password hashes, so no password is needed here.
# shellcheck disable=SC2086 # POSTGRES_INITDB_ARGS is a word list, as in the entrypoint
"$PGBINNEW/initdb" -D "$NEW" --username="$SUPERUSER" ${POSTGRES_INITDB_ARGS:-}
cp "$OLD/pg_hba.conf" "$NEW/pg_hba.conf"

log "pg_upgrade --check"
"$PGBINNEW/pg_upgrade" --old-bindir="$PGBINOLD" --new-bindir="$PGBINNEW" \
  --old-datadir="$OLD" --new-datadir="$NEW" --username="$SUPERUSER" \
  --socketdir="$WORK" --copy --check
log "pg_upgrade"
start=$(date +%s)
"$PGBINNEW/pg_upgrade" --old-bindir="$PGBINOLD" --new-bindir="$PGBINNEW" \
  --old-datadir="$OLD" --new-datadir="$NEW" --username="$SUPERUSER" \
  --socketdir="$WORK" --copy
log "pg_upgrade finished in $(( $(date +%s) - start )) s"

# pg_upgrade carries over most planner statistics; fill in whatever it could not.
"$PGBINNEW/pg_ctl" -D "$NEW" -l "$WORK/new-analyze.log" -w -t 600 \
  -o "-c listen_addresses='' -c unix_socket_directories='$WORK'" start
"$PGBINNEW/vacuumdb" --host="$WORK" --username="$SUPERUSER" --all --analyze-in-stages --missing-stats-only
"$PGBINNEW/pg_ctl" -D "$NEW" -m fast -w -t 600 stop

cd "$BASE"
rm -rf "$WORK"
rm -f "$MARKER"
log "done: version $NEW_MAJOR cluster in $NEW; the old cluster is untouched in $OLD"
