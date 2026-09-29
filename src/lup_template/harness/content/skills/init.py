"""Canonical declaration for the init skill."""

import lup.harness.models as models
import lup_template.harness.content.provenance as provenance

SPELLING = provenance.Provenance(
    project_devtools="uv run lup-devtools",
    library_checkout="<lup-checkout>",
    branch_tip="uv run lup-devtools dev init base",
)
"""One checkout, unqualified: this skill turns the checkout it runs in into the
project, and reads lup's branches through the clone `dev init base` fetched --
the checkout's own `origin` is the project's wherever it was generated rather
than cloned."""


def where_taken(
    module: str, name: str, values: dict[str, models.PromptPart] | None = None
) -> models.WhereTaken:
    """One of this skill's passages, rendered only where *module* was taken."""
    return models.WhereTaken(
        module=module,
        parts=[models.Passage(module=__name__, name=name, values=values or {})],
    )


SKILL = models.Skill(
    id="skill.init",
    name="init",
    description="Make this checkout a project for one domain — choose its modules, settle its seams, rename and scaffold it",
    tools=[
        "Bash(git:*, uv run lup-devtools:*, uv sync:*, uv run pyright:*, uv run ruff:*, uv run pytest:*)",
        "Read",
        "Grep",
        "Glob",
        "Edit",
        "Write",
        "AskUserQuestion",
    ],
    prompt=models.PromptDocument(
        source=__name__,
        parts=[
            models.Passage(
                module=__name__,
                values={
                    "ask": models.AskUser(
                        question="whether to go on from the base, which carries work the default branch has not reviewed, or from the default branch instead"
                    ),
                },
            ),
            models.Passage(
                module=__name__,
                name="phases",
                values={
                    "brainstorm_skill": models.SkillInvocation(
                        plugin="lup", skill="brainstorm"
                    ),
                    "ask": models.AskUser(
                        question="each identity answer that decides what gets generated, with the reading you would pick offered first"
                    ),
                    "ask_2": models.AskUser(
                        question="which modules this domain takes and which it declines, one option per module the roster offers"
                    ),
                    "marketplace_path": models.NativePath(
                        location="marketplace", scope="every_tree"
                    ),
                    "skill_pattern": models.SkillPattern(plugin="lup", placeholder="*"),
                },
            ),
            *provenance.acquisition(SPELLING),
            models.Passage(
                module=__name__,
                name="merge-the-guidance",
                values={
                    "ask": models.AskUser(
                        question="whether to pin the branch and carry the copied half across the commits it moved past the base with dev update now, or to pin the recorded commit as a revision and move later"
                    ),
                    "guidance_file_path": models.NativePath(
                        location="guidance_file", scope="every_tree"
                    ),
                    "guidance_template_path": models.PluginPath(
                        plugin="lup", location="guidance_template", scope="every_tree"
                    ),
                },
            ),
            *provenance.sync_baseline(SPELLING),
            # lup: defer: "Phase 1.6: Settle the Seams" (the `phases` passage) and "Phase 2.5: Settle the Seams" (this one) both walk file ownership and rule retirement, so an interview asks them twice; decide which phase owns the seams and fold the other's (approval trees, tree roles, acceptance guard) into it.
            models.Passage(
                module=__name__,
                name="verify",
                values={
                    "ask": models.AskUser(
                        question="which rule families this domain keeps, with retiring the anti-pattern family altogether as one answer"
                    ),
                    "ask_2": models.AskUser(
                        question="which files the human author owns, starting from whether README.md stays human-owned"
                    ),
                    "ask_3": models.AskUser(
                        question="whether any root this domain adds needs a path role, and which"
                    ),
                    "ask_4": models.AskUser(
                        question="whether this domain declares an acceptance guard over its test roots"
                    ),
                    "guidance_file_path": models.NativePath(
                        location="guidance_file", scope="every_tree"
                    ),
                    "watch": models.WatchOutput(
                        command="uv run lup-devtools dev check"
                    ),
                    # Steps customizing a module the interview may decline
                    # exist only where it was taken, after the numbered ones.
                    "setup_step": where_taken(
                        "setup",
                        "setup-step",
                        {
                            "ask_5": models.AskUser(
                                question="which external services the agent uses, and for each whether it authenticates by OAuth flow, API key, or a credentials file"
                            )
                        },
                    ),
                    "feedback_step": where_taken("feedback-loop", "feedback-step"),
                    "feedback_after": where_taken(
                        "feedback-loop",
                        "feedback-after",
                        {
                            "feedback_loop_skill": models.SkillInvocation(
                                plugin="lup", skill="feedback-loop"
                            )
                        },
                    ),
                },
            ),
        ],
    ),
)
