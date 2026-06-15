from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_PLACEHOLDER_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")


class MissingTemplateVariableError(ValueError):
    def __init__(self, variable_name: str) -> None:
        super().__init__(f"Missing template variable: {variable_name}")
        self.variable_name = variable_name


def render_template(template: str, parameters: Mapping[str, Any]) -> str:
    def _replace(match: re.Match[str]) -> str:
        variable_name = match.group(1)
        if variable_name not in parameters:
            raise MissingTemplateVariableError(variable_name)
        return str(parameters[variable_name])

    return _PLACEHOLDER_PATTERN.sub(_replace, template)
