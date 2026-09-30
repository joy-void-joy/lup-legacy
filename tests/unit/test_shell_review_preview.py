"""The sed a rewrite is judged by runs sandboxed over the text, and keeps every byte.

What a reviewer is shown of an in-place sed is the document the policy worked
out when it judged the command, so the reading it works that out with is the
one these pin: sed's own transformation of the text, under ``--sandbox``,
bounded in time, with the modes the command spelled and every byte of the
input -- carriage returns, NULs, text outside ASCII -- carried through.
"""

from pathlib import Path

import pytest

from lup.policy.assets.host import sed_output
from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.words import sed_invocation
from lup.policy.relay import QuestionRelay
from tests.unit.test_codex_review_queue import hook


@pytest.mark.parametrize(
    ("scripts", "options", "before", "after"),
    [
        (["s/old/new/g"], [], "old\n", "new\n"),
        (["s/old/new/g"], [], "old\r\n", "new\r\n"),
        (["s/old/new/g"], [], "old", "new"),
        (["s/(o)(ld)/\\2\\1/p"], ["-nE"], "old\nunchanged\n", "ldo\n"),
        (["s/old/new/g"], ["-z"], "old\x00other\x00", "new\x00other\x00"),
        (["s/(old)/new/p"], ["-nE"], "old\r\nunchanged\r\n", "new\r\n"),
        (["s/old/new/"], [], "the old way — ünïcödé\n", "the new way — ünïcödé\n"),
        (["s/é/e/g"], [], "café — é\n", "cafe — e\n"),
        (["s/old/new/", "s/keep/kept/"], [], "old\nkeep\n", "new\nkept\n"),
    ],
)
def test_a_rewrite_keeps_its_modes_and_every_byte(
    scripts: list[str], options: list[str], before: str, after: str
) -> None:
    assert sed_output(scripts, options, before) == {"text": after, "cause": None}


@pytest.mark.parametrize(
    "script",
    [
        "e touch unexpected",
        "w unexpected",
        "r secret",
        "s/old/new/e",
        "s/old/new/w unexpected",
    ],
)
def test_the_transformation_runner_enforces_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "secret").write_text("secret value", encoding="utf-8")

    result = sed_output([script], [], "old\n")

    assert result == {"text": None, "cause": "refused"}
    assert not (tmp_path / "unexpected").exists()


def test_a_nonterminating_script_is_stopped_without_partial_evidence() -> None:
    assert sed_output([":loop; b loop"], [], "old\n", timeout=0.02) == {
        "text": None,
        "cause": "refused",
    }


@pytest.mark.parametrize("started", ["C", "POSIX", "en_US.UTF-8"])
def test_the_preview_reads_characters_whatever_locale_started_the_runtime(
    started: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What `.` matches is the locale's to say, and no runtime says the shell's.

    A runtime started under the byte locale `C` read `é` as two characters,
    so `s/./X/` left half of it behind -- a preview of a document the shell's
    UTF-8 run never writes.
    """
    monkeypatch.setenv("LC_ALL", started)
    monkeypatch.setenv("LANG", started)

    assert sed_output(["s/./X/"], [], "é\n") == {"text": "X\n", "cause": None}
    assert sed_output(["s/[[:upper:]]/u/g"], [], "ÉA\n") == {
        "text": "uu\n",
        "cause": None,
    }


def test_sed_parser_retains_modes_and_distinguishes_backup_suffix_from_flags() -> None:
    plain = sed_invocation(["sed", "-nEi", "-e", "s/old/new/p", "--", "-document.txt"])
    backup = sed_invocation(["sed", "-iE", "s/old/new/", "document.txt"])

    assert not isinstance(plain, KernelDecision)
    assert plain["options"] == ["-nE"]
    assert plain["backup"] == ""
    assert plain["targets"] == ["-document.txt"]
    assert not isinstance(backup, KernelDecision)
    assert backup["options"] == []
    assert backup["backup"] == "E"


def test_native_shell_capture_preserves_crlf_for_the_preview(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    target = tmp_path / "document.txt"
    target.write_bytes(b"old\r\n")
    command = "# lup: escalate[sandbox]: review the exact file rewrite\nsed -i 's/old/new/' document.txt"

    hook(tmp_path, command, tool="Bash")

    store = QuestionRelay(tmp_path / ".lup/questions.jsonl")
    (entry,) = store.pending()
    assert store.resolve(entry).preconditions[target] == "old\r\n"
    assert target.read_bytes() == b"old\r\n"
