"""Where a project came from: which repository, and which commit of it.

A scaffold ships `sync.json` naming its own repository -- lup's names lup --
which is right for every project generated from it and wrong for one
generated from a fork of it: the registration would mount, review and merge
the copied half from a repository the project was never stamped from. The
forge knows which it was -- GitHub records the template a repository was
generated from -- so initialization asks once and writes the answer where
the project keeps it.

Which commit is the other half, and no forge records it. A clone of the
scaffold carries the scaffold's history, so the commit it stands on is in its
own. A repository made with GitHub's "Use this template" carries none of it:
GitHub copies the template's files into one fresh root commit, with no parent
and no remote naming the template, and what survives of the commit it copied
is the tree. So the commit is read out of the template's history by that tree
(:func:`stamped_from`), and it is what the pin, the upstream checkpoint and
the scaffold branch are all taken at.

Library code, although only initialization calls it, because the template
is copied: a module there is frozen in every project at the moment it was
generated, where one here reaches them through the dependency.

Where the project resolves lup from a repository, the registration follows
that pin rather than its own ``url`` (:func:`lup.devtools.sync.completed`), so
the pin is what has to name the template, and moving a pin is
``dev library git``'s: this says which command moves it rather than writing a
second copy of the answer.
"""

import json
import os
from collections.abc import Callable, Iterator
from itertools import pairwise
from pathlib import Path
from typing import Literal

import sh
import typer
from pydantic import BaseModel

from lup.devtools import sync
from lup.devtools.utils import decode_stderr, gh, short_sha, slug_from_remote
from lup.execution.shell import git
from lup.harness.credential import (
    parse_remote,
    remote_url,
    resolved_host,
    same_repository,
)
from lup.policy.kernel.diagnostic import devtools, spelled
from lup.types import StringMap


class TemplateRepository(BaseModel, frozen=True):
    """The repository GitHub says another was generated from."""

    clone_url: str


class GeneratedRepository(BaseModel, frozen=True):
    """The one field of GitHub's repository document this reads."""

    template_repository: TemplateRepository | None = None


class TemplateOrigin(BaseModel, frozen=True):
    """What the forge answered about where one repository was generated from."""

    repository: str = ""
    """The template's clone URL, empty where it was generated from none."""

    unanswered: str = ""
    """Why the forge could not be asked, empty where it answered."""


def template_origin(origin: str) -> TemplateOrigin:
    """Ask the forge which template the repository at ``origin`` came from.

    The host is the one the remote reaches: an ssh config alias is resolved
    first, because `gh` knows forges by hostname and an alias names none.
    """
    address = parse_remote(origin)
    slug = slug_from_remote(origin)
    if address is None or not slug:
        return TemplateOrigin(
            unanswered=f"the origin remote ({origin or 'none'}) names no forge repository"
        )
    host = address.host if address.proxied else resolved_host(address.host)
    try:
        document = gh.out("api", "--hostname", host, f"repos/{slug}")
    except sh.CommandNotFound:
        return TemplateOrigin(unanswered="gh is not installed")
    except sh.ErrorReturnCode as error:
        return TemplateOrigin(
            unanswered=f"gh api repos/{slug} failed: {decode_stderr(error)}"
        )
    template = GeneratedRepository.model_validate_json(document).template_repository
    return TemplateOrigin(repository=template.clone_url if template is not None else "")


