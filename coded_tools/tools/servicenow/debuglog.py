"""
Opt-in debug logging for local troubleshooting.

The durable audit stream deliberately omits the host and full URL — a hostname is
a deployment identifier and must not leave in shipped logs. That is exactly what
made URL stitching hard to debug: there was nothing to look at. This module adds a
separate, **off-by-default** DEBUG channel that shows the fully stitched URL and the
exact parameters at each composition point, so no one has to hand-add print
statements again.

Enable it two ways, both deliberate:

  * set ``SN_DEBUG=1`` in the environment, or
  * call :func:`enable_debug` from your own script.

Trade-off, stated plainly: DEBUG lines contain real hostnames and full URLs. Turn
this on only while troubleshooting, and do not point DEBUG at a log sink that
leaves the host. The default (off) keeps the shipped audit stream host-free.
"""

import logging
import os
import sys

#: Parent logger for the whole package. Module loggers (``getLogger(__name__)``)
#: are children of this, so raising this one to DEBUG lights them all up.
PACKAGE_LOGGER: str = "coded_tools.tools.servicenow"

_TRUTHY = {"1", "true", "yes", "on"}


def enable_debug() -> None:
    """
    Route the package's DEBUG lines to stderr, once.

    Idempotent: a second call does not add a second handler. Propagation is turned
    off so these host-bearing lines do not also flow into the framework's shipped
    log stream — they stay on this local handler.
    """
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.setLevel(logging.DEBUG)
    if any(getattr(handler, "_sn_debug", False) for handler in logger.handlers):
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[sn-debug] %(name)s: %(message)s"))
    handler._sn_debug = True  # type: ignore[attr-defined]  # marker for idempotency
    logger.addHandler(handler)
    logger.propagate = False


def enable_debug_if_requested() -> bool:
    """
    Enable debug logging when ``SN_DEBUG`` is set to a truthy value.

    :return: True if debug logging was enabled.
    """
    if os.environ.get("SN_DEBUG", "").strip().lower() in _TRUTHY:
        enable_debug()
        return True
    return False
