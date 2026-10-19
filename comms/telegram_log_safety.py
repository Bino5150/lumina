"""
TELEGRAM-BOT-TOKEN-LOG-01: keep the Telegram bot token out of log output.

The Bot API carries the token in the request URL path
(https://api.telegram.org/bot<TOKEN>/getUpdates). httpx logs every request
URL at INFO, telegram_bridge's basicConfig(level=INFO) sends INFO to stderr
for the whole process, and the bridge long-polls getUpdates continuously --
so the token was printed to the terminal on every poll. python-telegram-bot
also puts the token in its InvalidToken message ("The token `<TOKEN>` was
rejected by the server."), which reached stderr as a raw traceback.

What this module does:
  - httpx's per-request INFO lines for Bot API calls are dropped. They carry
    the token and nothing an operator needs: a failed call surfaces through
    python-telegram-bot's own WARNING/ERROR records. httpx request lines for
    other hosts (model providers etc.) are untouched.
  - Every record that reaches a root handler has the token redacted from its
    message, traceback and stack text. Levels, logger names and the
    surrounding error text are kept, so network failures and InvalidToken
    stay visible, minus the token.
  - redacted_exception_text() formats an exception and its chain for the
    bridge's own error report without the token.

install() only covers handlers on the root logger at the time it is called.
telegram_bridge calls it right after its own basicConfig, which is where the
root handler in the GUI and the standalone bridge comes from.
"""
import logging
import traceback

from core.redaction import redact_telegram_bot_tokens

_BOT_API_URL_MARKER = "api.telegram.org/bot"


def _redact(value):
    if isinstance(value, str):
        return redact_telegram_bot_tokens(value)
    return value


class TelegramTokenRedactingFilter(logging.Filter):
    """Rewrites a record so its rendered text carries no bot token. Never
    drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = redact_telegram_bot_tokens(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            # Formatter.format() reuses a cached exc_text instead of
            # re-rendering exc_info, so caching the redacted rendering here
            # is what keeps the traceback token-free downstream.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        record.exc_text = _redact(record.exc_text)
        record.stack_info = _redact(record.stack_info)
        return True


class TelegramHttpxRequestFilter(TelegramTokenRedactingFilter):
    """For the "httpx" logger: drop sub-WARNING records about Bot API
    requests, redact anything else."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            try:
                message = record.getMessage()
            except Exception:
                message = str(record.msg)
            if _BOT_API_URL_MARKER in message:
                return False
        return super().filter(record)


_HTTPX_FILTER = TelegramHttpxRequestFilter()
_ROOT_HANDLER_FILTER = TelegramTokenRedactingFilter()


def install() -> None:
    """Idempotent. Logger-level filter on "httpx" (applies whatever handlers
    exist, now or later) plus a redacting filter on each current root
    handler (covers python-telegram-bot's records and anything else that
    propagates to root)."""
    httpx_logger = logging.getLogger("httpx")
    if _HTTPX_FILTER not in httpx_logger.filters:
        httpx_logger.addFilter(_HTTPX_FILTER)
    for handler in logging.getLogger().handlers:
        if _ROOT_HANDLER_FILTER not in handler.filters:
            handler.addFilter(_ROOT_HANDLER_FILTER)


def redacted_exception_text(exc: BaseException) -> str:
    """The full traceback and cause chain of `exc`, token redacted."""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return redact_telegram_bot_tokens(text)
