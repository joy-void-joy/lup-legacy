"""Reading back what the dispatcher declined to interrupt about.

The writing half is :func:`~lup.policy.assets.host.record_deferral`, compiled
into the dispatcher because that is the only thing standing in front of a
command. This is everything a human does with the result: telling the two
kinds of deferral apart, and saying which of them is asking for a decision.

**The two kinds are not the same request.** An *unjudged* deferral is a gap:
nobody has ever said anything about that command, and under a boundary the
policy let the runtime decide. Each one is a candidate for a vocabulary row,
and the list of them is the reason this file exists. A *judged* deferral is a
rule having looked and the boundary having answered for the loss -- the
relaxation working as designed. It is an audit trail, and reading it is how
somebody checks that the relaxation is letting through what they meant.

**Nothing here writes a rule.** From one deferred `ruff check .`, a row of
`ruff` -> allow permits `ruff format --write` forever, and a row of
`ruff check` -> allow permits `ruff check --fix`; substitute `rm tmp/scratch`
-> `rm` -> allow and the same mechanism permits `rm -rf`. What separates the
safe generalisation from the catastrophic one is exactly the judgement a
person makes, so this prints and a person writes.
"""

from pathlib import Path

from pydantic import BaseModel

DEFAULT_CORPUS = ".lup/hooks/learned.jsonl"
"""Where the dispatcher appends, relative to the checkout it is judging in.

Session state rather than a committed artifact, which is why it sits under
`.lup/` with the script-run ledger and the boundary table: it is what *this*
checkout has met, and a machine that has met different commands has not
drifted from anything.
"""
# lup: defer: the deferrals are read back here and the verdicts are not --
# every dispatcher also appends each completed decision to `hook-events.jsonl`
# under the plugin data directory its runtime names, and four sessions tallied
# those by hand to learn which asks and denies dominate. A reader needs that
# directory on both runtimes, which only each runtime's environment spells


class Deferral(BaseModel, frozen=True):
    """One command the policy declined to interrupt about, and why."""

    command: str
    reason: str = ""
    judged: bool = False
    first_seen: str = ""


class Corpus(BaseModel, frozen=True):
    """Every deferral a checkout has recorded, told apart by kind."""

    deferrals: list[Deferral] = []

    def gaps(self) -> list[Deferral]:
        """The ones nobody has judged, which are the candidates for a rule."""
        return [item for item in self.deferrals if not item.judged]

    def settled(self) -> list[Deferral]:
        """The ones a rule judged and a boundary answered for."""
        return [item for item in self.deferrals if item.judged]


def read_corpus(root: Path, corpus: str = DEFAULT_CORPUS) -> Corpus:
    """Load what this checkout has recorded, skipping what it cannot read.

    A line that does not parse is skipped rather than fatal. Two processes
    append here and one may be part-way through a write, and a corpus that
    refused to load over a torn line would be a review surface that stops
    working exactly when the session it is reviewing is busiest.
    """
    path = root / corpus
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return Corpus()
    held: list[Deferral] = []
    for line in text.splitlines():
        try:
            held.append(Deferral.model_validate_json(line))
        except ValueError:
            continue
    return Corpus(deferrals=held)
