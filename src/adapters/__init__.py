"""Project-side adapter patches and overrides.

Each module here patches a specific NT adapter behavior we hit in
production. Keep this directory tightly scoped — every patch should
have:

- A docstring linking to the source-truth NT file/lines being patched.
- A docs/ entry explaining the bug and a re-verification recipe for
  future NT upgrades.
- A regression test (under tests/) that pins the patch's behavior.

Current patches:

- :mod:`src.adapters.patched_sandbox` — swaps the default ``FillModel``
  for ``BestPriceFillModel`` to work around NT 1.227.0's
  SandboxExecutionClient partial-fill race. See
  ``docs/SANDBOX_PARTIAL_FILL_AUDIT.md``.
"""
