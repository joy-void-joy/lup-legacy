"""The publishing workflow a release tag runs.

What a tag publishes is the tree it names, built as it stands: a candidate's
manifest says its candidate version and a release's says the release, so
nothing is rewritten between what was tagged and what is uploaded — and a
candidate arrives marked as the pre-release it is, on the index and on the
forge alike.
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


def test_a_tag_publishes_the_tree_it_names_as_it_stands() -> None:
    """The build is the only command: nothing rewrites the tagged tree first."""
    steps = read(PublishSpec(package="thing")).jobs["publish"].steps

    assert [step.run for step in steps if step.run] == ["uv build --package thing"]


def test_the_tag_prefix_is_what_triggers() -> None:
    assert read(PublishSpec(tag_prefix="release-")).on.push.tags == ["release-*"]


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
