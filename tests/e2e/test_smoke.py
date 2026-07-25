"""End-to-end smoke test: builds and runs the real Docker image, then makes real HTTP
calls against it over a real socket. This is the one thing tests/unit and
tests/integration structurally cannot verify:

- tests/integration's TestClient runs the ASGI app in-process via ASGITransport — no
  real uvicorn process, no real socket, no real HTTP wire behavior.
- Neither tier ever imports the app the way `uvicorn examples.fastapi_service.main:app`
  actually does, or verifies the packaged Docker image works at all.

Requires Docker. Skipped automatically if the `docker` CLI isn't on PATH. Set
E2E_IMAGE_TAG to reuse an image already built elsewhere (e.g. CI's "Smoke tests" job,
which builds with GHA-cached buildx first) instead of building one here — avoids a
redundant build when this is run as part of a pipeline that already produced the image.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest

pytestmark = pytest.mark.e2e

_DEFAULT_IMAGE_TAG = "platform-fastapicommon-example:e2e-test"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    if not _docker_available():
        pytest.skip("Docker is not available")

    image_tag = os.environ.get("E2E_IMAGE_TAG")
    build_image = image_tag is None
    image_tag = image_tag or _DEFAULT_IMAGE_TAG

    if build_image:
        subprocess.run(
            ["docker", "build", "-t", image_tag, "."],
            check=True,
            capture_output=True,
            text=True,
        )

    container_name = f"fastapicommon-e2e-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--name", container_name, "-P", image_tag],
        check=True,
        capture_output=True,
        text=True,
    )

    try:
        port_mapping = subprocess.run(
            ["docker", "port", container_name, "8080/tcp"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        # e.g. "0.0.0.0:54321" — take the host port after the last colon.
        host_port = port_mapping.rsplit(":", 1)[-1]
        url = f"http://localhost:{host_port}"

        for _ in range(30):
            try:
                if httpx.get(f"{url}/health", timeout=1.0).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(1)
        else:
            logs = subprocess.run(
                ["docker", "logs", container_name], capture_output=True, text=True
            ).stdout
            pytest.fail(f"Container did not become healthy in time. Logs:\n{logs}")

        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        if build_image:
            subprocess.run(["docker", "image", "rm", image_tag], capture_output=True)


def test_health(base_url: str) -> None:
    response = httpx.get(f"{base_url}/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready(base_url: str) -> None:
    response = httpx.get(f"{base_url}/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_metrics(base_url: str) -> None:
    response = httpx.get(f"{base_url}/metrics")
    assert response.status_code == 200
    assert "http_requests_total" in response.text


def test_whoami_anonymous(base_url: str) -> None:
    response = httpx.get(f"{base_url}/widgets/me")
    assert response.status_code == 200
    assert response.json() == {"user_id": None, "tenant_id": None, "roles": []}


def test_delete_requires_role(base_url: str) -> None:
    response = httpx.delete(f"{base_url}/widgets/42")
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_delete_with_role_succeeds(base_url: str) -> None:
    response = httpx.delete(
        f"{base_url}/widgets/42",
        headers={
            "x-user-id": "u1",
            "x-tenant-id": "t1",
            "x-tenant-roles": "tenant_admin",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "deleted"}
