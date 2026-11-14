# AGENTS.md

## Purpose

This repository follows a strict AI-assisted, Test-Driven Development (TDD) workflow.

The objective is to ensure all changes are:

- Correct
- Tested
- Maintainable
- Secure
- Consistent with existing architecture
- Small and reviewable

Agents must understand before changing and validate before completing.

Skipping phases is not permitted.

Changing this File is not permitted.

---

# Core Development Loop

```text
REVIEW
  ↓
ANALYZE
  ↓
PLAN
  ↓
TEST
  ↓
VERIFY TESTS FAIL
  ↓
IMPLEMENT
  ↓
VERIFY
  ↓
SELF CRITIQUE
  ↓
REVIEW
  ↓
COMMIT
  ↓
REPEAT
```

---

# Engineering Principles

## Primary Principle

First make it correct.

Then make it clear.

Then make it fast.

Never reverse the order.

## Design Principles

Prefer:

- Simplicity over flexibility
- Consistency over cleverness
- Existing patterns over new abstractions
- Small changes over large rewrites
- Explicit behavior over magic
- Readability over compactness

---

# Agent Behavior Rules

## Always

- Read existing code before modifying it.
- Understand architectural intent.
- Follow existing coding conventions.
- Analyze related code before proposing changes.
- Keep modifications minimal and focused.
- Add or update tests when behavior changes.
- Explain important architectural decisions.
- Perform a critical review of your own work.

## Never

- Implement before analysis.
- Skip tests.
- Change unrelated files.
- Silence failing tests.
- Introduce speculative features.
- Introduce speculative abstractions.
- Perform large refactors unless explicitly requested.
- Replace working code without justification.
- Continue optimizing after requirements are satisfied.

---

# Context Management

Local LLMs have limited context windows and may lose architectural understanding.

Before implementation:

- Read only relevant files.
- Identify dependencies.
- Summarize findings.
- Build a working understanding of affected areas.

If context becomes large:

1. Summarize the current state.
2. Summarize completed work.
3. Summarize remaining tasks.
4. Continue using the summary.

Avoid repeatedly re-analyzing the same files.

---

# Architecture Protection Rules

Respect existing architecture.

Do not introduce:

- New frameworks
- New ORMs
- New persistence layers
- New messaging systems
- Dependency injection frameworks
- Plugin systems
- Event buses
- Microservices

Unless explicitly requested.

Architectural consistency is preferred over theoretical improvements.

---

# Edge and Industrial Deployment Constraints

Assume deployment may occur on:

- Raspberry Pi
- Revolution Pi
- Industrial IPCs
- Resource-constrained edge environments

Optimize for:

- Reliability
- Maintainability
- Low memory usage
- Low CPU usage
- Low storage consumption
- Deterministic behavior
- Graceful failure handling
- Observability

Avoid unnecessary dependencies.

---

# Phase 1: REVIEW

## Goal

Understand the current implementation.

Analyze:

- Repository structure
- Architecture
- Coding standards
- Existing tests
- Dependency relationships
- Deployment architecture
- Recent modifications

Questions:

- What already exists?
- How is similar functionality implemented?
- Which patterns are already used?
- Which architectural constraints exist?
- Which components are affected?

Deliverable:

```markdown
## Review Summary

Architecture:
...

Relevant Components:
...

Existing Patterns:
...

Risks:
...
```

No code generation is allowed during this phase.

---

# Phase 2: ANALYZE

## Goal

Understand the requested change.

Identify:

- Functional requirements
- Non-functional requirements
- Edge cases
- Failure modes
- Security implications
- Performance implications
- Backward compatibility concerns
- Deployment implications

Deliverable:

```markdown
## Analysis

Requirements:
...

Edge Cases:
...

Failure Modes:
...

Risks:
...
```

No implementation is allowed during this phase.

---

# Phase 3: PLAN

## Goal

Create an implementation strategy.

Produce:

1. Components to modify
2. Tests to create
3. Implementation order
4. Validation approach
5. Rollback considerations

Deliverable:

```markdown
## Implementation Plan

Step 1:
...

Step 2:
...

Step 3:
...

Validation:
...
```

Large tasks must be split into small independent steps.

---

# Phase 4: TEST

## Goal

Define expected behavior before implementation.

Create tests covering:

### Happy Path

Expected successful behavior.

### Edge Cases

Boundary conditions and unusual inputs.

### Error Handling

Failures and invalid usage.

### Regression Protection

Prevent previously fixed issues from returning.

Rules:

- Tests define behavior.
- Tests must fail initially.
- Production code must not be modified during this phase.

Deliverable:

```text
Tests added.

Expected result:
FAILING
```

---

# Phase 5: VERIFY TEST FAILURE

## Goal

Prove the tests are meaningful.

Run applicable test commands.

Examples:

```bash
pytest
```

```bash
npm test
```

Expected result:

```text
FAIL
```

