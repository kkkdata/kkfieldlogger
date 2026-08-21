#!/usr/bin/env sh
set -eu

PROJECT_DIR="${KK_PROJECT_DIR:-/opt/kkfieldlogger-v2/app/photoserver}"

docker run --rm \
  -v /etc/letsencrypt:/etc/letsencrypt \
  -v /var/lib/letsencrypt:/var/lib/letsencrypt \
  -v "${PROJECT_DIR}/docker/certbot/www:/var/www/certbot" \
  certbot/certbot renew --webroot -w /var/www/certbot --quiet

cd "${PROJECT_DIR}"
docker compose -p kkfieldlogger-v2 exec -T nginx nginx -s reload
