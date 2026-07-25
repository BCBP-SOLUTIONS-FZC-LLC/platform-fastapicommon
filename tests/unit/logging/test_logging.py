import structlog

from fastapicommon.context import CurrentUser, RequestContext, request_context_scope
from fastapicommon.logging import configure_logging, get_logger
from fastapicommon.logging.config import _add_request_context


def test_configure_logging_is_idempotent() -> None:
    configure_logging()
    configure_logging("DEBUG")


def test_get_logger_returns_bound_logger() -> None:
    configure_logging()
    logger = get_logger(__name__)
    assert isinstance(logger, structlog.stdlib.BoundLogger) or hasattr(logger, "info")


def test_add_request_context_enriches_event_dict_when_bound() -> None:
    user = CurrentUser(user_id="u1", tenant_id="t1")
    context = RequestContext(request_id="r1", correlation_id="c1", user=user)
    with request_context_scope(context):
        event_dict = _add_request_context(None, "info", {"event": "test"})
    assert event_dict["request_id"] == "r1"
    assert event_dict["correlation_id"] == "c1"
    assert event_dict["tenant_id"] == "t1"
    assert event_dict["user_id"] == "u1"


def test_add_request_context_noop_when_unbound() -> None:
    event_dict = _add_request_context(None, "info", {"event": "test"})
    assert event_dict == {"event": "test"}


def test_add_request_context_omits_empty_tenant_and_user() -> None:
    context = RequestContext(request_id="r1", correlation_id="c1", user=CurrentUser())
    with request_context_scope(context):
        event_dict = _add_request_context(None, "info", {"event": "test"})
    assert event_dict["request_id"] == "r1"
    assert "tenant_id" not in event_dict
    assert "user_id" not in event_dict
