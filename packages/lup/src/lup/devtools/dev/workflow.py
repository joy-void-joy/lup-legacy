"""The continuous-integration workflow that runs a project's own gate.

A generated tree goes stale the moment the library that compiles it moves, and
nothing in a checkout says so until something asks. ``dev check`` asks, so what
CI owes the project is to run it — one job, not a second list of gates that
drifts from the first. Generated rather than scaffolded for the same reason:
a copy handed over once is a list nobody updates.

Both steps are spelled with the same constants the git guards install, so a
contributor who never armed the hooks is refused here by the identical
commands rather than by a second rule about them.
"""

from pathlib import Path

from pydantic import BaseModel

from lup.devtools.dev.git_guards import CHECK_COMMAND, DRIFT_COMMAND
from lup.devtools.dev.release import CANDIDATE_SEGMENT
from lup.formats.banner import GeneratedBanner
from lup.harness.materialization import write_generated_file
from lup.formats.banner import REGENERATE_COMMAND
from lup.formats.yaml import (
    ScalarValue,
    YamlDocument,
    YamlEntry,
    YamlFlow,
    YamlItem,
    YamlList,
    YamlMap,
    YamlScalar,
    scalars,
)
from lup.harness.models import Artifact
from lup.workspace.paths import project_root

RUNNER_IMAGE = "ubuntu-24.04"
"""The runner image a generated job names, pinned rather than `ubuntu-latest`.

A moving label is a change nobody makes: GitHub repoints `ubuntu-latest` at
the next release on a date of its choosing — 2026-10-19 for Ubuntu 26 — and
that lands under this repository and every project generated from it at once,
as a gate that fails with nothing in the checkout having moved. Pinned, the
move is a commit somebody writes, a release says, and an adopter takes when
their own tree is ready for it.
"""

# lup: ignore[constant-declaration] — the directory GitHub Actions itself reads
WORKFLOW_PATH = Path(".github/workflows/quality.yml")
WORKFLOW_COMMAND = REGENERATE_COMMAND
"""What the gate runs to rebuild every tree, taken from the command the banners
already tell a reader to type so the two cannot name different things."""


class WorkflowStep(BaseModel, frozen=True):
    """One step of a generated job, in the keys the forge reads it by.

    Declared rather than written, so a value that reaches a step — a command,
    a workspace path, a version a project pinned — enters the document as a
    value and cannot be the thing that ends the mapping it lands in. A field
    left empty writes no key at all, which is what tells `uses` and `run`
    apart without a second kind of step.
    """

    name: str = ""
    uses: str = ""
    settings: dict[str, ScalarValue] = {}
    """What the action is configured with, written as the `with:` it reads."""

    env: dict[str, ScalarValue] = {}
    """What the step's process is given, written as the `env:` it reads."""

    run: str = ""
    working_directory: str = ""

    comment: str = ""
    """Why the step is there, written above its dash for whoever reads the file."""

    def node(self) -> YamlItem:
        """This step as one dash of the job's sequence, with its reason above."""
        configured = [
            YamlEntry(key="with", value=YamlMap(entries=scalars(self.settings)))
        ]
        environment = [YamlEntry(key="env", value=YamlMap(entries=scalars(self.env)))]
        mapping = YamlMap(
            entries=[
                *scalars({"name": self.name, "uses": self.uses}),
                *(configured if self.settings else []),
                *(environment if self.env else []),
                *scalars(
                    {"run": self.run, "working-directory": self.working_directory}
                ),
            ]
        )
        return YamlItem(value=mapping, comment=self.comment)


class FrontendSpec(BaseModel, frozen=True):
    """A bun workspace the gate has to install before it can check bundles.

    Declared rather than assumed because most projects have none, and the
    one that does needs two steps on the runner — bun itself, and the
    workspace's dependencies from its lockfile — before `dev check` can
    rebuild the bundles it compares against what is committed.
    """

    workspace: str
    """Where `package.json` and `bun.lock` live, relative to the repository."""

    bun_version: str = "latest"
    """Which bun the runner installs; pin it to what the lockfile was made with."""


