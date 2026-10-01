"""The runner image every workflow in this repository names.

A generated workflow takes its runner from ``RUNNER_IMAGE`` and cannot drift
from it. A workflow written by hand spells the image itself, and a moving
label there fails on GitHub's schedule rather than on a commit somebody wrote
— the failure the pin exists to prevent, arriving through the one file the
pin does not reach. This is the sweep that holds every workflow to it, so an
unpinned job is a red gate rather than a red lane on the repoint date.
"""

from pydantic import BaseModel, Field
import yaml

from lup.devtools.dev.workflow import RUNNER_IMAGE
from lup.workspace.paths import project_root


class WorkflowJob(BaseModel, frozen=True):
    """The one job field this sweep reads."""

    runs_on: str = Field(alias="runs-on")


class Workflow(BaseModel, frozen=True):
    """A workflow file as the sweep reads it: the jobs it declares, by name."""

    jobs: dict[str, WorkflowJob]


def test_every_job_names_the_pinned_runner() -> None:
    directory = project_root() / ".github" / "workflows"
    files = sorted(directory.glob("*.yml")) + sorted(directory.glob("*.yaml"))
    runners = {
        f"{path.name}:{job}": spec.runs_on
        for path in files
        for job, spec in Workflow.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        ).jobs.items()
    }

    # An empty sweep would pass every assertion below it, and a workflows
    # directory this repository stopped having is the one way that happens.
    assert runners
    assert runners == dict.fromkeys(runners, RUNNER_IMAGE)
