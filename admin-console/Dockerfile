# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

FROM public.ecr.aws/docker/library/node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build

WORKDIR /app

COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .

# No build-time configuration. The bundle is environment-agnostic; every runtime
# value is read from `config.js`, which `docker-entrypoint.sh` writes at container
# start (or the deploy writes for S3/CloudFront). See src/config.js.
RUN npm run build

# ── Production: serve with nginx ──
FROM public.ecr.aws/docker/library/nginx:1.31-alpine@sha256:785835ea65db4bba6f021acc0f3be91b0fef76a53fe41f8588c6698d2aed0606

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

# Runtime config: writes config.js into the web root at container start
# (the web root is chowned to nginx above, so the non-root user can write it).
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

USER nginx

EXPOSE 8080

# Liveness probe (clears checkov CKV_DOCKER_2). wget ships with the alpine image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q --spider http://localhost:8080/ || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["nginx", "-g", "daemon off;"]
