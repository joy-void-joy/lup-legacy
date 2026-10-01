"""Which path a redirection actually writes, and which of them destroy nothing.

Two readings that had drifted from what the shell does. A target behind a
parameter was judged as the literal text `$B`, which spells no path and earns
no role, so it fell to the fallback and was retired by the recovery row --
spending a capture's discharge on a write to scratch. And every stream but
`/dev/null` fell there too, which told the reader that "the affected paths are
captured and restorable" about a terminal.
"""

from pathlib import Path

from lup.policy.assets.host import unleased_write_targets
from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.lex import shell_path_verb_targets, shell_write_targets
from lup.policy.kernel.words import flag_write_targets
from lup.policy.kernel.rows import PathRoleRow, ShellRuleRow
from lup.policy.kernel.shell import decide_shell
from lup.policy.shell_rules import erase_shell_rules
from lup.policy.vocabulary import default_vocabulary

SCRATCH = [PathRoleRow(root="tmp", role="scratch")]


VOCABULARY = erase_shell_rules(default_vocabulary())


def rows() -> list[ShellRuleRow]:
    """The library's offered table, which is what the contract describes."""
    return erase_shell_rules(default_vocabulary())


def verdict(command: str) -> KernelDecision:
    """One command judged with a scratch role declared over `tmp`."""
    return decide_shell(command, rows(), path_roles=SCRATCH)


def test_a_stream_target_raises_no_question_at_all() -> None:
    """Nothing prior is lost, so there is nothing for anybody to answer.

    Asserted as the absence of a question rather than as an allow, because
    these were already reaching allow -- by spending the recovery discharge,
    on the strength of a claim about captured paths that no stream has.
    """
    for target in ("/dev/null", "/dev/stderr", "/dev/stdout", "/dev/tty", "/dev/fd/2"):
        settled = verdict(f"echo hi > {target}")
        assert settled.effect == "allow", target
        assert "redirection" not in settled.reason, target


def test_a_descriptor_the_shell_opened_onto_a_file_is_not_a_stream() -> None:
    """`exec 3>notes.txt` makes `> /dev/fd/3` a write to `notes.txt`.

    So the family is named entry by entry rather than matched by shape, and
    `/dev/stdin` is left out for the same reason: a command run with
    `< notes.txt` would truncate it.
    """
    for target in ("/dev/fd/3", "/dev/fd/9", "/dev/stdin"):
        assert verdict(f"echo hi > {target}").effect == "ask", target


def test_a_device_that_destroys_is_not_a_stream() -> None:
    """The allowlist is named entry by entry because `/dev` is not a safe prefix.

    A prefix match would hand `> /dev/sda` the same silence it hands
    `> /dev/null`, and seeding the kernel's entropy pool is not a local loss a
    capture puts back either.
    """
    for target in ("/dev/sda", "/dev/urandom", "/dev/mem", "/dev/nvme0n1"):
        assert verdict(f"cat > {target}").effect == "ask", target


def test_a_standalone_assignment_names_the_path_that_is_written() -> None:
    """The shell wrote to `tmp/x`, and now so does the reading of it."""
    assert shell_write_targets("B=tmp/x; cat > $B") == ["tmp/x"]
    assert shell_write_targets("B=tmp/x; cat > ${B}") == ["tmp/x"]
    assert verdict("B=tmp/x; cat > $B").effect == "allow"


def test_a_command_prefix_assignment_is_not_the_shell_s_value() -> None:
    """`VAR=x cmd > $VAR` expands the redirection from the value already held.

    So the assignment beside it says nothing about where the write lands, and
    reading it would resolve a path the command never writes -- in the
    permissive direction, which is the one that matters.
    """
    assert shell_write_targets("B=tmp/x cat > $B") == ["$B"]
    assert verdict("B=tmp/x cat > $B").effect == "ask"


def test_only_a_literal_assignment_resolves() -> None:
    """A value carrying its own expansion names no path this can reconstruct."""
    assert shell_write_targets("B=$OTHER; cat > $B") == ["$B"]
    assert shell_write_targets("B=$(hostname); cat > $B") == ["$B"]
    assert verdict("B=$(hostname); cat > $B").effect == "ask"


def test_the_last_assignment_wins_as_the_shell_s_does() -> None:
    """Two assignments to one name leave the second standing, not the first."""
    assert shell_write_targets("B=tmp/x; B=tmp/y; cat > $B") == ["tmp/y"]


def test_an_unknown_name_stays_unresolved() -> None:
    """Nothing assigned it here, so the word keeps the only meaning we have."""
    assert shell_write_targets("cat > $B") == ["$B"]
    assert verdict("cat > $B").effect == "ask"