class WorkflowSpec(BaseModel, frozen=True):
    """The choices a project makes about running its own gate.

    Every field is a judgement rather than a fact, which is why each is a
    default a project replaces rather than a constant it would have to fork
    the generator to change. What the job *runs* is not among them: the whole
    argument for generating this file is that CI owes the project one gate,
    and a second list to keep in step is the thing being avoided.
    """

    branches: list[str] = ["main"]
    """Which pushed branches run the gate, beyond every pull request."""

    runner: str = RUNNER_IMAGE
    """The label the job asks for."""

    sync_flags: list[str] = ["--all-extras"]
    """What `uv sync` is given before the gate runs."""

    frontend: FrontendSpec | None = None
    """The bun workspace to install first, for a project that builds bundles."""

    system_packages: list[str] = []
    """Distribution packages the gate needs that `uv sync` cannot install.

    A project whose code shells out to a binary — poppler for a page count,
    a renderer, a compiler — needs it on the runner too, and no lock file
    reaches it. Without this the failure lands as a test asserting the thing
    the missing binary would have produced, several steps from the cause.

    Empty is the common case and renders no step at all, so a project that
    needs nothing carries no apt call it would have to read past.
    """

    user_namespaces: bool = False
    """Whether the gate's tests need an unprivileged user namespace that can mount.

    The pinned runner image restricts them through AppArmor: a namespace is
    created but holds no capabilities, so a test that mounts inside one —
    holding a directory read-only, as a contained launch holds its record —
    can only skip there. A project with such tests lifts the restriction for
    its throwaway runner; one without carries no sysctl to read past.
    """

    def namespace_steps(self) -> list[WorkflowStep]:
        """The sysctl lifting the runner's refusal, or nothing where none is needed."""
        if not self.user_namespaces:
            return []
        return [
            WorkflowStep(
                name="Unprivileged user namespaces",
                run="sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0",
                comment=(
                    "GitHub's ubuntu-24.04 image restricts unprivileged user"
                    " namespaces\nthrough AppArmor, leaving one no capabilities,"
                    " so the tests that\nmount inside one skip rather than run."
                    " Lifted for this throwaway runner."
                ),
            )
        ]

    def install_steps(self) -> list[WorkflowStep]:
        """The apt step, or nothing where the project declares no package."""
        if not self.system_packages:
            return []
        installed = " ".join(self.system_packages)
        return [
            WorkflowStep(
                name="System packages",
                run=f"sudo apt-get update && sudo apt-get install -y {installed}",
            )
        ]

    def frontend_steps(self) -> list[WorkflowStep]:
        """The bun steps, or nothing where the project declares no workspace."""
        if self.frontend is None:
            return []
        return [
            WorkflowStep(
                uses="oven-sh/setup-bun@v2",
                settings={"bun-version": self.frontend.bun_version},
            ),
            WorkflowStep(
                name="Frontend dependencies",
                run="bun install --frozen-lockfile",
                working_directory=self.frontend.workspace,
            ),
        ]

    def steps(self) -> list[WorkflowStep]:
        """Every step of the one job, in the order the runner takes them."""
        return [
            WorkflowStep(uses="actions/checkout@v4", settings={"fetch-depth": 0}),
            WorkflowStep(uses="astral-sh/setup-uv@v6", settings={"enable-cache": True}),
            *self.install_steps(),
            *self.frontend_steps(),
            WorkflowStep(run=f"uv sync {' '.join(self.sync_flags)}"),
            WorkflowStep(
                name="Merge driver", run="uv run lup-devtools git merge-driver"
            ),
            WorkflowStep(name="Generated artifact drift", run=DRIFT_COMMAND),
            *self.namespace_steps(),
            WorkflowStep(name="Quality gate", run=CHECK_COMMAND),
        ]

    def document(self) -> YamlDocument:
        """The workflow as the document these declared choices compile to."""
        return YamlDocument(
            root=YamlMap(
                entries=[
                    YamlEntry(key="name", value=YamlScalar(value="Quality")),
                    YamlEntry(
                        key="on",
                        spaced=True,
                        value=YamlMap(
                            entries=[
                                YamlEntry(
                                    key="pull_request", value=YamlScalar(value=None)
                                ),
                                YamlEntry(
                                    key="push",
                                    value=YamlMap(
                                        entries=[
                                            YamlEntry(
                                                key="branches",
                                                value=YamlFlow(items=self.branches),
                                            )
                                        ]
                                    ),
                                ),
                            ]
                        ),
                    ),
                    YamlEntry(
                        key="jobs",
                        spaced=True,
                        value=YamlMap(
                            entries=[
                                YamlEntry(
                                    key="check",
                                    value=YamlMap(
                                        entries=[
                                            *scalars({"runs-on": self.runner}),
                                            YamlEntry(
                                                key="steps",
                                                value=YamlList(
                                                    items=[
                                                        step.node()
                                                        for step in self.steps()
                                                    ]
                                                ),
                                            ),
                                        ]
                                    ),
                                )
                            ]
                        ),
                    ),
                ]
            )
        )

    def artifact(self) -> Artifact:
        """This workflow as one artifact, gated like any other generated file."""
        return Artifact.in_yaml(
            path=WORKFLOW_PATH,
            document=self.document(),
            semantic_id="ci.quality",
            banner=GeneratedBanner(source=__name__, command=WORKFLOW_COMMAND),
        )


