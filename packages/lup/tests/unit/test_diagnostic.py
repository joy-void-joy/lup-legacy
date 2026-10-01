"""One message shape: the verdict line, a line per way through, the page."""

from lup.policy.kernel.diagnostic import (
    devtools,
    diagnostic,
    rendered,
    spelled,
    step,
)


def test_a_refusal_reads_verdict_then_ways_through_then_page() -> None:
    said = diagnostic(
        "refused",
        "changes packages outside this project's lockfile",
        what="pip install",
        steps=[
            step(run=["uv", "add", "requests"]),
            step("or resubmit it with a first line `# lup: escalate[decision]: <why>`"),
        ],
        rule="pip",
        see="docs/permissions.md",
    )

    assert rendered(said) == (
        "refused: `pip install` — changes packages outside this project's lockfile\n"
        "→ `uv add requests`\n"
        "→ or resubmit it with a first line `# lup: escalate[decision]: <why>`\n"
        "see docs/permissions.md"
    )


def test_a_step_says_what_to_do_before_the_command_that_does_it() -> None:
    said = diagnostic(
        "error",
        "no session answers to it",
        what="foo",
        steps=[step("see who is here", devtools("coordination", "roster"))],
    )

    assert rendered(said) == (
        "error: `foo` — no session answers to it\n"
        "→ see who is here: `uv run lup-devtools coordination roster`"
    )


def test_a_diagnostic_with_no_operative_words_opens_on_why() -> None:
    assert rendered(diagnostic("warning", "nothing to do")) == "warning: nothing to do"


def test_a_word_is_quoted_only_where_a_shell_needs_it() -> None:
    assert spelled(["uv", "run", "python", "tmp/<name>.py"]) == (
        "uv run python tmp/<name>.py"
    )
    assert spelled(["git", "commit", "-m", "it's done"]) == (
        "git commit -m 'it'\\''s done'"
    )
    assert spelled(["echo", "$HOME", "~/x", ""]) == "echo $HOME ~/x ''"


def test_a_placeholder_the_reader_replaces_whole_is_never_quoted() -> None:
    assert spelled(["uv", "run", "lup-devtools", "<the same words>"]) == (
        "uv run lup-devtools <the same words>"
    )
    assert spelled(["echo", "<a>b"]) == "echo <a>b"


def test_a_devtools_command_is_spelled_through_uv_run() -> None:
    assert devtools("harness", "generate", "all") == [
        "uv",
        "run",
        "lup-devtools",
        "harness",
        "generate",
        "all",
    ]
