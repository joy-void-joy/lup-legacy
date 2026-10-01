"""Canonical permission-policy mechanism reference.

Rendered to ``docs/permissions.md`` rather than the always-loaded guidance:
the dispatcher denies at the moment of violation and its message already
names the recovery, so the lattice, the edit mechanics, and the two markers
that change a decision are all material a reader opens once a denial has told
them which one applies. The guidance keeps the rule and points here.
"""

import inspect

import lup.harness.models as models
from lup.formats.markdown import CodeCell, PlainCell
from lup.policy.kernel.settlement import SETTLEMENT_ORDER, SettlementRule


def settlement_table(
    order: list[SettlementRule] = SETTLEMENT_ORDER,
) -> models.MarkdownTable:
    """The settlement order, read off the order itself.

    Every row is one rule's own summary line, in the order the pass reads
    them — so a row added, moved or dropped arrives here by being added,
    moved or dropped, and this page cannot come to describe a precedence the
    kernel does not have. Written out by hand, the same names and claims
    would be a second copy in a second file with nothing holding the two
    together.

    A rule carrying no docstring fails generation loudly rather than
    rendering an empty claim, for the reason the roster in ``library.py``
    gives: a page that quietly drops a row reads exactly like a complete one.

    The id is the first column because it is the half a reader arrives with.
    A settled verdict cites it — ``unleased-write``, ``contained-effects`` —
    and every other rule id this policy states is indexed somewhere. This
    table is where these are: the reference the docs render, which a helper
    in the kernel names when it returns them.
    """

    def says(rule: SettlementRule) -> str:
        """One rule's whole claim: the summary line PEP 257 puts first.

        Read off the class itself rather than through ``inspect.getdoc``,
        which walks up to the base — and the base here is the seam every row
        implements, so a row that said nothing would render the seam's own
        description as its claim and read exactly like a described one.
        """
        docstring = type(rule).__doc__
        if not docstring:
            raise ValueError(
                f"{type(rule).__name__} carries no docstring, so this page has "
                "nothing to say about it: open the class with a summary line "
                "stating what the row settles"
            )
        return inspect.cleandoc(docstring).splitlines()[0]

    return models.MarkdownTable(
        headers=["id", "row", "what it says"],
        rows=[
            [
                CodeCell(text=rule.id),
                CodeCell(text=type(rule).__name__),
                PlainCell(text=says(rule)),
            ]
            for rule in order
        ],
    )


DOCUMENT = models.PromptDocument(
    source=__name__,
    parts=[
        models.Passage(module=__name__),
        settlement_table(),
        models.Passage(module=__name__, name="placement"),
        # The peer policy answers from the coordination roster, which a
        # project that declined the module never has: the section describing
        # it goes with it.
        models.WhereTaken(
            module="coordination",
            parts=[models.Passage(module=__name__, name="reaching-another-session")],
        ),
        models.Passage(
            module=__name__,
            name="forge-credentials",
            values={
                # The page reviews are answered on in a browser is the
                # dashboard module's, and goes with it where it is declined.
                "dashboard": models.WhereTaken(
                    module="dashboard",
                    parts=[
                        models.TextPart(
                            text=(
                                "The operator reviews every parked request, of "
                                "every repository and session, on one page: the "
                                "dashboard, in `docs/dashboard.md`. The verbs "
                                "reaching its capability are `operator_only` "
                                "too.\n\n"
                            )
                        )
                    ],
                ),
            },
        ),
    ],
)
