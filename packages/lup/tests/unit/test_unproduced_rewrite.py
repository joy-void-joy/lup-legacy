"""A rewrite nothing could produce says which of the reasons stopped it.

One sentence stood for five: the path names no file, it names something a
rewrite cannot replace, sed would not run the script, what came back is not
text, and nothing looked at all. A writer who mistyped a path and one who
aimed `-i` at a directory were told the same thing, and handed the one
recovery that fits neither — make the change as an edit instead, which
answers only the case where the document exists and could not be judged.
"""

from pathlib import Path

from lup.policy.kernel.rows import UnproducedDocumentRow, unproduced_cause
from lup.policy.kernel.shell import decide_shell
from lup.policy.models import ShellCommand
from lup.policy.rules import ShellPolicy
from lup.policy.shell_rules import erase_shell_rules
from lup.policy.vocabulary import default_vocabulary

REWRITE = "sed -i 's/a/b/' notes.md"


def reason(cause: str | None) -> str:
    """What the classifier says about a rewrite the host reported this about."""
    return decide_shell(
        REWRITE,
        erase_shell_rules(default_vocabulary()),
        unproduced_documents=[]
        if cause is None
        else [UnproducedDocumentRow(target="notes.md", cause=unproduced_cause(cause))],
    ).reason


def test_each_cause_says_what_it_is() -> None:
    """Five readings, five answers, because each sends the writer elsewhere."""
    assert "no file stands there" in reason("missing")
    assert "not a regular file" in reason("irregular")
    assert "sed itself would not run the script" in reason("refused")
    assert "reads as text" in reason("unreadable")
    assert "writes there by running" in reason("run")


def test_a_target_nothing_looked_at_still_says_so() -> None:
    """The silence a composition leaves is not one of the four, and stays a question."""
    assert "nothing read what it would leave behind" in reason(None)


def test_an_unknown_reading_is_the_least_specific_of_them() -> None:
    """A word this does not know must not crash a hook, which would grant."""
    assert unproduced_cause("something-else") == "unreadable"
    assert unproduced_cause(None) == "unreadable"
    assert unproduced_cause("missing") == "missing"


def test_the_host_reaches_each_of_them_over_a_real_tree(tmp_path: Path) -> None:
    """The reading and the wording join, over files rather than over rows."""
    (tmp_path / "notes.md").write_text("a\n", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    policy = ShellPolicy(default_vocabulary())

    def said(command: str) -> str:
        return policy.decide(ShellCommand(command=command, cwd=tmp_path)).reason

    assert "no file stands there" in said("sed -i 's/a/b/' absent.md")
    assert "not a regular file" in said("sed -i 's/a/b/' folder")
    assert "not a regular file" in said("sed -i 's/a/b/' /dev/null")
    assert "writes there by running" in said(
        "uv run gen > notes.md && sed -i 's/a/b/' notes.md"
    )
    # The file that is there reaches the edit gates instead, which is the
    # whole point of separating "could not be produced" from "was produced".
    assert "no file stands there" not in said("sed -i 's/a/b/' notes.md")
