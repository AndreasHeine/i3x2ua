# Semantic Profile Model-Building Review — 2026-11-17

## Scope and method

This review applies `misc/prompts/review.md` only to semantic-profile selection and its
direct model-building integrations: `semantic_profiles.py`, `build_phases.py`,
`reference_graph.py`, `mapper.py`, and `node_mapping.py`. It does not assess the rest
of the repository, its dependencies, deployment, or project-wide security posture.
Findings below are based on the checked-out source and focused tests.

Verified:

| Check | Result |
|---|---|
| `uv run pytest tests\test_mapper.py tests\test_model_build_phases.py -q` | 35 passed |
| Source worktree before adding this review | Clean |

## 1. Executive summary

| Area | Score | Scoped assessment |
|---|---:|---|
| Architecture | 7/10 | Profile resolution, graph classification, and node mapping have distinct responsibilities, but profile rules are partly coupled to generic reference heuristics. |
| Code quality | 7/10 | Immutable typed profile data and explicit helpers are clear; rule fields do not always directly determine their stated result. |
| Maintainability | 7/10 | The model is small and understandable. Adding profiles safely requires understanding rule matching, reference classification, and override matching across modules. |
| Performance | 8/10 | The default set is small and work is linear in nodes, profiles, and type ancestry; no present bottleneck is evident. |
| Security | 8/10 | The reviewed implementation uses static profile data and performs no I/O, execution, or deserialization. This is not a project-wide security score. |
| Testing | 6/10 | Selection and basic classification have focused coverage, but custom reference aliases and override collision cases are untested. |
| Operations | N/A | Operational readiness is outside this model-building scope. |

**Scoped maturity: 7/10.** The profile mechanism is compact, typed, and integrated into
the build pipeline. Its main risk is correctness at the extension boundary: profile
reference declarations can select a branch without actually dictating the resulting
relationship class, and override matching can treat an instance name as a type name.

## 2. Architecture and data flow

`build_active_profiles` resolves each node's namespace URI and type-definition ancestry,
then selects profiles (`build_phases.py:158-180`). `active_profiles_for_node` sorts
matches by descending priority (`semantic_profiles.py:140-148`). The graph phase uses
those profiles during reference classification and applies target-node overrides
(`reference_graph.py:160-180`). The mapping phase derives semantic roles and confidence
and records applied profile IDs (`node_mapping.py:82-107`).

This is a sensible division between input resolution, relationship construction, and
node mapping. The principal design weakness is that profile matching, profile rule
interpretation, and override matching use different notions of a "type match"; this
makes the resulting semantics harder to reason about and extend consistently.

## 3. Findings

### Medium — Profile-declared composition aliases can be classified as hierarchy

`mapper.py:103-124` checks whether any profile hierarchy/composition token intersects
the reference lineage, but then delegates both cases to `_classify_hierarchical_family`.
That helper recognizes only its built-in `HasProperty`/`PropertyOf` and
`HasComponent`/`HasOrderedComponent` tokens; it does not receive which profile rule
matched. An otherwise-unrecognized custom composition reference therefore falls
through to the helper's `"hierarchy"` default, including when a profile explicitly
declares that reference in `composition_references`. The machinery profile itself lists
`DataItemType` as a composition reference (`semantic_profiles.py:77-84`), which is not
one of the built-in family tokens.

**Impact:** a profile's declared composition relationship may be emitted as hierarchy,
changing parent selection and model structure while still receiving high confidence.
**Recommendation:** preserve the matched profile category through classification (or
directly classify custom composition tokens), and add tests for custom hierarchy,
composition, and graph aliases, including a target variable and object.
**Effort:** S.

### Medium — Overrides can match an instance's name or node ID as though it were a type

Role and relationship override lookup builds its match tokens from the type-definition
hierarchy, but also from `node.browse_name` and `node.node_id`
(`semantic_profiles.py:156-168`, `200-216`, `219-241`). The latter identify the instance,
not necessarily its type. If an instance happens to be named like an override type, it
can receive that override despite having no such type in its ancestry.

**Impact:** profile-specific semantic roles or forced relationships can be applied to
unrelated nodes based on a naming collision. The risk is most direct for custom profiles
whose type names overlap common instance names.
**Recommendation:** match overrides against type-definition identifiers and ancestry;
retain name fallback only when the name is known to represent a type rather than the
instance's browse name.
**Effort:** S.

### Low — An explicit empty profile collection silently means "use defaults"

