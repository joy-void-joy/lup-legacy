"""A project built on the scaffold opens lup read-write at its first launch.

The registration `sync.json` ships names lup's repository, so a launch
materializes it and mounts it with nothing set up on the machine -- whether
the project was cloned bare with its worktrees under `tree/`, or plainly,
because neither the registry nor the lease may care which. These read the
real shipped registration where they can, so a registration that stops naming
its repository fails here rather than on somebody's first launch.

Nothing reaches a network. A local bare repository stands in for the forge,
and git's own `insteadOf` carries the forge spelling the registration uses to
it, so the clone records the https origin a real one would -- which is what
the transport rewrite a contained session gets is computed from.
"""

import json
from typing import Any
import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest
import sh

from lup.devtools import sync
from lup.devtools.dev.policy_explain import verdict_for
import lup.launch.session as launch_session
from lup.launch.declaration import LaunchSandbox
from lup.launch.declaration import InnerSandbox, Mount
from lup.providers.claude import Claude
from lup.providers.claude.launch import claude_settings
from lup.providers.codex.launch import writable_root_arguments
from lup.devtools.harness.policy_refresh import refresh_destination_policy
from lup.policy.kernel.diagnostic import devtools, step
from lup.launch.preflight import (
    NONCE_VARIABLE,
    ROOT_VARIABLE,
    LaunchSentinels,
)
from lup.harness import credential
from lup.harness.credential import (
    HttpsTransport,
    RemoteRewrite,
    SshTransport,
    fleet_rewrites,
    parse_remote,
    same_repository,
)
from lup.harness.models import Plugin
from lup.harness.notice import Banner
from lup.policy.assets.host import destination_policy_binding
from lup.sandbox.rail import AccessibleRoot, fleet_lease
from lup_template.harness.catalog import declared_hook_set
from tests.unit.repos import commit_file, initialized_repo

SHIPPED = sync.load_json(Path("sync.json"))
"""The registration this checkout ships, read where the scaffold keeps it."""

LAYOUTS = ["bare", "plain"]
"""The two ways a project generated from the template is cloned."""


def settings_read(agent: Claude, tree: Path) -> dict[str, Any]:
    """The settings document a launch of ``agent`` carries, read back as a CLI reads it."""
    return json.loads(json.dumps(claude_settings(agent, tree)))


def shipped_lup() -> sync.ProjectEntry:
    """The lup entry of the shipped registration."""
    return next(entry for entry in SHIPPED["projects"] if entry["name"] == "lup")


SHIPPED_URL = shipped_lup().get("url", "")
"""The repository the shipped lup entry names."""


def forge_spelling(monkeypatch: pytest.MonkeyPatch, url: str, local: Path) -> None:
    """Make ``url`` reach ``local``, and nothing else rewrite anything.

    Set through the environment git reads above every file, the way a
    contained session's own rewrites arrive, and replacing whatever this
    session carries: an ambient ``insteadOf`` would answer the question these
    tests ask before the code under test could.
    """
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{local}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", url)


