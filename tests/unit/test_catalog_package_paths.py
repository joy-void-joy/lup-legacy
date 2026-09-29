"""The catalog's paths into its own package move when the package does.

`dev init rename-package` rewrites imports and dotted module paths, never a
path spelled in a string — so a seam declared at
`Path("src/lup_template/...")` went on naming the old package after the
rename, and `dev seams` then read a file that was gone. Spelled through the
layout, the paths are derived from where the package sits and a rename has
nothing to rewrite.
"""

import pytest

import lup_template.harness.catalog as catalog
from lup.devtools.dev.seams import survey
from lup.harness.content.application import ApplicationLayout


def test_every_path_into_the_package_is_spelled_through_its_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built as a project with another name, every such path follows it.

    A literal reads the same as a derivation under this repository's own
    name, so only a catalog resolved as somebody else can tell them apart.
    """
    monkeypatch.setattr(catalog, "LAYOUT", ApplicationLayout(package="adopter"))

    modules = [seam.module for seam in catalog.dev_project().seams if seam.module]
    protected = catalog.portable_harness().declared_hooks.protected_edit_roots

    assert [module.as_posix() for module in modules] == [
        "src/adopter/harness/content/catalog.py",
        "src/adopter/harness/content/image.py",
    ]
    assert "src/adopter/harness/catalog.py" in [path.as_posix() for path in protected]


def test_dev_seams_reads_every_seam_this_catalog_declares() -> None:
    """Each module a seam names is the file holding it, here where it sits."""
    project = catalog.dev_project()

    assert not any(
        "unreadable" in line for line in survey(project.catalog, project.seams)
    )