def test_a_stream_sink_is_no_target_for_the_lease_to_read() -> None:
    """The lease reads paths, and a sink is not one, so it never reaches it.

    The redirection reading retires a stream on its own, which is why the
    streams above never reach a row that asks. The lease reading takes its
    targets from :func:`shell_write_targets` instead and asks a question the
    redirection reading had already answered -- `/dev/null` exists, no capture
    holds it, and no writable root contains it -- so a contained session met an
    approval question in front of every `2>/dev/null` it wrote.
    """
    assert shell_write_targets("grep x f 2>/dev/null") == []
    assert shell_write_targets("a > out.txt 2>/dev/null") == ["out.txt"]
    assert (
        unleased_write_targets(
            shell_write_targets("grep x f 2>/dev/null"),
            {"writable_roots": ["/checkout"]},
            Path("/checkout"),
        )
        == []
    )


def test_a_program_a_command_carries_is_not_one_of_its_paths() -> None:
    """The same shape as the sink above, reached by a word rather than a target.

    Every non-flag word of a `sed` was named as something it acted on, which
    was defended as harmless because a script is not a file. Two of the three
    questions asked of these stat the path and drop whatever is not on disk;
    the lease resolves the string, and a sed address script *begins with a
    slash* -- so it resolved to an absolute path no writable root contains and
    was reported as a write outside the lease. The command was a read.
    """
    scripted = "sed -n '/^def one/,/^def two/p' vocabulary.py"
    assert shell_path_verb_targets(scripted, VOCABULARY) == []
    assert (
        unleased_write_targets(
            shell_path_verb_targets(scripted, VOCABULARY),
            {"writable_roots": ["/checkout"]},
            Path("/checkout"),
        )
        == []
    )


def test_a_sed_that_only_prints_names_no_file_for_the_lease_to_read() -> None:
    """`sed -n 1,80p file` is `cat` with an address, and its operand is read.

    The operand is a path, unlike the script beside it, and naming it was
    defended as under-naming's safe direction: two of the three questions stat
    it, and a file that is there answers them harmlessly. The third resolves
    it against what the launch mounted writable, and a file under a read-only
    mount is exactly the path no writable root contains -- so a print over one
    was reported as a write outside the lease, while `head` over the same file
    was allowed beside it.
    """
    printed = "sed -n 1,80p /mounted/findings/verify.py"
    assert shell_path_verb_targets(printed, VOCABULARY) == []
    assert shell_path_verb_targets("sed -ne '1p' a.py b.py", VOCABULARY) == []
    assert shell_path_verb_targets("sed --expression=s/a/b/ src.py", VOCABULARY) == []
    assert (
        unleased_write_targets(
            shell_path_verb_targets(printed, VOCABULARY),
            {"writable_roots": ["/checkout"]},
            Path("/checkout"),
        )
        == []
    )


def test_the_operand_a_rewrite_does_write_is_still_named() -> None:
    """Under-naming is the conservative direction, not a free one.

    `sed -i` is why this command is read at all, so dropping the script must
    not drop the file beside it -- by any of the spellings that say where the
    program came from.
    """
    assert shell_path_verb_targets("sed -i 's/a/b/' src.py", VOCABULARY) == ["src.py"]
    assert shell_path_verb_targets("sed -i.bak 's/a/b/' src.py", VOCABULARY) == [
        "src.py"
    ]
    assert shell_path_verb_targets("sed -i -e 's/a/b/' src.py", VOCABULARY) == [
        "src.py"
    ]
    assert shell_path_verb_targets("sed -i -ne '1p' a.py b.py", VOCABULARY) == [
        "a.py",
        "b.py",
    ]
    assert shell_path_verb_targets("sed -i --expression=s/a/b/ src.py", VOCABULARY) == [
        "src.py"
    ]
    # A script file is refused before any fact about `src.py` is consulted,
    # and a refusal is not reopened by the facts -- so nothing is named, as
    # nothing is for any other command the vocabulary did not judge.
    assert shell_path_verb_targets("sed -i -f rules.sed src.py", VOCABULARY) == []


def test_a_write_flag_names_its_path_by_every_spelling_that_reaches_it() -> None:
    """`-o path`, `--output path` and `--output=path` land the same file."""
    assert flag_write_targets(["sort", "-o", "out.txt", "f"], ["-o"]) == ["out.txt"]
    assert flag_write_targets(["git", "log", "--output", "l.txt"], ["--output"]) == (
        ["l.txt"]
    )
    assert flag_write_targets(["git", "log", "--output=l.txt"], ["--output"]) == (
        ["l.txt"]
    )
    assert flag_write_targets(
        ["find", ".", "-fprint", "a", "-fls", "b"], ["-fprint", "-fls"]
    ) == (["a", "b"])