def no_rewrites(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read every remote exactly as it was written."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A bare repository standing in for lup's own, with `main` and `dev`."""
    work = tmp_path / "upstream-work"
    git = initialized_repo(work, tmp_path / "hooks")
    commit_file(git, work, "pyproject.toml", "[tool.lup]\n", "lup")
    git("branch", "dev")
    bare = tmp_path / "upstream.git"
    sh.git("clone", "--quiet", "--bare", str(work), str(bare), _tty_out=False)
    return bare


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The per-user clone cache, moved somewhere a test may write."""
    root = tmp_path / "cache"
    monkeypatch.setattr(sync, "cache_dir", lambda: root)
    return root


def project(
    tmp_path: Path,
    layout: str,
    registry: sync.SyncConfig,
    manifest: str = '[tool.lup]\nagent_version = "0.1.0"\n',
    name: str = "project",
) -> Path:
    """A project generated from the template, published, and cloned by `layout`.

    Its history is published to a bare repository of its own first, and the
    checkout is cloned from there, which is what somebody does after "Use this
    template": bare with its worktree under `tree/main`, or plainly. The
    manifest declares no scaffold flag by default, which is a project that has
    been initialized.
    """
    seed = tmp_path / f"{name}-seed"
    git = initialized_repo(seed, tmp_path / "hooks")
    commit_file(git, seed, "pyproject.toml", manifest, "initial")
    commit_file(git, seed, "sync.json", json.dumps(registry), "registry")
    published = tmp_path / f"{name}-forge.git"
    sh.git("clone", "--quiet", "--bare", str(seed), str(published), _tty_out=False)
    match layout:
        case "bare":
            clone = tmp_path / f"{name}.git"
            sh.git("clone", "--quiet", "--bare", str(published), str(clone))
            checkout = clone / "tree" / "main"
            sh.git(
                "-C", str(clone), "worktree", "add", "--quiet", str(checkout), "main"
            )
            return checkout
        case _:
            checkout = tmp_path / name
            sh.git("clone", "--quiet", str(published), str(checkout))
            return checkout


def standing_in(monkeypatch: pytest.MonkeyPatch, checkout: Path) -> None:
    """Run the registry as it runs from inside this checkout."""
    monkeypatch.setattr(sync, "project_root", lambda: checkout)


def settled_launch(
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
    roots: list[AccessibleRoot],
    sandbox: LaunchSandbox = LaunchSandbox.OUTER,
    runtime: str = "codex",
) -> str:
    """Settle a launch's boundary from ``checkout`` over ``roots``; its nonce.

    The step a launch takes between resolving its roots and opening the
    session, which is where the ledger `harness policy-refresh` and every
    dispatcher read is written: the lease with its read-only holes, the
    policy accepted for every checkout granted, and the repositories a later
    worktree may be accepted beneath.
    """
    sentinels = LaunchSentinels()
    plugin = Plugin(
        id="test.upstream",
        name="test",
        marketplace="test",
        version="1.0.0",
        description="The launch a refresh extends",
        skills=[],
        agents=[],
    )
    launch_session.settle_boundary(
        checkout,
        plugin.hooks,
        sandbox,
        [],
        sentinels,
        {},
        Banner(),
        roots,
        runtime=runtime,
    )
    return sentinels.nonce


def cut_with_policy(repository: Path, branch: str) -> Path:
    """A worktree cut under ``tree/`` from ``main``, carrying a generated policy.

    What `/lup:upstream` has done before editing a new worktree -- both
    native trees generated there -- reduced to the one evaluator a refresh
    accepts.
    """
    checkout = repository / "tree" / branch
    sync.git_in(
        str(repository), "worktree", "add", "-q", "-b", branch, str(checkout), "main"
    )
    hooks = checkout / ".codex" / "plugins" / "lup" / "hooks"
    (hooks / "scripts").mkdir(parents=True)
    (hooks / "runtime").mkdir()
    (hooks / "scripts" / "policy_evaluator.py").write_text("print('inert')\n")
    (hooks / "runtime" / "policy_data.py").write_text("EDIT_LIMIT = 3\n")
    return checkout


def test_the_shipped_registration_names_its_repository_over_https() -> None:
    """The one fact every project built on the scaffold inherits and cannot set.

    https because the clone is made on the host before any credential is
    lent, and https reads a public repository with none; a session's remotes
    are rewritten onto the transport its credential reaches afterwards.
    """
    lup = shipped_lup()
    address = parse_remote(lup.get("url", ""))

    assert address is not None and address.proxied
    assert lup.get("required") is True
    assert lup.get("mount") == "rw"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_fresh_project_mounts_the_shipped_registration_read_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream: Path,
    cache: Path,
    layout: str,
) -> None:
    """No `sync setup`, no `sync remote`: the launch's roots already hold lup.

    The whole clone, as one writable mount with the two paths whose contents
    run on the host held read-only inside it -- so a worktree cut there after
    the launch is writable too -- while `refs/lup` names the worktree of its
    default branch.
    """
    checkout = project(tmp_path, layout, SHIPPED)
    standing_in(monkeypatch, checkout)
    forge_spelling(monkeypatch, SHIPPED_URL, upstream)
    said: list[str] = []

    roots = sync.accessible_roots(said.append)

    clone = cache / "lup.git"
    opened = clone / "tree" / "main"
    assert [(root.path, root.writable) for root in roots] == [(clone, True)]
    assert (checkout / "refs" / "lup").resolve() == opened
    no_rewrites(monkeypatch)
    assert sync.git_in(str(opened), "remote", "get-url", "origin") == SHIPPED_URL
    lease = fleet_lease(checkout, roots)
    assert [path for path in lease.writable if path.is_relative_to(cache)] == [clone]
    assert sorted(path for path in lease.read_only if path.is_relative_to(cache)) == [
        clone / "config",
        clone / "hooks",
    ]
    assert lease.writable_at(opened / "pyproject.toml")
    assert lease.writable_at(clone / "tree" / "cut-later" / "pyproject.toml")
    assert not lease.writable_at(clone / "hooks" / "pre-commit")


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_worktree_cut_in_the_clone_after_launch_is_judged_by_its_own_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream: Path,
    cache: Path,
    layout: str,
) -> None:
    """What `/lup:upstream` needs, with no `--mount` on the launch.

    The launch records the clone as an explicit writable repository, so the
    operator's `harness policy-refresh` accepts a branch cut there afterwards
    -- and an edit in it is then routed to that worktree's own generated
    policy rather than judged by this project's.
    """
    monkeypatch.delenv(NONCE_VARIABLE, raising=False)
    checkout = project(tmp_path, layout, SHIPPED)
    standing_in(monkeypatch, checkout)
    forge_spelling(monkeypatch, SHIPPED_URL, upstream)
    nonce = settled_launch(
        monkeypatch, checkout, sync.accessible_roots(lambda _said: None)
    )
    clone = cache / "lup.git"
    fix = cut_with_policy(clone, "fix-later")

    accepted = refresh_destination_policy(checkout, nonce, fix)

    assert (accepted.checkout, accepted.repository) == (str(fix), str(clone))
    assert accepted.writable_roots == [str(fix)]
    assert not accepted.error
    monkeypatch.setenv(NONCE_VARIABLE, nonce)
    monkeypatch.setenv(ROOT_VARIABLE, str(checkout))
    binding = destination_policy_binding(str(fix / "pyproject.toml"), checkout)
    assert json.loads(binding)["checkout"] == str(fix)
    assert (checkout / "refs" / "lup").resolve() == clone / "tree" / "main"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_materialized_https_clone_is_rewritten_onto_the_sessions_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream: Path,
    cache: Path,
    layout: str,
) -> None:
    """How a session pushes to lup with the operator's own key.

    The clone is walked with every other root the session opens, so an ssh
    credential gets the https spelling rewritten onto `git@host:`, and a
    token leaves it as it is -- the push goes out on whichever the launch
    selected, never on the spelling the registration was committed in.
    """
    checkout = project(tmp_path, layout, SHIPPED)
    standing_in(monkeypatch, checkout)
    forge_spelling(monkeypatch, SHIPPED_URL, upstream)
    roots = sync.accessible_roots(lambda _said: None)
    no_rewrites(monkeypatch)
    monkeypatch.setattr(credential, "resolved_host", lambda alias: alias)
    opened = [checkout, *(root.path for root in roots)]

    assert fleet_rewrites(opened, "github.com", SshTransport()) == [
        RemoteRewrite(spelling="https://github.com/", target="git@github.com:")
    ]
    assert fleet_rewrites(opened, "github.com", HttpsTransport()) == []


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_scaffold_mounts_nothing_for_the_entry_it_ships(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream: Path,
    cache: Path,
    layout: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Lup itself, or a fork of it, is the repository the entry names.

    `[tool.lup] template = true` says so without anybody writing an opt-out
    into a machine's local file: nothing is cloned, mounted or reported
    missing, and `sync status` says why the row has nothing to fetch.
    """
    checkout = project(
        tmp_path, layout, SHIPPED, manifest="[tool.lup]\ntemplate = true\n"
    )
    standing_in(monkeypatch, checkout)
    forge_spelling(monkeypatch, SHIPPED_URL, upstream)
    said: list[str] = []

    assert sync.accessible_roots(said.append) == []
    sync.fetch_cmd(None)
    sync.status_cmd()

    assert said == []
    assert not cache.exists()
    assert "shipped by this scaffold" in capsys.readouterr().out


def test_a_checkout_of_the_registered_repository_owes_itself_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream: Path,
    cache: Path,
) -> None:
    """Read off origin, so no flag and no local opt-out is needed to say it."""
    checkout = project(tmp_path, "bare", SHIPPED)
    sh.git("-C", str(checkout), "remote", "set-url", "origin", SHIPPED_URL)
    standing_in(monkeypatch, checkout)
    no_rewrites(monkeypatch)

    assert sync.exemption(sync.find_project("lup")) == (
        "this checkout is that repository"
    )
    sync.fetch_cmd(None)
    assert not cache.exists()


