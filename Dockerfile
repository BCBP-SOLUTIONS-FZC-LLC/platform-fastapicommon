# syntax=docker/dockerfile:1
#
# Packages the example FastAPI service (examples/fastapi_service) as a standalone image
# for local container testing of the fastapicommon middleware stack. This library itself
# ships as a wheel/sdist (see `make build`); this image is a runnable demonstration, not
# a production artifact of the library.

# Digest-pinned (refresh deliberately with `make pin-base-images`, then rescan): the image
# must be what CI tested, not whatever the floating tags point at on the day of the build.
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder

# SOURCE_DATE_EPOCH (commit time) is injected by CI so the wheel's file timestamps, the
# hash-based .pyc files pip compiles and BuildKit's layer timestamps are reproducible: the
# image CI pushes must be the one it scanned and smoke-tested (image-fingerprint.sh).
ARG SOURCE_DATE_EPOCH

WORKDIR /src

COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock build-constraints.txt README.md ./
COPY src ./src

# Exactly the locked dependency versions (with hashes) CI tested — the grpc and examples
# extras included, as examples/fastapi_service needs them — then the project's wheel, built
# with the hash-locked build backend (build-constraints.txt) and installed without
# re-resolving anything.
RUN uv export --frozen --no-dev --extra grpc --extra examples --no-emit-project --no-header \
        --format requirements-txt -o /tmp/requirements.txt \
    && uv build --wheel --no-cache --build-constraints build-constraints.txt --require-hashes \
        --out-dir /tmp/dist \
    && pip install --no-cache-dir --require-hashes --prefix=/install -r /tmp/requirements.txt \
    && pip install --no-cache-dir --no-deps --prefix=/install /tmp/dist/*.whl

FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

# First in this stage, so the commit time is part of every later layer's cache key and its
# file times are reproducible (see the builder stage).
ARG SOURCE_DATE_EPOCH

# Debian security fixes newer than the pinned base image, pinned to exact versions so the
# build stays deterministic. Drop a line once the base digest is refreshed past it
# (`make pin-base-images`; Trivy's gate flags any fixable one). apt's lists, logs and caches
# are removed so they do not vary the image between builds.
# DSA-6549-1 (xz-utils): liblzma5 5.8.1-1+deb13u1 -> 5.8.1-1+deb13u2.
# DSA-6572-1 (ca-certificates): ca-certificates 20250419 -> 20250419+deb13u1.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends liblzma5=5.8.1-1+deb13u2 \
        ca-certificates=20250419+deb13u1 \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /var/log/apt /var/log/dpkg.log \
        /var/cache/ldconfig/aux-cache

# The runtime image never invokes pip (dependencies are installed in the builder stage and
# copied in below) — strip it from the base image. pip vendors its own pinned copies of
# urllib3, msgpack and setuptools, which Trivy reports even when pip itself is current.
# Nor does a server use the stdlib's interactive / GUI parts (ensurepip, IDLE, Tk — no Tk
# libraries are installed anyway — turtle demos, pydoc's topic data): about 7 MB.
RUN PIP_PKG_DIR="$(python -c 'import pip, os; print(os.path.dirname(pip.__file__))')" \
    && STDLIB="$(dirname "$(dirname "$PIP_PKG_DIR")")" \
    && rm -rf "$PIP_PKG_DIR" "$(dirname "$PIP_PKG_DIR")"/pip-*.dist-info \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.* \
    && rm -rf "$STDLIB"/ensurepip "$STDLIB"/idlelib "$STDLIB"/tkinter "$STDLIB"/turtledemo \
        "$STDLIB"/pydoc_data /usr/local/bin/idle3* /usr/local/bin/pydoc3*

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

# Verify the HTTP server is responding before declaring the container healthy. Python's
# urllib, not curl: the runtime stage installs no extra packages.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:%s/health' % os.environ.get('APP_PORT','8080'), timeout=4).status == 200 else 1)"]

# Numeric UID:GID with no passwd entry (no useradd, whose /etc/shadow entry records the
# build day and made rebuilds on another day differ): Kubernetes can only verify runAsNonRoot
# for a numeric user (hadolint DL3066).
USER 10001:10001

ENTRYPOINT ["sh", "-c", "exec uvicorn examples.fastapi_service.main:app --host 0.0.0.0 --port ${APP_PORT}"]