def point_at_template(root: Path, name: str, dry_run: bool) -> bool:
    """Make the ``name`` registration mean the template this project came from.

    Answers whether the registration now means it, or already did. Nothing is
    written where the forge could not be asked, where the project was
    generated from no template, or where a git pin decides which repository
    the registration means -- that last one is said, with the command that
    moves the pin.
    """
    registration = next(
        (entry for entry in sync.load_projects(root) if entry["name"] == name), None
    )
    if registration is None:
        typer.echo(f"No '{name}' registration in sync.json, so none to point.")
        return False
    current = sync.registered_repository(registration)
    found = template_origin(remote_url(root, "origin"))
    if found.unanswered:
        typer.echo(
            f"Could not ask which template this repository was generated from: "
            f"{found.unanswered}. '{name}' stays {current or 'unplaced'}; where "
            f"the template is another repository, set \"url\" on the '{name}' "
            "entry in sync.json to it."
        )
        return False
    if not found.repository:
        typer.echo(
            f"This repository was generated from no template, so '{name}' stays "
            f"{current or 'unplaced'}."
        )
        return True
    if current and same_repository(current, found.repository):
        typer.echo(
            f"'{name}' already means {current}, the template this repository "
            "was generated from."
        )
        return True
    pinned = sync.pinned_source(name, root)
    if pinned is not None:
        repoint = devtools(
            "dev",
            "library",
            "git",
            "--url",
            found.repository,
            f"--{pinned.ref_kind}",
            pinned.ref,
        )
        typer.echo(
            f"'{name}' follows the git pin in pyproject.toml, which names "
            f"{pinned.url}; this repository was generated from "
            f"{found.repository}. To build on it: {spelled(repoint)}"
        )
        return False
    declared = sync.load_json(root / "sync.json")
    if not any(entry["name"] == name for entry in declared["projects"]):
        typer.echo(
            f"sync.json declares no '{name}' registration -- it is this "
            "machine's alone -- so there is no committed one to point."
        )
        return False
    declared["projects"] = [
        sync.PROJECT_ENTRY_ADAPTER.validate_python({**entry, "url": found.repository})
        if entry["name"] == name
        else entry
        for entry in declared["projects"]
    ]
    if not dry_run:
        (root / "sync.json").write_text(json.dumps(declared, indent=2) + "\n")
    typer.echo(
        f"{'Would point' if dry_run else 'Pointed'} '{name}' at "
        f"{found.repository} in sync.json (it meant {current or 'nothing'}); "
        "the next launch materializes and mounts it."
    )
    cached = sync.cached_clone(name)
    held = remote_url(cached, "origin") if cached is not None else ""
    if cached is not None and held and not same_repository(held, found.repository):
        typer.echo(
            f"  {cached} holds a clone of {held}, which is refused under this "
            "name from now on: remove it so the next launch clones the template."
        )
    return True


type StampEvidence = Literal["history", "tree", "nearest"]
"""What names the commit a project was stamped from, strongest first."""


class Stamp(BaseModel, frozen=True):
    """Which upstream commit a project was stamped from, and what says so.

    Three readings, each tried where the one before it found nothing, and each
    weaker than the one before it. A clone of upstream carries upstream's own
    commits, so the newest of them its history shares is the commit it stands
    on. A repository generated from a template carries none, and its root
    commit holding exactly the tree of one upstream commit names that commit.
    Where no upstream commit holds that tree -- the template's history was
    rewritten after the copy, or the root commit was amended -- the nearest is
    the one whose tree differs from the root commit's in the fewest files,
    which is an estimate and says so.
    """

    commit: str
    """The upstream commit, in full."""

    evidence: StampEvidence

    carrier: str
    """This project's commit the reading was taken from: the shared commit
    itself, or the root commit whose tree was compared."""

    differing: int = 0
    """For a nearest reading, how many files differ between the carrier's tree
    and the commit's, a file only one of them holds included."""

    alike: int = 1
    """For a nearest reading, how many upstream commits read as well as this
    one, itself included."""

    def exact(self) -> bool:
        """Whether history or an identical tree names the commit, not a likeness."""
        return self.evidence != "nearest"

    def explained(self) -> str:
        """How the commit was found, in the terms a reader checks it by."""
        match self.evidence:
            case "history":
                return (
                    "This checkout's history holds it: the newest commit it "
                    "shares with upstream."
                )
            case "tree":
                return (
                    f"This repository's root commit {short_sha(self.carrier)} "
                    "holds its tree exactly, which is what GitHub's \"Use this "
                    "template\" copies into a repository's one fresh commit."
                )
            case "nearest":
                ties = (
                    f", as do {self.alike - 1} other commit(s), of which it is "
                    "the newest"
                    if self.alike > 1
                    else ""
                )
                return (
                    f"No upstream commit holds this repository's root tree "
                    f"({short_sha(self.carrier)}) exactly. This one differs "
                    f"from it in {self.differing} file(s){ties}: an estimate, to "
                    "confirm before anything is taken at it."
                )


