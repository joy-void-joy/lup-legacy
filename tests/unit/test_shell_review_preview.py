"""Shell reviews derive every displayed byte from the captured operation and input."""

from pathlib import Path

import pytest

from lup.devtools.dev.questions import ReviewDetail
from lup.policy.assets.host import rewritten_text, sed_output
from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.words import sed_invocation
from lup.policy.operations import Operation
from lup.policy.relay import PersistentQuestion
from lup.policy.review import reviewed_preview
from lup.policy.relay import QuestionRelay
from tests.unit.test_codex_review_queue import hook


def requested(root: Path, command: str, before: str = "old\n") -> PersistentQuestion:
    operation = Operation(
        id="sed-operation",
        session="requester",
        requester="requester",
        tool="Bash",
        payload={"command": command},
        cwd=root,
        worktree=root,
    )
    return PersistentQuestion(
        id="sed-question",
        operation=operation,
        fingerprint=operation.fingerprint(),
        preconditions={root / "document.txt": before},
        reason="Review this exact rewrite.",
        eligible=["operator"],
    )


@pytest.mark.parametrize(
    ("command", "before", "after"),
    [
        ("sed -i 's/old/new/g' document.txt", "old\n", "new\n"),
        ("sed -i 's/old/new/g' document.txt", "old\r\n", "new\r\n"),
        ("sed -i 's/old/new/g' document.txt", "old", "new"),
        ("sed -nEi 's/(o)(ld)/\\2\\1/p' document.txt", "old\nunchanged\n", "ldo\n"),
        ("sed -zi 's/old/new/g' document.txt", "old\x00other\x00", "new\x00other\x00"),
        (
            "sed --in-place --quiet --regexp-extended -e 's/(old)/\\1new/p' -- document.txt",
            "old\nother\n",
            "oldnew\n",
        ),
    ],
)
def test_sed_diff_uses_only_captured_input_and_preserves_modes_and_bytes(
    tmp_path: Path, command: str, before: str, after: str
) -> None:
    entry = requested(tmp_path, command, before)
    target = tmp_path / "document.txt"
    target.write_text("an intervening change", encoding="utf-8")

    preview = reviewed_preview(entry)

    assert preview.unavailable == ""
    assert len(preview.files) == 1
    assert preview.files[0].before == before
    assert preview.files[0].after == after
    assert target.read_text(encoding="utf-8") == "an intervening change"
    detail = ReviewDetail.of(tmp_path, entry, "operator")
    assert detail.command == command
    assert detail.preview_unavailable == ""
    assert "inbox environment" in detail.preview_notice
    assert "captured input" in detail.preview_notice
    assert detail.stale_reason


def test_each_named_target_is_transformed_from_its_own_captured_document(
    tmp_path: Path,
) -> None:
    entry = requested(tmp_path, "sed -i 's/old/new/g' document.txt second.txt")
    entry = entry.model_copy(
        update={
            "preconditions": {
                tmp_path / "document.txt": "old first\n",
                tmp_path / "second.txt": "old second\n",
            }
        }
    )

    preview = reviewed_preview(entry)

    assert preview.unavailable == ""
    assert [file.after for file in preview.files] == ["new first\n", "new second\n"]
    assert not (tmp_path / "document.txt").exists()
    assert not (tmp_path / "second.txt").exists()


