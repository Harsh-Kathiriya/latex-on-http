# Slim LaTeX-On-HTTP container for Cloudflare Containers.
#
# Stateless compile-only image: receive files → compile → return PDF.
# No PostgreSQL, no ZeroMQ cache process, no supervisord, no migrations.
#
# The entrypoint FUSE-mounts a shared R2 bucket so that every container
# instance shares the same persistent compile cache directory.
#
# Base: TeX Live + Python runtime

FROM yoant/latexonhttp-python:debian@sha256:d8a9b9c38db79b2fbfb69685d5dcfcb1e97e96de7f1111352fb23c9de9088ae1

ENV LC_ALL=C.UTF-8
ENV LANG=C.UTF-8
ENV LOGGING_LEVEL=INFO
ENV DEFAULT_COMPILE_TIMEOUT=45
ENV LATEX_COMPILER_USER=latex-compiler
ENV LATEX_COMPILER_GROUP=latex-compiler

# Install FUSE (required by tigrisfs) and curl (to fetch the binary).
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends fuse curl ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Run submitted TeX under a dedicated account. The API process stays root so
# it can access the root-only R2 mount, then drops only the compiler child.
RUN useradd --system --create-home --home-dir /var/lib/latex-compiler \
    --shell /usr/sbin/nologin latex-compiler

# Install a reviewed tigrisfs release and verify the architecture-specific
# checksum published with that release.
ARG TIGRISFS_VERSION=1.2.2
RUN ARCH=$(dpkg --print-architecture) \
    && case "$ARCH" in \
       amd64) TIGRISFS_SHA256=fd0c19c5c1d19eebd466cd4b6ce8f38286bedad6d792e6202cc3c916ce5e4281 ;; \
       arm64) TIGRISFS_SHA256=a74df75b731403dffb8969e7066ea7cf367ba18ca65f2d9496f4a8bfbf0ef9e5 ;; \
       *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;; \
       esac \
    && curl -fsSL \
       "https://github.com/tigrisdata/tigrisfs/releases/download/v${TIGRISFS_VERSION}/tigrisfs_${TIGRISFS_VERSION}_linux_${ARCH}.tar.gz" \
       -o /tmp/tigrisfs.tar.gz \
    && echo "${TIGRISFS_SHA256}  /tmp/tigrisfs.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/tigrisfs.tar.gz -C /usr/local/bin/ \
    && rm /tmp/tigrisfs.tar.gz \
    && chmod +x /usr/local/bin/tigrisfs

RUN mkdir -p /app/latex-on-http
WORKDIR /app/latex-on-http/

COPY app.py Makefile pyproject.toml poetry.lock /app/latex-on-http/
COPY ./latexonhttp/ /app/latex-on-http/latexonhttp/

RUN pip3 install --no-cache-dir --break-system-packages poetry==2.2.1 \
    && poetry install --only main --no-interaction --no-ansi

COPY ./container/cloudflare/entrypoint.sh /app/latex-on-http/entrypoint.sh
RUN chmod +x /app/latex-on-http/entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/app/latex-on-http/entrypoint.sh"]