def test_a_machine_pushing_over_ssh_repoints_the_clone_it_already_has(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream: Path,
    cache: Path,
) -> None:
    """On the host no launch rewrites a remote, so the clone's origin is the push.

    The shipped entry cloned it over https; a machine that pushes lup with its
    own key says so once, and the clone it already made follows.
    """
    address = parse_remote(SHIPPED_URL)
    assert address is not None
    ssh = f"git@{address.host}:{address.repository}.git"
    checkout = project(tmp_path, "plain", SHIPPED)
    standing_in(monkeypatch, checkout)
    forge_spelling(monkeypatch, SHIPPED_URL, upstream)
    sync.accessible_roots(lambda _said: None)
    no_rewrites(monkeypatch)

    sync.set_remote("lup", ssh)

    clone = cache / "lup.git"
    assert sync.git_in(str(clone), "config", "--get", "remote.origin.url") == ssh
    assert sync.transport_url(sync.find_project("lup")) == ssh


def test_a_checkpoint_outlives_the_spelling_its_clone_was_read_over(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream: Path,
    cache: Path,
) -> None:
    """Recorded inside a container, read on the host.

    `/lup:init` baselines the checkpoint in a contained session, whose
    rewrites answer the clone's origin over ssh; the host reads it over
    https. One repository and one ref, so one review -- where comparing the
    spellings would report every commit up to it as never reviewed.
    """
    address = parse_remote(SHIPPED_URL)
    assert address is not None
    checkout = project(tmp_path, "plain", SHIPPED)
    standing_in(monkeypatch, checkout)
    forge_spelling(monkeypatch, SHIPPED_URL, upstream)
    sync.accessible_roots(lambda _said: None)
    no_rewrites(monkeypatch)
    clone = cache / "lup.git"
    reviewed = sync.git_in(str(clone), "rev-parse", "refs/remotes/origin/main")
    ssh = f"git@{address.host}:{address.repository}.git"
    sync.git_in(str(clone), "remote", "set-url", "origin", ssh)
    sync.mark_synced("lup", at=reviewed)
    sync.git_in(str(clone), "remote", "set-url", "origin", SHIPPED_URL)

    found = sync.existing_upstream(sync.find_project("lup"))

    assert found is not None
    assert sync.checkpoint(sync.find_project("lup"), found) == reviewed


