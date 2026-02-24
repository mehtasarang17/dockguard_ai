#!/bin/sh
# Substitute only $INTERNAL_TOKEN in nginx config, preserving $uri, $host, etc.
envsubst '${INTERNAL_TOKEN}' < /etc/nginx/nginx.conf.template > /etc/nginx/conf.d/default.conf