def write_workflow(
    spec: WorkflowSpec, root: Path | None = None, *, check: bool = False
) -> Path:
    """Write or verify the generated continuous-integration workflow."""
    return write_generated_file(
        spec.artifact(),
        root or project_root(),
        WORKFLOW_COMMAND,
        check=check,
    )


# lup: ignore[constant-declaration] — the directory GitHub Actions itself reads
PUBLISH_PATH = Path(".github/workflows/publish.yml")


class PublishSpec(BaseModel, frozen=True):
    """What a project publishes when a release tag arrives, and from where.

    Declared rather than assumed, because publishing is the one thing in this
    module a repository can be wrong about silently: a workflow that builds
    the wrong distribution still passes, and what it uploads is what everyone
    installs.

    Nothing here carries a credential and nothing has to. Publication is by
    the forge's own OIDC token, exchanged for a short-lived upload token by a
    publisher the index has been told to trust — so the secret that would
    otherwise sit in the repository does not exist to leak, and a fork running
    this workflow cannot publish because the trust names this repository.

    What is published is then recorded as the forge's own release, by a
    second job holding the one grant that takes and not the identity the
    index trusts. A release candidate goes out marked a pre-release in both
    places: the index reads it so from the version itself, and the forge is
    told.
    """

    package: str = ""
    """Which workspace member to build, empty where the project is the package.

    A repository whose distribution sits in a subdirectory names it, and
    ``uv build`` is told which member to build rather than building the
    workspace root — which is a different distribution with a different name
    and, in the case this exists for, one nobody publishes.
    """

    environment: str = "pypi"
    """The deployment environment the publishing job runs in.

    Named because the index's trusted publisher is declared against it, and
    because an environment is where a forge can be told to hold a release for
    review before it uploads. A project wanting neither still names one; it
    costs a line and gives it somewhere to put the pause later.
    """

    tag_prefix: str = "v"
    """What a release tag puts before its version: `ReleaseSpec.tag_prefix`.

    A pushed tag carrying it publishes the tree it names, built as it stands:
    every release and candidate commit carries its own version in the
    manifest, so nothing is rewritten between what was tagged and what is
    uploaded.
    """

    runner: str = RUNNER_IMAGE
    """The label the jobs ask for."""

    def steps(self) -> list[WorkflowStep]:
        """Every step of the publishing job, in the order the runner takes them."""
        member = f" --package {self.package}" if self.package else ""
        return [
            WorkflowStep(uses="actions/checkout@v4"),
            WorkflowStep(uses="astral-sh/setup-uv@v6", settings={"enable-cache": True}),
            WorkflowStep(name="Build the distribution", run=f"uv build{member}"),
            WorkflowStep(
                name="Publish to PyPI", uses="pypa/gh-action-pypi-publish@release/v1"
            ),
        ]

    def recorded(self) -> list[WorkflowStep]:
        """The step that records what was published as the forge's own release.

        A candidate's is marked a pre-release there, as the index marks its
        version one: the version after the prefix holds a letter only where
        it carries a pre-release segment, and the only one a release cuts is
        :data:`CANDIDATE_SEGMENT`.
        """
        candidate = f"contains(github.ref_name, '{CANDIDATE_SEGMENT}')"
        return [
            WorkflowStep(
                name="Record the release on GitHub",
                env={"GH_TOKEN": "${{ github.token }}"},
                run='gh release create "$GITHUB_REF_NAME" --repo "$GITHUB_REPOSITORY"'
                f" --verify-tag --generate-notes --prerelease=${{{{ {candidate} }}}}",
            )
        ]

    def job(
        self,
        steps: list[WorkflowStep],
        grants: dict[str, ScalarValue],
        why: str,
        needs: str = "",
        environment: str = "",
    ) -> YamlMap:
        """One job of the workflow: what it waits on, what it may do, its steps."""
        return YamlMap(
            entries=[
                *scalars(
                    {
                        "needs": needs,
                        "runs-on": self.runner,
                        "environment": environment,
                    }
                ),
                YamlEntry(
                    key="permissions",
                    comment=why,
                    value=YamlMap(entries=scalars(grants)),
                ),
                YamlEntry(
                    key="steps",
                    value=YamlList(items=[step.node() for step in steps]),
                ),
            ]
        )

    def document(self) -> YamlDocument:
        """The publishing workflow as the document these choices compile to."""
        return YamlDocument(
            root=YamlMap(
                entries=[
                    YamlEntry(key="name", value=YamlScalar(value="Publish")),
                    YamlEntry(
                        key="on",
                        spaced=True,
                        value=YamlMap(
                            entries=[
                                YamlEntry(
                                    key="push",
                                    value=YamlMap(
                                        entries=[
                                            YamlEntry(
                                                key="tags",
                                                value=YamlFlow(
                                                    items=[f"{self.tag_prefix}*"]
                                                ),
                                            )
                                        ]
                                    ),
                                )
                            ]
                        ),
                    ),
                    YamlEntry(
                        key="jobs",
                        spaced=True,
                        value=YamlMap(
                            entries=[
                                YamlEntry(
                                    key="publish",
                                    value=self.job(
                                        self.steps(),
                                        {"id-token": "write"},
                                        why=(
                                            "What stands in for a stored token:"
                                            " the forge mints an identity for"
                                            " this\nrun, and the index trusts it"
                                            " for this repository and this"
                                            " workflow."
                                        ),
                                        environment=self.environment,
                                    ),
                                ),
                                YamlEntry(
                                    key="release",
                                    spaced=True,
                                    value=self.job(
                                        self.recorded(),
                                        {"contents": "write"},
                                        why=(
                                            "Writing the forge's release is all"
                                            " this job does, and it holds no\n"
                                            "identity the index would trust."
                                        ),
                                        needs="publish",
                                    ),
                                ),
                            ]
                        ),
                    ),
                ]
            )
        )

    def artifact(self) -> Artifact:
        """This workflow as one artifact, gated like any other generated file."""
        return Artifact.in_yaml(
            path=PUBLISH_PATH,
            document=self.document(),
            semantic_id="ci.publish",
            banner=GeneratedBanner(source=__name__, command=WORKFLOW_COMMAND),
        )


def write_publish(
    spec: PublishSpec, root: Path | None = None, *, check: bool = False
) -> Path:
    """Write or verify the generated publishing workflow."""
    return write_generated_file(
        spec.artifact(),
        root or project_root(),
        WORKFLOW_COMMAND,
        check=check,
    )
