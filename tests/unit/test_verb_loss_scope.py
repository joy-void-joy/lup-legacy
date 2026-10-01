"""What a verb destroys is read off its own targets, not off its row.

A row carries one value for every path it might touch, and for `rm`, `cp`,
`mv`, `ln` and the archive verbs that value is `boundary_wide`: a glob
prevents an exact footprint, so the wider capture is what the opacity costs.
That is the right reading inside the checkout and a false one the moment a
path leaves it, because the capture it names is a snapshot of the checkout --
and a variable is a path that may leave it, since only the run says where it
lands.

Read off the row, with a snapshot taken, `rm /etc/hosts` would be *allowed*,
with the reason "the affected paths are captured and restorable" — said of a
file no snapshot of this checkout has ever held. The settlement row that
discharges a covered loss would be handed a claim nothing backs, the same
defect `write_checkpoint` keeps out of a redirection, on the other spelling
of a write.
"""

from pathlib import Path

from lup.policy.kernel.rows import PathRoleRow
from lup.policy.models import Decision, ShellCommand
from lup.policy.rules import ShellPolicy
from lup.policy.vocabulary import default_vocabulary

SCRATCH = [PathRoleRow(root="tmp", role="scratch")]


def recovered(command: str, root: Path) -> Decision:
    """One command in a session whose checkout is held by a proven capture."""
    return ShellPolicy(default_vocabulary(), path_roles=SCRATCH, recovered=True).decide(
        ShellCommand(command=command, cwd=root)
    )


def test_a_capture_of_this_checkout_does_not_discharge_a_loss_beyond_it(
    tmp_path: Path,
) -> None:
    """No capture of this checkout is claimed for a file beyond it."""
    verdict = recovered("rm /etc/hosts", tmp_path)

    assert verdict.effect == "ask"
    assert "captured and restorable" not in verdict.reason


def test_one_operand_outside_the_checkout_answers_for_the_line(
    tmp_path: Path,
) -> None:
    """A mixed command is judged by the loss nothing holds, not by the other one.

    The scope is the strongest of the operands, on the same reasoning the
    segment join uses: a command that destroys scratch *and* a file beyond the
    boundary is the second of those, and nothing about the harmless half
    weakens it.
    """
    assert recovered("rm tmp/a /etc/hosts", tmp_path).effect == "ask"


def test_only_the_operands_the_verb_writes_are_read(tmp_path: Path) -> None:
    """A source `cp` merely reads is an ordinary read, however far out it sits.

    Reading every operand would be the conservative mistake: it keeps a
    question, and it keeps it for a command that destroys nothing at all.
    """
    scratch = tmp_path / "tmp"
    scratch.mkdir()

    assert recovered("cp /etc/hosts tmp/hosts", tmp_path).effect == "allow"
    assert recovered("cp README.md /etc/hosts", tmp_path).effect == "ask"


def test_an_archive_verb_is_read_the_same_way_its_targets_already_were(
    tmp_path: Path,
) -> None:
    """Verbs that name their targets answer from those targets, not the row.

    `archive_lands_on_nothing` reads exactly these paths to grant an
    extraction that replaces nothing, so the targets are there to be had —
    and where the grant does not apply, answering from the row's own
    `boundary_wide` would let the settlement row discharge it, allowing
    `gzip /etc/hosts` and `tar -xf a.tgz -C /etc` as "captured and
    restorable" whenever a snapshot is taken.
    """
    assert recovered("gzip /etc/hosts", tmp_path).effect == "ask"
    assert recovered("gunzip /etc/hosts.gz", tmp_path).effect == "ask"
    assert recovered("tar -xf a.tgz -C /etc", tmp_path).effect == "ask"


def test_work_inside_the_checkout_keeps_the_answer_it_had(tmp_path: Path) -> None:
    """The everyday case, which the row is right about.

    Scratch is disposable by declaration and the object store holds the rest,
    so nothing here changes: this reads the targets to find the losses a
    capture cannot cover, not to find more of them.
    """
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    (scratch / "scratch.txt").write_text("x", encoding="utf-8")

    assert recovered("rm tmp/scratch.txt", tmp_path).effect == "allow"
    assert recovered("rm -r tmp/build", tmp_path).effect == "allow"
    assert recovered("mv tmp/a tmp/b", tmp_path).effect == "allow"
    assert recovered("gzip tmp/notes.txt", tmp_path).effect == "allow"
    assert recovered("tar -xf a.tgz -C tmp/out", tmp_path).effect == "allow"


def test_a_target_only_the_run_resolves_is_not_settled_by_a_capture(
    tmp_path: Path,
) -> None:
    """A write whose path carries an expansion lands wherever the run says.

    Read off the row with a snapshot taken, every one of these would be
    allowed as "captured and restorable" -- `> ~/.bashrc` and `> $HOME/x`
    included, and `sort -o a$X` and `cp f a$X` granted as the create of a
    file literally named `a$X`. The snapshot holds this checkout, and nothing
    says `$X` does not climb out of it. A glob is read where it stands, and a
    scratch root reached through its own variable keeps its grant.
    """
    for command in (
        "echo x > a$X",
        "echo x > ~/.bashrc",
        "echo x > $HOME/x",
        "sort -o a$X f",
        "cp f a$X",
        "mv f a$X",
        "tee a$X",
        "rm tmp/$X",
        "dd if=f of=a$X",
    ):
        verdict = recovered(command, tmp_path)
        assert verdict.effect == "ask", command
        assert "captured and restorable" not in verdict.reason, command

    assert recovered("echo x > $TMPDIR/out.txt", tmp_path).effect == "allow"
    assert recovered("rm *.pyc", tmp_path).effect == "allow"
    assert recovered("sort -o out.txt f", tmp_path).effect == "allow"


def test_a_patch_sent_outside_the_checkout_is_not_settled_by_its_capture(
    tmp_path: Path,
) -> None:
    """`--unsafe-paths` is the flag that lets a patch leave the working area.

    The row's `boundary_wide` is right for the ordinary apply, which lands in
    the checkout, and read for the flagged one too it would allow
    `git apply --unsafe-paths x.patch` as "captured and restorable" whenever a
    snapshot is taken. The flag's own effect says where the write goes, and
    no capture of this checkout holds it. Reset and switch keep the
    targeted loss their flags declare.
    """
    for command in (
        "git apply --unsafe-paths x.patch",
        "git apply --unsafe-paths --directory=/etc x.patch",
        "git apply --build-fake-ancestor=/tmp/index x.patch",
    ):
        verdict = recovered(command, tmp_path)
        assert verdict.effect == "ask", command
        assert "captured and restorable" not in verdict.reason

    assert recovered("git reset --hard", tmp_path).effect == "allow"


def test_operands_piped_to_xargs_are_not_settled_by_a_capture(
    tmp_path: Path,
) -> None:
    """What xargs appends is on stdin, so no capture was taken of it by name.

    Judged as a bare `rm` whose missing operands no human-owned rule can
    match, `echo README.md | xargs rm` would be allowed as "captured and
    restorable" whenever a snapshot is taken. A reader of the piped
    names keeps its verdict.
    """
    for command in (
        "echo README.md | xargs rm",
        "ls | xargs rm -rf",
        "find . -name '*.pyc' | xargs rm",
    ):
        verdict = recovered(command, tmp_path)
        assert verdict.effect == "ask", command
        assert "captured and restorable" not in verdict.reason

    assert recovered("git ls-files | xargs grep foo", tmp_path).effect == "allow"
