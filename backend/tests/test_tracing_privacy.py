"""Langfuse may receive operational telemetry, never patient content."""

from __future__ import annotations

import importlib
from pathlib import Path

from langfuse import LangfuseOtelSpanAttributes
from langfuse.types import (
    MaskOtelSpansParams,
    OtelSpanData,
    OtelSpanIdentifier,
)


TRACING_MODULE = (
    Path(__file__).resolve().parents[1]
    / "app/observability/tracing.py"
)


def _tracing():
    assert TRACING_MODULE.is_file(), "the privacy-safe tracing module is missing"
    return importlib.import_module("app.observability.tracing")


def test_export_mask_removes_content_and_identity_but_keeps_cost_metrics():
    tracing = _tracing()
    attributes = {
        LangfuseOtelSpanAttributes.TRACE_NAME: "agentcare-workflow",
        LangfuseOtelSpanAttributes.TRACE_INPUT: '{"request_text":"Jane needs help"}',
        LangfuseOtelSpanAttributes.TRACE_OUTPUT: '{"final_response":"private"}',
        LangfuseOtelSpanAttributes.TRACE_METADATA: '{"workflow_id":42}',
        LangfuseOtelSpanAttributes.TRACE_USER_ID: "patient-7",
        LangfuseOtelSpanAttributes.TRACE_SESSION_ID: "session-9",
        LangfuseOtelSpanAttributes.OBSERVATION_INPUT: '{"messages":["private"]}',
        LangfuseOtelSpanAttributes.OBSERVATION_OUTPUT: '{"content":"private"}',
        LangfuseOtelSpanAttributes.OBSERVATION_METADATA: '{"document":"private"}',
        LangfuseOtelSpanAttributes.OBSERVATION_STATUS_MESSAGE: "private failure",
        "input.value": '{"request_text":"private"}',
        "output.value": '{"response":"private"}',
        "gen_ai.prompt.0.content": "private prompt",
        "gen_ai.completion.0.content": "private completion",
        "tool.call.arguments": '{"patient_id":7}',
        "tool.call.result": '{"appointment":"private"}',
        "exception.message": "private request failed",
        "exception.stacktrace": "private stack",
        LangfuseOtelSpanAttributes.OBSERVATION_MODEL: "gemini-2.5-flash",
        LangfuseOtelSpanAttributes.OBSERVATION_USAGE_DETAILS: (
            '{"input":120,"output":30}'
        ),
        LangfuseOtelSpanAttributes.OBSERVATION_COST_DETAILS: (
            '{"input":0.0001,"output":0.0002}'
        ),
        LangfuseOtelSpanAttributes.ENVIRONMENT: "production",
    }
    identifier = OtelSpanIdentifier(trace_id="1" * 32, span_id="2" * 16)
    span = OtelSpanData(
        trace_id=identifier.trace_id,
        span_id=identifier.span_id,
        parent_span_id=None,
        name="ChatVertexAI",
        instrumentation_scope_name="langchain",
        instrumentation_scope_version="1",
        attributes=attributes,
        resource_attributes={},
    )

    result = tracing.mask_otel_spans(
        MaskOtelSpansParams(spans={identifier: span})
    )

    assert result is not None
    patch = result.span_patches[identifier]
    assert patch is not None
    for key in (
        LangfuseOtelSpanAttributes.TRACE_INPUT,
        LangfuseOtelSpanAttributes.TRACE_OUTPUT,
        LangfuseOtelSpanAttributes.TRACE_METADATA,
        LangfuseOtelSpanAttributes.TRACE_USER_ID,
        LangfuseOtelSpanAttributes.TRACE_SESSION_ID,
        LangfuseOtelSpanAttributes.OBSERVATION_INPUT,
        LangfuseOtelSpanAttributes.OBSERVATION_OUTPUT,
        LangfuseOtelSpanAttributes.OBSERVATION_METADATA,
        LangfuseOtelSpanAttributes.OBSERVATION_STATUS_MESSAGE,
        "input.value",
        "output.value",
        "gen_ai.prompt.0.content",
        "gen_ai.completion.0.content",
        "tool.call.arguments",
        "tool.call.result",
        "exception.message",
        "exception.stacktrace",
    ):
        assert key in patch.delete_attributes

    assert LangfuseOtelSpanAttributes.OBSERVATION_MODEL not in patch.delete_attributes
    assert (
        LangfuseOtelSpanAttributes.OBSERVATION_USAGE_DETAILS
        not in patch.delete_attributes
    )
    assert (
        LangfuseOtelSpanAttributes.OBSERVATION_COST_DETAILS
        not in patch.delete_attributes
    )
    assert patch.set_attributes == {"agentcare.content_masked": True}


def test_export_mask_leaves_content_free_span_unchanged():
    tracing = _tracing()
    identifier = OtelSpanIdentifier(trace_id="3" * 32, span_id="4" * 16)
    span = OtelSpanData(
        trace_id=identifier.trace_id,
        span_id=identifier.span_id,
        parent_span_id=None,
        name="agentcare-workflow",
        instrumentation_scope_name="langfuse",
        instrumentation_scope_version="4.14.1",
        attributes={
            LangfuseOtelSpanAttributes.TRACE_NAME: "agentcare-workflow",
            LangfuseOtelSpanAttributes.ENVIRONMENT: "production",
        },
        resource_attributes={},
    )

    result = tracing.mask_otel_spans(
        MaskOtelSpansParams(spans={identifier: span})
    )

    assert result is None
