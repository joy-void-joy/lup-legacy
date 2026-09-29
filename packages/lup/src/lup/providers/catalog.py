"""What a runtime's CLI says about its own models, recorded as data.

A model id is a closed set the vendor owns and moves: a lineup gains a model,
retires another, and changes which reasoning efforts each accepts. Typing a
session's model as ``str`` makes a misspelt id a runtime failure in the
vendor's words, so each provider's ids are a ``Literal`` compiled from what
its CLI reports. :class:`ModelCatalog` is that report, reduced to the facts a
declaration is checked against and committed beside the module compiled from
it — so the committed module is checked offline against the snapshot, and the
snapshot against the live CLI only by the command that refreshes it.
"""

from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

type CatalogRuntime = Literal["claude", "codex"]
"""Which runtime a catalog was read from."""

type SessionEffort = Literal["low", "medium", "high", "xhigh", "max", "ultra"]
"""How hard a session is asked to think before it answers.

Every rung is one both runtimes' catalogs list, so none is narrowed on the
way to either: ``ultra`` is Codex's own top rung, and Claude's ``xhigh`` with
ultracode on. Nothing sits below ``low``, because neither catalog lists a
rung there — ``minimal`` and ``none`` left Codex's, and admitting either here
would turn "barely reason" into "reason a little" without saying so. Which
rungs one *model* takes is narrower still, and refused where it is declared.
"""


class CatalogModel(BaseModel, frozen=True, extra="forbid"):
    """One model a runtime's CLI accepts, and the efforts it takes."""

    id: str
    efforts: list[str]
    """Every reasoning effort this model accepts, lowest first, in the
    runtime's own words.

    Empty for a model that takes no effort at all, which is a fact to refuse
    against rather than an unknown: a session naming an effort for it would
    have that effort dropped by the runtime without a word.
    """


class CatalogAlias(BaseModel, frozen=True, extra="forbid"):
    """One other name a runtime resolves to a model, wherever it routes.

    A short family name, a dated spelling of one model, or either with a
    context-window suffix: every one is a name a session may be opened with,
    and none is a model of its own.
    """

    name: str
    targets: list[str]
    """Every model id this name resolves to on any route the runtime knows."""


class ModelCatalog(BaseModel, frozen=True, extra="forbid"):
    """One runtime's model lineup, as its CLI reported it."""

    runtime: CatalogRuntime
    observed: str
    """The CLI version the lineup was read from."""

    models: list[CatalogModel]
    aliases: list[CatalogAlias] = []

    def lineup(self) -> "ModelCatalog":
        """The same catalog without its version, which is what drift compares.

        A CLI upgrade that changed no model is not drift: the compiled module
        would say the same thing, and a check failing on the version alone
        would teach whoever reads it to refresh without looking.
        """
        return self.model_copy(update={"observed": ""})

    def efforts(self) -> list[str]:
        """Every effort any model accepts, as one ladder, lowest first.

        Each model lists its own rungs lowest first, and no model lists all of
        them — one stops at ``max``, another adds ``xhigh`` below it — so the
        ladder is the order every model's list agrees on together. A catalog
        whose models disagree about which of two rungs is higher has no
        ladder, and is refused rather than ordered arbitrarily.
        """
        ladder = TopologicalSorter(
            {
                effort: [
                    lower
                    for model in self.models
                    if effort in model.efforts
                    for lower in model.efforts[: model.efforts.index(effort)]
                ]
                for model in self.models
                for effort in model.efforts
            }
        )
        try:
            return list(ladder.static_order())
        except CycleError as error:
            raise ValueError(
                f"the {self.runtime} catalog's models disagree about the order "
                f"of their efforts: {error.args[1]}"
            ) from error

    def names(self) -> list[str]:
        """Every name a session may select a model by: aliases, then ids."""
        return list(
            dict.fromkeys(
                [alias.name for alias in self.aliases]
                + [model.id for model in self.models]
            )
        )

    def efforts_of(self, name: str) -> list[str]:
        """The efforts one name accepts on any route it could resolve to.

        An alias answers the union of its targets, because refusing an effort
        one route accepts would refuse a configuration that works there; the
        runtime's own refusal remains the check for the route that lacks it.
        """
        known = {model.id: model.efforts for model in self.models}
        targets = next(
            (alias.targets for alias in self.aliases if alias.name == name), [name]
        )
        accepted = {effort for target in targets for effort in known[target]}
        return [effort for effort in self.efforts() if effort in accepted]

    @classmethod
    def read(cls, path: Path) -> "ModelCatalog":
        """The committed snapshot at ``path``."""
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def text(self) -> str:
        """The snapshot's text, one stable spelling so a diff shows only drift."""
        return self.model_dump_json(indent=2) + "\n"


def read_lineup(runtime: CatalogRuntime) -> ModelCatalog:
    """The lineup one runtime's installed CLI reports, asked now.

    Each reader is imported where it is asked for: it reaches its provider's
    adapter, and a caller reading one runtime should not load the other.
    """
    match runtime:
        case "claude":
            from lup.providers.claude.catalog import claude_catalog

            return claude_catalog()
        case "codex":
            from lup.providers.codex.catalog import codex_catalog

            return codex_catalog()
