"""A runtime's own tree is spelled by its adapter, and read off it everywhere else.

`.claude` and `.codex` lived in the provider-neutral catalog, the ownership
reader's defaults, the generator's per-runtime recipes, the policy kernel's
generated-plugin refusal and a handful of devtools, each spelled by hand. A
third runtime would have meant a sweep through every one of them. Now each
adapter states its tree, its plugin directory and each file in it, and the
library asks every supported runtime for them.
"""

from pathlib import Path

from lup.harness.models import ArtifactTree
from lup.harness.ownership import build_manifest, save_manifest
from lup.providers.harness import (
    AdapterName,
    every_runtime,
    runtime_generated,
    runtime_plugin_directories,
    spellings_of,
)
from lup_template.harness.catalog import declared_hook_set, portable_harness


def test_every_runtime_is_asked_of_its_adapter_in_one_order() -> None:
    runtimes = every_runtime()

    assert [runtime.runtime_name for runtime in runtimes] == [
        spellings_of(adapter).runtime_name for adapter in AdapterName
    ]
    assert [runtime.tree("tree_root") for runtime in runtimes] == [
        ".claude/",
        ".codex/",
    ]


def test_each_plugin_sits_in_the_directory_its_runtime_names() -> None:
    for runtime in every_runtime():
        plugin = Path(runtime.plugin("lup", "root", None))

        assert plugin.parent == Path(runtime.plugins_directory)
        assert plugin.is_relative_to(Path(runtime.tree("tree_root")))


def test_the_hook_set_refuses_every_runtimes_generated_plugins() -> None:
    """Both directories, whichever runtime a session runs, from the adapters."""
    assert declared_hook_set().generated_plugin_roots == runtime_plugin_directories()
    assert runtime_plugin_directories() == [
        Path(".claude/plugins"),
        Path(".codex/plugins"),
    ]


def test_ownership_is_read_where_each_runtime_keeps_its_proof(tmp_path: Path) -> None:
    for runtime in every_runtime():
        save_manifest(
            tmp_path / runtime.tree("ownership_manifest"),
            build_manifest(
                portable_harness(),
                ArtifactTree(artifacts=[]),
                generator_version="test",
                target_requirements=[],
            ),
        )

    owned = runtime_generated(tmp_path)

    for runtime in every_runtime():
        assert owned.owning(runtime.tree("ownership_manifest")) is not None
