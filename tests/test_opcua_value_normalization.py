from dataclasses import dataclass
from typing import Any

import pytest
from asyncua import ua

from i3x_server.schemas.opcua_value import normalize_opcua_value


@pytest.mark.parametrize("rank", [-3, -2, -1, 0])
def test_nonpositive_argument_rank_uses_null_dimensions(rank: int) -> None:
    argument = ua.Argument(ValueRank=rank, ArrayDimensions=[])
    normalized = normalize_opcua_value(argument)
    assert normalized.ArrayDimensions is None
    assert argument.ArrayDimensions == []
    assert normalize_opcua_value(normalized) is normalized


@pytest.mark.parametrize("dimensions", [None, [], [0], [10]])
def test_positive_argument_rank_preserves_dimensions(dimensions: list[int] | None) -> None:
    argument = ua.Argument(ValueRank=1, ArrayDimensions=dimensions)  # type: ignore[arg-type]
    assert normalize_opcua_value(argument) is argument


def test_nested_arguments_are_normalized_without_changing_other_arrays() -> None:
    @dataclass
    class Structure:
        body: Any

    argument = ua.Argument(ValueRank=-1, ArrayDimensions=[])
    original = Structure({"arguments": ([argument],), "empty": [], "null": None})
    normalized = normalize_opcua_value(original)
    assert normalized.body["arguments"][0][0].ArrayDimensions is None
    assert normalized.body["empty"] == []
    assert normalized.body["null"] is None
    assert original.body["arguments"][0][0].ArrayDimensions == []
