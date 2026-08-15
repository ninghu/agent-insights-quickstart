from __future__ import annotations

import asyncio
import os
import re

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponsesAgentServerHost,
    TextResponse,
)
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

_MODEL_DEPLOYMENT_NAME = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "")
_FAULT_TEXT = (
    "I could not check that order because the order lookup service is temporarily "
    "unavailable. Please try again later or contact support."
)
_EXPECTED_ISSUE_CATEGORY = "lookup_order_dependency_failure"
_ORDER_MESSAGES = {
    "ORDER-1001": "Order ORDER-1001 is processing and is expected to ship in 2 days.",
    "ORDER-1002": "Order ORDER-1002 has shipped and is expected to arrive tomorrow.",
    "ORDER-1003": "Order ORDER-1003 is ready for pickup at the sample support desk.",
    "ORDER-1004": "Order ORDER-1004 was delivered earlier today.",
    "ORDER-1005": "Order ORDER-1005 is on hold while the payment check completes.",
    "ORDER-1006": (
        "Order ORDER-1006 has been refunded and the refund should post in 3 business days."
    ),
}
_TRACER = trace.get_tracer("agent_insights_onboarding.sample_order_status")
_REQUEST_PATTERN = re.compile(r"^(healthy|fault):", re.IGNORECASE)
_ORDER_ID_PATTERN = re.compile(r"ORDER-[0-9]{4}", re.IGNORECASE)

app = ResponsesAgentServerHost()


class LookupOrderServiceError(RuntimeError):
    pass


def _parse_request(user_input: str) -> tuple[str | None, str | None]:
    prefix_match = _REQUEST_PATTERN.match(user_input.strip())
    if not prefix_match:
        return None, None

    mode = prefix_match.group(1).lower()
    order_match = _ORDER_ID_PATTERN.search(user_input)
    if not order_match:
        return mode, None

    return mode, order_match.group(0).upper()


def _lookup_order(sample_mode: str, order_id: str) -> str:
    with _TRACER.start_as_current_span("lookup_order", kind=SpanKind.CLIENT) as span:
        span.set_attribute("tool.name", "lookup_order")
        span.set_attribute("gen_ai.tool.name", "lookup_order")
        span.set_attribute("sample.mode", sample_mode)
        span.set_attribute("sample.order_id", order_id)
        span.set_attribute("sample.no_retries", True)
        span.set_attribute("sample.issue.category", _EXPECTED_ISSUE_CATEGORY)
        span.set_attribute(
            "sample.model_deployment_name_present",
            bool(_MODEL_DEPLOYMENT_NAME),
        )

        if sample_mode == "fault":
            error = LookupOrderServiceError(
                "Deterministic sample failure for lookup_order dependency tracing."
            )
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, str(error)))
            raise error

        message = _ORDER_MESSAGES.get(order_id)
        if message is None:
            message = f"Order {order_id} was not found in the sample order catalog."
        span.set_status(Status(StatusCode.OK))
        return message


@app.response_handler
async def handler(
    request: CreateResponse,
    context: ResponseContext,
    _cancellation_signal: asyncio.Event,
):
    user_input = (await context.get_input_text()) or ""
    sample_mode, order_id = _parse_request(user_input)

    if sample_mode is None or order_id is None:
        return TextResponse(
            context,
            request,
            text=(
                "Send one of the sample requests, for example 'healthy: please check "
                "ORDER-1001' or 'fault: please check ORDER-9001'."
            ),
        )

    try:
        customer_message = _lookup_order(sample_mode, order_id)
    except LookupOrderServiceError:
        customer_message = _FAULT_TEXT

    return TextResponse(context, request, text=customer_message)


if __name__ == "__main__":
    app.run()
