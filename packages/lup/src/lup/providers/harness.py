"""Named native harness composition roots over canonical declarations."""

from enum import StrEnum
from pathlib import Path

from lup.providers.claude.harness import (
    ClaudeAgentRenderer,
    ClaudeGuidanceRenderer,
    ClaudeHookRenderer,
    ClaudePluginManifestRenderer,
    ClaudeSkillRenderer,
    ClaudeSpellings,
)
from lup.providers.codex.patch import patched_files
from lup.providers.codex.harness import (
    CodexAgentRenderer,
    CodexGuidanceRenderer,
    CodexHookRenderer,
    CodexPluginManifestRenderer,
    CodexSkillRenderer,
    CodexSpellings,
)
from lup.harness.codescan.portable import PORTABLE_RULE
from lup.harness.contracts import NativeSpellings
from lup.harness.prompts import SpelledPromptRenderer
from lup.harness.models import (
    GUIDANCE_BUDGET,
    GuidanceBudget,
    document_byte_size,
    Artifact,
    ArtifactTree,
    Harness,
)
from lup.harness.validation import validated_tree
from lup.policy.review import ReviewedFile
from lup.policy.kernel.review import copied_paths, literal_input


class AdapterName(StrEnum):
    """Which native runtime a caller means, as a closed set.

    The library ships exactly these adapter packages, so naming them is a
    fact about this library rather than a judgement an adopter could make
    differently — which is why it is a type and not a table of strings.
    Carrying it as one keeps a mistyped selector from resolving to whichever
    branch a chain of string comparisons happened to end in.
    """

    CLAUDE = "claude"
    CODEX = "codex"


def prompt_renderer(own: NativeSpellings) -> SpelledPromptRenderer:
    """Compose the one renderer around the vocabulary of one runtime.

    Prose that teaches every tree names the runtimes in this order, so the
    ordering is a single composition decision rather than a claim any one
    adapter makes about the others.
    """
    return SpelledPromptRenderer(own=own, every=[ClaudeSpellings(), CodexSpellings()])


def claude_prompt_renderer() -> SpelledPromptRenderer:
    """Render prompts in Claude's vocabulary."""
    return prompt_renderer(ClaudeSpellings())


def codex_prompt_renderer() -> SpelledPromptRenderer:
    """Render prompts in Codex's vocabulary."""
    return prompt_renderer(CodexSpellings())


def guidance_artifacts(tree: ArtifactTree) -> list[Artifact]:
    """Every artifact in a tree that is a copy of the always-loaded document.

    Which artifact that is belongs beside the gate that refuses on its size,
    so a reader measuring the document and the gate weighing it select it the
    same way. Codex renders two artifacts and only one is the guidance, so the
    identity has to be asked rather than assumed from position.
    """
    return [
        artifact
        for artifact in tree.artifacts
        if artifact.semantic_id == "harness.guidance"
    ]


def reject_oversized_guidance(
    tree: ArtifactTree, budget: GuidanceBudget = GUIDANCE_BUDGET
) -> None:
    """Hold the always-loaded document to its budget as a session sees it.

    The declaration-time lower bound in ``Harness`` cannot know what the parts
    render to, and the gap grows with every part that replaces literal prose.
    Bytes, not characters: that is the unit the runtime's own ceiling counts
    in, and the two differ wherever the document uses non-ASCII punctuation.
    """
    for artifact in guidance_artifacts(tree):
        used = document_byte_size(artifact.content)
        if used <= budget.ceiling:
            continue
        raise ValueError(
            f"rendered guidance {artifact.path.as_posix()} is {used} bytes, "
            f"over the {budget.ceiling} budget by {used - budget.ceiling}. "
            "Move a section to a "
            "generated document under docs/ and leave a file-path pointer, the "
            "way Self-Improvement Loop and Permission Hooks were split."
        )


def reject_native_prose(source: Harness) -> None:
    """Keep every native spelling inside the adapter that owns it.

    Composition is the only place that sees the assembled text, so a
    description built elsewhere and folded into a prompt is judged here too.
    """
    breaches = PORTABLE_RULE.judge(source, [ClaudeSpellings(), CodexSpellings()])
    if breaches:
        named = ", ".join(
            f"{breach.declaration_id} names {breach.spelling!r}"
            for breach in breaches[:8]
        )
        raise ValueError(
            "prose every tree renders must name no platform; a typed part "
            f"spells these for each runtime instead: {named}"
        )


def worker_identity_of(source: Harness) -> str:
    """The identity a resolver worker declares, or nothing where none is declared.

    A project that declined the resolver module has no worker session, so the
    policy it compiles grants autonomy to nobody: the renderers read the empty
    spelling as an empty list rather than as a session called "".
    """
    return source.resolver.worker_identity if source.resolver is not None else ""


