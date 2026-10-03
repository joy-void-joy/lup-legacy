"""What a session-wide capture may be spent on, read per path rather than per row.

A capture is proven once per session and the settlement layer is told so once
per session, so the only thing standing between "a snapshot completed" and "so
this command is authorized" is the scope a decision carries. A row stating one
scope for every path it can touch spends that discharge everywhere:
``tee .git/config`` would truncate the repository's config as an unprompted
allow reading "the affected paths are captured and restorable", and
``tee -a /etc/hosts`` would say the same of a file outside the checkout
entirely.

Three kinds of blindness reach that allow, and each is answered here:

- a writing verb whose targets go unread when nothing models where it writes
  -- ``tee``, ``truncate``, ``ln``, and ``dd``, which names its destination in
  an option rather than an operand;
- a row whose loss is not a path at all, where no capture of a checkout has
  anything to say -- a process, a package database, a unit, a crontab;
- the repository itself reached by absolute path. A linked worktree's ``.git``
  is a file pointing at ``<somewhere>/repo.git/worktrees/<name>``, so the
  config and hooks these sessions actually run out of carry no ``.git``
  segment, would grade ``outside``, and a contained placement would write
  there freely.

The other direction is the point of the mechanism and is asserted just as
hard: an ordinary file inside the checkout is genuinely covered by the
snapshot, and turning every redirect into a question would cost more than the
blindness it answers.
"""

from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.rows import PathRoleRow, ShellRuleRow
from lup.policy.kernel.shell import decide_shell
from lup.policy.kernel.words import write_scope
from lup.policy.shell_rules import erase_shell_rules
from lup.policy.vocabulary import default_vocabulary

SCRATCH = [PathRoleRow(root="tmp", role="scratch")]

SHARED_REPOSITORY = "/home/somebody/work/project.git"
"""The directory a linked worktree's `.git` file points into, spelled absolutely."""


def rows() -> list[ShellRuleRow]:
    """The library's offered table, which is what the contract describes."""
    return erase_shell_rules(default_vocabulary())


def settled(command: str, contained: bool = False) -> KernelDecision:
    """One command judged in a session whose capture completed.

    ``recovered`` is the fact the scope decides about: it is a session-level
    answer, so every command in such a session is told a capture exists and
    only the decision's own scope decides whether that capture covers it.
    """
    return decide_shell(
        command,
        rows(),
        path_roles=SCRATCH,
        recovered=True,
        contained=contained,
        inside_placement=contained,
    )


def test_a_write_to_the_repository_is_not_discharged_by_a_capture() -> None:
    """`.git` is graded protected, and the capture holds the working tree."""
    for command in (
        "tee .git/config",
        "truncate -s 0 .git/config",
        "ln -sf /tmp/payload .git/config",
        "dd if=/dev/zero of=.git/config",
    ):
        assert settled(command).effect == "ask", command


def test_a_write_outside_the_checkout_is_not_either() -> None:
    """No snapshot of this checkout has ever held `/etc/hosts`."""
    for command in (
        "tee -a /etc/hosts",
        "truncate -s 0 /etc/hosts",
        "ln -sf /tmp/payload /etc/hosts",
        "dd if=/dev/zero of=/etc/hosts",
    ):
        assert settled(command).effect == "ask", command


def test_an_ordinary_file_inside_the_checkout_still_settles() -> None:
    """The whole value of the mechanism, and the reason this is not a widening.

    A scratch tree is disposable by declaration and a fresh path replaces
    nothing, so neither is worth a person's attention -- and a policy that
    asked about them would be paid for on every command rather than on the
    handful no capture covers.
    """
    for command in (
        "tee tmp/note.txt",
        "tee -a tmp/run.log",
        "truncate -s 0 tmp/scratch",
        "dd if=/dev/zero of=tmp/image",
        "ln -s tmp/a tmp/b",
    ):
        assert settled(command).effect == "allow", command


def test_the_piped_spelling_keeps_the_verdict_it_already_reached() -> None:
    """Resolving the target through the edit gate is the right answer.

    It is asserted because it is the control: one write with two spellings
    gets one answer, and it is this spelling's answer that the bare form
    shares, not the other way round.
    """
    assert settled("echo x | tee .git/config").effect == "ask"
    assert settled("echo x | tee -a /etc/hosts").effect == "ask"


def test_a_flag_nothing_models_widens_rather_than_declining_to_answer() -> None:
    """Refusing something owes the widened reading, not the confident one.

    Whatever `--interactive=never` turns out to do, the operand names a path
    no capture of this checkout holds -- and returning "unknown" would leave
    the row claiming otherwise.
    """
    assert settled("rm --interactive=never /etc/hosts").effect == "ask"
    assert settled("rm --interactive=never tmp/build").effect == "allow"


def test_a_loss_that_is_not_a_path_names_no_capture() -> None:
    """A checkout snapshot holds no process, package, unit, or crontab.

    Every one of these rows states in its own reason that approval is
    required, so the scope column must not answer the question for them --
    unprompted, on every invocation of a recovered session.
    """
    for command in (
        "kill 123",
        "pkill -9 node",
        "apt-get install nginx",
        "pacman -S vim",
        "brew install jq",
        "systemctl restart nginx",
        "crontab -r",
    ):
        assert settled(command).effect == "ask", command


def test_the_repository_reached_by_absolute_path_is_the_same_repository() -> None:
    """Graded on the spelling, which is what every write reading shares."""
    assert write_scope(f"{SHARED_REPOSITORY}/config", SCRATCH) == "protected"
    assert write_scope(f"{SHARED_REPOSITORY}/hooks/pre-commit", SCRATCH) == "protected"
    assert write_scope(".git/config", SCRATCH) == "protected"
    assert write_scope("src/lup_template/agent/config.py", SCRATCH) == "production"


def test_a_writable_mount_does_not_make_the_repository_ordinary() -> None:
    """The discriminator cannot be whether the launch mounted the path writable.

    A contained launch mounts the shared administrative directory writable on
    purpose -- no session could cut a worktree otherwise -- so the measured
    lease covers it and the rule that catches an unleased write never fires.
    What makes the write worth a question is what is at the path: a hook there
    runs on the operator's next Git command, outside the boundary that granted
    it.
    """
    for command in (
        f"echo x >> {SHARED_REPOSITORY}/config",
        f"echo x >> {SHARED_REPOSITORY}/hooks/pre-commit",
        f"tee -a {SHARED_REPOSITORY}/config",
        f"dd if=/dev/zero of={SHARED_REPOSITORY}/config",
    ):
        assert settled(command, contained=True).effect == "ask", command
        assert settled(command).effect == "ask", command
