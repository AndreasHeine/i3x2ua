"""
V1 router compatibility wrapper.

This module maintains backward compatibility during the refactoring of the
monolithic v1.py into feature-based modules. Currently, it re-exports the
router from the single-file implementation.

During Phase 2+, this package will gradually absorb routes into focused
modules as they are extracted.
"""

# Phase 2 placeholder: import from monolithic implementation
# Future phases will introduce feature-split modules here
from i3x_server.api.v1.monolithic import router

__all__ = ["router"]