def asked(repository: Path, names: list[str], field: str) -> StringMap:
    """Each name, and what one ``cat-file --batch-check`` pass answers for it.

    ``field`` is the format atom asked for -- ``objectname``, ``objecttype``.
    A name the repository cannot resolve answers with its own spelling and
    ``missing``, which equals no object id and no type. One pass rather than
    one process per name, because a search asks this of every commit upstream
    holds. Keyed by the name asked, which is a revision in git's own grammar.
    """
    if not names:
        return {}
    answers = git.lines(
        "-C",
        str(repository),
        "cat-file",
        f"--batch-check=%({field})",
        _in="".join(f"{name}\n" for name in names),
    )
    return dict(zip(names, answers, strict=True))


def shared_commit(root: Path, upstream: Path) -> str:
    """The newest commit of this checkout's history that upstream holds too.

    Walked in topological order, so the first one upstream holds has no shared
    descendant: upstream's history is closed under parents, so everything
    beneath the first shared commit is shared as well. Empty where there is
    none, which is a repository whose history upstream has never seen.
    """
    ancestry = git.lines("-C", str(root), "rev-list", "--topo-order", "HEAD")
    kinds = asked(upstream, ancestry, "objecttype")
    return next((commit for commit in ancestry if kinds[commit] == "commit"), "")


class Candidate(BaseModel, frozen=True):
    """One upstream commit a root commit may have been copied from."""

    commit: str
    tree: str
    """The tree it holds, which is all a copy keeps of it."""


class TreeDistance(BaseModel, frozen=True):
    """What differs between a root commit's tree and one upstream tree."""

    tree: str
    """The upstream tree compared."""

    differing: list[str]
    """Every path whose object differs, one only one side holds included."""


def distances(
    root: Path, upstream: Path, tree: str, others: list[str], deep: bool
) -> list[TreeDistance]:
    """What differs between ``tree`` and each of ``others``, in one pass.

    `diff-tree --stdin` over every pair, run in this checkout with upstream's
    objects lent as an alternate, so both sides of a pair are readable without
    copying either and nothing is written into either repository. Top-level
    entries alone unless ``deep``: git compares two trees by the ids of their
    entries, so an unchanged directory costs one comparison however much it
    holds, and a whole history compared at the top stays cheap.

    Git answers each pair with the pair on a line of its own, then the paths
    that differ, so a pair's paths are the lines between its header and the
    next one.
    """
    lent = git.out(
        "-C",
        str(upstream),
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        "objects",
    )
    headers = {f"{tree} {other}": other for other in others}
    output = git.lines(
        "-C",
        str(root),
        "diff-tree",
        "--stdin",
        "--name-only",
        *(["-r"] if deep else []),
        _in="".join(f"{header}\n" for header in headers),
        # lup: ignore[os-environ] — inherited, not read: git keeps its PATH and
        # configuration, and the one name added lends upstream's objects
        _env={**os.environ, "GIT_ALTERNATE_OBJECT_DIRECTORIES": lent},
    )
    starts = [index for index, line in enumerate(output) if line in headers]
    return [
        TreeDistance(tree=headers[output[start]], differing=output[start + 1 : end])
        for start, end in pairwise([*starts, len(output)])
    ]


def tree_match(
    root: Path, carriers: list[str], candidates: list[Candidate]
) -> Stamp | None:
    """The newest upstream commit whose tree one of these root commits holds.

    Newest because two commits holding one tree compile the same library and
    the same copied half, and the later one leaves the review fewer commits
    to read again.
    """
    return next(
        (
            Stamp(commit=candidate.commit, evidence="tree", carrier=carrier)
            for carrier in carriers
            for tree in [git.out("-C", str(root), "rev-parse", f"{carrier}^{{tree}}")]
            for candidate in candidates
            if candidate.tree == tree
        ),
        None,
    )


