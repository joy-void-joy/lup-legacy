"""The validated surface for keeping a peer's mail on the record, and its erasure.

The library declares none. Whether this project's sessions coordinate at all
is the coordination module's answer rather than the policy's, so this states
the shape a declaration takes and
:func:`lup.coordination.policy.peer_policy` builds one from the store's own
layout — which is what keeps the directory spelled once, in the module that
owns it, instead of once there and once in a compiled hook.
:mod:`lup.policy.kernel.peers` decides against the erased row.
"""

from pydantic import BaseModel, Field

from lup.policy.kernel.diagnostic import Step
from lup.policy.kernel.rows import PeerPolicyRow


class PeerPolicy(BaseModel, frozen=True):
    """Where this project's sessions find each other, and what a caller is told.

    Carried as one declaration rather than two because it is one subject read
    two ways — reaching a peer, and finding out who there is to reach. A
    project stating only half of it would redirect a send while leaving a
    listing to speak, unqualified, for a population its roster does not hold.

    ``store`` is the roster's directory beneath the repository's shared git
    directory, as path parts. Parts rather than a joined string because the
    dispatcher rebuilds the path with the host's own separator, and a compiled
    literal carrying one platform's answers on one platform.

    What lives *inside* that directory is not declared here. The fold the
    dispatcher reads the store with ships into the plugin beside it and owns
    every file name, every stamp directory and the window a silence is read
    against — so a declaration restating any of them would be the second
    spelling this arrangement exists to remove. ``windows_dir`` stays because
    it is the one place under the store nothing but the dispatcher touches.

    ``server`` is the tool server the coordination verbs are served from,
    which each runtime's caller hook is matched against in that runtime's own
    spelling of a server's tools. Read at generation and compiled into no row:
    the dispatcher never sees a coordination call.
    """

    server: str = Field(min_length=1)
    store: list[str] = Field(min_length=1)
    windows_dir: str = Field(min_length=1)
    member_env: str = Field(min_length=1)
    send_reason: str = Field(min_length=1)
    send_recovery: list[Step] = Field(min_length=1)
    listing_note: str = Field(min_length=1)
    claim_reason: str = Field(min_length=1)
    claim_recovery: list[Step] = Field(min_length=1)


def erase_peer_policy(declared: PeerPolicy | None) -> PeerPolicyRow | None:
    """Erase the validated declaration into the primitive row the kernel reads."""
    if declared is None:
        return None
    return PeerPolicyRow(
        store=list(declared.store),
        windows_dir=declared.windows_dir,
        member_env=declared.member_env,
        send_reason=declared.send_reason,
        send_recovery=declared.send_recovery,
        listing_note=declared.listing_note,
        claim_reason=declared.claim_reason,
        claim_recovery=declared.claim_recovery,
    )
