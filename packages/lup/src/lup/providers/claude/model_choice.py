"""What a Claude session names its model and effort as, compiled for the CLI.

A session names its model three ways: a name from Claude Code's own catalog, a
portable :data:`~lup.types.ModelTier`, or a :class:`~lup.types.CustomModel`
for an id the catalog does not list. All three reach the CLI as one string,
and this module is where they become it — for the SDK's options and for the
interactive launcher alike, so the two cannot spell one choice differently.

Effort is the same: lup's ladder has ``ultra``, which the CLI spells as
``xhigh`` effort plus the session-scoped ``ultracode`` settings key, so an
effort compiles to a rung and the settings it switches on. Where the catalog
knows the model, an effort the model does not accept is refused here, at the
declaration, rather than dropped by the CLI or narrowed to one it has.
"""

import json
from typing import Literal, get_args

from pydantic import BaseModel, TypeAdapter, ValidationError

from lup.providers.claude.models import CLAUDE_MODEL_EFFORTS, ClaudeEffort, ClaudeModel
from lup.providers.claude.subagents import model_alias
from lup.types import CustomModel, JsonObject, ModelTier

type ClaudeModelChoice = ClaudeModel | CustomModel | ModelTier
"""Every way a Claude session may name its model."""


def claude_model_choice(model: str | CustomModel) -> ClaudeModelChoice:
    """One model a caller named in words both runtimes share, as Claude takes it.

    A setting shared by both runtimes can name either catalog, so a name only the
    other runtime lists reaches here too, and is refused rather than sent to
    a CLI that would answer with its own error, later and less plainly.
    """
    try:
        return TypeAdapter(ClaudeModelChoice).validate_python(model)
    except ValidationError as error:
        raise ValueError(
            f"{model!r} is not a model Claude Code lists, nor a tier; name one "
            "from its catalog, or wrap an id outside it as CustomModel(id=...)"
        ) from error


type ClaudeEffortLevel = Literal["low", "medium", "high", "xhigh", "max"]
"""The rungs Claude Code's own ``--effort`` flag and the SDK's option take."""


def claude_model_name(model: ClaudeModelChoice | None) -> ClaudeModel | None:
    """The catalog name a choice resolves to, where the catalog knows it.

    A tier resolves to the alias it compiles to; a custom id and an inherited
    model resolve to nothing, because the catalog has no row to consult.
    """
    match model:
        case None | "inherit" | CustomModel():
            return None
        case "frontier" | "strongest" | "balanced" | "fast":
            alias = model_alias(model)
            return None if alias == "inherit" else alias
        case _:
            return model


def claude_model_id(model: ClaudeModelChoice | None) -> str | None:
    """The name the CLI is started with, or ``None`` to leave it the CLI's."""
    match model:
        case CustomModel(id=identifier):
            return identifier
        case _:
            return claude_model_name(model)


class ClaudeEffortSetting(BaseModel, frozen=True):
    """One effort in the words Claude Code takes it: a rung and its settings."""

    level: ClaudeEffortLevel
    settings: JsonObject = {}
    """The settings keys this effort switches on, carried by ``--settings``
    on the command line and by the SDK's ``settings`` option."""

    def arguments(self) -> list[str]:
        """The rung as the interactive CLI's flag; the settings travel apart.

        Apart because the CLI reads one ``--settings`` document, which a
        launch composes from every part that contributes one.
        """
        return ["--effort", self.level]


def claude_effort_named(value: str) -> ClaudeEffort:
    """One effort arriving as text — a launcher flag — read against the ladder."""
    try:
        return TypeAdapter(ClaudeEffort).validate_python(value)
    except ValidationError as error:
        raise ValueError(
            f"{value!r} is not an effort Claude Code takes; name one of "
            f"{', '.join(get_args(ClaudeEffort.__value__))}"
        ) from error


def claude_effort(effort: ClaudeEffort) -> ClaudeEffortSetting:
    """Compile one of lup's efforts into Claude Code's own.

    ``ultra`` is ``xhigh`` with ``ultracode`` on: the CLI describes it as
    xhigh effort plus standing dynamic-workflow orchestration, a boolean
    settings key it refuses for a model without ``xhigh``.
    """
    match effort:
        case "ultra":
            return ClaudeEffortSetting(level="xhigh", settings={"ultracode": True})
        case "low" | "medium" | "high" | "xhigh" | "max":
            return ClaudeEffortSetting(level=effort)


def claude_model_arguments(
    model: ClaudeModelChoice, effort: ClaudeEffort
) -> list[str] | None:
    """A one-shot ``claude -p``'s words for *model* at *effort*, or nothing for no model.

    An inherited model is nothing: a process started outside any session has
    no session's model to take. An effort the model's catalog row lacks is
    refused, as a session declaring it would be, and ``ultra``'s settings
    travel in the call's own ``--settings`` document.
    """
    name = claude_model_id(model)
    if name is None:
        return None
    refuse_unsupported_effort(model, effort)
    setting = claude_effort(effort)
    return [
        "--model",
        name,
        *setting.arguments(),
        *(["--settings", json.dumps(setting.settings)] if setting.settings else []),
    ]


def refuse_unsupported_effort(
    model: ClaudeModelChoice | None, effort: ClaudeEffort | None
) -> None:
    """Refuse an effort the catalog says this model does not take.

    Only where both are known: an effort left unset asks for nothing, and a
    custom or inherited model has no row, so the CLI's own refusal is the
    check there.
    """
    name = claude_model_name(model)
    if effort is None or name is None:
        return
    accepted = CLAUDE_MODEL_EFFORTS[name]
    if effort not in accepted:
        offered = ", ".join(accepted) or "no effort at all"
        raise ValueError(
            f"Claude model {name!r} does not take effort {effort!r}; its catalog "
            f"row accepts {offered}. Name an effort it takes, or leave effort "
            "unset for the model's default"
        )


def claude_default_effort(
    model: ClaudeModelChoice | None, preferred: ClaudeEffort = "xhigh"
) -> ClaudeEffort | None:
    """The effort a session on ``model`` thinks at when it names none.

    ``preferred`` wherever the model's catalog row takes it, and otherwise the
    highest rung the row has below it, so the default adapts to the model
    where a named effort would be refused by it; a model whose row lists no
    effort gets none. A model with no row — inherited, a tier resolving to
    nothing, a custom id — gets ``preferred``, since nothing says it cannot.
    ``xhigh`` is lup's own preference; a person's config may name another.
    """
    name = claude_model_name(model)
    if name is None:
        return preferred
    ladder: tuple[ClaudeEffort, ...] = get_args(ClaudeEffort.__value__)
    descending = reversed(ladder[: ladder.index(preferred) + 1])
    accepted = CLAUDE_MODEL_EFFORTS[name]
    return next((rung for rung in descending if rung in accepted), None)
