#!/bin/sh
# Reload nginx after certbot renews a certificate.
# Finds the nginx container by its service label rather than a hardcoded name,
# so this works regardless of the Docker Compose project name or directory name.
set -e

nginx_id=$(docker ps -qf 'label=family-radio.service=nginx')
if [ -z "$nginx_id" ]; then
    echo "reload-nginx: nginx container not found, skipping reload"
    exit 0
fi
docker exec "$nginx_id" nginx -s reload