def test_a_write_flag_it_cannot_resolve_names_nothing() -> None:
    """Silence costs a relaxation; a guess would cost the grant itself.

    The guard beside this reads a short flag anywhere inside a cluster, which
    is right for asking whether one is present and wrong for deciding which
    word is the path -- `-no` would carry `-o` and take somebody else's
    operand as the file. Whatever is unresolved here leaves the row's own
    verdict standing, so under-naming is the safe direction and taken.
    """
    assert flag_write_targets(["sort", "-no", "f"], ["-o"]) == []
    assert flag_write_targets(["sort", "-o"], ["-o"]) == []
    assert flag_write_targets(["sort", "-o", "-x", "f"], ["-o"]) == []
    assert flag_write_targets(["git", "log", "--output="], ["--output"]) == []


def written(
    command: str, tracked: list[str] | None = None, contained: bool = False
) -> KernelDecision:
    """One command judged with `tmp` scratch and a stated tracked reading."""
    return decide_shell(
        command,
        rows(),
        path_roles=SCRATCH,
        existing_targets=["notes.txt", "src.py"],
        tracked_targets=tracked or [],
        contained=contained,
    )


class TestAFlagThatWritesIsJudgedWhereEveryWriteIs:
    """`-o path` and `> path` land the same bytes, so they answer alike.

    Before this the flag never looked at the path. It carried one verdict per
    row and a checkpoint decided whether anything discharged it, so `tree -o`
    and `base64 -o` allowed onto tracked source while `sort -o` asked about a
    file in a scratch tree.
    """

    def test_landing_a_new_file_allows(self) -> None:
        """A create replaces nothing, which is the commonest form by far."""
        assert written("sort -o fresh.txt f").effect == "allow"

    def test_landing_in_a_scratch_tree_allows_whatever_is_there(self) -> None:
        """The tree is disposable by declaration, so nothing is being lost."""
        assert written("sort -o tmp/out.txt f").effect == "allow"

    def test_replacing_an_untracked_file_allows(self) -> None:
        """`cmd -o run.log` over yesterday's log: nothing reviews it."""
        assert written("sort -o notes.txt f").effect == "allow"

    def test_replacing_tracked_source_asks_rather_than_refusing(self) -> None:
        """The content question is asked here, and is not refused here.

        What a command writes is produced by running it, so no gate could read
        it in advance and a refusal would fall on exactly the writes for which
        that is unavoidable. What a reviewed version being replaced by unread
        bytes earns is therefore the question, answered before the fact, with
        the file itself read after it as before.
        """
        assert written("sort -o src.py f", tracked=["src.py"]).effect == "ask"

    def test_the_same_file_answers_alike_by_either_spelling(self) -> None:
        """The whole point: the path decides, not which word named it."""
        for spelling in ("sort -o src.py f", "sort f > src.py"):
            assert written(spelling, tracked=["src.py"]).effect == "ask", spelling
        for spelling in ("sort -o tmp/o.txt f", "sort f > tmp/o.txt"):
            assert written(spelling, tracked=["src.py"]).effect == "allow", spelling

    def test_a_path_the_reading_can_answer_for_still_decides_beforehand(self) -> None:
        """Generous about content is not generous about where it lands.

        Which tree a path is in is knowable before the command runs, so it is
        still answered then: a protected path asks and one outside the
        checkout asks unless a boundary confines it.
        """
        for spelling in ("sort -o ../elsewhere.txt f", "sort f > ../elsewhere.txt"):
            assert written(spelling).effect == "ask", spelling
        for spelling in ("sort -o /etc/hosts f", "sort f > /etc/hosts"):
            assert written(spelling).effect == "ask", spelling

    def test_a_boundary_confines_both_spellings_or_neither(self) -> None:
        """Whether a write outside is confined is one question for both spellings.

        A write beyond the checkout is settled by where the host measured it
        landing, not by the row's declared `sandbox` -- which states where a
        command must run, not where this one is. So a flag value and a
        redirection reach one answer in a contained session: the question,
        until the host places the path as the container's own.
        """
        answers = {
            written(spelling, contained=True).effect
            for spelling in ("sort -o /etc/hosts f", "sort f > /etc/hosts")
        }
        assert answers == {"ask"}

    def test_a_flag_naming_no_resolvable_path_keeps_the_row_s_question(self) -> None:
        """A write nobody can locate is what the guard was written for."""
        assert written("sort -no f").effect == "ask"

    def test_a_flag_that_runs_a_program_still_asks_beside_one_that_writes(
        self,
    ) -> None:
        """The stronger question survives: a file's answer is not a program's."""
        assert written("sort --compress-program=x -o tmp/o.txt f").effect == "ask"


