# syntax=docker/dockerfile:1
#
# Packages the example FastAPI service (examples/fastapi_service) as a standalone image
# for local container testing of the fastapicommon middleware stack. This library itself
# ships as a wheel/sdist (see `make build`); this image is a runnable demonstration, not
# a production artifact of the library.

FROM python:3.14-slim AS builder

WORKDIR /src

COPY pyproject.toml README.md ./
COPY src ./src
COPY examples ./examples

RUN pip install --no-cache-dir --prefix=/install ".[grpc,examples]"

FROM python:3.14-slim

# Pinning apt versions on python:3.12-slim is brittle: Debian's archive rotates old
# point-release versions out, breaking the build on every base image bump.
# hadolint ignore=DL3008
# pip is not needed at runtime: removing it drops the copies of urllib3, msgpack and
# setuptools that pip vendors (Trivy reports them even when pip itself is current).
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y pip \
    && useradd --no-create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

ARG APP_NAME=platform-fastapicommon-example
ARG BUILD_VERSION=unknown

COPY --from=builder /install /usr/local
COPY examples ./examples

ENV APP_NAME=${APP_NAME} \
    APP_ENV=prod \
    APP_PORT=8080 \
    BUILD_VERSION=${BUILD_VERSION} \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# Verify the HTTP server is responding before declaring the container healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://localhost:${APP_PORT}/health" || exit 1

USER appuser

ENTRYPOINT ["sh", "-c", "exec uvicorn examples.fastapi_service.main:app --host 0.0.0.0 --port ${APP_PORT}"]
