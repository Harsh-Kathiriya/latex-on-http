# Slim LaTeX-On-HTTP container for Cloudflare Containers.
#
# Stateless compile-only image: receive files → compile → return PDF.
# No PostgreSQL, no ZeroMQ cache process, no supervisord, no migrations.
#
# The entrypoint FUSE-mounts a shared R2 bucket so that every container
# instance shares the same persistent compile cache directory.
#
# Base: TeX Live + Python runtime

FROM yoant/latexonhttp-python:debian

ENV LC_ALL=C.UTF-8
ENV LANG=C.UTF-8

# Install FUSE (required by tigrisfs) and curl (to fetch the binary).
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends fuse curl ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install tigrisfs — lightweight FUSE adapter for S3-compatible storage.
RUN ARCH=$(dpkg --print-architecture) \
    && VERSION=$(curl -s https://api.github.com/repos/tigrisdata/tigrisfs/releases/latest \
       | grep -o '"tag_name": "[^"]*' | cut -d'"' -f4) \
    && curl -L "https://github.com/tigrisdata/tigrisfs/releases/download/${VERSION}/tigrisfs_${VERSION#v}_linux_${ARCH}.tar.gz" \
       -o /tmp/tigrisfs.tar.gz \
    && tar -xzf /tmp/tigrisfs.tar.gz -C /usr/local/bin/ \
    && rm /tmp/tigrisfs.tar.gz \
    && chmod +x /usr/local/bin/tigrisfs

RUN mkdir -p /app/latex-on-http
WORKDIR /app/latex-on-http/

COPY app.py Makefile pyproject.toml poetry.lock /app/latex-on-http/
COPY ./latexonhttp/ /app/latex-on-http/latexonhttp/

RUN pip3 install -U --break-system-packages poetry \
    && make install

COPY ./container/cloudflare/entrypoint.sh /app/latex-on-http/entrypoint.sh
RUN chmod +x /app/latex-on-http/entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/app/latex-on-http/entrypoint.sh"]
