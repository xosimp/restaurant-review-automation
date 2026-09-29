"""
logging_setup.py — one log format for the whole process, carrying the
request or job each line belongs to.

The web process never configured logging (#38). The only basicConfig was
scheduler.py's, and it applied only because hosted_dashboard happened to
import scheduler: every module's lines came out as "<time> [scheduler]
<message>", with no level and no logger name, so Railway's log view could
not filter an error from an info line, nothing tied a line to the request
that wrote it, and a traceback logged with exc_info was the only one anyone
would ever see.

configure() installs one handler on the root logger, first thing at boot:

  * on Railway (or LOG_FORMAT=json) one JSON object per line. Railway reads
    `level` and `message` and makes every other key filterable
    (@request_id:..., @logger:ops);
  * locally, a readable line with the same fields.

Every record carries whatever bind() put into this thread's context. The
response layer (http_layer) binds the request id, route and method for each
request; code that knows a restaurant or a job binds it too. Tracebacks are
rendered into the record (`traceback`), and an exception that kills a
thread is logged the same way instead of going to raw stderr.

Stdlib only (layer 0): it is imported before anything else logs.
"""
import json
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager

_ctx = threading.local()
_configured = False

# Railway's structured-log levels. Python's WARNING/CRITICAL are spelled
# differently there, and a level Railway does not know reads as info.
_LEVELS = {"DEBUG": "debug", "INFO": "info", "WARNING": "warn", "ERROR": "error", "CRITICAL": "error"}

# Attributes every LogRecord has; anything else on a record came from
# `extra=` or from the bound context and is emitted as a field.
_RECORD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}

# Fields rendered in the text format's bracket, in this order.
_TEXT_FIELDS = ("request_id", "restaurant_id", "job")


# ── the per-thread context ──────────────────────────────────────────────────

def current() -> dict:
    """This thread's bound fields (a copy)."""
    return dict(getattr(_ctx, "fields", None) or {})


def bind(**fields):
    """Add fields to every record this thread logs until unbind()/clear().
    A None value removes the field."""
    cur = current()
    for k, v in fields.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    _ctx.fields = cur


def unbind(*names):
    cur = current()
    for n in names:
        cur.pop(n, None)
    _ctx.fields = cur


def clear():
    _ctx.fields = {}


@contextmanager
def context(**fields):
    """bind() for the length of a block, restoring what was bound before —
    for a job run inside a thread that already carries other fields."""
    before = current()
    bind(**fields)
    try:
        yield
    finally:
        _ctx.fields = before


class ContextFilter(logging.Filter):
    """Copies the thread's bound fields onto the record. A field the call
    passed explicitly (`extra=`) wins."""

    def filter(self, record):
        for k, v in current().items():
            if not hasattr(record, k):
                setattr(record, k, v)
        return True


# ── formatters ──────────────────────────────────────────────────────────────

def _fields(record):
    return {k: v for k, v in record.__dict__.items()
            if k not in _RECORD_ATTRS and not k.startswith("_")}


def _iso(created):
    t = time.gmtime(created)
    return time.strftime("%Y-%m-%dT%H:%M:%S", t) + ".%03dZ" % int((created % 1) * 1000)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, message, thread, the
    bound and extra fields, and the rendered traceback when there is one."""

    def format(self, record):
        out = {
            "ts": _iso(record.created),
            "level": _LEVELS.get(record.levelname, record.levelname.lower()),
            "logger": record.name,
            "message": record.getMessage(),
            "thread": record.threadName,
        }
        for k, v in _fields(record).items():
            out.setdefault(k, v)
        if record.exc_info and record.exc_info[0] is not None:
            out["exc_type"] = getattr(record.exc_info[0], "__name__", str(record.exc_info[0]))
            out["traceback"] = self.formatException(record.exc_info)
        elif record.exc_text:
            out["traceback"] = record.exc_text
        if record.stack_info:
            out["stack"] = self.formatStack(record.stack_info)
        try:
            return json.dumps(out, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            return json.dumps({"ts": out["ts"], "level": out["level"], "logger": out["logger"],
                               "message": str(out["message"])}, default=str)


class TextFormatter(logging.Formatter):
    """The same fields as a line a person reads in a terminal."""

    def format(self, record):
        fields = _fields(record)
        tag = " ".join(f"{k}={fields.pop(k)}" for k in _TEXT_FIELDS if k in fields)
        line = "%s %-5s %s%s %s" % (
            time.strftime("%H:%M:%S", time.localtime(record.created)),
            _LEVELS.get(record.levelname, record.levelname.lower()),
            record.name, f" [{tag}]" if tag else "", record.getMessage())
        extras = {k: v for k, v in fields.items() if k not in ("route", "method")}
        if extras:
            line += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info and record.exc_info[0] is not None:
            line += "\n" + self.formatException(record.exc_info)
        return line


def use_json(environ=None) -> bool:
    """JSON on Railway, text elsewhere; LOG_FORMAT=json|text overrides."""
    env = os.environ if environ is None else environ
    fmt = (env.get("LOG_FORMAT") or "").strip().lower()
    if fmt in ("json", "text"):
        return fmt == "json"
    return any(env.get(v) for v in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID",
                                     "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT_NAME"))


# ── install ─────────────────────────────────────────────────────────────────

def _thread_excepthook(args):
    """An exception that ends a thread (the scheduler loop's, a boot helper's)
    went to raw stderr as an unstructured traceback. SystemExit stays silent,
    as it is by default."""
    if args.exc_type is SystemExit:
        return
    name = getattr(args.thread, "name", "?") if args.thread is not None else "?"
    logging.getLogger("thread").error(
        "Uncaught exception in thread %s", name,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def configure(level=None, fmt=None, stream=None, force=False):
    """Install the process's log handler on the root logger. Idempotent:
    a second call does nothing unless force=True. Handlers someone else
    installed (pytest's capture, Sentry) are left alone; only this module's
    handler is replaced. Returns the handler."""
    global _configured
    root = logging.getLogger()
    if _configured and not force:
        return next((h for h in root.handlers if getattr(h, "_cavnar_handler", False)), None)
    json_lines = use_json() if fmt is None else (fmt == "json")
    handler = logging.StreamHandler(stream or sys.stdout)
    handler._cavnar_handler = True
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if json_lines else TextFormatter())
    root.handlers = [h for h in root.handlers if not getattr(h, "_cavnar_handler", False)] + [handler]
    root.setLevel((level or os.getenv("LOG_LEVEL") or "INFO").upper())
    logging.captureWarnings(True)
    threading.excepthook = _thread_excepthook
    # print() is still the most common way this codebase says something, and
    # under gunicorn stdout is a pipe, block-buffered: a crash lost the last
    # lines before it, and they surfaced out of order with the log lines.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError, OSError):
        pass
    _configured = True
    return handler
