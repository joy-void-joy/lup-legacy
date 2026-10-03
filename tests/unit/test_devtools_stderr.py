"""A failed command's stderr reads as what it says, and as what caused it.

A refusal by a session's lease and a genuinely read-only disk print the
same words, so the mount table is what tells them apart; everything else
a command printed is passed through as itself.
"""

import sh

from lup.sandbox.models import Mount
from lup.sandbox.translation import MountTopology


class TestDecodeStderr:
    def test_decodes_bytes_and_trims_framing(self) -> None:
        from lup.devtools.utils import decode_stderr

        err = sh.ErrorReturnCode.__new__(sh.ErrorReturnCode)
        err.stderr = b"boom\n"
        assert decode_stderr(err) == "boom"

    def test_passes_through_str(self) -> None:
        from lup.devtools.utils import decode_stderr

        err = sh.ErrorReturnCode.__new__(sh.ErrorReturnCode)
        # sh 2.4 declares `stderr` read-only, and this pins the branch that
        # runs when it holds text rather than the bytes the type promises.
        object.__setattr__(err, "stderr", "already text")
        assert decode_stderr(err) == "already text"


class TestAttributedStderr:
    """`Read-only file system` is what a lease refusing a write looks like.

    It is also what a genuinely read-only disk looks like, which is the whole
    difficulty: the words are identical and only the mount table tells them
    apart. These pin both directions, because a wrong boundary claim sends a
    reader looking for a rail that was not involved.
    """

    def refusal(self, message: str) -> sh.ErrorReturnCode:
        """A failed command carrying this stderr."""
        err = sh.ErrorReturnCode.__new__(sh.ErrorReturnCode)
        err.stderr = message.encode()
        return err

    def leased(self) -> MountTopology:
        """A session's own tree, with a sibling present and unwritable."""
        return MountTopology(
            mounts=[
                Mount(
                    container_path="/repo",
                    source="/host/repo",
                    kind="bind",
                    mode="rw",
                    purpose="the worktree this session owns",
                ),
                Mount(
                    container_path="/repo/siblings",
                    source="/host/siblings",
                    kind="bind",
                    mode="ro",
                    purpose="other worktrees, present and unwritable",
                ),
            ]
        )

    def test_a_refusal_under_a_leased_mount_gains_the_account(self) -> None:
        from lup.devtools.utils import attributed_stderr

        said = attributed_stderr(
            self.refusal(
                "error: failed to delete '/repo/siblings/dev': Read-only file system"
            ),
            self.leased(),
        )
        assert "Read-only file system" in said
        assert "This is confinement, not a broken filesystem" in said

    def test_a_refusal_the_table_cannot_explain_is_left_alone(self) -> None:
        from lup.devtools.utils import attributed_stderr

        message = "error: failed to delete '/elsewhere/x': Read-only file system"
        assert attributed_stderr(self.refusal(message), self.leased()) == message

    def test_an_ordinary_failure_is_reported_as_itself(self) -> None:
        from lup.devtools.utils import attributed_stderr

        message = "fatal: not a valid ref"
        assert attributed_stderr(self.refusal(message), self.leased()) == message
