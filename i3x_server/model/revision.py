"""Content-derived model revision.

The revision changes only when the model content changes, so an equivalent periodic
rebuild keeps the same revision and HTTP clients can revalidate cheaply via ETag.
"""

from __future__ import annotations

import hashlib
import json

from i3x_server.schemas.state import BuildResult


def compute_model_revision(model: BuildResult) -> str:
    # Each node carries its children, parent, relationships, role, confidence and metadata
    # (uaNodeId, namespaceUri, appliedProfileIds), so the lookup indexes derived from them
    # are covered by the node hash. Only fields not reflected in the nodes are added.
    digest = hashlib.sha256()
    for node_id in sorted(model.nodes_by_id):
        digest.update(model.nodes_by_id[node_id].model_dump_json().encode())
        digest.update(b"\n")
    extra = {
        "root_ids": model.root_ids,
        "action_to_method": model.action_to_method,
        "node_id_by_name": model.node_id_by_name,
        "node_id_by_type": model.node_id_by_type,
        "graph_relationship_names": sorted(model.graph_relationship_names),
    }
    digest.update(json.dumps(extra, sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()[:32]


def model_revision(model: BuildResult) -> str:
    """Return the model revision, computing it once for models built outside ModelBuilder."""
    if not model.revision:
        model.revision = compute_model_revision(model)
    return model.revision