`active_profiles_for_node` uses `available_profiles or load_default_profiles()`
(`semantic_profiles.py:140-148`). Thus an explicit empty tuple cannot request that no
profiles be active; it silently activates the defaults. This is ambiguous for callers
and makes the optional argument's two states less explicit.

**Impact:** a future caller attempting to disable profiles with `()` could unknowingly
retain default classification behavior.
**Recommendation:** distinguish `None` (use defaults) from an empty tuple (use none), or
document and test that empty means defaults if this is intentional.
**Effort:** S.

## 4. Python engineering and maintainability

- `SemanticProfile` and `ProfileOverride` are frozen, slotted dataclasses with useful
  literal types (`semantic_profiles.py:12-37`); this keeps configuration immutable and
  constrains recognized roles and confidence labels.
- Profile priorities and tie behavior are deterministic for the supplied tuple order,
  though equal-priority precedence is not documented or tested
  (`semantic_profiles.py:140-148`).
- `_normalize_token` is duplicated in `semantic_profiles.py` and `mapper.py`. The
  implementations currently agree, but future changes could make type matching and
  reference matching inconsistent (`semantic_profiles.py:101-118`,
  `mapper.py:86-101`).
- The module's size and function boundaries are reasonable. The two override scans
  repeat token construction and matching logic; consolidating them could reduce
  divergence, but should follow—not precede—clarifying the intended match semantics.
- Growth is approximately proportional to nodes × profiles × type ancestors during
  profile selection, plus references × profiles during classification. With two built-in
  profiles, this is not a current performance concern. If profiles become numerous,
  pre-normalized profile indexes would avoid repeated normalization.

## 5. Reliability, security, and scalability

The matcher relies on resolved type-definition ancestry for non-generic profiles
(`semantic_profiles.py:39-58`). When hierarchy resolution fails, the build phase passes
an empty ancestry (`build_phases.py:164-177`), so only the generic profile applies.
This is a predictable degradation path, but it means profile-specific semantics are
lost without a direct failure signal in the profile result. Consider exposing that
resolution limitation in diagnostics if profile semantics are operationally important.

No direct security vulnerability was identified in the reviewed scope: profiles are
static Python constants, and matching performs string operations only. The principal
risk is semantic integrity rather than code execution or data exposure. No measured
scalability concern was found for the current two-profile configuration.

## 6. Testing assessment

The focused tests verify generic reference classification, machinery profile selection
through a type ancestor, rejection of a same-named type from another namespace, and
basic semantic-role defaults (`tests/test_mapper.py:108-257`). The model-build phase
tests also exercise resolution behavior.

The focused command passed: **35 tests**. Coverage from this run reports
`semantic_profiles.py` at 80% line coverage, but that figure includes only the selected
test files and should not be read as total-suite coverage.

Missing high-value cases:

1. A custom profile composition reference without built-in `HasComponent`/`HasProperty`
   lineage must classify as composition.
2. A profile override must not match solely because an instance browse name or node ID
   equals the override's type name.
3. Explicit `available_profiles=()` behavior should be pinned down.
4. Equal-priority profile ordering and conflicting overrides should have defined,
   tested precedence.

## 7. Scoped technical-debt backlog

| Priority | Area | Description | Impact | Effort |
|---|---|---|---|---|
| Medium | Classification | Honor the category of a custom profile reference rule rather than only using it as a match gate. | Incorrect model hierarchy/composition | S |
| Medium | Overrides | Restrict override matches to type information rather than instance identifiers/names. | Incorrect roles and parent classification | S |
| Low | API semantics | Define the behavior of an explicit empty profile tuple. | Potentially surprising default activation | S |
| Low | Maintainability | Share token normalization and override matching after semantics are clarified. | Prevent drift between helpers | S |

## 8. Final verdict

**Architecture decision:** the semantic-profile portion is healthy and does not require
redesign, but needs moderate targeted correction before relying on custom profiles for
non-standard reference types.

**Strengths**
- Clear profile data model and separation across build phases.
- Namespace-aware type-ancestor matching avoids matching a same-named type in an
  unrelated namespace.
- Focused tests cover key built-in matching and classification paths.

**Weaknesses**
- Custom reference categories do not necessarily control the resulting classification.
- Instance names and IDs participate in type-override matching.
- Empty-profile semantics and conflict precedence are underspecified.

**Top recommendations**
1. Fix and test custom composition-reference classification.
2. Match semantic overrides only against validated type identity/ancestry.
3. Clarify and test empty-profile and equal-priority precedence behavior.
4. Consolidate duplicated normalization/matching only after the above rules are settled.
