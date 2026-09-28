"""The publishing workflow a release tag runs.

A release candidate and the release it is promoted to are one commit under
two tags, so what the workflow publishes has to come from the tag rather than
from the manifest in the tree — and a candidate has to arrive marked as the
pre-release it is, on the index and on the forge alike.
"""

import yaml
from pydantic import BaseModel, Field

from lup.devtools.dev.workflow import PublishSpec


class Step(BaseModel, frozen=True):
    """The step keys these tests read."""

    name: str = ""
    run: str = ""
    env: dict[str, str] = {}


class Job(BaseModel, frozen=True):
    """The job keys these tests read."""

    needs: str = ""
    permissions: dict[str, str]
    steps: list[Step]


class Trigger(BaseModel, frozen=True):
    tags: list[str]


class On(BaseModel, frozen=True):
    push: Trigger


class Workflow(BaseModel, frozen=True):
    on: On = Field(alias="on")
    jobs: dict[str, Job]


def read(spec: PublishSpec) -> Workflow:
    """The workflow a spec compiles to, as a runner reads it."""
    return Workflow.model_validate(yaml.safe_load(spec.document().text()))


def test_a_tag_publishes_the_version_it_names() -> None:
    """Stamped before the build, so a promoted candidate builds as the release."""
    steps = read(PublishSpec(package="thing")).jobs["publish"].steps
    runs = [step.run for step in steps if step.run]

    assert runs[0] == 'uv version --frozen --package thing "${GITHUB_REF_NAME#v}"'
    assert runs[1] == "uv build --package thing"


def test_the_tag_prefix_is_what_triggers_and_what_is_taken_off() -> None:
    workflow = read(PublishSpec(tag_prefix="release-"))

    assert workflow.on.push.tags == ["release-*"]
    assert '"${GITHUB_REF_NAME#release-}"' in workflow.jobs["publish"].steps[2].run


def test_a_candidate_is_recorded_on_the_forge_as_a_prerelease() -> None:
    job = read(PublishSpec()).jobs["release"]
    (step,) = job.steps

    assert job.needs == "publish"
    assert job.permissions == {"contents": "write"}
    assert "--prerelease=${{ contains(github.ref_name, 'rc') }}" in step.run
    assert "--verify-tag" in step.run
    assert step.env == {"GH_TOKEN": "${{ github.token }}"}


def test_publishing_holds_the_index_identity_and_nothing_else() -> None:
    """The job that may write the forge's releases is not the one the index trusts."""
    jobs = read(PublishSpec()).jobs

    assert jobs["publish"].permissions == {"id-token": "write"}
    assert "id-token" not in jobs["release"].permissions