def nearest(
    root: Path, upstream: Path, carriers: list[str], candidates: list[Candidate]
) -> Stamp | None:
    """The upstream commit whose tree differs least from a root commit's.

    Two passes, because one pass at full depth over a whole history is not
    affordable and one at the top alone cannot tell neighbours apart. The
    first counts differing top-level entries against every candidate, which
    finds the region; the second counts differing files against the ones that
    read best there, which finds the commit. A candidate sharing no top-level
    entry at all is no neighbour of anything and is not measured further.

    The newest of the commits reading best is taken, and how many read as
    well is kept: a field of ties is a weaker answer than a single peak, and
    the reader is owed the difference.
    """

    def readings() -> Iterator[Stamp]:
        """The closest candidates' readings against each root commit."""
        for carrier in carriers:
            tree = git.out("-C", str(root), "rev-parse", f"{carrier}^{{tree}}")
            own = git.lines("-C", str(root), "ls-tree", "--name-only", carrier)
            shallow = {
                reading.tree: len(reading.differing)
                for reading in distances(
                    root, upstream, tree, [one.tree for one in candidates], deep=False
                )
                if any(entry not in reading.differing for entry in own)
            }
            closest = min(shallow.values(), default=None)
            if closest is None:
                continue
            tied = [one for one in candidates if shallow.get(one.tree) == closest]
            files = {
                reading.tree: len(reading.differing)
                for reading in distances(
                    root, upstream, tree, [one.tree for one in tied], deep=True
                )
            }
            for one in tied:
                yield Stamp(
                    commit=one.commit,
                    evidence="nearest",
                    carrier=carrier,
                    differing=files[one.tree],
                )

    scored = list(readings())
    best = min(scored, key=lambda reading: reading.differing, default=None)
    if best is None:
        return None
    alike = sum(reading.differing == best.differing for reading in scored)
    return best.model_copy(update={"alike": alike})


def stamped_from(root: Path, upstream: Path) -> Stamp | None:
    """Which commit of ``upstream`` the checkout at ``root`` was stamped from.

    History first, then the root commit's tree, then the nearest tree (see
    :class:`Stamp`). Every ref upstream holds is searched: a template is
    copied from whatever its default branch held that day, and that branch
    may have moved on or gone since. Nothing where upstream holds no commit
    at all, or none sharing anything with this checkout.
    """
    shared = shared_commit(root, upstream)
    if shared:
        return Stamp(commit=shared, evidence="history", carrier=shared)
    carriers = git.lines("-C", str(root), "rev-list", "--max-parents=0", "HEAD")
    commits = git.lines("-C", str(upstream), "rev-list", "--all")
    trees = asked(upstream, [f"{commit}^{{tree}}" for commit in commits], "objectname")
    candidates = [
        Candidate(commit=commit, tree=trees[f"{commit}^{{tree}}"]) for commit in commits
    ]
    return tree_match(root, carriers, candidates) or nearest(
        root, upstream, carriers, candidates
    )


class BranchStanding(BaseModel, frozen=True):
    """One upstream branch holding the base, and how far past it the branch is."""

    name: str
    tip: str
    """The commit the branch stands at, in full."""

    past: int
    """How many commits the branch holds that the base does not."""

    default: bool = False
    """Whether it is the branch upstream's own HEAD names."""

    def spelled(self) -> str:
        """The branch as one line of the report."""
        named = f"{self.name} (default)" if self.default else self.name
        if not self.past:
            return f"{named}: stands at it"
        return f"{named}: {self.past} commit(s) past it, at {short_sha(self.tip)}"


