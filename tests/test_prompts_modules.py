from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from i3x_server.prompts.api import execute_prompt, get_prompt, list_prompt_metadata
from i3x_server.prompts.registry import PromptDefinition, PromptRegistry
from i3x_server.prompts.renderer import MissingTemplateVariableError, render_template


def test_prompt_definition_and_registry_basics() -> None:
    prompt = PromptDefinition(
        name="demo",
        description="Demo prompt",
        inputs=("asset_id",),
        template="Asset {{asset_id}}",
    )
    registry = PromptRegistry({"demo": prompt})
    assert prompt.to_metadata() == {"name": "demo", "description": "Demo prompt"}
    assert prompt.to_dict()["template"] == "Asset {{asset_id}}"
    assert registry.list_metadata() == [{"name": "demo", "description": "Demo prompt"}]
    assert registry.get("demo") is prompt
    assert registry.get("missing") is None


def test_registry_load_from_overrides_and_validation_errors() -> None:
    loaded = PromptRegistry.load_from_overrides(
        {
            "demo": {
                "description": "Demo prompt",
                "inputs": ["asset_id", "minutes"],
                "template": "Asset {{asset_id}} for {{minutes}} min",
            }
        }
    )
    assert loaded.get("demo") is not None

    assert PromptRegistry.load_from_overrides(None).list_metadata() == []
    with pytest.raises(ValueError):
        PromptRegistry.load_from_overrides({"": {"description": "d", "inputs": ["x"], "template": "{{x}}"}})
    with pytest.raises(ValueError):
        PromptRegistry.load_from_overrides({"demo": {"description": 1, "inputs": ["x"], "template": "{{x}}"}})
    with pytest.raises(ValueError):
        PromptRegistry.load_from_overrides({"demo": {"description": "d", "inputs": [""], "template": "{{x}}"}})
    with pytest.raises(ValueError):
        PromptRegistry.load_from_overrides({"demo": {"description": "d", "inputs": ["x"], "template": 1}})


def test_renderer_substitutes_and_raises_for_missing_variable() -> None:
    rendered = render_template("Asset {{asset_id}}", {"asset_id": "A-1"})
    assert rendered == "Asset A-1"
    with pytest.raises(MissingTemplateVariableError) as exc_info:
        render_template("Asset {{asset_id}}", {})
    assert exc_info.value.variable_name == "asset_id"


def test_api_list_and_get_prompt_behaviors() -> None:
    prompt = PromptDefinition(
        name="demo",
        description="Demo prompt",
        inputs=("asset_id",),
        template="Asset {{asset_id}}",
    )
    registry = PromptRegistry({"demo": prompt})

    assert list_prompt_metadata(None) == []
    assert list_prompt_metadata(registry) == [{"name": "demo", "description": "Demo prompt"}]
    assert get_prompt(registry, "demo")["name"] == "demo"

    with pytest.raises(HTTPException) as missing_registry:
        get_prompt(None, "demo")
    with pytest.raises(HTTPException) as missing_prompt:
        get_prompt(registry, "missing")
    assert missing_registry.value.status_code == 404
    assert missing_prompt.value.status_code == 404


def test_execute_prompt_success_missing_inputs_and_render_error(monkeypatch: pytest.MonkeyPatch) -> None:
    prompt = PromptDefinition(
        name="demo",
        description="Demo prompt",
        inputs=("asset_id",),
        template="Asset {{asset_id}}",
    )
    registry = PromptRegistry({"demo": prompt})

    success = execute_prompt(registry, "demo", {"asset_id": "A-1"})
    assert success["rendered"] == "Asset A-1"

    with pytest.raises(HTTPException) as missing_inputs:
        execute_prompt(registry, "demo", {})
    assert missing_inputs.value.status_code == 400

    def _raise_missing(template: str, parameters: dict[str, object]) -> str:
        del template, parameters
        raise MissingTemplateVariableError("asset_id")

    monkeypatch.setattr("i3x_server.prompts.api.render_template", _raise_missing)
    with pytest.raises(HTTPException) as render_error:
        execute_prompt(registry, "demo", {"asset_id": "A-1"})
    assert render_error.value.status_code == 400


def test_execute_prompt_with_span_context(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Span:
        def __init__(self) -> None:
            self.attrs: dict[str, object] = {}

        def set_attribute(self, key: str, value: object) -> None:
            self.attrs[key] = value

        def record_exception(self, exc: Exception) -> None:
            self.attrs["exception"] = str(exc)

        def set_status(self, status: object) -> None:
            self.attrs["status"] = status

    span = _Span()
    tracer = SimpleNamespace(start_as_current_span=lambda _: nullcontext(span))
    monkeypatch.setattr("i3x_server.prompts.api.trace", SimpleNamespace(get_tracer=lambda _: tracer))

    prompt = PromptDefinition(
        name="demo",
        description="Demo prompt",
        inputs=("asset_id",),
        template="Asset {{asset_id}}",
    )
    registry = PromptRegistry({"demo": prompt})
    result = execute_prompt(registry, "demo", {"asset_id": "A-1"})
    assert result["name"] == "demo"
    assert span.attrs["prompt.name"] == "demo"
    assert span.attrs["render.success"] is True