def test_a_clone_another_project_made_is_mounted_and_linked_in_this_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream: Path,
    cache: Path,
) -> None:
    """The cache is per machine, so the second project finds lup already there.

    Nothing is cloned for it, and `refs/lup` -- what `/lup:upstream` opens --
    still has to resolve in it.
    """
    forge_spelling(monkeypatch, SHIPPED_URL, upstream)
    first = project(tmp_path, "bare", SHIPPED, name="first")
    standing_in(monkeypatch, first)
    sync.accessible_roots(lambda _said: None)
    no_rewrites(monkeypatch)
    second = project(tmp_path, "plain", SHIPPED, name="second")
    standing_in(monkeypatch, second)

    roots = sync.accessible_roots(lambda _said: None)

    assert [root.path for root in roots] == [cache / "lup.git"]
    assert (second / "refs" / "lup").resolve() == cache / "lup.git" / "tree" / "main"


def test_the_library_registration_follows_its_git_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One source of truth: the repository the project consumes lup from.

    The shipped `url` stays in `sync.json` for a project that resolves a
    release or keeps a vendored copy; a git pin supersedes it rather than
    being restated beside it, and a refusal names the pin and its command.
    """
    pinned = "https://forge.example/fork/lup"
    checkout = project(
        tmp_path,
        "plain",
        SHIPPED,
        manifest=(
            '[tool.lup]\nagent_version = "0.1.0"\n\n'
            f'[tool.uv.sources]\nlup-agents = {{ git = "{pinned}", branch = "dev" }}\n'
        ),
    )
    standing_in(monkeypatch, checkout)
    no_rewrites(monkeypatch)

    assert sync.find_project("lup").get("url") == pinned
    assert sync.declaring_file("lup", "url").name == "pyproject.toml"
    assert sync.renaming("lup", "https://forge.example/other/lup") == step(
        "repoint the pin it follows",
        devtools(
            "dev",
            "library",
            "git",
            "--url",
            "https://forge.example/other/lup",
            "--branch",
            "dev",
        ),
    )


def test_a_registration_nothing_places_falls_back_to_its_distribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: Path, cache: Path
) -> None:
    """A project made before the shipped entry named its repository.

    Root files are the project's own from the first day, so its `sync.json`
    never receives the url; the installed library says where it comes from
    instead -- asked under the distribution's name, which is not the
    registration's. A registration this machine placed keeps its own answer.
    """
    checkout = project(
        tmp_path,
        "bare",
        {"projects": [{"name": "lup", "required": True, "mount": "rw"}]},
    )
    standing_in(monkeypatch, checkout)
    no_rewrites(monkeypatch)
    declared = {"lup-agents": str(upstream)}
    monkeypatch.setattr(sync, "distribution_repository", lambda name: declared[name])

    assert sync.find_project("lup").get("url") == str(upstream)
    assert [root.path for root in sync.accessible_roots(lambda _said: None)] == [
        cache / "lup.git"
    ]

    (checkout / "sync.json.local").write_text(
        json.dumps({"projects": [{"name": "lup", "path": str(tmp_path)}]})
    )
    assert "url" not in sync.find_project("lup")


def test_the_source_label_is_read_as_the_standard_spells_it() -> None:
    """PEP 753 folds `Source`, `Repository` and their spellings into one label."""
    assert same_repository(
        sync.distribution_repository("httpx"), "https://github.com/encode/httpx"
    )
    assert sync.distribution_repository("no-such-distribution-installed") == ""


def test_a_bare_registration_links_refs_to_its_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: Path
) -> None:
    """`refs/<name>` is a working tree whatever the registration named.

    So `uv run --directory refs/lup` runs lup's own tooling for a path
    registration of a bare clone as it does for a materialized one.
    """
    checkout = project(tmp_path, "plain", {"projects": []})
    standing_in(monkeypatch, checkout)
    no_rewrites(monkeypatch)
    attached = upstream / "tree" / "main"
    sh.git("-C", str(upstream), "worktree", "add", "--quiet", str(attached), "main")

    sync.setup_project("lup", str(upstream), mount="rw")

    assert (checkout / "refs" / "lup").resolve() == attached


def test_a_checkout_this_machine_keeps_is_mounted_at_its_working_tree_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: Path
) -> None:
    """A registration naming a path keeps the narrower grant.

    The checkout is somebody's own and so may be its other worktrees, so the
    launch mounts the working tree it resolves to and records no repository
    a later worktree could be accepted beneath: one cut beside it is refused
    by `harness policy-refresh` until a launch names the bare directory.
    """
    monkeypatch.delenv(NONCE_VARIABLE, raising=False)
    checkout = project(tmp_path, "plain", {"projects": []})
    standing_in(monkeypatch, checkout)
    no_rewrites(monkeypatch)
    attached = upstream / "tree" / "main"
    sh.git("-C", str(upstream), "worktree", "add", "--quiet", str(attached), "main")
    sync.setup_project("lup", str(upstream), mount="rw")
    roots = sync.accessible_roots(lambda _said: None)
    nonce = settled_launch(monkeypatch, checkout, roots)
    beside = cut_with_policy(upstream, "fix-later")

    assert [(root.path, root.writable) for root in roots] == [(attached, True)]
    with pytest.raises(ValueError, match="No recorded writable repository authority"):
        refresh_destination_policy(checkout, nonce, beside)


POSTURES = {
    "inner": (LaunchSandbox.INNER, True),
    "none": (LaunchSandbox.NONE, False),
}
"""The host postures, and whether each has the runtime's own sandbox vouched for."""