class Base(BaseModel, frozen=True):
    """A stamp, with what upstream says about the commit it names."""

    stamp: Stamp
    subject: str
    """What that commit says it did."""

    repository: str
    """The repository it was read in, as its clone reaches it."""

    branches: list[BranchStanding]
    """Every branch upstream publishes that holds the commit, default first."""

    default: str = ""
    """Upstream's default branch, empty where its clone names none."""

    def lines(self) -> list[str]:
        """The whole report, the commit in full so nothing retypes it."""
        held = [f"  {branch.spelled()}" for branch in self.branches] or [
            "  none: only a tag, or a branch deleted since, reaches it, so a "
            "pin names it as a revision (dev library git --rev <commit>)."
        ]
        unreviewed = (
            [
                f"{self.default}, the default branch, does not hold it: the base "
                "carries work the stable branch has not reviewed."
            ]
            if self.default and not any(branch.default for branch in self.branches)
            else []
        )
        return [
            f"Base: {self.stamp.commit}  {self.subject}",
            f"  {self.stamp.explained()}",
            f"Branches of {self.repository} holding it:",
            *held,
            *unreviewed,
        ]


def read_base(found: sync.Upstream, stamp: Stamp) -> Base:
    """What upstream's clone says of the commit a stamp names.

    The published branches are the remote-tracking refs where the clone has
    an origin to fetch them from -- what the forge holds, where the clone's
    own branches are whatever a session last left in it -- and its branches
    where it has none.
    """
    checkout = str(found.checkout)
    remote = found.tip.startswith("refs/remotes/origin/")
    namespace = "refs/remotes/origin" if remote else "refs/heads"
    head = f"{namespace}/HEAD" if remote else "HEAD"
    default = git.out(
        "-C", checkout, "symbolic-ref", "--quiet", head, _ok_code=[0, 1, 128]
    )
    refs = git.lines(
        "-C",
        checkout,
        "for-each-ref",
        "--contains",
        stamp.commit,
        "--format=%(refname)",
        namespace,
    )
    branches = [
        BranchStanding(
            name=ref.removeprefix(f"{namespace}/"),
            tip=git.out("-C", checkout, "rev-parse", ref),
            past=int(
                git.out("-C", checkout, "rev-list", "--count", f"{stamp.commit}..{ref}")
            ),
            default=ref == default,
        )
        for ref in refs
        if ref != head
    ]
    return Base(
        stamp=stamp,
        subject=git.out("-C", checkout, "log", "-1", "--format=%s", stamp.commit),
        repository=remote_url(found.checkout, "origin") or checkout,
        branches=sorted(branches, key=lambda branch: not branch.default),
        default=default.removeprefix(f"{namespace}/"),
    )


def report_base(
    root: Path, name: str, report: Callable[[str], None] = typer.echo
) -> bool:
    """Say which commit of the ``name`` registration this project was stamped from.

    The registration is fetched first, and cloned the first time, because the
    answer is read out of upstream's own history. Refused where the forge
    says this repository was generated from another repository than the one
    registered: the base is a commit of the template, and pointing the
    registration there is `dev init upstream`'s. Answers whether the base was
    read exactly, rather than estimated or not found at all.
    """
    registration = next(
        (entry for entry in sync.load_projects(root) if entry["name"] == name), None
    )
    if registration is None:
        report(f"No '{name}' registration in sync.json, so no repository to read from.")
        return False
    registered = sync.registered_repository(registration)
    template = template_origin(remote_url(root, "origin"))
    if template.repository and not same_repository(template.repository, registered):
        report(
            f"This repository was generated from {template.repository}, while "
            f"'{name}' means {registered or 'nothing'}. The base is a commit of "
            "the template, so point the registration at it first: "
            f"{spelled(devtools('dev', 'init', 'upstream'))}"
        )
        return False
    if template.unanswered:
        report(
            f"Could not ask which template this repository was generated from: "
            f"{template.unanswered}. Reading '{name}' as registered: "
            f"{registered or 'unplaced'}."
        )
    found = sync.ensure_local(registration, report)
    stamp = stamped_from(root, found.checkout)
    if stamp is None:
        report(f"{found.checkout} holds no commit sharing anything with this one.")
        return False
    for line in read_base(found, stamp).lines():
        report(line)
    return stamp.exact()
