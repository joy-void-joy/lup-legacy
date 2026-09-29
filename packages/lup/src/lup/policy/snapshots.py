"""Accept destination repository policy bytes under one launch's explicit grants."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import sh
from pydantic import BaseModel

from lup.execution.shell import git
from lup.policy.assets.host import policy_snapshot_digest, policy_snapshot_files
from lup.sandbox.rail import AccessibleRoot, Lease, repository_layout, sibling_worktrees


def snapshot_directory(root: Path, snapshots: str = ".lup/policy-snapshots") -> Path:
    """Where a launch keeps the destination policies it accepted, one per digest."""
    return root / snapshots


class DestinationPolicy(BaseModel, frozen=True, extra="forbid"):
    """A checkout and evaluator the operator's launch explicitly made reachable."""

    protocol: Literal[1] = 1
    runtime: str = ""
    repository: str
    checkout: str
    writable_roots: list[str]
    read_only_roots: list[str]
    source: str = ""
    snapshot: str = ""
    digest: str = ""
    error: str = ""

    def accepted(self, root: Path, runtime: str) -> "DestinationPolicy":
        """Copy only verified source into a content-addressed launch snapshot."""
        checkout = Path(self.checkout)
        candidates = list(
            (checkout / f".{runtime}" / "plugins").glob(
                "*/hooks/scripts/policy_evaluator.py"
            )
        )
        if len(candidates) != 1:
            return self.model_copy(
                update={
                    "error": (
                        f"Expected one generated {runtime} destination policy evaluator "
                        f"in {checkout}; found {len(candidates)}. Run "
                        "`uv run lup-devtools harness generate all` there, then refresh "
                        "this launch's accepted policy snapshot."
                    ),
                    "runtime": runtime,
                }
            )
        source = candidates[0].parent.parent
        try:
            if any(path.is_symlink() for path in [source, *source.parents]):
                raise ValueError(f"Destination policy source has a symlink: {source}")
            digest = policy_snapshot_digest(source)
            destination = snapshot_directory(root) / digest
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                with TemporaryDirectory(
                    prefix=".accept-", dir=destination.parent
                ) as area:
                    staged = Path(area) / "revision"
                    for item in policy_snapshot_files(source):
                        copied = staged / item.relative_to(source)
                        copied.parent.mkdir(parents=True, exist_ok=True)
                        copied.write_bytes(item.read_bytes())
                    if policy_snapshot_digest(staged) != digest:
                        raise ValueError(
                            f"Destination policy changed while copying {source}"
                        )
                    try:
                        staged.rename(destination)
                    except OSError:
                        if not destination.is_dir():
                            raise
            if policy_snapshot_digest(destination) != digest:
                raise ValueError(
                    f"Accepted destination snapshot is corrupt: {destination}"
                )
            if policy_snapshot_digest(source) != digest:
                raise ValueError(f"Destination policy changed while accepting {source}")
        except (OSError, ValueError) as error:
            return self.model_copy(
                update={"source": str(source), "error": str(error), "runtime": runtime}
            )
        return self.model_copy(
            update={
                "source": str(source),
                "runtime": runtime,
                "snapshot": str(destination),
                "digest": digest,
                "error": "",
            }
        )


class GrantedCheckout(BaseModel, frozen=True):
    """The exact directory grant and the Git worktree that owns it."""

    checkout: Path
    scope: Path
    writable: bool


class RepositoryPolicyAuthority(BaseModel, frozen=True, extra="forbid"):
    """An explicitly mounted bare repository's scope for later worktree acceptance."""

    repository: str
    root: str
    runtime: str


def destination_authorities(
    accessible: list[AccessibleRoot], runtime: str
) -> list[RepositoryPolicyAuthority]:
    """Retain original explicit repository roots, never mounts inferred from Git."""

    def authority(declared: AccessibleRoot) -> RepositoryPolicyAuthority | None:
        if not declared.writable or not declared.path.is_dir():
            return None
        try:
            bare = git.out(
                "-C", str(declared.path), "rev-parse", "--is-bare-repository"
            ).strip()
            if bare != "true":
                return None
            layout = repository_layout(declared.path)
        except sh.ErrorReturnCode:
            return None
        return RepositoryPolicyAuthority(
            repository=str(layout.common.resolve()),
            root=str(declared.path.resolve()),
            runtime=runtime,
        )

    return [row for declared in accessible if (row := authority(declared)) is not None]


def granted_checkouts(root: AccessibleRoot) -> list[GrantedCheckout]:
    """Resolve a named directory through Git, without scanning unrelated children."""
    path = root.path.resolve()
    if not path.is_dir():
        return []
    try:
        layout = repository_layout(path)
        bare = git.out("-C", str(path), "rev-parse", "--is-bare-repository").strip()
        if bare == "true":
            return [
                GrantedCheckout(
                    checkout=checkout.resolve(),
                    scope=checkout.resolve(),
                    writable=root.writable,
                )
                for checkout in sibling_worktrees(path)
                if (checkout / ".git").exists()
                and repository_layout(checkout).common.resolve()
                == layout.common.resolve()
            ]
        checkout = Path(
            git.out("-C", str(path), "rev-parse", "--show-toplevel").strip()
        ).resolve()
        return [GrantedCheckout(checkout=checkout, scope=path, writable=root.writable)]
    except sh.ErrorReturnCode:
        return []


def accept_destination_policies(
    root: Path,
    accessible: list[AccessibleRoot],
    lease: Lease,
    runtime: str,
) -> list[DestinationPolicy]:
    """Record exact repository identities separately from broad execution mounts."""
    grants = [grant for declared in accessible for grant in granted_checkouts(declared)]
    return [
        DestinationPolicy(
            repository=str(repository_layout(checkout).common.resolve()),
            checkout=str(checkout),
            writable_roots=sorted(
                {
                    str(grant.scope)
                    for grant in grants
                    if grant.checkout == checkout and grant.writable
                }
            ),
            read_only_roots=sorted(
                {
                    *[
                        str(grant.scope)
                        for grant in grants
                        if grant.checkout == checkout
                        and not grant.writable
                        and not any(
                            other.checkout == checkout
                            and other.scope == grant.scope
                            and other.writable
                            for other in grants
                        )
                    ],
                    *[str(path.resolve()) for path in lease.read_only],
                }
            ),
        ).accepted(root.resolve(), runtime)
        for checkout in sorted({grant.checkout for grant in grants})
    ]