def mounted_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: Path, cache: Path
) -> tuple[Path, Path, list[AccessibleRoot]]:
    """A project whose registry mounted lup's cache clone, with both dispatchers.

    The clone is materialized and declared by the registry itself, the way a
    launch declares it, and carries an armed hook for an in-place rewrite to
    reach; the generated dispatchers are the ones this checkout ships.
    """
    checkout = project(tmp_path, "plain", SHIPPED)
    standing_in(monkeypatch, checkout)
    forge_spelling(monkeypatch, SHIPPED_URL, upstream)
    roots = sync.accessible_roots(lambda _said: None)
    no_rewrites(monkeypatch)
    clone = cache / "lup.git"
    (clone / "hooks").mkdir(exist_ok=True)
    (clone / "hooks" / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    for runtime in ("claude", "codex"):
        shutil.copytree(
            Path(f".{runtime}/plugins/lup/hooks"),
            checkout / f".{runtime}/plugins/lup/hooks",
        )
    return checkout, clone, roots


def dispatched(
    runtime: str,
    checkout: Path,
    call: tuple[str, dict[str, str]],  # lup: ignore[dict-str-payload] — a tool input
    nonce: str,
    sandboxed: bool,
) -> str:
    """What one runtime's generated dispatcher answers for one tool call."""
    tool, tool_input = call
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("LUP_")
    }
    environment.update(
        {
            NONCE_VARIABLE: nonce,
            ROOT_VARIABLE: str(checkout),
            "PLUGIN_DATA": str(checkout.parent / f"plugin-data-{runtime}"),
            **({"LUP_SANDBOX_ACTIVE": "1"} if sandboxed else {}),
        }
    )
    payload = {
        "session_id": "control-files",
        "cwd": str(checkout),
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }
    result = sh.Command(sys.executable)(
        "-I",
        "-S",
        str(checkout / f".{runtime}/plugins/lup/hooks/scripts/policy.py"),
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env=environment,
        _cwd=str(checkout),
    )
    assert isinstance(result, sh.RunningCommand)
    if result.exit_code == 2:
        return "deny"
    if not str(result).strip():
        return "allow"
    answer = json.loads(str(result))
    return answer.get("hookSpecificOutput", {}).get("permissionDecision", "allow")


