"""Defects this project measured in components it does not own.

`dev report-friction` files unasked on the repository it is run in, and on a
tracker this project declares once `--repo` names it and somebody is asked:
the right targets while the repair is one somebody here, or in a project this
one builds on, could make. A defect in the runtime underneath has no such
home: the fix is somebody else's, the
evidence is ours, and a finding that lives only in a session's narration
teaches nobody -- the same argument the guidance makes for filing friction,
pointed one component outwards.

So a report is declared rather than written into a page. One declaration
renders the roster a reader browses, the body somebody pastes into an issue,
and the record of where it was filed once it has been -- which is what keeps
the page from claiming a report was sent when the sending never happened.
Nothing here files anything: publishing under somebody's account is theirs to
do, and a command that did it as a side effect of generation would do it
without being asked.
"""

from pydantic import BaseModel, Field


class UpstreamReport(BaseModel, frozen=True):
    """One measured defect, addressed to whoever owns the component.

    ``version`` is the version the measurement was taken against and not the
    version the defect was introduced in: the second is a claim about a
    history nobody here can read, and stating it is how a report gets closed
    as inaccurate on a detail that was never the point.
    """

    slug: str = Field(
        description=(
            "Stable handle naming this report on a command line. Stable "
            "because the command that prints a body is what somebody pipes "
            "into `gh`, and a handle that moves breaks the shell history "
            "that was working"
        )
    )
    component: str = Field(description="What was measured, e.g. 'Claude Code'")
    version: str = Field(description="The version the measurement was taken against")
    repository: str = Field(
        description="Where the report goes, as `owner/name`, or empty when unknown"
    )
    title: str = Field(description="The issue title, as it should be filed")
    body: str = Field(description="The issue body in Markdown, paste-ready")
    filed: str = Field(
        default="",
        description=(
            "The issue URL once somebody filed this, empty until then. Empty "
            "is the honest default: nothing in this repository can file on "
            "another account, so a report is unsent until a human says "
            "otherwise by writing the URL here"
        ),
    )

    def status(self) -> str:
        """How the roster names this report's disposition."""
        return f"filed as {self.filed}" if self.filed else "not filed"

    def command(self) -> str:
        """The invocation that files this report, for a reader to run.

        Spelled with a body read from this command's own output rather than
        from a file: the body is generated, so a copy on disk is one more
        thing that can go stale against the declaration it came from.
        """
        target = f" --repo {self.repository}" if self.repository else ""
        return (
            f"uv run lup-devtools sync upstream {self.slug} | "
            f"gh issue create{target} --title {self.title!r} --body-file -"
        )

    def section(self) -> str:
        """This report as one section of the page that lists them all."""
        return "\n".join(
            [
                f"## {self.title}",
                "",
                f"Measured against **{self.component} {self.version}**. "
                f"Goes to `{self.repository or 'an unknown repository'}`; "
                f"currently **{self.status()}**.",
                "",
                "```bash",
                self.command(),
                "```",
                "",
                self.body.strip(),
                "",
            ]
        )


class UpstreamRoster(BaseModel, frozen=True):
    """Every report this project holds, asked for one of them by handle."""

    reports: list[UpstreamReport]

    def named(self, slug: str) -> UpstreamReport | None:
        """The report with this handle, or nothing when none carries it."""
        return next((report for report in self.reports if report.slug == slug), None)

    def handles(self) -> str:
        """Every handle, for a diagnostic that has to say what was available."""
        return ", ".join(report.slug for report in self.reports) or "none declared"
