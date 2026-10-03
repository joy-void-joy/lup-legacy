"""Agent version display, changelog, and bump.

The agent version (``[tool.lup] agent_version``) tracks the behavior of the
agent the feedback loop improves, not the package release. ``changelog``
answers the question a version bump needs: *what changed in agent behavior
since the last tag?* — so a reviewer can decide whether the next bump is patch,
minor, or major, and ``bump`` then writes the new version and tags the commit.

Examples::

    $ uv run lup-devtools version
    $ uv run lup-devtools version --json
    $ uv run lup-devtools version changelog
    $ uv run lup-devtools version changelog --json
    $ uv run lup-devtools version bump minor
    $ uv run lup-devtools version bump minor "What changed" -d "one" -d "another"
"""

import datetime as dt
from pathlib import Path
from typing import Annotated, Literal, TypedDict

import sh
import typer
from tomlkit import TOMLDocument

from lup.execution.git import GitError, Repository
from lup.formats.toml import edited_manifest
from lup.workspace.history import parse_semver
from lup.workspace.paths import agent_version

from lup.devtools.changelog import Changelog, ReleaseNote
from lup.devtools.subapps import subapp
from lup.devtools.utils import output_json, short_sha
from lup.execution.shell import git


ChangelogCategory = Literal["behavior", "data", "infrastructure"]

app = typer.Typer(invoke_without_command=True, no_args_is_help=False)
SUBAPP = subapp("version", "Agent version, changelog, and bump", app)


class VersionInfo(TypedDict):
    version: str
    latest_tag: str | None
    commits_since_tag: int
    files_changed: list[str]


class ChangelogEntry(TypedDict):
    sha: str
    message: str
    category: str


class ChangelogReport(TypedDict):
    since_tag: str | None
    behavior: list[ChangelogEntry]
    data: list[ChangelogEntry]
    infrastructure: list[ChangelogEntry]


DEFAULT_BEHAVIOR_PREFIXES = ("feat", "fix", "refactor")
"""Commit types whose changes an agent's behaviour can be read from.

A default rather than a rule: the vocabulary is a project's own, and one that
spells its types differently classifies its changelog by passing its own."""

DEFAULT_DATA_PREFIXES = ("data",)
"""Commit types that carry generated output rather than a change to it."""


def get_latest_tag() -> str | None:
    try:
        return git.out("describe", "--tags", "--abbrev=0", _ok_code=[0])
    except sh.ErrorReturnCode:
        return None


def classify_commit(
    message: str,
    behavior: tuple[str, ...] = DEFAULT_BEHAVIOR_PREFIXES,
    data: tuple[str, ...] = DEFAULT_DATA_PREFIXES,
) -> ChangelogCategory:
    """Bucket a commit by the ``type(scope):`` prefix of its message.

    Commits in this repo follow the conventional ``type(scope): description``
    format (see CLAUDE.md), so the leading type *is* structured metadata, not
    free prose — reading it is parsing a known field, not guessing intent.

    The three buckets answer the only question a version bump asks of the log:
    *did the agent's behavior change, did its data change, or neither?*
    ``feat``/``fix``/``refactor`` change what the agent does (behavior);
    ``data`` changes generated outputs (data); everything else (``docs``,
    ``chore``, ``test``, ``meta``) is infrastructure that does not move the
    agent version. The ontology is deliberately narrow because that is the
    decision it feeds; a richer taxonomy is `git log` itself.
    """
    lower = message.lower()
    if lower.startswith(behavior):
        return "behavior"
    if lower.startswith(data):
        return "data"
    return "infrastructure"