def control_writes(clone: Path, source: Path) -> dict[str, str]:
    """Every spelling of a write into the clone's `hooks/` and its `config`."""
    hook = shlex.quote(str(clone / "hooks" / "post-checkout"))
    armed = shlex.quote(str(clone / "hooks" / "pre-push"))
    config = shlex.quote(str(clone / "config"))
    copied = shlex.quote(str(source))
    return {
        "redirection": f"echo x > {hook}",
        "copy": f"cp {copied} {hook}",
        "move": f"mv {copied} {hook}",
        "tee": f"echo x | tee {hook}",
        "in-place sed": f"sed -i 's/exit 0/exit 1/' {armed}",
        "symlink": f"ln -s {copied} {hook}",
        "config append": f"printf '[core]\\n\\thooksPath = /x\\n' >> {config}",
        "config copy": f"cp {copied} {config}",
    }


def git_config_writes(clone: Path) -> dict[str, str]:
    """The config keys that name a program, set through git itself."""
    return {
        "this project": "git config core.hooksPath /x",
        "the clone": f"git -C {shlex.quote(str(clone))} config core.hooksPath /x",
        "its worktree": (
            f"git -C {shlex.quote(str(clone / 'tree' / 'main'))} config alias.x '!sh'"
        ),
    }


def tool_edits(
    runtime: str, clone: Path
) -> list[tuple[str, dict[str, str]]]:  # lup: ignore[dict-str-payload] — tool inputs
    """The runtime's own file-writing tool, pointed at the hook and the config."""
    hook = clone / "hooks" / "post-checkout"
    config = clone / "config"
    if runtime == "claude":
        return [
            ("Write", {"file_path": str(hook), "content": "#!/bin/sh\n"}),
            ("Write", {"file_path": str(config), "content": "[core]\n"}),
            (
                "Edit",
                {
                    "file_path": str(config),
                    "old_string": "[core]",
                    "new_string": "[core]\n\thooksPath = /x",
                },
            ),
        ]
    first = config.read_text().splitlines()[0]
    return [
        (
            "apply_patch",
            {"command": f"*** Begin Patch\n*** Add File: {hook}\n+x\n*** End Patch"},
        ),
        (
            "apply_patch",
            {
                "command": f"*** Begin Patch\n*** Update File: {config}\n@@\n"
                f"-{first}\n+{first}\n+\thooksPath = /x\n*** End Patch"
            },
        ),
    ]


