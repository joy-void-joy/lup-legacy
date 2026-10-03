"""A committed artifact names the checkout's feedback directory, never a session's.

The notes override moves where a process writes, and the fb-status prose is
rendered into the checkout by whichever process regenerates it. Rendered under
an override it would name that session's private directory, and every other
session would then read the artifact as stale. These pin that the checkout
path ignores the override and that the rendered prose uses it.
"""

import importlib
from pathlib import Path

import pytest

from lup.harness.content.skills import fb_status
from lup.providers.harness import claude_prompt_renderer
from lup.workspace import paths


@pytest.fixture(autouse=True)
def configured_paths_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand the next test on this worker the paths it would have had.

    Each case configures the process-wide path state for a throwaway root.
    Left configured, every later test on the worker resolves the project
    there: one reading the template's own declaration opens a
    `pyproject.toml` in a directory pytest has already removed, and fails or
    passes by which worker the scheduler happens to hand it to.
    """
    monkeypatch.setattr(paths.state, "config", paths.state.config)


def test_checkout_feedback_path_ignores_the_notes_override(tmp_path: Path) -> None:
    paths.configure(root=tmp_path, notes_dir=tmp_path / "elsewhere", version="0.0.0")

    assert paths.feedback_path() == tmp_path / "elsewhere" / "feedback_loop"
    assert paths.checkout_feedback_path() == tmp_path / "notes" / "feedback_loop"


def test_fb_status_names_the_checkout_directory_under_an_override(
    tmp_path: Path,
) -> None:
    """The regeneration a session runs must render what every session shares."""
    paths.configure(
        root=tmp_path, notes_dir=tmp_path / "session" / "nested", version="0.0.0"
    )

    rendered = importlib.reload(fb_status)

    assert rendered.ANALYSIS_DIRECTORY == "notes/feedback_loop"
    # Read off the rendering rather than the declaration: the directory is a
    # value the passage names, so the prose beside the module carries that
    # name and the rendered document carries the path it resolved to — which
    # is the half a session is shown, and the half this is about.
    assert "notes/feedback_loop" in claude_prompt_renderer().render(
        rendered.SKILL.prompt
    )
