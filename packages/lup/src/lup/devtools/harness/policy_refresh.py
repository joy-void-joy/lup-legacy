"""Accept changed destination policy bytes for a live, already granted launch."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import typer
import sh
from pydantic import BaseModel, TypeAdapter, ValidationError

from lup.devtools.utils import refuse
from lup.launch.preflight import NONCE_VARIABLE, ledger_path
from lup.execution.shell import git
from lup.policy.snapshots import DestinationPolicy, RepositoryPolicyAuthority
from lup.sandbox.rail import repository_layout


class PolicyLaunchLedger(BaseModel, extra="allow"):
    """The grant field plus boundary measurements preserved without reinterpretation."""

    destination_policies: list[str] = []
    destination_authorities: list[str] = []
    writable_roots: list[str] = []
    read_only_roots: list[str] = []

    def grant_for(self, checkout: Path) -> DestinationPolicy:
        """A new checkout must retain the exact repository and original mount scope."""
        if not checkout.is_dir():
            raise ValueError(
                f"No recorded writable repository authority covers {checkout}"
            )
        try:
            top = Path(
                git.out("-C", str(checkout), "rev-parse", "--show-toplevel").strip()
            ).resolve()
        except sh.ErrorReturnCode as error:
            raise ValueError(f"{checkout} is not a Git worktree") from error
        if top != checkout:
            raise ValueError(f"Use the canonical Git worktree root: {top}")
        repository = str(repository_layout(checkout).common.resolve())
        authorities = [
            RepositoryPolicyAuthority.model_validate_json(row)
            for row in self.destination_authorities
        ]
        matches = [
            row
            for row in authorities
            if row.repository == repository and checkout.is_relative_to(Path(row.root))
        ]
        covering = {
            Path(path): writable
            for paths, writable in (
                (self.writable_roots, True),
                (self.read_only_roots, False),
            )
            for path in paths
            if checkout.is_relative_to(Path(path))
        }
        deepest = max(covering, key=lambda path: len(path.parts), default=None)
        if (
            not matches
            or len({row.runtime for row in matches}) != 1
            or deepest is None
            or not covering[deepest]
        ):
            raise ValueError(
                f"No recorded writable repository authority covers {checkout}; changing repository access requires another launch"
            )
        return DestinationPolicy(
            repository=repository,
            checkout=str(checkout),
            runtime=matches[0].runtime,
            writable_roots=[str(checkout)],
            read_only_roots=self.read_only_roots,
        )


def refresh_destination_policy(
    root: Path, nonce: str, repository: Path
) -> DestinationPolicy:
    """Replace one accepted snapshot without extending that launch's authority."""
    # lup: ignore[os-environ] — reject an operator-only action inherited by the requesting session
    if os.environ.get(NONCE_VARIABLE):
        raise ValueError(
            "Policy snapshot refresh requires an independent operator terminal outside "
            "the agent session. The requesting session cannot accept replacement policy."
        )
    if not nonce or Path(nonce).name != nonce or nonce in {".", ".."}:
        raise ValueError("The launch nonce must be a single ledger name")
    path = ledger_path(root, nonce)
    adapter = TypeAdapter(dict[str, list[str]])
    captured = path.read_text(encoding="utf-8")
    document = PolicyLaunchLedger.model_validate(adapter.validate_json(captured))
    policies = [
        DestinationPolicy.model_validate_json(row)
        for row in document.destination_policies
    ]
    checkout = str(repository.resolve())
    matches = [row for row in policies if row.checkout == checkout]
    if len(matches) > 1:
        raise ValueError(
            f"Launch {nonce} has no unique explicit policy grant for {checkout}; "
            "changing repository access requires another launch."
        )
    existing = matches[0] if matches else document.grant_for(repository.resolve())
    if str(repository_layout(repository).common.resolve()) != existing.repository:
        raise ValueError("Destination Git repository identity changed; launch again")
    if not existing.runtime:
        raise ValueError("Launch grant has no runtime identity; launch again")
    accepted = existing.accepted(root.resolve(), existing.runtime)
    if accepted.error:
        raise ValueError(accepted.error)
    document.destination_policies = [
        (accepted if row.checkout == checkout else row).model_dump_json()
        for row in policies
    ] + ([accepted.model_dump_json()] if not matches else [])
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as staged:
        json.dump(document.model_dump(), staged, indent=2)
        replacement = Path(staged.name)
    if path.read_text(encoding="utf-8") != captured:
        replacement.unlink()
        raise ValueError("The launch ledger changed during refresh; read it and retry")
    replacement.replace(path)
    return accepted


def refresh_command(root: Path, nonce: str, repository: Path) -> None:
    """Report the exact destination revision accepted by the operator."""
    try:
        accepted = refresh_destination_policy(root, nonce, repository)
    except (OSError, ValueError, ValidationError, sh.ErrorReturnCode) as error:
        refuse(str(error), what=str(repository), code=2)
    typer.echo(
        f"Accepted {accepted.checkout} policy {accepted.digest} for launch {nonce}"
    )