@pytest.mark.parametrize(
    "command",
    [
        "sed -i 'e touch unexpected' document.txt",
        "sed --sandbox -i 'e touch unexpected' document.txt",
        "sed -i 's/old/new/e' document.txt",
        "sed -i 's/old/new/w unexpected' document.txt",
        "sed -i 'w unexpected' document.txt",
        "sed -i 'r secret' document.txt",
        "sed -i 'R secret' document.txt",
        "sed -i F document.txt",
        "sed -i q document.txt",
        "sed -i Q document.txt",
        "sed -i.bak 's/old/new/' document.txt",
        "sed --in-place=.bak 's/old/new/' document.txt",
        "sed -i 's/old/new/' document.txt document.txt",
        "sed -i 's/old/new/' document.txt; touch unexpected",
        "sed -i 's/old/new/' document.txt > unexpected",
        "sed -i 's/old/new/' document.txt | cat",
        "sed -i 's/old/new/' $DOCUMENT",
        "sed -i 's/old/new/' *.txt",
        "sed -i 's/old/new/' $(touch unexpected)",
        "LC_ALL=C sed -i 's/old/new/' document.txt",
        "sh -c \"sed -i 's/old/new/' document.txt\"",
        "/tmp/sed -i 's/old/new/' document.txt",
        "sed --debug -i 's/old/new/' document.txt",
        "sed --version -i 's/old/new/' document.txt",
        "sed --inplace 's/old/new/' document.txt",
        "sed -i -f script.sed document.txt",
        "sed 's/old/new/' document.txt",
    ],
)
def test_unsupported_commands_explain_absence_without_running_or_guessing(
    tmp_path: Path, command: str
) -> None:
    (tmp_path / "secret").write_text("must not enter review", encoding="utf-8")
    preview = reviewed_preview(requested(tmp_path, command))

    assert preview.files == []
    assert preview.unavailable
    assert not (tmp_path / "unexpected").exists()
    assert not (tmp_path / "document.txt").exists()


def test_missing_preimage_is_not_replaced_with_a_live_file(tmp_path: Path) -> None:
    entry = requested(tmp_path, "sed -i 's/old/new/' other.txt")
    (tmp_path / "other.txt").write_text("old\n", encoding="utf-8")

    preview = reviewed_preview(entry)

    assert preview.files == []
    assert "captured" in preview.unavailable
    assert (tmp_path / "other.txt").read_text(encoding="utf-8") == "old\n"


def test_in_place_sed_treats_dash_as_a_captured_file(tmp_path: Path) -> None:
    entry = requested(tmp_path, "sed -i 's/old/new/' -- -")
    entry = entry.model_copy(update={"preconditions": {tmp_path / "-": "old\n"}})

    preview = reviewed_preview(entry)

    assert preview.unavailable == ""
    assert preview.files[0].path == tmp_path / "-"
    assert preview.files[0].after == "new\n"


def test_explicit_directory_mismatch_cannot_reinterpret_a_relative_target(
    tmp_path: Path,
) -> None:
    entry = requested(tmp_path, "sed -i 's/old/new/' document.txt")
    entry = entry.model_copy(
        update={
            "operation": entry.operation.model_copy(
                update={
                    "payload": {
                        "command": entry.operation.payload["command"],
                        "workdir": str(tmp_path / "elsewhere"),
                    }
                }
            )
        }
    )

    preview = reviewed_preview(entry)

    assert preview.files == []
    assert "directory differs" in preview.unavailable


def test_native_execution_rewrite_cannot_borrow_the_original_preview(
    tmp_path: Path,
) -> None:
    entry = requested(tmp_path, "sed -i 's/old/new/' document.txt")
    entry = entry.model_copy(
        update={"execution_payload": {"command": "sed -i 's/old/other/' document.txt"}}
    )

    preview = reviewed_preview(entry)

    assert preview.files == []
    assert "native execution" in preview.unavailable.lower()


def test_unresolvable_target_has_an_explanation_instead_of_a_failed_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(path: Path) -> Path:
        raise OSError(f"Cannot resolve {path}")

    monkeypatch.setattr(Path, "resolve", unavailable)

    preview = reviewed_preview(requested(tmp_path, "sed -i 's/old/new/' document.txt"))

    assert preview.files == []
    assert "cannot be resolved" in preview.unavailable


def test_relative_captured_directory_cannot_use_the_server_working_directory(
    tmp_path: Path,
) -> None:
    entry = requested(tmp_path, "sed -i 's/old/new/' document.txt")
    entry = entry.model_copy(
        update={
            "operation": entry.operation.model_copy(update={"cwd": Path("relative")})
        }
    )

    preview = reviewed_preview(entry)

    assert preview.files == []
    assert "absolute command directory" in preview.unavailable


