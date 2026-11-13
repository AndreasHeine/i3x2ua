"""Shared model-build dependency: preload wait, lock, and app-state cache.

``get_or_build_model`` is the single implementation used by both the bootstrap
and application dependency modules (the two previously held word-identical
copies). It waits for a scheduled preload, serves the cached ``BuildResult``,
and rebuilds under the app-state model lock.
"""

from __future__ import annotations

import asyncio
import logging
from time import perf_counter

from fastapi import Request

from i3x_server.application.state import gateway_state
from i3x_server.errors import i3x_http_error
from i3x_server.http_caching import MODEL_PART, record_revision
from i3x_server.model.builder import ModelBuilder
from i3x_server.model.revision import model_revision
from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)


def get_model_builder(request: Request) -> ModelBuilder:
    builder = gateway_state(request).model_builder
    if builder is None:
        raise i3x_http_error(500, "InternalError", "ModelBuilder not initialized")
    return builder


async def get_or_build_model(request: Request) -> BuildResult:
    model = await _get_or_build_model(request)
    if isinstance(model, BuildResult):
        record_revision(request, MODEL_PART, model_revision(model))
    return model


async def _build_model_task(request: Request) -> BuildResult:
    """Build the model outside the state lock, then swap it in atomically.

    Concurrent lazy callers await the same task instead of serializing behind
    the lock; readers keep serving the previous ``BuildResult`` until the swap.
    """
    builder = get_model_builder(request)
    state = gateway_state(request)
    started = perf_counter()
    logger.info("Model build started")
    try:
        built = await builder.build()
    except BaseException:
        state.model_build_task = None
        raise
    logger.info(
        "Model build finished nodes=%d roots=%d properties=%d actions=%d duration_s=%.3f",
        len(built.nodes_by_id),
        len(built.root_ids),
        len(built.property_to_node),
        len(built.action_to_method),
        perf_counter() - started,
    )
    async with state.model_lock:
        state.model_cache = built
        state.object_type_context_cache = None
    return built


async def _get_or_build_model(request: Request) -> BuildResult:
    state = gateway_state(request)
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
        cache = state.model_cache
        if cache is not None:
            logger.debug("Model cache hit after lock")
            return cache
        build_task = state.model_build_task
        if build_task is None or build_task.done():
            build_task = asyncio.create_task(_build_model_task(request))
            state.model_build_task = build_task

    # The round-trip runs outside the lock; this caller and any concurrent ones
    # share the single in-flight build task.
    return await build_task
