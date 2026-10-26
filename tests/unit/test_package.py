import fastapicommon
from fastapicommon import create_platform_app


def test_version_is_exposed() -> None:
    assert fastapicommon.__version__ == "0.4.1"


def test_create_platform_app_is_reexported_at_top_level() -> None:
    assert create_platform_app is fastapicommon.bootstrap.create_platform_app
