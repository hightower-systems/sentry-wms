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
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html
# Ensure nginx can write runtime state to /var/run and /var/cache/nginx
# as the unprivileged user.
RUN chown -R nginx:nginx /var/cache/nginx /var/run /usr/share/nginx/html \
    && chmod -R 755 /usr/share/nginx/html \
    && sed -i 's|pid /var/run/nginx.pid;|pid /tmp/nginx.pid;|' /etc/nginx/nginx.conf \
    && sed -i '/user  nginx;/d' /etc/nginx/nginx.conf

USER nginx
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
