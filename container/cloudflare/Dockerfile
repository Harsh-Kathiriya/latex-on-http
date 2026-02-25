# Slim LaTeX-On-HTTP container for Cloudflare Containers.
#
# Stateless compile-only image: receive files → compile → return PDF.
# No PostgreSQL, no ZeroMQ cache process, no supervisord, no migrations.
#
# Base: TeX Live + Python runtime

FROM yoant/latexonhttp-python:debian

ENV LC_ALL=C.UTF-8
ENV LANG=C.UTF-8

RUN mkdir -p /app/latex-on-http
WORKDIR /app/latex-on-http/

COPY app.py Makefile pyproject.toml poetry.lock /app/latex-on-http/
COPY ./latexonhttp/ /app/latex-on-http/latexonhttp/

RUN make install

EXPOSE 8080

CMD ["poetry", "run", "gunicorn", "--workers=2", "--threads=8", "--bind=0.0.0.0:8080", "app:app"]
