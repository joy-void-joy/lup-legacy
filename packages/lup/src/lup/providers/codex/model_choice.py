"""What a Codex session names its model and effort as, compiled for the CLI.

A session names its model three ways: a slug from Codex's own catalog, a
portable :data:`~lup.types.ModelTier` resolved through the session's
:class:`~lup.providers.codex.subagents.CodexModelTiers`, or a
:class:`~lup.types.CustomModel` for an id the catalog does not list — a
compatible endpoint's own model. All three reach the app-server as one slug,
and this module is where they become it.

Codex's efforts are its catalog's own words, ``ultra`` among them, so an effort
passes through unchanged. What the catalog does add is which efforts each
model accepts, and an effort a model lacks is refused here, at the
declaration: the API's own refusal arrives as a 400 before the first turn,
naming neither the caller nor the model it paired.
"""

import json
from typing import get_args

from pydantic import TypeAdapter, ValidationError

from lup.providers.codex.models import CODEX_MODEL_EFFORTS, CodexEffort, CodexModel
from lup.providers.codex.subagents import CodexModelTiers
from lup.types import CustomModel, ModelTier

type CodexModelChoice = CodexModel | CustomModel | ModelTier
"""Every way a Codex session may name its model."""


def codex_model_choice(model: str | CustomModel) -> CodexModelChoice:
    """One model a caller named in words both runtimes share, as Codex takes it.

    A setting shared by both runtimes can name either catalog, so a name only the
    other runtime lists reaches here too, and is refused rather than sent to
    an app-server that would refuse it after the process had started.
    """
    try:
        return TypeAdapter(CodexModelChoice).validate_python(model)
    except ValidationError as error:
        raise ValueError(
            f"{model!r} is not a model Codex lists, nor a tier; name one from its "
            "catalog, or wrap an id outside it as CustomModel(id=...)"
        ) from error


def listed_codex_model(model: str) -> CodexModel | None:
    """The catalog slug ``model`` spells, or ``None`` where the catalog lacks it.

    For a name arriving as text — a command-line flag the launcher passes
    through — where an unlisted slug is the CLI's to judge rather than this
    library's to refuse.
    """
    try:
        return TypeAdapter(CodexModel).validate_python(model)
    except ValidationError:
        return None


def codex_effort_named(value: str) -> CodexEffort:
    """One effort arriving as text — a launcher flag — read against the ladder."""
    try:
        return TypeAdapter(CodexEffort).validate_python(value)
    except ValidationError as error:
        raise ValueError(
            f"{value!r} is not an effort Codex takes; name one of "
            f"{', '.join(get_args(CodexEffort.__value__))}"
        ) from error


def codex_effort_arguments(effort: CodexEffort) -> list[str]:
    """One effort as the interactive CLI takes it: a configuration override."""
    return ["--config", f"model_reasoning_effort={json.dumps(effort)}"]


def codex_model_resolved(
    model: CodexModelChoice | None, tiers: CodexModelTiers
) -> CodexModel | CustomModel | None:
    """The model a choice selects: a tier through ``tiers``, anything else as named.

    ``None`` and ``inherit`` both leave the model to the home the session
    opens against.
    """
    match model:
        case None | "inherit":
            return None
        case "frontier" | "strongest" | "balanced" | "fast":
            return tiers.resolve(model)
        case _:
            return model


def codex_model_id(
    model: CodexModelChoice | None, tiers: CodexModelTiers
) -> str | None:
    """The slug the app-server is asked for, or ``None`` to inherit the home's."""
    match codex_model_resolved(model, tiers):
        case CustomModel(id=identifier):
            return identifier
        case resolved:
            return resolved


def refuse_unsupported_effort(
    model: CodexModelChoice | None,
    effort: CodexEffort | None,
    tiers: CodexModelTiers,
) -> None:
    """Refuse an effort the catalog says this model does not take.

    Only where both are known: an effort left unset asks for nothing, and a
    custom or inherited model has no row, so the app-server's own refusal is
    the check there.
    """
    match codex_model_resolved(model, tiers):
        case None | CustomModel():
            return
        case name:
            accepted = CODEX_MODEL_EFFORTS[name]
    if effort is not None and effort not in accepted:
        raise ValueError(
            f"Codex model {name!r} does not take effort {effort!r}; its catalog "
            f"row accepts {', '.join(accepted)}. Name an effort it takes, or "
            "leave effort unset for the model's default"
        )


def codex_default_effort(
    model: CodexModelChoice | None,
    tiers: CodexModelTiers,
    preferred: CodexEffort = "xhigh",
) -> CodexEffort | None:
    """The effort a session on ``model`` reasons at when it names none.

    ``preferred`` wherever the model's catalog row takes it, and otherwise the
    highest rung the row has below it, so the default adapts to the model
    where a named effort would be refused by it; a model whose row lists no
    effort gets none. A model with no row — inherited, a tier ``tiers``
    resolves to nothing, a custom id — gets ``preferred``, since nothing says
    it cannot. ``xhigh`` is lup's own preference; a person's config may name
    another.
    """
    match codex_model_resolved(model, tiers):
        case None | CustomModel():
            return preferred
        case name:
            accepted = CODEX_MODEL_EFFORTS[name]
    ladder: tuple[CodexEffort, ...] = get_args(CodexEffort.__value__)
    descending = reversed(ladder[: ladder.index(preferred) + 1])
    return next((rung for rung in descending if rung in accepted), None)
