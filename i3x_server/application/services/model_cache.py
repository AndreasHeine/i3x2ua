"""Shared model-build dependency: preload wait, lock, and app-state cache.

``get_or_build_model`` is the single implementation used by both the bootstrap
and application dependency modules (the two previously held word-identical
copies). Preload, periodic refresh, and lazy requests all run through the one
in-flight build pipeline (``build_model`` and ``state.model_build_task``):
callers join a running walk instead of starting a second one, and the swap only
publishes the walk with the newest sequence, so a stale walk cannot replace a
fresher revision. One cache-invalidation policy: the object-type context cache
survives model swaps and detects genuine changes through its token keys.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from time import perf_counter
from typing import Any

from fastapi import Request

from i3x_server.application.state import gateway_state
from i3x_server.errors import i3x_http_error
from i3x_server.http_caching import MODEL_PART, record_revision
from i3x_server.model.builder import ModelBuilder
from i3x_server.model.revision import model_revision
from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)

# Applies a client budget (the refresh budget) to the walk this caller starts.
_ModelBuildThrottle = Callable[[], AbstractAsyncContextManager[None]]


def get_model_builder(request: Request) -> ModelBuilder:
    builder = gateway_state(request).model_builder
    if builder is None:
        raise i3x_http_error(500, "InternalError", "ModelBuilder not initialized")
    return builder


async def get_or_build_model(request: Request) -> BuildResult:
    model = await build_model(request)
    if isinstance(model, BuildResult):
        record_revision(request, MODEL_PART, model_revision(model))
    return model


async def build_model(
    request: Any,
    *,
    force: bool = False,
    throttle: _ModelBuildThrottle | None = None,
) -> BuildResult:
    """The single in-flight build pipeline used by preload, periodic refresh, and lazy requests.

    Callers join the in-flight walk instead of starting a second one; the walk runs outside
    the model lock so readers keep serving the previous ``BuildResult``, and only the swap is
    serialized. ``force`` re-walks the server even with a warm cache (preload and refresh);
    ``throttle`` applies a client budget to a walk this caller starts.
    """
    state = gateway_state(request)
    if not force:
        preload_task = state.model_preload_task
        if preload_task is not None and not preload_task.done():
            logger.info("Model preload in progress; waiting for completion")
            try:
                await preload_task
            except Exception:
                logger.exception("Background model preload failed; falling back to lazy build")

        cache = state.model_cache
        if cache is not None:
            logger.debug("Model cache hit")
            return cache

    build_task = state.model_build_task
    if build_task is not None and not build_task.done():
        logger.debug("Model build in progress; waiting for completion")
        return await build_task

    async with state.model_lock:
        if not force:
            cache = state.model_cache
            if cache is not None:
                logger.debug("Model cache hit after lock")
                return cache
        build_task = state.model_build_task
        if build_task is None or build_task.done():
            state.model_build_sequence += 1
            build_task = asyncio.create_task(_build_model_task(request, state.model_build_sequence, throttle))
            state.model_build_task = build_task

    # The round-trip runs outside the lock; this caller and any concurrent ones
    # share the single in-flight build task.
    return await build_task


async def _build_model_task(request: Any, sequence: int, throttle: _ModelBuildThrottle | None) -> BuildResult:
    """Walk the server outside the state lock, then swap the result in atomically.

    The swap publishes only the newest sequence: a walk that finished after a fresher
    result was already published is discarded, and its callers receive the published
    result instead of the stale one.
    """
    state = gateway_state(request)
    started = perf_counter()
    logger.info("Model build started sequence=%d", sequence)
    try:
        builder = get_model_builder(request)
        if throttle is None:
            built = await builder.build()
        else:
            async with throttle():
                built = await builder.build()
    except BaseException:
        async with state.model_lock:
            if state.model_build_task is asyncio.current_task():
                state.model_build_task = None
        raise
    logger.info(
        "Model build finished nodes=%d roots=%d properties=%d actions=%d sequence=%d duration_s=%.3f",
        len(built.nodes_by_id),
        len(built.root_ids),
        len(built.property_to_node),
        len(built.action_to_method),
        sequence,
        perf_counter() - started,
    )
    async with state.model_lock:
        if state.model_build_task is asyncio.current_task():
            state.model_build_task = None
        if sequence >= state.model_published_sequence:
            state.model_cache = built
            state.model_published_sequence = sequence
            # One invalidation policy: the object-type context cache stays. Contexts are
            # keyed by model/namespace/object-type tokens, so a new revision misses the key
            # and the context helpers rebuild or lazily refresh it in the background.
            return built
        logger.info(
            "Model build discarded as stale sequence=%d published_sequence=%d",
            sequence,
            state.model_published_sequence,
        )
        # A fresher walk already published; serve its result to the callers awaiting this one.
        return state.model_cache if state.model_cache is not None else built
