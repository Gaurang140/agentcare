"""Optional Langfuse tracing with export-stage content removal."""

from __future__ import annotations

import contextlib
from typing import Any

from app.config import settings
from app.logging_setup import get_logger


logger = get_logger(__name__)

_client: Any | None = None

_SAFE_ATTRIBUTE_KEYS = frozenset(
    {
        "error.type",
        "exception.type",
        "http.response.status_code",
        "langfuse.environment",
        "langfuse.observation.completion_start_time",
        "langfuse.observation.cost_details",
        "langfuse.observation.level",
        "langfuse.observation.model.name",
        "langfuse.observation.model.parameters",
        "langfuse.observation.type",
        "langfuse.observation.usage_details",
        "langfuse.release",
        "langfuse.trace.name",
        "langfuse.trace.tags",
        "langfuse.version",
    }
)
_SAFE_ATTRIBUTE_PREFIXES = (
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.usage.",
    "llm.token_usage.",
)


def _attribute_is_safe(name: str) -> bool:
    return name in _SAFE_ATTRIBUTE_KEYS or name.startswith(
        _SAFE_ATTRIBUTE_PREFIXES
    )


def mask_otel_spans(params: Any) -> Any:
    """Remove every non-operational span attribute before Langfuse export."""
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches = {}
    for identifier, span in params.spans.items():
        delete_attributes = tuple(
            sorted(
                name
                for name in span.attributes
                if not _attribute_is_safe(name)
            )
        )
        if delete_attributes:
            patches[identifier] = OtelSpanPatch(
                delete_attributes=delete_attributes,
                set_attributes={"agentcare.content_masked": True},
            )

    if not patches:
        return None
    return MaskOtelSpansResult(span_patches=patches)


def tracing_enabled() -> bool:
    return bool(
        settings.langfuse_public_key
        and settings.langfuse_secret_key
        and settings.langfuse_sample_rate > 0
    )


def _ensure_langfuse_client() -> Any:
    global _client
    if _client is not None:
        return _client

    from langfuse import Langfuse

    _client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url,
        environment=settings.environment,
        release=settings.app_release or None,
        sample_rate=settings.langfuse_sample_rate,
        mask_otel_spans=mask_otel_spans,
    )
    return _client


@contextlib.contextmanager
def observe_workflow(config: dict[str, Any]):
    """Attach one optional, content-free Langfuse trace to a graph run."""
    if not tracing_enabled():
        yield
        return

    try:
        from langfuse import propagate_attributes
        from langfuse.langchain import CallbackHandler
    except ImportError as exc:
        logger.warning("langfuse_disabled_missing_dependency", error=str(exc))
        yield
        return

    try:
        _ensure_langfuse_client()
        callback = CallbackHandler(
            public_key=settings.langfuse_public_key,
        )
    except Exception as exc:  # noqa: BLE001 - optional telemetry must fail open
        logger.warning(
            "langfuse_disabled_initialization_failed",
            error_type=type(exc).__name__,
        )
        yield
        return

    config.setdefault("callbacks", []).append(callback)
    with propagate_attributes(
        trace_name="agentcare-workflow",
        tags=["agentcare", "administration"],
        environment=settings.environment,
    ):
        yield


def shutdown_tracing() -> None:
    """Flush and stop the optional exporter without blocking app shutdown."""
    global _client
    client, _client = _client, None
    if client is None:
        return
    try:
        client.shutdown()
    except Exception as exc:  # noqa: BLE001 - shutdown remains best effort
        logger.warning(
            "langfuse_shutdown_failed",
            error_type=type(exc).__name__,
        )