def test_native_shell_capture_preserves_crlf_for_the_preview(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    target = tmp_path / "document.txt"
    target.write_bytes(b"old\r\n")
    command = "# lup: escalate[sandbox]: review the exact file rewrite\nsed -i 's/old/new/' document.txt"

    hook(tmp_path, command, tool="Bash")

    (entry,) = QuestionRelay(tmp_path / ".lup/questions.jsonl").pending()
    assert entry.preconditions[target] == "old\r\n"
    assert target.read_bytes() == b"old\r\n"


def test_stale_check_preserves_captured_crlf(tmp_path: Path) -> None:
    entry = requested(tmp_path, "sed -i 's/old/new/' document.txt", "old\r\n")
    (tmp_path / "document.txt").write_bytes(b"old\r\n")

    detail = ReviewDetail.of(tmp_path, entry, "operator")

    assert detail.stale_reason == ""
    assert detail.files[0].before == "old\r\n"
    assert detail.files[0].after == "new\r\n"


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
def test_transformation_runner_enforces_sandbox_even_without_caller_screen(
    tmp_path: Path, script: str
) -> None:
    (tmp_path / "secret").write_text("secret value", encoding="utf-8")

    result = sed_output([script], [], before="old\n", root=tmp_path)

    assert result["text"] is None
    assert result["cause"] == "refused"
    assert not (tmp_path / "unexpected").exists()


def test_nonterminating_script_is_stopped_without_partial_evidence() -> None:
    result = sed_output([":loop; b loop"], [], before="old\n", timeout=0.02)

    assert result == {"text": None, "cause": "refused"}


def test_host_preview_preserves_extended_quiet_modes_without_writing(
    tmp_path: Path,
) -> None:
    target = tmp_path / "document.txt"
    target.write_bytes(b"old\r\nunchanged\r\n")

    result = rewritten_text(["s/(old)/new/p"], "document.txt", tmp_path, ["-nE"])

    assert result == {"text": "new\r\n", "cause": None}
    assert target.read_bytes() == b"old\r\nunchanged\r\n"


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


@pytest.mark.parametrize(
    ("command", "before"),
    [
        ("sed -i 's/old/new/' document.txt", "old é\n"),
        ("sed -i 's/é/new/' document.txt", "old\n"),
        ("sed -i 's/[a-z]/new/' document.txt", "old\n"),
        ("sed -i 's/[[:alpha:]]/new/' document.txt", "old\n"),
        ("sed -i 's/old/new/I' document.txt", "old\n"),
        ("sed -i '/old/I s/old/new/' document.txt", "old\n"),
        ("sed -i 's/old/\\Unew/' document.txt", "old\n"),
    ],
)
def test_unknown_execution_locale_withholds_sensitive_previews(
    tmp_path: Path, command: str, before: str
) -> None:
    preview = reviewed_preview(requested(tmp_path, command, before))

    assert preview.files == []
    assert "locale" in preview.unavailable
    assert preview.notice == ""


@pytest.mark.parametrize(
    ("command", "before", "after"),
    [
        ("sed -i 's/old/[new]/' document.txt", "old\n", "[new]\n"),
        ("sed -i 's/\\[old\\]/new/' document.txt", "[old]\n", "new\n"),
    ],
)
def test_literal_brackets_remain_supported_without_locale_regex_semantics(
    tmp_path: Path, command: str, before: str, after: str
) -> None:
    preview = reviewed_preview(requested(tmp_path, command, before))

    assert preview.unavailable == ""
    assert preview.files[0].after == after
    assert "inbox environment" in preview.notice


@pytest.mark.parametrize(
    "command",
    [
        r"sed -i 's/x/\xc3\xa9/;s/./X/g' document.txt",
        r"sed -i 's/x/\o303\o251/;s/./X/g' document.txt",
        r"sed -i 's/x/\d195\d169/;s/./X/g' document.txt",
        r"sed -i -e 'a \xc3\xa9' -e 's/./X/g' document.txt",
        r"sed -i -e 'i \o303\o251' -e 's/./X/g' document.txt",
        r"sed -i -e 'c \d195\d169' -e 's/./X/g' document.txt",
    ],
)
def test_numeric_escapes_cannot_hide_locale_sensitive_intermediate_text(
    tmp_path: Path, command: str
) -> None:
    preview = reviewed_preview(requested(tmp_path, command, "x\n"))

    assert preview.files == []
    assert "locale" in preview.unavailable
    assert preview.notice == ""


def test_escaped_literal_byte_escape_spelling_stays_ascii(tmp_path: Path) -> None:
    preview = reviewed_preview(
        requested(tmp_path, r"sed -i 's/old/\\xc3/' document.txt")
    )

    assert preview.unavailable == ""
    assert preview.files[0].after == r"\xc3" + "\n"