If tests pass immediately:

- Re-evaluate test quality.
- Confirm new behavior is not already implemented.
- Confirm assertions are meaningful.

Do not continue until tests fail for the expected reason.

---

# Phase 6: IMPLEMENT

## Goal

Make tests pass with the smallest possible change.

Rules:

- Implement only required behavior.
- Avoid overengineering.
- Follow existing patterns.
- Reuse existing components.
- Keep changes small.

Preferred order:

1. Smallest solution
2. Clearest solution
3. Most maintainable solution
4. Performance optimization only if required

Deliverable:

```text
All newly added tests pass.
```

---

# Phase 7: VERIFY

## Goal

Validate the entire repository.

Run all applicable validation steps.

## Python

```bash
pytest
ruff check .
mypy .
```

## TypeScript

```bash
npm test
eslint .
tsc --noEmit
```

## Generic

```bash
build
test
lint
typecheck
```

Verify:

- Tests pass
- Lint passes
- Type checks pass
- Build succeeds
- No regressions exist

---

# Phase 8: SELF CRITIQUE

## Goal

Assume the implementation is wrong.

Attempt to identify:

- Hidden bugs
- Missed edge cases
- Missing tests
- Race conditions
- Resource leaks
- Security issues
- Architectural violations
- Unnecessary complexity
- Performance bottlenecks

Deliverable:

```markdown
## Self Critique

Potential Issues:
...

Additional Test Opportunities:
...

Decision:
Proceed / Rework
```

---

# Phase 9: REVIEW

## Goal

Perform a senior-level engineering review.

### Correctness

- Requirements satisfied
- Edge cases covered
- Failure paths covered

### Architecture

- Fits existing patterns
- Acceptable coupling
- Proper responsibility boundaries

### Maintainability

- Readable
- Testable
- Justified complexity

### Security

Review for:

- Command injection
- SQL injection
- Path traversal
- Authentication bypass
- Authorization issues
- Secret leakage
- XSS
- CSRF
- SSRF
- Unsafe deserialization
- Data exposure

### Performance

Review:

- Algorithmic complexity
- Database access
- Memory usage
- Network access
- Resource usage

Deliverable:

```markdown
## Review Findings

Approved:
...

Issues:
...

Security Review:
Completed

Architecture Review:
Completed
```

Fix findings before proceeding.

---

# Phase 10: COMMIT

Commits are allowed only if:

- Requirements satisfied
- Tests added
- Tests pass
- Lint passes
- Type checks pass
- Review completed
- Security review completed
- Architecture review completed

Commit style:

```text
feat(scope): concise description
fix(scope): concise description
refactor(scope): concise description
test(scope): concise description
docs(scope): concise description
chore(scope): concise description
```

Example:

```text
feat(mqtt): add reconnect state machine

- add retry policy
- add reconnection metrics
- add integration tests
```

---

# Commit Gate

Before creating a commit verify:

```text
[ ] Requirements satisfied
[ ] Tests added
[ ] Tests passing
[ ] Lint passing
[ ] Type checks passing
[ ] Security review completed
[ ] Architecture review completed
[ ] Self critique completed
```

If any item fails:

```text
DO NOT COMMIT
```

---

# Pull Request Requirements

Every pull request must contain:

## Summary

What changed?

## Motivation

Why was the change needed?

## Testing

Which tests were added or modified?

## Security Impact

Any security implications?

## Risks

Potential migration or compatibility risks.

## Rollback Plan

How can the change be reverted if necessary?

---

# Preferred Agent Commands

## Review

```text
Review the repository.

Focus on:
- architecture
- maintainability
- testing strategy
- code quality

Do not generate code.
```

## Analyze

```text
Analyze the requested change.

Identify:
- affected components
- edge cases
- security implications
- performance impact

Do not implement.
```

## Plan

```text
Create a detailed implementation plan.

Keep changes minimal and aligned with existing architecture.
```

## Test

```text
Write failing tests first.

Cover:
- expected behavior
- edge cases
- error handling
- regressions

Do not implement functionality.
```

## Implement

```text
Implement the minimum code necessary
to satisfy the tests.

Avoid overengineering.
```

## Review

```text
Perform a senior engineer review.

Focus on:
- correctness
- maintainability
- architecture
- security
- performance
```

---

# Stop Criteria

Stop immediately when:

- Requirements are satisfied
- Tests pass
- Validation passes
- Review passes
- No additional meaningful value is created

Do not continue refactoring solely because improvements are possible.

A finished, correct solution is preferred over endless optimization.

---

# Success Criteria

A change is complete only when:

- Requirements are satisfied
- Tests exist
- Tests pass
- Lint passes
- Type checks pass
- Self critique completed
- Security review completed
- Architecture review completed
- Review completed
- Commit is ready

Review without tests is incomplete.

Tests without review are incomplete.

Implementation without analysis is incomplete.

Implementation without a plan is incomplete.