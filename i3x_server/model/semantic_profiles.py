from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from i3x_server.domain.ports.opcua import OpcUaNodeInfo

SemanticRole = Literal["asset", "group", "component", "datapoint", "property", "unknown"]
MappingConfidence = Literal["high", "medium", "low"]


@dataclass(frozen=True, slots=True)
class ProfileOverride:
    type_name: str
    semantic_role: SemanticRole | None = None
    force_hierarchy: bool = False
    force_composition: bool = False
    confidence: MappingConfidence | None = None


@dataclass(frozen=True, slots=True)
class SemanticProfile:
    profile_id: str
    priority: int
    namespace_uri_fragment: str
    known_types: tuple[str, ...] = ()
    hierarchy_references: tuple[str, ...] = ()
    composition_references: tuple[str, ...] = ()
    graph_references: tuple[str, ...] = ()
    overrides: tuple[ProfileOverride, ...] = ()

    def matches_namespace(self, namespace_uri: str | None) -> bool:
        if not self.namespace_uri_fragment:
            return True
        if not namespace_uri:
            return False
        return self.namespace_uri_fragment.lower() in namespace_uri.lower()

    def matches_node(self, node: OpcUaNodeInfo, namespace_uri: str | None) -> bool:
        if not self.matches_namespace(namespace_uri):
            return False
        if not self.known_types:
            return True
        tokens = {
            _normalize_token(node.node_id),
            _normalize_token(node.browse_name),
            _normalize_token(node.type_definition_id),
        }
        return bool(tokens & {_normalize_token(item) for item in self.known_types})


@dataclass(frozen=True, slots=True)
class ResolvedProfileSet:
    profiles: tuple[SemanticProfile, ...]

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(profile.profile_id for profile in self.profiles)


_GENERIC_PROFILE = SemanticProfile(
    profile_id="generic",
    priority=10,
    namespace_uri_fragment="",
    hierarchy_references=("Organizes", "HasChild", "HierarchicalReferences"),
    composition_references=("HasComponent", "HasOrderedComponent", "HasProperty", "PropertyOf"),
    graph_references=("NonHierarchicalReferences",),
)

_MACHINERY_PROFILE = SemanticProfile(
    profile_id="machinery",
    priority=100,
    namespace_uri_fragment="http://opcfoundation.org/UA/Machinery",
    known_types=("FunctionalGroupType", "DataItemType"),
    hierarchy_references=("Organizes",),
    composition_references=("HasProperty", "DataItemType"),
    graph_references=("FlowsTo",),
    overrides=(
        ProfileOverride(
            type_name="FunctionalGroupType",
            semantic_role="group",
            force_hierarchy=True,
            confidence="high",
        ),
        ProfileOverride(
            type_name="DataItemType",
            semantic_role="datapoint",
            force_composition=True,
            confidence="high",
        ),
    ),
)

_DEFAULT_PROFILES: tuple[SemanticProfile, ...] = (_MACHINERY_PROFILE, _GENERIC_PROFILE)


def _normalize_token(value: str | None) -> str:
    if not value:
        return ""
    lowered = value.strip().lower()
    if not lowered:
        return ""
    if ":" in lowered:
        lowered = lowered.rsplit(":", 1)[-1]
    if "/" in lowered:
        lowered = lowered.rsplit("/", 1)[-1]
    if ";" in lowered:
        lowered = lowered.rsplit(";", 1)[-1]
    return "".join(ch for ch in lowered if ch.isalnum())


def load_default_profiles() -> tuple[SemanticProfile, ...]:
    return _DEFAULT_PROFILES


def resolve_namespace_uri(node_id: str, namespace_uri_by_index: dict[int, str] | None = None) -> str | None:
    if not namespace_uri_by_index:
        return None
    prefix, _, _rest = node_id.partition(";")
    if not prefix.startswith("ns="):
        return None
    try:
        namespace_index = int(prefix[3:])
    except ValueError:
        return None
    return namespace_uri_by_index.get(namespace_index)


def active_profiles_for_node(
    node: OpcUaNodeInfo,
    namespace_uri: str | None,
    available_profiles: tuple[SemanticProfile, ...] | None = None,
) -> ResolvedProfileSet:
    profiles = available_profiles or load_default_profiles()
    matched = [profile for profile in profiles if profile.matches_node(node, namespace_uri)]
    matched.sort(key=lambda profile: profile.priority, reverse=True)
    return ResolvedProfileSet(profiles=tuple(matched))


def resolve_semantic_role(
    node: OpcUaNodeInfo,
    active_profiles: ResolvedProfileSet | None = None,
    *,
    relationship_class: str | None = None,
) -> SemanticRole:
    profiles = active_profiles.profiles if active_profiles else load_default_profiles()
    type_tokens = {
        _normalize_token(node.type_definition_id),
        _normalize_token(node.browse_name),
        _normalize_token(node.node_id),
    }
    for profile in profiles:
        for override in profile.overrides:
            if _normalize_token(override.type_name) in type_tokens:
                if override.semantic_role is not None:
                    return override.semantic_role

    if node.event_notifier:
        return "group"
    if node.node_class == "Variable":
        return "datapoint"
    if node.node_class == "Method":
        return "component"
    if relationship_class == "composition":
        return "property"
    return "asset"


def resolve_mapping_confidence(
    node: OpcUaNodeInfo,
    active_profiles: ResolvedProfileSet | None = None,
    *,
    relationship_class: str | None = None,
    has_profile_override: bool = False,
) -> MappingConfidence:
    if has_profile_override:
        return "high"
    if active_profiles and active_profiles.profile_ids != ("generic",):
        return "high"
    if node.node_class == "Variable":
        return "medium"
    if relationship_class == "graph":
        return "high"
    if relationship_class == "composition":
        return "medium"
    return "medium"


def has_profile_override_for_node(
    node: OpcUaNodeInfo,
    active_profiles: ResolvedProfileSet | None = None,
) -> bool:
    profiles = active_profiles.profiles if active_profiles else load_default_profiles()
    type_tokens = {
        _normalize_token(node.type_definition_id),
        _normalize_token(node.browse_name),
        _normalize_token(node.node_id),
    }
    for profile in profiles:
        for override in profile.overrides:
            if _normalize_token(override.type_name) in type_tokens:
                return True
    return False


def reference_classification_overrides(active_profiles: ResolvedProfileSet | None = None) -> dict[str, str]:
    overrides: dict[str, str] = {}
    profiles = active_profiles.profiles if active_profiles else load_default_profiles()
    for profile in profiles:
        for name in profile.hierarchy_references:
            overrides[_normalize_token(name)] = "hierarchy"
        for name in profile.composition_references:
            overrides[_normalize_token(name)] = "composition"
        for name in profile.graph_references:
            overrides[_normalize_token(name)] = "graph"
    return overrides
