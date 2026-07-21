FROM nginx:1.27-alpine
LABEL org.opencontainers.image.source="https://github.com/jasondcamp/mayfly" \
      org.opencontainers.image.description="mayfly-caddis-frontend: nginx UI for the caddis sample app (same-origin /api proxy to caddis-api)" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
EXPOSE 8080
