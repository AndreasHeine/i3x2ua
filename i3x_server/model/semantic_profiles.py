"""Semantic profiles that override node and relationship classification per namespace or node."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from i3x_server.domain.ports.opcua import OpcUaNodeInfo, OpcUaTypeDefinitionInfo

SemanticRole = Literal["asset", "group", "component", "datapoint", "property", "unknown"]
MappingConfidence = Literal["high", "medium", "low"]


@dataclass(frozen=True, slots=True)
class ProfileOverride:
    type_name: str
    # When set, the override matches only type entries whose namespace URI equals this
    # canonical URI, so a same-named type in an unrelated namespace cannot carry it.
    namespace_uri: str | None = None
    semantic_role: SemanticRole | None = None
    force_hierarchy: bool = False
    force_composition: bool = False
    confidence: MappingConfidence | None = None

    def __post_init__(self) -> None:
        if self.force_hierarchy and self.force_composition:
            raise ValueError("A profile override cannot force both hierarchy and composition")


@dataclass(frozen=True, slots=True)
class SemanticProfile:
    profile_id: str
    priority: int
    # Canonical namespace URI compared against the namespace of any node in the instance
    # type's inheritance chain; an empty value means no namespace constraint.
    namespace_uri: str
    known_types: tuple[str, ...] = ()
    hierarchy_references: tuple[str, ...] = ()
    composition_references: tuple[str, ...] = ()
    graph_references: tuple[str, ...] = ()
    overrides: tuple[ProfileOverride, ...] = ()

    def matches_node(
        self,
        node: OpcUaNodeInfo,
        type_definition_hierarchy: tuple[OpcUaTypeDefinitionInfo, ...] = (),
    ) -> bool:
        if not self.namespace_uri and not self.known_types:
            return True
        # Namespace and known-type constraints are evaluated independently: a chain entry
        # satisfies the namespace constraint, another entry may satisfy the type constraint.
        # Companion models mix namespaces - a Machinery type derives from the DI
        # FunctionalGroupType (OPC 40001-1 §8.3) - so requiring both on the same entry
        # would never match a real hierarchy.
        if self.namespace_uri and not any(
            _namespace_uri_matches(type_info.namespace_uri, self.namespace_uri)
            for type_info in type_definition_hierarchy
        ):
            return False
        if self.known_types:
            known_type_tokens = {_normalize_token(item) for item in self.known_types}
            if not any(
                _normalize_token(type_info.browse_name) in known_type_tokens for type_info in type_definition_hierarchy
            ):
                return False
        return True


@dataclass(frozen=True, slots=True)
class ResolvedProfileSet:
    profiles: tuple[SemanticProfile, ...]

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(profile.profile_id for profile in self.profiles)


# Canonical namespace URI declared by the standard (trailing slash included): OPC 40001-1
# §20.1-20.2. The Machinery types derive from the DI namespace (OPC 10000-100 §4.4.1).
_MACHINERY_NAMESPACE_URI = "http://opcfoundation.org/UA/Machinery/"

_GENERIC_PROFILE = SemanticProfile(
    profile_id="generic",
    priority=10,
    namespace_uri="",
    hierarchy_references=("Organizes", "HasChild", "HierarchicalReferences"),
    composition_references=("HasComponent", "HasOrderedComponent", "HasProperty", "PropertyOf"),
    graph_references=("NonHierarchicalReferences", "GeneratesEvent", "HasEventSource"),
)

_MACHINERY_PROFILE = SemanticProfile(
    profile_id="machinery",
    priority=100,
    # Membership of any chain entry in the canonical Machinery namespace identifies the
    # model; the standard intentionally does not define a generic MachineryItem ObjectType
    # and leaves its domain-specific organization open (OPC 40001-1 §6.3.1), so known
    # types are deliberately not enumerated here.
    namespace_uri=_MACHINERY_NAMESPACE_URI,
    known_types=(),
    hierarchy_references=("Organizes",),
    # ``HasAddIn`` (OPC 10000-5 §11.21) is the ReferenceType Machinery uses for AddIns
    # (OPC 40001-1 §9.2, §11.2) and is a ``HasComponent`` subtype, so the generic
    # target-class-aware family classification already maps object AddIns to hierarchy
    # and Variable targets to composition; no Machinery-specific relationship rule is
    # declared. ``DataItemType`` is a Core VariableType (OPC 10000-8 §5.3.1) - not a
    # relationship type - and ``FlowsTo`` belongs to the TMC model (OPC 30060 §12.1),
    # so neither may appear as a Machinery reference alias.
    composition_references=("HasProperty",),
    graph_references=(),
    overrides=(
        # Machinery identification AddIns describe a MachineryItem's identity and are
        # defined in the Machinery namespace (OPC 40001-1 §8.3, §10.2), derived from the
        # DI FunctionalGroupType (OPC 10000-100 §4.4.1). Scoped to that namespace, the
        # identity maps to the asset role and keeps the AddIn structural plane on the
        # hierarchy. Concrete subtypes inherit the identity through their ancestry - an
        # intentionally broad rule confined to Machinery-namespace entries; types that
        # merely inherit the DI FunctionalGroupType, and the open-ended domain-specific
        # MachineryItem types (§6.3.1), are intentionally left to generic mapping.
        ProfileOverride(
            type_name="MachineryItemIdentificationType",
            namespace_uri=_MACHINERY_NAMESPACE_URI,
            semantic_role="asset",
            force_hierarchy=True,
            confidence="high",
        ),
        ProfileOverride(
            type_name="MachineryComponentIdentificationType",
            namespace_uri=_MACHINERY_NAMESPACE_URI,
            semantic_role="asset",
            force_hierarchy=True,
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


def _namespace_uri_matches(namespace_uri: str | None, canonical_uri: str) -> bool:
    """Exact namespace URI comparison, normalized by case and trailing slash.

    Never a substring match: ``http://opcfoundation.org/UA/Machinery/ProcessValues/`` is a
    distinct namespace and must not satisfy the canonical Machinery namespace.
    """
    if not namespace_uri:
        return False
    return namespace_uri.strip().rstrip("/").lower() == canonical_uri.strip().rstrip("/").lower()


def load_default_profiles() -> tuple[SemanticProfile, ...]:
    return _DEFAULT_PROFILES


def resolve_namespace_uri(node_id: str, namespace_uri_by_index: dict[int, str] | None = None) -> str | None:
    if not namespace_uri_by_index:
        return None
    prefix, _, _rest = node_id.partition(";")
    if prefix.startswith("ns="):
        try:
            namespace_index = int(prefix[3:])
        except ValueError:
            return None
    elif prefix.startswith(("i=", "s=", "g=", "b=")):
        # OPC UA NodeId defaults to namespace index 0 when omitted.
        namespace_index = 0
    else:
        return None
    return namespace_uri_by_index.get(namespace_index)


def active_profiles_for_node(
    node: OpcUaNodeInfo,
    type_definition_hierarchy: tuple[OpcUaTypeDefinitionInfo, ...] = (),
    available_profiles: tuple[SemanticProfile, ...] | None = None,
) -> ResolvedProfileSet:
    """Resolve the profiles active for a node.

    ``available_profiles=None`` uses the default profiles; an explicit empty tuple means no
    profiles are active. Matches are ordered by descending priority; equal priorities keep the
    order in which they were supplied (a stable sort), so the first profile in the tuple wins.
    """
    profiles = load_default_profiles() if available_profiles is None else available_profiles
    matched = [profile for profile in profiles if profile.matches_node(node, type_definition_hierarchy)]
    matched.sort(key=lambda profile: profile.priority, reverse=True)
    return ResolvedProfileSet(profiles=tuple(matched))


def _type_override_tokens(
    node: OpcUaNodeInfo,
    type_definition_hierarchy: tuple[OpcUaTypeDefinitionInfo, ...],
) -> set[str]:
    """Type identity used to match overrides.

    Only the node's type definition and its supertype ancestry identify a type; the instance's
    browse name and node id must not participate, otherwise an instance named like an override
    type would receive it without carrying that type in its ancestry.
    """
    tokens = {_normalize_token(node.type_definition_id)}
    tokens.update(_normalize_token(item.browse_name) for item in type_definition_hierarchy)
    return tokens


def _override_matches(
    override: ProfileOverride,
    node: OpcUaNodeInfo,
    type_definition_hierarchy: tuple[OpcUaTypeDefinitionInfo, ...],
) -> bool:
    """Match an override against type identity, optionally scoped to its namespace.

    A namespace-scoped override applies only to chain entries whose BrowseName *and*
    namespace URI both identify it, so a same-named type in an unrelated namespace - and
    mere inheritance of the generic DI ``FunctionalGroupType`` - cannot carry the override.
    """
    token = _normalize_token(override.type_name)
    if not override.namespace_uri:
        return token in _type_override_tokens(node, type_definition_hierarchy)
    return any(
        _normalize_token(type_info.browse_name) == token
        and _namespace_uri_matches(type_info.namespace_uri, override.namespace_uri)
        for type_info in type_definition_hierarchy
    )


def resolve_semantic_role(
    node: OpcUaNodeInfo,
    active_profiles: ResolvedProfileSet | None = None,
    *,
    type_definition_hierarchy: tuple[OpcUaTypeDefinitionInfo, ...] = (),
) -> SemanticRole:
    # Overrides are applied in profile priority order (descending); equal priorities keep the
    # supplied tuple order, so the first matching override wins.
    profiles = active_profiles.profiles if active_profiles else load_default_profiles()
    for profile in profiles:
        for override in profile.overrides:
            if _override_matches(override, node, type_definition_hierarchy):
                if override.semantic_role is not None:
                    return override.semantic_role

    if node.event_notifier:
        return "group"
    if node.node_class == "Variable":
        return "datapoint"
    if node.node_class == "Method":
        return "component"
    # Containers (Objects/Folders) with composition children are assets, not properties:
    # value-bearing Variables map to "datapoint" above, and "property" stays reserved
    # for profile overrides. `findElementsByRole(role="asset")` must return containers.
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
    return "medium"


def has_profile_override_for_node(
    node: OpcUaNodeInfo,
    active_profiles: ResolvedProfileSet | None = None,
    type_definition_hierarchy: tuple[OpcUaTypeDefinitionInfo, ...] = (),
) -> bool:
    profiles = active_profiles.profiles if active_profiles else load_default_profiles()
    for profile in profiles:
        for override in profile.overrides:
            if _override_matches(override, node, type_definition_hierarchy):
                return True
    return False


def forced_relationship_class_for_node(
    node: OpcUaNodeInfo,
    active_profiles: ResolvedProfileSet | None = None,
    type_definition_hierarchy: tuple[OpcUaTypeDefinitionInfo, ...] = (),
) -> Literal["hierarchy", "composition"] | None:
    # Overrides are applied in profile priority order (descending); equal priorities keep the
    # supplied tuple order, so the first override with a forced class wins.
    profiles = active_profiles.profiles if active_profiles else load_default_profiles()
    for profile in profiles:
        for override in profile.overrides:
            if not _override_matches(override, node, type_definition_hierarchy):
                continue
            if override.force_hierarchy:
                return "hierarchy"
            if override.force_composition:
                return "composition"
    return None
