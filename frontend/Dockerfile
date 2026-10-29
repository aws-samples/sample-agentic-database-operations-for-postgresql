# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

FROM public.ecr.aws/docker/library/node:26-alpine@sha256:c6ad9e9c1d80f6eb607c26b4b0a132ff7a2344fad1c78ea3baa2a7d4c8de6d94 AS build

WORKDIR /app

COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .

# No build-time configuration. The bundle is environment-agnostic; every runtime
# value is read from `config.js`, which `docker-entrypoint.sh` writes at container
# start (or the deploy writes for S3/CloudFront). See src/config.js.
RUN npm run build

# ── Production: serve with nginx ──
FROM public.ecr.aws/docker/library/nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10

COPY --from=build /app/dist /usr/share/nginx/html

# SPA routing: serve index.html for all routes.
# Listen on 8080 (unprivileged port) so the nginx master process does not need
# root to bind — required for the non-root USER below (clears CKV_DOCKER_3).
RUN printf 'server {\n\
    listen 8080;\n\
    root /usr/share/nginx/html;\n\
    index index.html;\n\
    location / {\n\
        try_files $uri $uri/ /index.html;\n\
    }\n\
    location /assets/ {\n\
        expires 1y;\n\
        add_header Cache-Control "public, immutable";\n\
    }\n\
}\n' > /etc/nginx/conf.d/default.conf \
    && sed -i 's,^pid .*,pid /tmp/nginx.pid;,' /etc/nginx/nginx.conf \
    && sed -i '/^user /d' /etc/nginx/nginx.conf \
    && chown -R nginx:nginx /usr/share/nginx/html /var/cache/nginx /etc/nginx/conf.d \
    && touch /tmp/nginx.pid && chown nginx:nginx /tmp/nginx.pid

# Runtime config: writes config.js into the web root at container start.
# Lets the same image serve any environment without a rebuild.
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

USER nginx

EXPOSE 8080

# Liveness probe (clears checkov CKV_DOCKER_2). wget ships with the alpine image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q --spider http://localhost:8080/ || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