@pytest.mark.parametrize("posture", list(POSTURES))
@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_the_mounted_clone_s_config_and_hooks_are_refused_on_a_host_posture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream: Path,
    cache: Path,
    runtime: str,
    posture: str,
) -> None:
    """What the container's read-only binds refuse, refused by each dispatcher.

    No container stands behind either posture, so the launch's recorded holes
    are all there is: every spelling of a write into `hooks/` or `config`
    is refused, the runtime's own file tools included, a git config write
    naming a program is never allowed, and reading them stays ordinary work.
    The clone sits under the machine's temporary root here, a scratch root
    where a copy is otherwise granted unprompted -- the refusal is the
    measurement's, so where the clone sits cannot move it.
    """
    checkout, clone, roots = mounted_clone(tmp_path, monkeypatch, upstream, cache)
    sandbox, sandboxed = POSTURES[posture]
    nonce = settled_launch(monkeypatch, checkout, roots, sandbox, runtime)

    # lup: ignore[dict-str-payload]
    def answer(call: tuple[str, dict[str, str]]) -> str:
        return dispatched(runtime, checkout, call, nonce, sandboxed)

    written = {
        label: answer(("Bash", {"command": command}))
        for label, command in control_writes(clone, checkout / "pyproject.toml").items()
    }
    configured = {
        label: answer(("Bash", {"command": command}))
        for label, command in git_config_writes(clone).items()
    }
    edited = [answer(call) for call in tool_edits(runtime, clone)]
    read = [
        answer(("Bash", {"command": f"cat {shlex.quote(str(clone / 'config'))}"})),
        answer(("Bash", {"command": f"ls {shlex.quote(str(clone / 'hooks'))}"})),
    ]

    assert written == dict.fromkeys(written, "deny")
    assert edited == ["deny"] * len(edited)
    assert set(configured.values()) <= {"ask", "deny"}
    assert read == ["allow", "allow"]


def test_dev_policy_answers_those_writes_as_the_dispatchers_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: Path, cache: Path
) -> None:
    """The preview a session is sent to before spending a turn agrees with them."""
    checkout, clone, roots = mounted_clone(tmp_path, monkeypatch, upstream, cache)
    nonce = settled_launch(monkeypatch, checkout, roots, LaunchSandbox.INNER, "claude")
    monkeypatch.setenv(NONCE_VARIABLE, nonce)
    monkeypatch.setenv(ROOT_VARIABLE, str(checkout))

    def effects(subject: str, kind: str) -> set[str]:
        verdict = verdict_for(subject, kind, False, checkout, declared_hook_set())
        return {reading.effect for reading in verdict.readings}

    writes = control_writes(clone, checkout / "pyproject.toml")
    for label, command in writes.items():
        assert effects(command, "shell") == {"deny"}, label
    for path in (clone / "hooks" / "post-checkout", clone / "config"):
        assert effects(str(path), "edit") == {"deny"}
    for label, command in git_config_writes(clone).items():
        assert effects(command, "shell") <= {"ask", "deny"}, label
    assert effects(f"cat {shlex.quote(str(clone / 'config'))}", "shell") == {"allow"}


def test_neither_runtime_s_host_sandbox_can_write_the_clone_s_config_or_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: Path, cache: Path
) -> None:
    """The same holes, where the runtime's own sandbox is the wall.

    Claude's widening admits the whole clone and denies the two paths the
    container binds read-only, since a deny holds inside a wider allow.
    Codex's cannot hold one inside a root and does not recognise a bare
    repository, so it admits the clone's worktrees and not its git directory.
    """
    checkout, clone, roots = mounted_clone(tmp_path, monkeypatch, upstream, cache)
    tree = checkout / "tree"
    claude = settings_read(
        Claude(
            policy=declared_hook_set(),
            sandbox=InnerSandbox(
                mounts=[Mount(path=root.path, writable=root.writable) for root in roots]
            ),
        ),
        tree,
    )
    codex = writable_root_arguments(roots, tree)

    filesystem = claude["sandbox"]["filesystem"]
    assert str(clone) in filesystem["allowWrite"]
    assert sorted(filesystem["denyWrite"]) == [
        str(clone / "config"),
        str(clone / "hooks"),
    ]
    admitted = json.loads(
        codex[1].removeprefix("sandbox_workspace_write.writable_roots=")
    )
    assert str(clone / "tree" / "main") in admitted
    assert str(clone) not in admitted
