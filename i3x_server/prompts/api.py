from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from time import perf_counter
from typing import Any

from i3x_server.errors import i3x_http_error
from i3x_server.prompts.registry import PromptRegistry
from i3x_server.prompts.renderer import MissingTemplateVariableError, render_template

try:
    from opentelemetry import trace
    from opentelemetry.trace import Status as _OtelStatus
    from opentelemetry.trace import StatusCode as _OtelStatusCode
except ImportError:  # pragma: no cover - optional dependency
    trace = None
    _OtelStatus = None
    _OtelStatusCode = None


def list_prompt_metadata(registry: PromptRegistry | None) -> list[dict[str, str]]:
    if registry is None:
        return []
    return registry.list_metadata()


def get_prompt(registry: PromptRegistry | None, name: str) -> dict[str, Any]:
    if registry is None:
        raise i3x_http_error(404, "Not Found", f"Unknown prompt {name}")
    prompt = registry.get(name)
    if prompt is None:
        raise i3x_http_error(404, "Not Found", f"Unknown prompt {name}")
    return prompt.to_dict()


def execute_prompt(
    registry: PromptRegistry | None,
    name: str,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    if registry is None:
        raise i3x_http_error(404, "Not Found", f"Unknown prompt {name}")

    prompt = registry.get(name)
    if prompt is None:
        raise i3x_http_error(404, "Not Found", f"Unknown prompt {name}")

    tracer = trace.get_tracer("i3x_server.prompts") if trace is not None else None
    span_context = tracer.start_as_current_span("prompt.execute") if tracer is not None else nullcontext()

    started = perf_counter()
    with span_context as span:
        if span is not None:
            span.set_attribute("prompt.name", prompt.name)
            span.set_attribute("prompt.inputs", ",".join(prompt.inputs))

        missing_inputs = [input_name for input_name in prompt.inputs if input_name not in parameters]
        if missing_inputs:
            if span is not None:
                span.set_attribute("render.success", False)
                span.set_attribute("execution.time", perf_counter() - started)
                if _OtelStatus is not None and _OtelStatusCode is not None:
                    span.set_status(_OtelStatus(_OtelStatusCode.ERROR, description="Missing prompt inputs"))
            raise i3x_http_error(400, "Bad Request", f"Missing prompt inputs: {', '.join(missing_inputs)}")

        try:
            rendered = render_template(prompt.template, parameters)
        except MissingTemplateVariableError as exc:
            if span is not None:
                span.set_attribute("render.success", False)
                span.set_attribute("execution.time", perf_counter() - started)
                span.record_exception(exc)
                if _OtelStatus is not None and _OtelStatusCode is not None:
                    span.set_status(_OtelStatus(_OtelStatusCode.ERROR, description=str(exc)))
            raise i3x_http_error(400, "Bad Request", str(exc)) from exc

        if span is not None:
            span.set_attribute("render.success", True)
            span.set_attribute("execution.time", perf_counter() - started)

        return {
            "name": prompt.name,
            "description": prompt.description,
            "inputs": list(prompt.inputs),
            "template": prompt.template,
            "rendered": rendered,
        }