@app.callback()
def show(
    ctx: typer.Context,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Show agent version, latest tag, and commits since last tag."""
    if ctx.invoked_subcommand is not None:
        return

    latest_tag = get_latest_tag()

    def count_commits(rev_range: str) -> int:
        try:
            return Repository(Path.cwd()).count(rev_range)
        except GitError:
            return 0

    if latest_tag:
        ref_since = latest_tag
        commits_since = count_commits(f"{latest_tag}..HEAD")
    else:
        commits_since = count_commits("HEAD")
        try:
            roots = git.lines("rev-list", "--max-parents=0", "HEAD", _ok_code=[0])
            ref_since = roots[0] if roots else "HEAD"
        except sh.ErrorReturnCode:
            ref_since = "HEAD"

    try:
        rows = git.lines("diff", "--name-only", f"{ref_since}..HEAD", _ok_code=[0, 128])
        files_changed = [f for f in rows if f]
    except sh.ErrorReturnCode:
        files_changed = []

    if as_json:
        info: VersionInfo = {
            "version": agent_version(),
            "latest_tag": latest_tag,
            "commits_since_tag": commits_since,
            "files_changed": files_changed,
        }
        output_json(info)
        return

    typer.echo(f"\nAgent version: {agent_version()}")
    if latest_tag:
        typer.echo(f"Latest tag: {latest_tag} (+{commits_since} commits)")
    else:
        typer.echo("Latest tag: (none)")
    if files_changed:
        typer.echo(f"Files changed: {len(files_changed)}")


@app.command("changelog")
def changelog_cmd(
    since: Annotated[
        str | None,
        typer.Option(
            "--since", "-s", help="Tag or commit to start from (default: latest tag)"
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Show changes since a version tag, classified by type."""
    latest_tag = get_latest_tag()
    tag = since or latest_tag
    if not tag:
        roots = git.lines("rev-list", "--max-parents=0", "HEAD", _ok_code=[0])
        tag = roots[0] if roots else "HEAD"

    try:
        log_lines = git.lines("log", "--oneline", f"{tag}..HEAD", _ok_code=[0])
    except sh.ErrorReturnCode:
        typer.echo(f"Could not read log since {tag}")
        raise typer.Exit(1)

    if not log_lines:
        typer.echo(f"No commits since {tag}")
        return

    entries: list[ChangelogEntry] = [
        {"sha": sha, "message": message, "category": classify_commit(message)}
        for sha, _, message in (
            line.partition(" ")  # lup: ignore[string-split] — log line fields
            for line in log_lines
            if line
        )
    ]
    report: ChangelogReport = {
        "since_tag": since or latest_tag,
        "behavior": [e for e in entries if e["category"] == "behavior"],
        "data": [e for e in entries if e["category"] == "data"],
        "infrastructure": [e for e in entries if e["category"] == "infrastructure"],
    }

    if as_json:
        output_json(report)
        return

    tag_display = since or latest_tag or "(root)"
    typer.echo(f"\n=== Changes since {tag_display} ===\n")

    if report["behavior"]:
        typer.echo("Behavior changes:")
        for e in report["behavior"]:
            typer.echo(f"  {short_sha(e['sha'])} {e['message']}")

    if report["data"]:
        typer.echo("\nData changes:")
        for e in report["data"]:
            typer.echo(f"  {short_sha(e['sha'])} {e['message']}")

    if report["infrastructure"]:
        typer.echo("\nInfrastructure changes:")
        for e in report["infrastructure"]:
            typer.echo(f"  {short_sha(e['sha'])} {e['message']}")

    total = (
        len(report["behavior"]) + len(report["data"]) + len(report["infrastructure"])
    )
    typer.echo(f"\nTotal: {total} commits ({len(report['behavior'])} behavior)")


def release_note(version: str, summary: str, details: list[str]) -> ReleaseNote:
    """What a bump would record, dated today.

    Separate from writing it so a dry run shows the entry itself rather than
    a description of one — the summary and its bullets are what a reader is
    deciding about, and the rendering is where they can be damaged.
    """
    return ReleaseNote(
        version=version, date=dt.date.today(), summary=summary, details=details
    )


def write_release_note(
    root: Path, version: str, summary: str | None, details: list[str]
) -> list[Path]:
    """Record this release in the changelog, and say what that wrote.

    A bump with no summary writes nothing and says so by returning nothing:
    the version alone is a fact the manifest already carries, and a changelog
    entry with no sentence in it is worse than the absence of one.
    """
    if summary is None:
        return []
    path = root / "CHANGELOG.md"
    note = release_note(version, summary, details)
    path.write_text(Changelog.read(path).with_note(note).render())
    return [path]


def write_agent_version(pyproject: Path, version: str) -> None:
    """Record a new agent version, changing nothing else in the manifest.

    Raises where the manifest declares no ``[tool.lup]`` table, which is a
    project that has not adopted the version this bumps rather than a
    manifest this could repair.
    """

    def moved(document: TOMLDocument) -> None:
        document["tool"]["lup"]["agent_version"] = version

    edited_manifest(pyproject, moved)


@app.command("bump")
def bump_cmd(
    level: Annotated[
        str | None,
        typer.Argument(help="Bump level: patch, minor, or major"),
    ] = None,
    summary: Annotated[
        str | None,
        typer.Argument(help="One-line summary of what changed, for the changelog"),
    ] = None,
    details: Annotated[
        list[str] | None,
        typer.Option("--detail", "-d", help="One changelog bullet; repeat for several"),
    ] = None,
    no_tag: Annotated[
        bool,
        typer.Option("--no-tag", help="Skip creating a git tag"),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output result as JSON"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would happen"),
    ] = False,
) -> None:
    """Bump agent version, record the release, and create a git tag.

    Each ``--detail`` is one bullet, taken whole. They are separate options
    rather than one delimited string so that a detail may contain whatever
    punctuation its prose needs.
    """
    from lup.workspace.paths import find_project_root, read_agent_version

    root = find_project_root()
    pyproject = root / "pyproject.toml"
    current = read_agent_version(root)

    semver = parse_semver(current)
    if semver is None:
        typer.echo(f"Version {current} is not in X.Y.Z format")
        raise typer.Exit(1)

    major, minor, patch_v = semver.major, semver.minor, semver.patch

    if level is None:
        typer.echo(f"Current version: {current}")
        typer.echo("Specify bump level: patch, minor, or major")
        raise typer.Exit(1)

    if details and summary is None:
        typer.echo("A detail belongs under a summary; give one, or drop --detail")
        raise typer.Exit(1)

    match level:
        case "patch":
            new_version = f"{major}.{minor}.{patch_v + 1}"
        case "minor":
            new_version = f"{major}.{minor + 1}.0"
        case "major":
            new_version = f"{major + 1}.0.0"
        case _:
            typer.echo(f"Unknown bump level: {level}. Use patch, minor, or major.")
            raise typer.Exit(1)

    if dry_run:
        if as_json:
            output_json({"old": current, "new": new_version, "tag": f"v{new_version}"})
        else:
            typer.echo(f"\nWould bump: {current} → {new_version}")
            if summary is not None:
                typer.echo(release_note(new_version, summary, details or []).render())
            typer.echo("Would not tag" if no_tag else f"Would tag: v{new_version}")
        return

    try:
        write_agent_version(pyproject, new_version)
    except (KeyError, TypeError):
        typer.echo("No [tool.lup] agent_version table in pyproject.toml")
        raise typer.Exit(1) from None

    written = [
        pyproject,
        *write_release_note(root, new_version, summary, details or []),
    ]

    git.add(*(str(path) for path in written))
    git.commit("-m", f"chore(version): bump {current} → {new_version}")
    if not no_tag:
        git.tag(f"v{new_version}")

    if as_json:
        output_json({"old": current, "new": new_version, "tag": f"v{new_version}"})
    else:
        typer.echo(f"\nBumped: {current} → {new_version}")
        typer.echo(f"Tag: v{new_version}")
