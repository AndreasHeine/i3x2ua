# FIXME: Semantic Profile Model-Building

Source review: [SEMANTIC_PROFILES_REVIEW_2026-11-17.md](../reviews/SEMANTIC_PROFILES_REVIEW_2026-11-17.md)

Resolve these scoped semantic-profile correctness and contract issues. Follow the
repository's test-first workflow: add focused regression tests, verify they fail for the
expected reason, implement the smallest fix, then rerun the focused suite.

## Tasks

- [x] Make a matched profile reference rule determine the declared relationship class.
  Custom composition aliases must not fall through to hierarchy classification. Add
  focused tests for custom hierarchy, composition, and graph aliases, including variable
  and object targets.
- [x] Restrict semantic overrides to type-definition identity and ancestry. An instance
  browse name or node ID alone must not trigger an override. Add collision regression
  tests for semantic-role and forced-relationship overrides.
- [x] Define and test explicit empty-profile behavior: distinguish `None` (default
  profiles) from an empty `available_profiles` tuple (no profiles), or document and test
  another intentional contract.
- [x] Document and test precedence for equal-priority profiles and conflicting
  overrides.

## Verification

- [x] Verify each regression test fails before its fix and passes afterward.
- [x] Run `uv run pytest tests\test_mapper.py tests\test_model_build_phases.py -q`.
- [x] Review the final diff to ensure only semantic-profile behavior, its tests, and this
  FIXME are changed.