class TestAWriteNobodyCanRead:
    """A redirection whose bytes only running produces, landing in the tree.

    The asymmetry these pin: `echo x > docs/f.md` reached the content gates
    and `date +%s > docs/f.md` reached nothing, for one file and one write.
    Which spelling carried its bytes is a fact about the command, not about
    what is being replaced, so it cannot be what decides whether anybody is
    asked.
    """

    def test_replacing_tracked_content_with_unread_bytes_asks(self) -> None:
        answer = written("date +%s > src.py", tracked=["src.py"])

        assert answer.effect == "ask"
        assert "src.py" in answer.reason

    def test_appending_is_the_same_answer_as_replacing(self) -> None:
        """The bytes are equally unread either way, so the question is too."""
        assert written("date +%s >> src.py", tracked=["src.py"]).effect == "ask"

    def test_the_question_names_both_ways_past_it(self) -> None:
        """A question an agent cannot act on becomes a retry."""
        answer = written("date +%s > src.py", tracked=["src.py"])

        assert "scratch" in answer.recovery
        assert "carry the content" in answer.recovery

    def test_a_create_replaces_nothing_and_keeps_its_allow(self) -> None:
        """What the question is about is the reviewed version being replaced."""
        assert written("date +%s > fresh.txt", tracked=["src.py"]).effect == "allow"

    def test_a_file_git_never_held_is_the_ordinary_work_it_was(self) -> None:
        """Yesterday's log rewritten in place loses no reviewed version."""
        assert written("date +%s > notes.txt").effect == "allow"

    def test_scratch_stays_out_of_it(self) -> None:
        assert (
            written("date +%s > tmp/out.txt", tracked=["tmp/out.txt"]).effect == "allow"
        )

    def test_a_command_carrying_its_bytes_is_left_to_the_content_gates(self) -> None:
        """Judged twice would be this rule holding a weaker copy of that one."""
        answer = written("echo x > src.py", tracked=["src.py"])

        assert answer.effect == "allow"
        assert "only running the command produces" not in answer.reason


def test_a_tee_or_a_copy_into_a_stream_names_nothing_for_the_lease() -> None:
    """`| tee /dev/null` writes into a device every process holds open.

    Its operand was named among the paths the verb acts on, so the lease
    resolved it, found no writable root holding `/dev/null`, and a read-only
    `git grep | tee /dev/null | wc -l` was parked as a write the launch did not
    mount. A copy into one is the same write spelled by `cp`. A device the
    write would replace keeps its name, and so does a copy whose flag could
    replace the device rather than write into it.
    """
    teed = "git grep -l x | tee /dev/null | wc -l"
    assert shell_path_verb_targets(teed, VOCABULARY) == []
    assert shell_path_verb_targets("ls | tee -a /dev/stderr out.txt", VOCABULARY) == [
        "out.txt"
    ]
    assert shell_path_verb_targets("cp f.txt /dev/null", VOCABULARY) == ["f.txt"]
    assert shell_path_verb_targets("tee /dev/sda", VOCABULARY) == ["/dev/sda"]
    assert shell_path_verb_targets("tee /dev/fd/3", VOCABULARY) == ["/dev/fd/3"]
    assert shell_path_verb_targets(
        "cp --remove-destination f.txt /dev/null", VOCABULARY
    ) == ["f.txt", "/dev/null"]
    assert shell_path_verb_targets("rm /dev/null", VOCABULARY) == ["/dev/null"]


def test_a_write_flag_naming_a_stream_writes_no_file() -> None:
    """`curl -o /dev/null -w '%{http_code}'` lands its body nowhere.

    The flag's value was judged as a file created outside the checkout, so
    `sort -o /dev/null` and `git diff --output=/dev/null` asked as `sort -o
    out.txt` beyond the tree does, and a copy into one asked as a copy over a
    file. A descriptor the shell opened onto a file is still a file, and a
    verb replacing the device itself keeps its question.
    """
    for command in (
        "sort -o /dev/null f.txt",
        "git diff --output=/dev/null",
        "cp f.txt /dev/null",
    ):
        assert verdict(command).effect == "allow", command
    for command in ("sort -o /dev/fd/3 f.txt", "rm /dev/null", "mv f.txt /dev/null"):
        assert verdict(command).effect == "ask", command
