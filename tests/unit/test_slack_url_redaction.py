import logging
import sys

import httpx
import pytest

from app.logging_config import (
    SLACK_URL_REDACTED,
    CorrelationIdFilter,
    JsonFormatter,
    SlackUrlRedactingFilter,
    configure_logging,
)

TOKEN = "xoxSECRETTOKEN42"
SLACK_URL = f"https://hooks.slack.com/commands/T000/B000/{TOKEN}"
HTTP_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2")


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    saved_levels = {name: logging.getLogger(name).level for name in HTTP_LOGGERS}
    yield
    root.handlers, root.level = saved
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


def post_to_slack() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    with httpx.Client(transport=transport) as client:
        client.post(SLACK_URL, json={"text": "answer"})


def test_httpx_logs_the_url_without_our_config(restore_logging) -> None:
    # Negative control: shows the test below would catch the leak.
    handler = ListHandler()
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.NOTSET)

    post_to_slack()

    assert any(TOKEN in r.getMessage() for r in handler.records)


@pytest.mark.parametrize("log_format", ["json", "text"])
def test_configure_logging_keeps_httpx_request_lines_out(
    restore_logging, capsys, log_format
) -> None:
    configure_logging("DEBUG", log_format)  # even at DEBUG

    post_to_slack()

    err = capsys.readouterr().err
    assert TOKEN not in err
    assert "HTTP Request" not in err
    assert logging.getLogger("httpx").level == logging.WARNING


def test_filter_redacts_message_args_exception_stack_and_data() -> None:
    try:
        raise httpx.HTTPStatusError(
            f"Client error '404 Not Found' for url '{SLACK_URL}'",
            request=httpx.Request("POST", SLACK_URL),
            response=httpx.Response(404),
        )
    except httpx.HTTPStatusError:
        exc_info = sys.exc_info()
    context = {
        "id": "task-1",
        "exc": f"HTTPStatusError('... {SLACK_URL}')",
        "kwargs": f"{{'response_url': '{SLACK_URL}'}}",
        "nested": [SLACK_URL, (SLACK_URL,)],
    }
    record = logging.LogRecord(
        "celery.app.trace",
        logging.ERROR,
        __file__,
        1,
        "Task %(name)s[%(id)s] raised unexpected: %(exc)s",
        ({**context, "name": "t"},),
        exc_info,
    )
    record.data = context
    record.stack_info = f"Stack: {SLACK_URL}"

    CorrelationIdFilter().filter(record)
    SlackUrlRedactingFilter().filter(record)
    formatted = JsonFormatter().format(record)

    everything = " ".join(
        [
            record.getMessage(),
            record.exc_text,
            record.stack_info,
            str(record.data),
            formatted,
        ]
    )
    assert TOKEN not in everything
    assert "hooks.slack.com" not in everything
    assert SLACK_URL_REDACTED in record.getMessage()
    assert SLACK_URL_REDACTED in record.exc_text
    assert record.correlation_id == "task-1"  # read before data was replaced


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.slack.com/actions/T1/2/abc",
        "https://hooks-global.slack.com/commands/T1/2/abc",
        "https://hooks.eu.slack.com/commands/T1/2/abc",
    ],
)
def test_all_allowed_slack_hosts_are_redacted(url: str) -> None:
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "post to %s", (url,), None
    )
    SlackUrlRedactingFilter().filter(record)
    assert record.getMessage() == f"post to {SLACK_URL_REDACTED}"
