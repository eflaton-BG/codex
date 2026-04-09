---
name: pytest-tests
description: Write or rewrite Python unit and integration tests with pytest. Use when Codex needs to add focused regression coverage, improve an existing pytest test for clarity or robustness, validate diagnostic behavior, choose between parametrized and split-out test shapes, or align new tests with local repository pytest conventions.
---

# Pytest Tests

Use this skill to write the smallest test that proves the behavior under change.

## Workflow

1. Read the production code and the nearest existing tests before writing anything.
2. Test observable behavior first. Prefer public return values, emitted state, exceptions, and rendered diagnostics over implementation details.
3. Keep one behavior per test. If several inputs exercise the same behavior, use `@pytest.mark.parametrize`.
4. Use descriptive test names that state the expected behavior, not the implementation mechanism.
5. Structure each test with clear `ARRANGE`, `ACT`, and `ASSERT` sections when the local file style permits it.
6. Add a short docstring to each unit test explaining the behavior it is intended to cover.
7. Keep setup minimal. Reuse existing fixtures when they remove noise; avoid pulling in broad fixtures for small logic tests.
8. Prefer explicit assertions over indirect coverage. If the bug is about `repr`, assert the exact `repr(...)` output.
9. Cover both the normal path and the failure-tolerant path when diagnostics must not crash.
10. Run the narrowest relevant pytest command first, then widen only if needed.

## Patterns

### Parametrize one behavior

Use parametrization when each case should satisfy the same assertion shape. Add readable case values or `ids=` when that improves failure output.

### Arrange, Act, Assert

Use `ARRANGE`, `ACT`, and `ASSERT` comments to separate setup, the behavior under test, and the verification step. Keep them concise and skip only when the surrounding file has a clearly different established style.

### Test docstrings

Add a brief docstring to each unit test describing the intended coverage. State the behavior being protected, not the implementation detail.

### Split unrelated behaviors

Do not force unrelated expectations into one parametrized table. If one case is about formatting and another is about raising, use separate tests.

### Diagnostic safety

When a ticket is about logging, reprs, or debugging output, assert that the output is readable and that malformed inputs do not crash the diagnostic path.

### Minimal mocking

Mock only the boundary you need. Prefer real value objects and lightweight fixtures over broad patching for pure state or formatting tests.

## Repo Conventions

- Follow local test style in the nearest existing file unless there is a clear reason not to.
- Use pytest for unit tests and run focused validation from the repo root.
- In this workspace, prefer containerized pytest commands when host pytest or imports are unreliable.
- Avoid adding sleeps or timeout increases when testing asynchronous behavior; prefer explicit events, futures, or notifications.

## Validation

- Start with the most focused test file or node that covers the change.
- If imports resolve to stale installed code, prepend the repo `src` paths in `PYTHONPATH`.
- If helper packages are needed, add their `src` roots too rather than weakening the test.

## Output

When you update tests, report:

- what behavior the test now covers
- the focused pytest command you ran
- whether the result passed or what failed
