FROM nginx:alpine

COPY nginx.conf /etc/nginx/nginx.conf.template
COPY index.html /usr/share/nginx/html/
COPY styles.css /usr/share/nginx/html/
COPY app.js /usr/share/nginx/html/

# Use entrypoint script to substitute only INTERNAL_TOKEN in config
COPY docker-entrypoint.sh /docker-entrypoint.d/40-envsubst-internal-token.sh
RUN chmod +x /docker-entrypoint.d/40-envsubst-internal-token.sh

EXPOSE 80