def compile_claude(source: Harness) -> ArtifactTree:
    """Compile canonical declarations directly to Claude-owned artifacts."""
    reject_native_prose(source)
    spellings = ClaudeSpellings()
    prompts = prompt_renderer(spellings)
    manifest_renderer = ClaudePluginManifestRenderer()
    guidance_renderer = ClaudeGuidanceRenderer(prompts)
    artifacts: list[Artifact] = []  # lup: ignore[empty-collection]
    for plugin in source.plugins:
        skill_renderer = ClaudeSkillRenderer(prompts, plugin)
        agent_renderer = ClaudeAgentRenderer(prompts, plugin, spellings)
        for declaration in plugin.skills:
            artifacts.extend(skill_renderer.render(declaration).artifacts)
        for declaration in plugin.agents:
            artifacts.extend(agent_renderer.render(declaration).artifacts)
        artifacts.extend(manifest_renderer.render(plugin).artifacts)
        if plugin.hooks is not None:
            artifacts.extend(
                ClaudeHookRenderer(plugin.name, worker_identity_of(source), spellings)
                .render(plugin.hooks)
                .artifacts
            )
    guidance = guidance_renderer.render(source)
    reject_oversized_guidance(guidance)
    artifacts.extend(guidance.artifacts)
    return validated_tree(artifacts)


def compile_codex(source: Harness) -> ArtifactTree:
    """Compile canonical declarations directly to Codex-owned artifacts."""
    reject_native_prose(source)
    spellings = CodexSpellings()
    prompts = prompt_renderer(spellings)
    manifest_renderer = CodexPluginManifestRenderer()
    guidance_renderer = CodexGuidanceRenderer(prompts, spellings)
    artifacts: list[Artifact] = []  # lup: ignore[empty-collection]
    for plugin in source.plugins:
        skill_renderer = CodexSkillRenderer(prompts, plugin.name)
        agent_renderer = CodexAgentRenderer(prompts, spellings)
        for declaration in plugin.skills:
            artifacts.extend(skill_renderer.render(declaration).artifacts)
        for declaration in plugin.agents:
            artifacts.extend(agent_renderer.render(declaration).artifacts)
        artifacts.extend(manifest_renderer.render(plugin).artifacts)
        if plugin.hooks is not None:
            artifacts.extend(
                CodexHookRenderer(plugin.name, worker_identity_of(source), spellings)
                .render(plugin.hooks)
                .artifacts
            )
    guidance = guidance_renderer.render(source)
    reject_oversized_guidance(guidance)
    artifacts.extend(guidance.artifacts)
    return validated_tree(artifacts)


def patch_review(
    command: str, cwd: Path, preconditions: dict[Path, str | None], shell: bool
) -> list[ReviewedFile]:
    """Decode a patch envelope into the before/after pairs a reviewer reads.

    Named here rather than at the surface that renders them, for the reason
    every other function in this module is: the envelope's grammar is one
    adapter's word, and this is where an adapter may be named. What a review
    receives is the neutral pair, so the surface stays portable and a second
    runtime's envelope becomes another arm here rather than another import
    there.

    The decoder is the one the dispatcher judges with, which is the whole
    point of not writing a second: a reviewer shown a different reading of the
    patch would be approving a different patch.

    Both documents come out of the decode rather than off disk, because an
    approval binds to the operation as submitted -- a preimage re-read from a
    file that moved since would show a change nobody proposed.
    """

    def resolved(path: str) -> Path:
        """One decoded path against the directory the operation ran in."""
        named = Path(path)
        return named if named.is_absolute() else cwd / named

    def document(path: str) -> str | None:
        """What stood at one path, with absence kept apart from emptiness."""
        target = resolved(path)
        if target not in preconditions:
            raise ValueError(f"no captured preimage for {target}")
        return preconditions[target]

    try:
        if shell:
            copied = copied_paths(command)
            if copied is not None:
                source, target = copied["source"], copied["target"]
                return [
                    ReviewedFile(
                        path=resolved(target),
                        before=document(target),
                        after=document(source),
                        overwrite=document(target) is not None,
                    )
                ]
            envelope = literal_input(command, "apply_patch")
            if envelope is None:
                return []
            command = envelope
        decoded = patched_files(command, document)
    except ValueError:
        # An envelope this cannot read is a question with no diff rather than
        # a listing that fails: the payload is still printed, and a reviewer
        # reads exactly what the agent submitted.
        return []
    return [
        ReviewedFile(
            path=resolved(change.path),
            before=change.before,
            after=change.after,
            overwrite=change.overwrite,
        )
        for change in decoded
    ]
