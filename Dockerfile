FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VME_HOST=0.0.0.0 \
    VME_PORT=8080 \
    VME_STATE_PATH=/var/lib/vme/service.sqlite3

RUN groupadd --system --gid 10001 vme \
    && useradd --system --uid 10001 --gid vme --home-dir /var/lib/vme vme \
    && mkdir -p /var/lib/vme \
    && chown -R vme:vme /var/lib/vme \
    && chmod 700 /var/lib/vme

WORKDIR /app
COPY --from=builder /wheels /wheels
RUN wheel_path="$(find /wheels -name 'vector_migration_engine-*.whl' -print -quit)" \
    && python -m pip install --no-cache-dir "${wheel_path}[server,chroma-client,qdrant]" \
    && rm -rf /wheels \
    && python -m pip uninstall --yes pip setuptools wheel

USER 10001:10001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=2)"

ENTRYPOINT ["vme"]
CMD ["serve"]
