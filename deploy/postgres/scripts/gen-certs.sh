#!/usr/bin/env bash
# Generate a self-signed CA and a server certificate for the EBS PostgreSQL stack.
#
#   scripts/gen-certs.sh [OUT_DIR] [EXTRA_DNS_NAME...]
#
# OUT_DIR defaults to deploy/postgres/certs (git-ignored). The server certificate covers the
# compose service names (primary, standby, pgbouncer), localhost and any extra names given.
# Existing certificates are kept; delete OUT_DIR to rotate them. Use a site CA in production.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
out=${1:-$here/certs}
shift || true
days=${EBS_CERT_DAYS:-825}

if [[ -s "$out/server.crt" && -s "$out/server.key" && -s "$out/ca.crt" ]]; then
    echo "gen-certs: $out already has certificates; delete it to regenerate" >&2
    exit 0
fi

umask 077
mkdir -p "$out"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

sans="DNS:primary,DNS:standby,DNS:pgbouncer,DNS:localhost,IP:127.0.0.1"
for name in "$@"; do
    sans+=",DNS:$name"
done

openssl req -x509 -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -keyout "$out/ca.key" -out "$out/ca.crt" -days "$days" \
    -subj "/CN=EBS PostgreSQL self-signed CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    2>/dev/null

openssl req -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -keyout "$out/server.key" -out "$tmp/server.csr" -subj "/CN=primary" 2>/dev/null

printf '%s\n' \
    "basicConstraints=CA:FALSE" \
    "keyUsage=critical,digitalSignature,keyEncipherment" \
    "extendedKeyUsage=serverAuth" \
    "subjectAltName=$sans" >"$tmp/server.ext"

openssl x509 -req -in "$tmp/server.csr" -CA "$out/ca.crt" -CAkey "$out/ca.key" \
    -CAcreateserial -out "$out/server.crt" -days "$days" -extfile "$tmp/server.ext" 2>/dev/null

chmod 0600 "$out/ca.key" "$out/server.key"
chmod 0644 "$out/ca.crt" "$out/server.crt"
rm -f "$out/ca.srl"
echo "gen-certs: wrote $out/{ca.crt,server.crt,server.key}" >&2
