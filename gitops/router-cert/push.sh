#!/bin/sh
# Sends the gw.sync.lt certificate and key to the router over SSH. The router runs only
# router/router-cert-install.sh for this key (forced command): it checks the certificate,
# installs it if it changed and restarts the web UI only then.
# The exit code is the router script's: 0 = installed or unchanged, 1 = rejected.
set -eu
umask 077
# Doppler may drop the key file's final newline, which OpenSSH needs.
printf '%s\n' "$(cat /ssh/id_ed25519)" > /tmp/id_ed25519
user=$(cat /ssh/user)
cat /tls/tls.crt /tls/tls.key > /tmp/bundle.pem
ssh -F /dev/null -T \
    -i /tmp/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/etc/router-cert/known_hosts \
    -o ConnectTimeout=20 -o ServerAliveInterval=15 -o LogLevel=ERROR \
    "$user@192.168.1.1" install < /tmp/bundle.pem
