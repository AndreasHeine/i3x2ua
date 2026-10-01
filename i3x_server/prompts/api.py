from __future__ import annotations

from collections.abc import Mapping
from time import perf_counter
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status as _OtelStatus
from opentelemetry.trace import StatusCode as _OtelStatusCode

from i3x_server.errors import i3x_http_error
from i3x_server.prompts.registry import PromptRegistry
from i3x_server.prompts.renderer import MissingTemplateVariableError, render_template


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

    tracer = trace.get_tracer("i3x_server.prompts")
    span_context = tracer.start_as_current_span("prompt.execute")

    started = perf_counter()
    with span_context as span:
        span.set_attribute("prompt.name", prompt.name)
        span.set_attribute("prompt.inputs", ",".join(prompt.inputs))

        missing_inputs = [input_name for input_name in prompt.inputs if input_name not in parameters]
        if missing_inputs:
            span.set_attribute("render.success", False)
            span.set_attribute("execution.time", perf_counter() - started)
            span.set_status(_OtelStatus(_OtelStatusCode.ERROR, description="Missing prompt inputs"))
            raise i3x_http_error(400, "Bad Request", f"Missing prompt inputs: {', '.join(missing_inputs)}")

        try:
            rendered = render_template(prompt.template, parameters)
        except MissingTemplateVariableError as exc:
            span.set_attribute("render.success", False)
            span.set_attribute("execution.time", perf_counter() - started)
            span.record_exception(exc)
            span.set_status(_OtelStatus(_OtelStatusCode.ERROR, description=str(exc)))
            raise i3x_http_error(400, "Bad Request", str(exc)) from exc

        span.set_attribute("render.success", True)
        span.set_attribute("execution.time", perf_counter() - started)

        return {
            "name": prompt.name,
            "description": prompt.description,
            "inputs": list(prompt.inputs),
            "template": prompt.template,
            "rendered": rendered,
        }
