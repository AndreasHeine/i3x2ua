from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class PromptDefinition:
    name: str
    description: str
    inputs: tuple[str, ...]
    template: str

    def to_metadata(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PromptRegistry:
    def __init__(self, prompts: Mapping[str, PromptDefinition] | None = None) -> None:
        self._prompts = dict(prompts or {})

    @classmethod
    def load_from_directory(
        cls,
        prompt_directory: str | Path,
        overrides: Mapping[str, Any] | None = None,
    ) -> "PromptRegistry":
        directory = Path(prompt_directory)
        if not directory.exists() or not directory.is_dir():
            return cls({})

        prompt_map: dict[str, PromptDefinition] = {}
        prompt_overrides = _as_mapping(overrides)

        for prompt_file in sorted(directory.glob("*.json")):
            prompt_data = json.loads(prompt_file.read_text(encoding="utf-8"))
            if not isinstance(prompt_data, Mapping):
                raise ValueError(f"Prompt file must contain a JSON object: {prompt_file}")

            prompt = _prompt_from_mapping(prompt_data)
            override = _as_mapping(prompt_overrides.get(prompt.name, {}))
            prompt = _apply_prompt_override(prompt, override)

            if prompt.name in prompt_map:
                raise ValueError(f"Duplicate prompt name: {prompt.name}")
            prompt_map[prompt.name] = prompt

        return cls(prompt_map)

    def list_metadata(self) -> list[dict[str, str]]:
        return [prompt.to_metadata() for _, prompt in sorted(self._prompts.items())]

    def get(self, name: str) -> PromptDefinition | None:
        return self._prompts.get(name)


def _prompt_from_mapping(data: Mapping[str, Any]) -> PromptDefinition:
    name = data.get("name")
    description = data.get("description")
    inputs = data.get("inputs")
    template = data.get("template")

    if not isinstance(name, str) or not name.strip():
        raise ValueError("Prompt field 'name' must be a non-empty string")
    if not isinstance(description, str):
        raise ValueError(f"Prompt {name}: field 'description' must be a string")
    if not isinstance(inputs, list) or not all(isinstance(item, str) and item for item in inputs):
        raise ValueError(f"Prompt {name}: field 'inputs' must be an array of non-empty strings")
    if not isinstance(template, str):
        raise ValueError(f"Prompt {name}: field 'template' must be a string")

    return PromptDefinition(
        name=name,
        description=description,
        inputs=tuple(inputs),
        template=template,
    )


def _apply_prompt_override(prompt: PromptDefinition, override: Mapping[str, Any]) -> PromptDefinition:
    if not override:
        return prompt

    description = override.get("description", prompt.description)
    template = override.get("template", prompt.template)
    inputs_override = override.get("inputs", list(prompt.inputs))

    if not isinstance(description, str):
        raise ValueError(f"Prompt override for {prompt.name}: field 'description' must be a string")
    if not isinstance(template, str):
        raise ValueError(f"Prompt override for {prompt.name}: field 'template' must be a string")
    if not isinstance(inputs_override, list) or not all(
        isinstance(item, str) and item for item in inputs_override
    ):
        raise ValueError(f"Prompt override for {prompt.name}: field 'inputs' must be an array of non-empty strings")

    return PromptDefinition(
        name=prompt.name,
        description=description,
        inputs=tuple(inputs_override),
        template=template,
    )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
