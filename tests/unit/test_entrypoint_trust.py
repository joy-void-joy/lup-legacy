"""The entrypoint trusts the checkout and the repository it belongs to, every start.

Measured on a contained session probe: the runtime asks for the linked
worktree's main repository to be trusted -- `/home/.../lup.git`, not the
worktree under `tree/` -- and, finding only the worktree in the document,
drops the declared `permissions.allow` entries with a notice and refuses the
turn. The document outlives the image in its volume, so a seed written once
on first start cannot carry the second path; it is merged on every start
instead.
"""

from lup.harness.image import Image
from lup.harness.requirements import Manifest


def entrypoint() -> str:
    rendered = Image().dockerfile(Manifest())
    start = rendered.index("COPY <<'ENTRY' /usr/local/bin/lup-entrypoint")
    return rendered[start : rendered.index("\nENTRY\n", start)]


def test_the_repository_root_is_trusted_beside_the_checkout() -> None:
    script = entrypoint()

    assert "rev-parse --path-format=absolute --git-common-dir" in script
    assert '"$config/$trust" "$PWD" "$repository"' in script
    assert 'case "$repository" in */.git) repository=${repository%/.git} ;; esac' in (
        script
    )


def test_trust_is_recorded_on_every_start_by_the_program_holding_the_lock() -> None:
    """For the runtime that names a trust document, never through a shared name.

    The seed and the merge are one program's, so the document is written by
    something that holds a lock and renames a file of its own; what it does
    is `test_trust_seed.py`'s to exercise.
    """
    script = entrypoint()
    guarded = script.index('if [ -n "$trust" ]; then')
    recorded = script.index(
        'python3 /opt/lup/trust-seed.py /opt/lup/trust-seed.json "$config/$trust" '
    )

    assert script.index('trust="${LUP_TRUST_DOCUMENT:-}"') < guarded < recorded
    assert recorded < script.index("fi\n", guarded)
    assert "$trust.lup" not in script
    assert "cp /opt/lup/trust-seed.json" not in script
    assert "COPY <<'TRUST' /opt/lup/trust-seed.py" in Image().dockerfile(Manifest())
