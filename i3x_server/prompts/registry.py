from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
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
    def load_from_overrides(
        cls,
        overrides: Mapping[str, Any] | None,
    ) -> PromptRegistry:
        prompt_overrides = _as_mapping(overrides)
        if not prompt_overrides:
            return cls({})

        prompt_map: dict[str, PromptDefinition] = {}
        for prompt_name, prompt_data in sorted(prompt_overrides.items()):
            if not isinstance(prompt_name, str) or not prompt_name.strip():
                raise ValueError("Prompt key must be a non-empty string")
            prompt_mapping = _as_mapping(prompt_data)
            prompt = _prompt_from_mapping(prompt_name, prompt_mapping)
            prompt_map[prompt.name] = prompt

        return cls(prompt_map)

    def list_metadata(self) -> list[dict[str, str]]:
        return [prompt.to_metadata() for _, prompt in sorted(self._prompts.items())]

    def get(self, name: str) -> PromptDefinition | None:
        return self._prompts.get(name)


def _prompt_from_mapping(name: str, data: Mapping[str, Any]) -> PromptDefinition:
    description = data.get("description")
    inputs = data.get("inputs")
    template = data.get("template")

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


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
