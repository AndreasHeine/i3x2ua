"""Batch-size helpers for OPC UA round-trips.

Browse/read requests are capped per call by the upstream server. Splitting a request batch
into fixed-size chunks (``chunked``) and halving it when the server rejects the whole batch
(``split_batch``) is the same arithmetic everywhere; this module is its single implementation.
"""

from __future__ import annotations

from typing import Any


def chunked(values: list[Any], size: int) -> list[list[Any]]:
    """Split a list into batches of ``size``, last batch possibly shorter.

    ``size`` is clamped to at least 1 so a zero/negative limit cannot raise on the ``range``
    step; the degenerate limit then means "one item per batch", like the old call-site clamps.
    """
    effective = max(1, size)
    return [values[idx : idx + effective] for idx in range(0, len(values), effective)]


def halved_size(values: list[Any]) -> int:
    """Batch size for a retry after the whole batch was rejected: halve it.

    ``max(1, ...)`` keeps the retry batch non-empty; a single item cannot be halved further.
    """
    return max(1, len(values) // 2)


def split_batch(values: list[Any]) -> tuple[list[Any], list[Any]]:
    """Halve a batch so each half can be retried as its own request.

    The first half stays non-empty (``halved_size``): with a single item there is nothing to
    split, and an empty half would be sent as a pointless empty request.
    """
    split_at = halved_size(values)
    return values[:split_at], values[split_at:]
