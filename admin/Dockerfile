# Multi-stage build: compile the React SPA, then serve the static output
# from nginx. The final image contains no Node runtime, no source, no
# dev-server, and runs as a non-root user.

FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
# nginx:alpine ships a default nginx.conf that runs worker processes as
# the "nginx" user and listens on port 80. We override the server block
# to listen on 8080 (allows running the master as non-root) and drop
# privileges everywhere.
#
# Server-block config is delivered as a template; nginx's
# /docker-entrypoint.d/20-envsubst-on-templates.sh substitutes ${VAR}
# placeholders at container start and writes the result into
# /etc/nginx/conf.d/. Default API_UPSTREAM keeps docker-compose deploys
# working unchanged; override per-environment (e.g. ACA: sentry-api:5000).
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=builder /app/dist /usr/share/nginx/html

ENV API_UPSTREAM_URL=http://api:5000
ENV API_UPSTREAM_HOST=api:5000

# Ensure nginx can write runtime state to /var/cache/nginx and rewrite
# /etc/nginx/conf.d/ from the template, all as the unprivileged user.
RUN chown -R nginx:nginx /var/cache/nginx /usr/share/nginx/html /etc/nginx/conf.d /etc/nginx/templates \
    && chmod -R 755 /usr/share/nginx/html \
    && sed -i -E -e 's|^pid[[:space:]]+/[^;]+;|pid /tmp/nginx.pid;|' \
                 -e '/^user /d' \
                 /etc/nginx/nginx.conf

USER nginx
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
