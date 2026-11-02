#!/bin/sh
# Installs the Let's Encrypt certificate for the router's web UI (https://gw.sync.lt:8443).
#
# Lives on the router as /jffs/scripts/router-cert-install.sh. It is the forced command of the
# router-cert SSH key (nvram sshd_authkeys: command="...",restrict), so that key can run
# nothing else: no shell, no port forwarding, and the requested command is ignored.
# The sender is the router-cert-push Job in the cluster (gitops/router-cert).
#
# stdin: the PEM certificate chain (leaf first), then the PEM private key.
# Accepted only if: issued by Let's Encrypt, valid for exactly gw.sync.lt, chains to a trusted
# root, at least 7 days left, and the key matches. Otherwise nothing changes (exit 1).
# Unchanged certificate: exit 0 without touching anything. New certificate: stored where the
# web UI's own "upload certificate" puts it (/jffs/.cert, le_enable=2), web UI restarted.
set -u
NAME=gw.sync.lt
DIR=/jffs/.cert
W=/tmp/router-cert.$$
umask 077

log() { logger -t router-cert "$*"; echo "$*"; }
fail() { log "rejected: $*"; rm -rf "$W"; exit 1; }

exec 9>/tmp/router-cert.lock
flock -n 9 || { echo "another install is running"; exit 1; }
rm -rf "$W"
mkdir "$W" || exit 1

# Read at most 64 KiB (a chain + key is ~6 KiB) and split it into leaf, chain and key.
head -c 65536 > "$W/in.pem"
awk -v d="$W" '
/-----BEGIN CERTIFICATE-----/ { n++; f = (n == 1) ? d "/leaf.pem" : d "/chain.pem" }
/-----BEGIN [A-Z ]*PRIVATE KEY-----/ { k++; f = d "/key.pem" }
f { print >> f }
/-----END [A-Z ]*-----/ { f = "" }
END { if (n < 2 || k != 1) exit 3 }' "$W/in.pem" || fail "expected a certificate chain and one private key"
cat "$W/leaf.pem" "$W/chain.pem" > "$W/full.pem"

openssl x509 -in "$W/leaf.pem" -noout -issuer 2>/dev/null | grep -qF "O=Let's Encrypt" ||
    fail "not issued by Let's Encrypt"
san=$(openssl x509 -in "$W/leaf.pem" -noout -ext subjectAltName 2>/dev/null | sed -n '2,$p' | tr -d ' \n')
[ "$san" = "DNS:$NAME" ] || fail "names are '$san', expected DNS:$NAME only"
openssl verify -purpose sslserver -verify_hostname "$NAME" -CAfile /etc/ssl/certs/ca-certificates.crt \
    -untrusted "$W/chain.pem" "$W/leaf.pem" > /dev/null 2>&1 || fail "chain does not verify"
openssl x509 -in "$W/leaf.pem" -noout -checkend 604800 > /dev/null || fail "expires within 7 days"
openssl x509 -in "$W/leaf.pem" -noout -pubkey > "$W/a" 2>/dev/null
openssl pkey -in "$W/key.pem" -pubout > "$W/b" 2>/dev/null
if [ ! -s "$W/a" ] || ! cmp -s "$W/a" "$W/b"; then fail "key does not match the certificate"; fi
until=$(openssl x509 -in "$W/leaf.pem" -noout -enddate | cut -d= -f2)

if cmp -s "$W/full.pem" "$DIR/cert.pem" && cmp -s "$W/key.pem" "$DIR/key.pem" &&
    cmp -s "$W/full.pem" /etc/cert.pem && [ "$(nvram get le_enable)" = 2 ]; then
    echo "unchanged: $NAME valid until $until"
    rm -rf "$W"
    exit 0
fi

mkdir -p "$DIR" || fail "cannot create $DIR"
chmod 700 "$DIR"
if ! { cp "$W/full.pem" "$DIR/cert.pem.new" && cp "$W/key.pem" "$DIR/key.pem.new" &&
    mv -f "$DIR/cert.pem.new" "$DIR/cert.pem" && mv -f "$DIR/key.pem.new" "$DIR/key.pem"; }; then
    fail "cannot write $DIR"
fi
# An uploaded CA would take precedence over an uploaded server certificate.
rm -f "$DIR/cacert.pem" "$DIR/cakey.pem"
if [ "$(nvram get le_enable)" != 2 ]; then
    nvram set le_enable=2
    nvram commit
fi
# httpd start copies /jffs/.cert to /etc/cert.pem, checks it and saves it to /jffs/cert.tgz.
service restart_httpd > /dev/null 2>&1

i=0
while [ $i -lt 30 ]; do
    if cmp -s "$W/full.pem" /etc/cert.pem; then
        log "installed: $NAME valid until $until"
        rm -rf "$W"
        exit 0
    fi
    sleep 1
    i=$((i + 1))
done
fail "the web UI did not take the certificate (le_enable=$(nvram get le_enable))"
