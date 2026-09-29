"""The operator's answers, lent to every session read-only.

A review is parked in the checkout of the session that asked, and answered
on the host, into lup's own state (:func:`~lup.sandbox.known.answers_directory`),
where no session writes. A session still has to read the answer to its own
review -- its hook spends it, `review wait` carries it out -- so every launch
holds this companion: it hands the session the host's path for the answers,
and mounts its repository's answers there read-only, so a contained session
reads them at the path the host has them and can write none.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from lup.harness.environment import inside_a_container
from lup.launch.companions import (
    CompanionLaunch,
    CompanionName,
    Contribution,
    HostCompanion,
)
from lup.launch.declaration import Mount
from lup.policy.assets.host import review_answers
from lup.policy.identity import REVIEW_ANSWERS_ENV
from lup.sandbox.known import answers_directory


class ReviewAnswers(HostCompanion, frozen=True):
    """The host's answers to one repository's reviews, lent to each of its sessions read-only.

    Nothing runs: holding it readies the directory, owned by the operator and
    readable by nobody else, and contributes the variable naming it and the
    read-only mount of this repository's part of it. A launch from inside a
    container mounts nothing it could not reach, and passes on the path its
    own launch handed it.
    """

    name: CompanionName = "review-answers"

    @contextmanager
    def held(self, launch: CompanionLaunch) -> Iterator[Contribution]:
        if inside_a_container(launch.environment):
            handed = launch.environment
            yield Contribution(
                environment=(
                    {REVIEW_ANSWERS_ENV: handed[REVIEW_ANSWERS_ENV]}
                    if REVIEW_ANSWERS_ENV in handed
                    else {}
                )
            )
            return
        home = answers_directory()
        repository = review_answers(launch.root / ".lup/questions.jsonl", home).parent
        for directory in (home, repository):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        yield Contribution(
            environment={REVIEW_ANSWERS_ENV: str(home)},
            mounts=[Mount(path=repository)],
        )
