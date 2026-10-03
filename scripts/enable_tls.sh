#!/usr/bin/env bash
# Completes HTTPS only after both public DNS resolvers reach this origin.
set -euo pipefail

: "${PROJECT_DIR:?Set PROJECT_DIR outside the repository}"
: "${ORIGIN_IPV4:?Set ORIGIN_IPV4 outside the repository}"
CERT_ROOT=/etc/letsencrypt
BOOTSTRAP_CONTAINER=hamzavpn_bootstrap
CERT_MIN_SECONDS=604800

for command in python3 dig openssl flock docker; do
    command -v "$command" >/dev/null || { echo "TLS dependency missing: $command" >&2; exit 1; }
done
[[ "$PROJECT_DIR" = /* && -d "$PROJECT_DIR" ]] || { echo "Invalid PROJECT_DIR" >&2; exit 1; }

# Treat .env as data: interpolation, command substitutions and shell execution
# are deliberately unsupported. Other settings and secrets are never printed.
DOMAIN=$(python3 - "$PROJECT_DIR/.env" "$ORIGIN_IPV4" <<'PY'
import ipaddress
import pathlib
import re
import shlex
import sys

try:
    ipaddress.IPv4Address(sys.argv[2])
    values = {}
    for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
        match = re.match(r'^\s*(?:export\s+)?(DOMAIN|CERT_DIR)\s*=\s*(.*)$', line)
        if match:
            parts = shlex.split(match[2], comments=True, posix=True)
            if len(parts) != 1:
                raise ValueError('invalid setting')
            values[match[1]] = parts[0]
    domain = values.get('DOMAIN', '').lower()
    label = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
    if len(domain) > 253 or not re.fullmatch(label + r'(?:\.' + label + r')+', domain):
        raise ValueError('invalid domain')
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError('domain must be a hostname')
    if values.get('CERT_DIR') != '/etc/letsencrypt':
        raise ValueError('invalid certificate mount')
except (OSError, ValueError):
    sys.exit('Set a literal DOMAIN and CERT_DIR=/etc/letsencrypt in .env; verify ORIGIN_IPV4')
print(domain)
PY
)

mkdir -p "$PROJECT_DIR/certbot/data"
exec 9>"$PROJECT_DIR/certbot/enable_tls.lock"
flock -n 9 || exit 0

# DNS-only is required for this initial gate. An unrelated IPv6 record can also
# divert HTTP-01, so this IPv4-only deployment must not publish an AAAA record.
for resolver in 1.1.1.1 8.8.8.8; do
    answers=$(dig +time=3 +tries=1 +short "@$resolver" "$DOMAIN" A 2>/dev/null || true)
    ipv4=$(printf '%s\n' "$answers" | awk '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {print}' | sort -u)
    ipv6=$(dig +time=3 +tries=1 +short "@$resolver" "$DOMAIN" AAAA 2>/dev/null || true)
    if [[ "$ipv4" != "$ORIGIN_IPV4" || "$ipv6" == *:* ]]; then
        echo "TLS pending DNS"
        exit 0
    fi
done

certificate="$CERT_ROOT/live/$DOMAIN/fullchain.pem"
private_key="$CERT_ROOT/live/$DOMAIN/privkey.pem"
certificate_ready() {
    [[ -s "$certificate" && -s "$private_key" ]] &&
        openssl x509 -in "$certificate" -noout -checkend "$CERT_MIN_SECONDS" >/dev/null 2>&1 &&
        openssl x509 -in "$certificate" -noout -checkhost "$DOMAIN" >/dev/null 2>&1 &&
        openssl verify -untrusted "$certificate" "$certificate" >/dev/null 2>&1
}

if ! certificate_ready; then
    command -v certbot >/dev/null || { echo "TLS dependency missing: certbot" >&2; exit 1; }
    certbot certonly --webroot -w "$PROJECT_DIR/certbot/data" \
        --cert-name "$DOMAIN" -d "$DOMAIN" --non-interactive \
        --agree-tos --register-unsafely-without-email
    certificate_ready || { echo "TLS certificate validation failed; HTTP listener retained" >&2; exit 1; }
fi

for name in fullchain.pem privkey.pem; do
    path="$CERT_ROOT/$name"
    target="live/$DOMAIN/$name"
    if [[ -e "$path" || -L "$path" ]]; then
        [[ -L "$path" && "$(readlink "$path")" == "$target" ]] || {
            echo "TLS certificate mount path is occupied" >&2
            exit 1
        }
    fi
    ln -sfn "$target" "$path"
done

compose=(docker compose --project-directory "$PROJECT_DIR" --env-file "$PROJECT_DIR/.env" -f "$PROJECT_DIR/docker-compose.yml")
# A failed configuration check leaves the existing HTTP challenge listener up.
"${compose[@]}" run --rm --no-deps -T nginx nginx -t

bootstrap_running=$(docker inspect --type container --format '{{.State.Running}}' "$BOOTSTRAP_CONTAINER" 2>/dev/null || true)
if [[ "$bootstrap_running" == true ]]; then
    docker stop "$BOOTSTRAP_CONTAINER" >/dev/null
fi

if "${compose[@]}" up -d --no-deps nginx && "${compose[@]}" exec -T nginx nginx -t; then
    echo "TLS ready"
else
    # Restore only this project's HTTP listener if the HTTPS container failed.
    "${compose[@]}" stop nginx >/dev/null 2>&1 || true
    docker start "$BOOTSTRAP_CONTAINER" >/dev/null 2>&1 || {
        echo "TLS startup failed; could not restore the project HTTP listener" >&2
        exit 1
    }
    echo "TLS startup failed; HTTP listener restored" >&2
    exit 1
fi
