"""The command reference is the composed CLI, not a list beside it.

What makes this page worth generating is the failure it removes: a
hand-written command list loses whichever commands were added most recently,
and reads exactly as it did when it was complete. So these check the walk
against the CLI this repository actually wires, rather than against a fixture
that would have to be kept current by the same hand that let the page rot.
"""

import typer

from lup.devtools.dev.commands import (
    COMMAND_REFERENCE_PATH,
    CommandEntry,
    CommandGroup,
    command_reference_artifact,
    summarized,
)
from lup_template.devtools.main import app


def served() -> list[CommandEntry]:
    return CommandEntry.served_by(app)


def test_every_wired_sub_app_reaches_the_page() -> None:
    """No group is left out, including the ones a session rarely types."""
    groups = [group.name for group in CommandGroup.over(served())]

    assert {"agent", "dev", "feedback", "git", "harness", "resolve", "setup"} <= set(
        groups
    )


def test_the_commands_a_hand_written_list_had_dropped_are_present() -> None:
    """Each of these existed while no content named it; existing is the ticket."""
    spelled = {entry.spelled() for entry in served()}

    assert {
        "dev refutations",
        "agent capabilities",
        "dev hooks sweep",
        "feedback trends",
        "feedback costs",
        "git pr-body",
    } <= spelled


def test_nested_sub_apps_are_reached_at_the_depth_a_reader_types() -> None:
    """`git pr create` is three words on the page because it is three to run."""
    spelled = {entry.spelled() for entry in served()}

    assert "git pr create" in spelled
    assert "dev library status" in spelled
    assert "dev init rename-package" in spelled


def test_a_group_that_runs_on_its_own_is_a_command_on_the_page() -> None:
    """A sub-app mounted as a callback runs when typed, subcommands or none."""
    app = typer.Typer()
    page = typer.Typer(invoke_without_command=True)
    leads = typer.Typer(no_args_is_help=True)

    @page.callback()
    def page_cmd() -> None:
        """Host the page."""

    @leads.command("only")
    def only_cmd() -> None:
        """The one thing the group leads to."""

    app.add_typer(page, name="page")
    app.add_typer(leads, name="leads")
    # The composed CLI's own instances: the ones a hand-written list had lost.
    spelled = {entry.spelled() for entry in served()}

    assert [entry.spelled() for entry in CommandEntry.served_by(app)] == [
        "page",
        "leads only",
    ]
    assert {"dev report", "setup"} <= spelled


def test_every_command_carries_a_summary() -> None:
    """A row with an empty cell is a command whose docstring never got written."""
    silent = [entry.spelled() for entry in served() if not entry.summary]

    assert silent == []


def test_a_summary_is_one_line_however_the_docstring_wraps() -> None:
    assert "\n" not in summarized(typer.main.get_command(app))


def test_the_artifact_lands_where_the_index_links_and_ends_in_one_newline() -> None:
    artifact = command_reference_artifact(app)

    assert artifact.path == COMMAND_REFERENCE_PATH
    assert artifact.semantic_id == "docs.commands"
    assert artifact.content.endswith("\n")
    assert not artifact.content.endswith("\n\n")


def test_the_walk_is_deterministic_so_the_drift_gate_means_something() -> None:
    assert (
        command_reference_artifact(app).content
        == command_reference_artifact(app).content
    )
