# Tailwind CSS v4 standalone CLI (no Node build step). glibc base -> linux-x64 asset.
FROM debian:bookworm-slim

ARG TAILWIND_VERSION=v4.3.2

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sL -o /usr/local/bin/tailwindcss \
        "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-x64" \
    && chmod +x /usr/local/bin/tailwindcss

WORKDIR /app

# --poll: filesystem events don't always propagate over Docker Desktop bind mounts on Windows.
CMD ["tailwindcss", "-i", "assets/css/app.css", "-o", "static/css/tailwind.build.css", "--watch", "--poll"]
