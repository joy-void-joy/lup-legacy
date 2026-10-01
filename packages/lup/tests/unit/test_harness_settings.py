"""What the settings artifact carries out of a declaration.

The artifact is committed and drift-checked, so most of the rendering proves
itself. What does not is anything a value could be lost to on the way — a
refusal is falsey, and it has to reach the file as a decision rather than be
filtered out for looking like an absence.
"""

from lup.devtools.harness.settings import (
    Settings,
    credential_read_denials,
    project_settings,
)
from lup.harness.models import HookSandbox, HookSet
from lup.policy.kernel.diagnostic import step
from lup.policy.refused_paths import RefusedPaths, credential_files


def test_a_refused_plugin_is_rendered_rather_than_dropped() -> None:
    """False says something absence cannot, so it has to survive the render.

    Settings scopes resolve per key: a project entry outranks the user's, so a
    refusal here holds against a plugin that is enabled globally. Leaving the
    key out defers to whatever the user has instead — which for an editor
    language server is a second checker over the same files, contending with
    the per-edit check that is actually wired to the gates.
    """
    declared = Settings(official_plugins={"kept@vendor": True, "refused@vendor": False})

    rendered = project_settings(declared, None)

    assert rendered["enabledPlugins"] == {"kept@vendor": True, "refused@vendor": False}


def test_every_withheld_path_is_denied_to_the_file_tools_too() -> None:
    """The shell's refusal and the file tools' denial are one declaration.

    Read, Grep and Glob run in the session's own process and never reach the
    policy hook, so a path the shell withholds was readable by the file tools
    unless something else denied it -- and the list that did named two paths
    out of the set. Each anchor keeps its meaning: a home is `~/`, the root
    `//`, and anywhere `//**/`; a pattern spanning a directory names the
    directory too, as the kernel's own match does. `Read` rather than `Grep`
    because Claude Code consults file permissions against `Read` and `Edit`
    rules only.
    """
    hooks = HookSet(
        id="probe",
        policy_ids=[],
        refused_paths=[
            credential_files(
                paths=["~/.ssh/**", "~/.netrc", "/proc/*/environ"],
                also=["**/profile-home/auth.json"],
            ),
            RefusedPaths(paths=["/tmp/lup-wake/**"], reason="a", recovery=[step("b")]),
        ],
    )

    assert credential_read_denials(hooks) == [
        "Read(~/.ssh/**)",
        "Read(~/.ssh)",
        "Read(~/.netrc)",
        "Read(//proc/*/environ)",
        "Read(//**/profile-home/auth.json)",
        "Read(//tmp/lup-wake/**)",
        "Read(//tmp/lup-wake)",
    ]


def test_a_declaration_withholding_nothing_denies_nothing_extra() -> None:
    """A project that withholds no path gets no rules invented for it.

    One that says nothing inherits the library's key and login files, which
    is what the shell withholds from it too; neither needs a sandbox declared.
    """
    assert credential_read_denials(None) == []
    assert (
        credential_read_denials(
            HookSet(id="probe", policy_ids=[], refused_paths=[], sandbox=HookSandbox())
        )
        == []
    )
    assert "Read(~/.netrc)" in credential_read_denials(
        HookSet(id="probe", policy_ids=[])
    )
