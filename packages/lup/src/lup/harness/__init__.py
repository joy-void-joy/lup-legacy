"""Harness generation: canonical agent declarations compiled to native plugins.

The application declares one provider-neutral :class:`~lup.harness.models.Harness`
(plugins, skills, agents, hooks, guidance); this package turns it into native
plugin files on disk without ever touching content it cannot prove it
generated. Modules map to concerns:

- :mod:`lup.harness.models` — the genuinely shared vocabulary: the canonical
  declaration graph and the rendered ``Artifact``/``ArtifactTree``.
- :mod:`lup.harness.contracts` — the neutral capability seams neutral
  composition code holds; adapters and the concern modules implement them.
- :mod:`lup.harness.generation` — small deterministic helpers shared by the
  adapter renderers, the compilation roots, and reconciliation.
- :mod:`lup.harness.validation` — whole-tree validation of a rendered tree.
- :mod:`lup.harness.ownership` — the ownership proof (manifest) recording
  which on-disk files the generator owns.
- :mod:`lup.harness.reconciliation` — classifies the current tree under that
  proof and proposes writes, proven deletions, and conflicts.
- :mod:`lup.harness.materialization` — atomically applies a conflict-free
  proposal.
- :mod:`lup.harness.proposals` — persists backpropagation source patches for
  review instead of applying them.
- :mod:`lup.harness.environment` — the non-interactive shell defaults merged
  into every agent-spawned command so credential prompts fail fast.

Dependencies point one way: everything may import ``models``; the concern
modules implement seams from ``contracts`` (whose own imports are type-only);
``reconciliation`` builds on ``ownership``; ``materialization`` and
``proposals`` build on ``reconciliation``. Nothing here imports an adapter.

Placement rule: a model with one managing module lives in that module
(proposal rows in ``reconciliation``, ...); only
vocabulary shared across stages lives in ``models``. File position itself
documents ownership.
"""
