"""The command tree a person reads and preserves this repository's notes through.

An agent records nodes as it works, and the record is worth nothing if the
person the work is for cannot read it. This is that half: what is in the DAG,
where each node stands *right now* rather than where it was labelled, and the
one command that puts a copy somewhere git will keep.

It is the reader whose subject is the log rather than any one type, so it is
also the one place a list of node classes is needed: a record names its type
and not the class that reads it, so something has to try. Everything else in
the codebase asks the store for the type it actually wants.

Every command folds both halves of the log — the journal committed with the
code and the one under the shared git directory — so any checkout answers
and none of them owns the answer. Nothing holds a lock: a listing folds
append-only files, which is why a session recording flat out does not delay
a console reading it, and a console cannot wedge a session.
"""

from datetime import datetime
from typing import Annotated

import typer

from lup.coordination.briefs import (
    Audience,
    DetachedBrief,
    PeerBrief,
    PersonBrief,
    render_brief,
)
from lup.coordination.delegate import delegate
from lup.coordination.handoffs import Established, Handoff, hand_off
from lup.coordination.identity import mint_member_id
from lup.coordination.rendering import USER_HOLDER, render
from lup.coordination.repository import RepositoryPeers
from lup.coordination.tasks import NEEDS_NAMES, Needs, Task
from lup.coordination.refs import ActorRef
from lup.devtools.utils import refuse
from lup.ledger.cite import read_cites
from lup.ledger.journal import LedgerRefusal, LedgerStore
from lup.ledger.kinds import by_kind, declared_fields, kind_of, summary_of
from lup.ledger.migrate import migrate
from lup.ledger.models import LedgerEdge, LedgerNode
from lup.ledger.snapshot import snapshot
from lup.ledger.writeup import Writeup, WriteupError, write_writeup
from lup.observability.sessions import session_recorder
from lup.observability.sweep import index_notes
from lup.policy.kernel.diagnostic import devtools, step
from lup.types import JsonObject
from pathlib import Path
from pydantic import TypeAdapter, ValidationError
from lup.ledger.store import LedgerLayout
from lup.workspace.edition import shared_git_directory
from lup.workspace.paths import project_root

SNAPSHOT_BRANCH = "lup/ledger"
"""The branch a snapshot lands on: the record's own, never the code's.

Nodes accumulate as work happens, and one per turn in the history of the code
would make that history unreadable. On a branch nothing merges, the record is
preserved and shares no history with what it is about.
"""


def validated_needs(spelling: str) -> Needs:
    """One `--needs` word as the closed vocabulary, refusing anything else.

    Refused rather than coerced, because the whole value of the field is that
    a rendering can order by it: one unrecognised spelling is a row nothing
    groups, and a silent fallback would produce exactly that.
    """
    if spelling in NEEDS_NAMES:
        return spelling
    refuse(
        "is not a word --needs takes; it takes one of "
        + ", ".join(name for name in NEEDS_NAMES if name),
        what=spelling,
        code=2,
    )


def validated_audience(spelling: str) -> Audience:
    """One `--for` word as the reader it names, refusing anything else.

    Narrowing a word somebody typed into one of the declared readers, which is
    the boundary case: past here nothing asks what kind of audience it holds,
    it asks the audience.
    """
    match spelling:
        case "peer":
            return PeerBrief()
        case "detached":
            return DetachedBrief()
        case "person":
            return PersonBrief()
        case _:
            refuse(
                "is not a reader --for names; it names peer, detached or person",
                what=spelling,
                code=2,
            )


def validated_results(spellings: list[str]) -> list[Established]:
    """Each `--result` as the model it has to be, refusing anything short.

    JSON through the model rather than a delimiter this would have to split,
    because a statement containing whatever character was chosen is a
    statement that would silently lose half of itself.
    """

    def parsed(spelling: str) -> Established:
        try:
            return Established.model_validate_json(spelling)
        except ValueError:
            refuse(
                "is not an established result: every one needs a statement, a"
                ' source and a grade, as {"statement": ..., "source": ...,'
                ' "grade": ...}',
                what=spelling,
                code=2,
            )

    return [parsed(spelling) for spelling in spellings]


def node_line(
    store: LedgerStore, node: LedgerNode, classes: list[type[LedgerNode]]
) -> str:
    """One node as a person reads it: what it is, where it stands, who said so.

    The classes resolve the neighbourhood, so a claim shown here asks its
    evidence the evidence's own answer — stale, superseded — and not the
    base's, which counts every piece as standing.
    """
    where = store.standing(node, classes)
    label = f"{where.label}: {where.reason}" if where.reason else where.label
    return " — ".join(
        part
        for part in (
            f"{node.id}  [{node.kind}] {node.title}",
            label,
            node.author.label(),
        )
        if part
    )


def parsed_payload(text: str) -> JsonObject:
    """One `--json` object as the fields it names, refusing anything else.

    Through the JSON parser and into the store's own payload type, so the type
    being recorded validates the fields and this never has to know them.
    """
    try:
        return TypeAdapter(JsonObject).validate_json(text or "{}")
    except ValidationError as invalid:
        refuse(f"must be a JSON object: {invalid}", what="--json", code=2)


def create_ledger_app(
    classes: list[type[LedgerNode]],
    edges: list[type[LedgerEdge]] | None = None,
    writeups: list[Writeup] | None = None,
    layout: LedgerLayout = LedgerLayout(),
) -> typer.Typer:
    """Wire the command tree for this repository's notes, over these types.

    The types are the project's and arrive here rather than being looked up,
    which is the same inversion every other sub-app takes: the library ships
    the commands and the project ships what they are about. A project that
    declares none still reads the DAG — every record is listed, each as the
    base class, which is the honest answer for a log a later build may hold
    types for. Recording and relating are generic for the same reason: a kind
    is looked up in what the project declared, and the type validates the
    fields, so there is one `record` rather than one command per kind. The
    layout is the project's too: which kinds are committed with the code and
    which stay local under the git directory.
    """
    app = typer.Typer(no_args_is_help=True)
    relations = list(edges or [])
    node_kinds = by_kind(classes)
    edge_kinds = by_kind(relations)

    def author() -> ActorRef:
        """Whoever is at the console, as every record this tree writes is stamped."""
        return ActorRef(kind="console", id=mint_member_id())

    def store() -> LedgerStore:
        return LedgerStore(project_root(), author(), layout)

    def found(held: LedgerStore, node_id: str) -> LedgerNode:
        node = held.resolve(node_id, classes)
        if node is None:
            refuse(
                "is the id of no node in this repository",
                what=node_id,
                steps=[step("see every node", devtools("ledger", "list"))],
            )
        return node

    @app.command("types")
    def types_cmd() -> None:
        """List the node and edge types this project declares, with their fields.

        The fields are what `record --json` and `relate --json` accept, read
        off the types so the listing cannot disagree with what is validated.
        Each node kind says which half of the log it is written to; a
        relation's placement follows its two ends.
        """
        if not classes and not relations:
            typer.echo("This project declares no ledger types.")
            return
        for declared in classes:
            typer.echo(
                f"{kind_of(declared)}  ({declared.__name__},"
                f" {layout.placement(kind_of(declared))}) — {summary_of(declared)}"
            )
            for line in declared_fields(declared):
                typer.echo(f"    {line}")
        for declared in relations:
            typer.echo(
                f"{kind_of(declared)}  ({declared.__name__}) — {summary_of(declared)}"
            )
            for line in declared_fields(declared):
                typer.echo(f"    {line}")

    @app.command("record")
    def record_cmd(
        kind: Annotated[str, typer.Argument(help="Which declared node kind")],
        title: Annotated[str, typer.Argument(help="What it is, in one line")],
        payload: Annotated[
            str,
            typer.Option("--json", help="The kind's other fields, as a JSON object"),
        ] = "",
        text: Annotated[str, typer.Option("--text", help="What it says")] = "",
        slug: Annotated[
            str, typer.Option("--slug", help="A readable handle, unique in this log")
        ] = "",
        attach: Annotated[
            list[Path] | None,
            typer.Option("--attach", help="A file whose bytes this node carries"),
        ] = None,
    ) -> None:
        """Record one node of any declared kind; the kind validates the fields.

        Anything the kind derives from the working tree — evidence pinning the
        digests of the files it was checked against — it fills itself as it is
        recorded, so a caller names paths and never computes a digest.
        """
        declared = node_kinds.get(kind)
        if declared is None:
            refuse(
                "is not a declared node kind",
                what=kind,
                steps=[
                    step(
                        "see the kinds this project declares",
                        devtools("ledger", "types"),
                    )
                ],
                code=2,
            )
        fields = parsed_payload(payload)
        if slug:
            fields = {**fields, "slug": slug}
        try:
            node = store().record_fields(
                declared,
                title,
                fields,
                text=text,
                attachments=[path.read_bytes() for path in attach or []],
            )
        except ValidationError as invalid:
            refuse(str(invalid), what=kind, code=2)
        except LedgerRefusal as refused:
            refuse(str(refused), what=kind, code=2)
        typer.echo(
            f"{node.id}: {node.title}" + (f"  ({node.slug})" if node.slug else "")
        )

    @app.command("relate")
    def relate_cmd(
        kind: Annotated[str, typer.Argument(help="Which declared edge kind")],
        source: Annotated[str, typer.Argument(help="The node the relation runs from")],
        target: Annotated[str, typer.Argument(help="The node it runs to")],
        payload: Annotated[
            str,
            typer.Option("--json", help="The kind's other fields, as a JSON object"),
        ] = "",
    ) -> None:
        """Draw one edge of any declared kind between two nodes.

        The edge may refuse the pair — a verification of your own work is not
        one — and the refusal is printed in its own words rather than swallowed.
        """
        declared = edge_kinds.get(kind)
        if declared is None:
            refuse(
                "is not a declared edge kind",
                what=kind,
                steps=[
                    step(
                        "see the kinds this project declares",
                        devtools("ledger", "types"),
                    )
                ],
                code=2,
            )
        held = store()
        try:
            edge = held.relate_fields(
                declared,
                found(held, source),
                found(held, target),
                parsed_payload(payload),
            )
        except ValidationError as invalid:
            refuse(str(invalid), what=kind, code=2)
        except LedgerRefusal as refused:
            refuse(str(refused), what=kind, code=2)
        typer.echo(f"{edge.kind}: {edge.source} -> {edge.target}")

    @app.command("amend")
    def amend_cmd(
        node_id: Annotated[str, typer.Argument(help="The node to change, by id")],
        payload: Annotated[
            str, typer.Option("--json", help="The fields to change, as a JSON object")
        ],
    ) -> None:
        """Record a node again with some fields changed; nothing is overwritten.

        Validated as the whole node rather than patched, so a change that
        would leave the record invalid is refused the way a fresh one is.
        """
        held = store()
        node = found(held, node_id)
        try:
            changed = type(node).model_validate(
                {**node.model_dump(), **parsed_payload(payload)}
            )
        except ValidationError as invalid:
            refuse(str(invalid), what=node_id, code=2)
        held.amend(changed)
        typer.echo(f"{node.id}: amended")

    @app.command("cite")
    def cite_cmd(
        document: Annotated[Path, typer.Argument(help="A markdown file to check")],
    ) -> None:
        """Check every cite in one document against where its node stands now."""
        readings = read_cites(document.read_text(encoding="utf-8"), store(), classes)
        failing = [reading for reading in readings if not reading.holds()]
        for reading in failing:
            typer.echo(f"{document}:{reading.cite.line}  {reading.problem()}")
        typer.echo(f"{len(readings) - len(failing)} of {len(readings)} cite(s) hold")
        if failing:
            raise typer.Exit(1)

    @app.command("writeup")
    def writeup_cmd(
        name: Annotated[
            str, typer.Argument(help="Which declared writeup; every one when omitted")
        ] = "",
        check: Annotated[
            bool,
            typer.Option("--check", help="Verify the file on disk against this ledger"),
        ] = False,
    ) -> None:
        """Generate the documents this project declares over its ledger.

        Written from this machine's log and committed like any document. One
        whose every rendered kind is committed renders the same on every
        machine, so it is also generated and drift-checked with every other
        repository artifact. One rendering a local kind is not drift-checked
        by `dev check`: the local half is live state under the git directory,
        so the same declaration renders differently where nothing has been
        recorded, and `--check` asks whether the file on disk is what this
        machine would render — the question a person about to commit one has.
        """
        declared = list(writeups or [])
        chosen = [each for each in declared if not name or each.name == name]
        if not chosen:
            names = ", ".join(each.name for each in declared) or "none"
            refuse(
                f"is not a declared writeup; this project declares {names}",
                what=name,
            )
        for each in chosen:
            try:
                written = write_writeup(
                    each, classes, project_root(), check=check, layout=layout
                )
            except (RuntimeError, WriteupError) as problem:
                refuse(str(problem), what=each.name)
            typer.echo(f"{'verified' if check else 'written'} {written}")

    @app.command("list")
    def list_cmd(
        kind: Annotated[
            str, typer.Option("--kind", help="Show only nodes of this kind")
        ] = "",
        since: Annotated[
            str,
            typer.Option(
                "--since", help="Only nodes with a record after this ISO 8601 moment"
            ),
        ] = "",
    ) -> None:
        """Show every node in this repository, with its standing read now.

        The whole DAG rather than one type's share of it, because a person
        reading the record wants what is there — and a node whose type this
        build does not declare is exactly the row worth seeing, since it means
        another session recorded something. It reads as the base class and
        says its kind, rather than being hidden. `--since` is how a reader
        opens on what moved: nodes with a record — theirs, or an edge touching
        them — newer than the moment they last looked.
        """
        held = store()
        # ISO 8601 through the standard library's own parser, because the
        # option type's fixed formats refuse the offsets and microseconds a
        # node's own `at` prints — and that is exactly what a reader pastes.
        try:
            moment = datetime.fromisoformat(since) if since else None
        except ValueError as invalid:
            refuse(f"must be ISO 8601: {invalid}", what="--since", code=2)
        moved = held.moved_since(moment) if moment is not None else None
        with held.batch():
            rows = [
                node
                for node in held.all_nodes(classes)
                if (not kind or node.kind == kind)
                and (moved is None or node.id in moved)
            ]
            if not rows:
                typer.echo(
                    f"No node of kind {kind!r} is recorded."
                    if kind
                    else "This repository has recorded no nodes."
                )
                return
            for node in rows:
                typer.echo(node_line(held, node, classes))

    @app.command("show")
    def show_cmd(
        node_id: Annotated[str, typer.Argument(help="The node to read, by id")],
    ) -> None:
        """Print one node in full, with its edges and what it attaches."""
        held = store()
        found = held.resolve(node_id, classes)
        if found is None:
            refuse(
                "is the id of no node in this repository",
                what=node_id,
                steps=[step("see every node", devtools("ledger", "list"))],
            )
        typer.echo(node_line(held, found, classes))
        if found.text:
            typer.echo(found.text)
        if found.slug:
            typer.echo(f"  slug {found.slug}")
        incoming = held.into(found.id)
        # Counted by kind before they are listed, because the count is what a
        # reader weighs a node by — how much rests on it, how many answer it —
        # and it is shown rather than folded into any stored importance.
        for kind in dict.fromkeys(edge.kind for edge in incoming):
            count = sum(1 for edge in incoming if edge.kind == kind)
            typer.echo(f"  {count} incoming {kind}")
        for edge in held.out_of(found.id):
            typer.echo(f"  out  {edge.kind} -> {edge.target}")
        for edge in incoming:
            typer.echo(f"  in   {edge.kind} <- {edge.source}")
        for digest in found.attachments:
            missing = "" if held.blobs.holds(digest) else "  (not on this machine)"
            typer.echo(f"  blob {digest}{missing}")

    @app.command("delegate")
    def delegate_cmd(
        title: Annotated[str, typer.Argument(help="What the work is")],
        to: Annotated[
            str, typer.Option("--to", help="Which peer holds it, by name or id")
        ] = "",
        text: Annotated[str, typer.Option("--text", help="What it involves")] = "",
        needs: Annotated[
            str, typer.Option("--needs", help="What class of input it waits on")
        ] = "",
        path: Annotated[
            list[str] | None,
            typer.Option("--path", help="A path it touches, taken as a lock"),
        ] = None,
    ) -> None:
        """Record one task, hand it to a peer if there is one, and say what happened.

        A name nobody answers to parks the task rather than refusing it, which
        is a real state: work is often scoped before there is anybody to do it.
        """
        root = project_root()
        result = delegate(
            RepositoryPeers(root),
            store(),
            title,
            to=to,
            text=text,
            needs=validated_needs(needs),
            paths=list(path or []),
            root=root,
        )
        typer.echo(f"{result.task.id}: {result.task.title}")
        if result.holder:
            typer.echo(f"  held by {result.holder}")
        for taken in result.locked:
            typer.echo(f"  locked {taken}")
        if result.woken:
            typer.echo("  woken")
        if result.note:
            typer.echo(f"  {result.note}")

    @app.command("handoff")
    def handoff_cmd(
        title: Annotated[str, typer.Argument(help="What body of work this is")],
        open_question: Annotated[
            list[str],
            typer.Option("--open", help="Something still undecided; at least one"),
        ],
        to: Annotated[
            str, typer.Option("--to", help="Which peer takes it, by name or id")
        ] = "",
        text: Annotated[str, typer.Option("--text", help="What it involves")] = "",
        result: Annotated[
            list[str] | None,
            typer.Option("--result", help="An established result, as a JSON object"),
        ] = None,
        not_again: Annotated[
            list[str] | None,
            typer.Option("--not-again", help="An approach already tried and dropped"),
        ] = None,
        task: Annotated[
            list[str] | None,
            typer.Option("--task", help="A task id that crosses with it"),
        ] = None,
        path: Annotated[
            list[str] | None,
            typer.Option("--path", help="A path in scope, whose lock moves with it"),
        ] = None,
        watch: Annotated[
            list[str] | None,
            typer.Option(
                "--watch", help="A node the work rests on; the brief says if it moved"
            ),
        ] = None,
    ) -> None:
        """Move a body of work to a peer, and say exactly what crossed.

        A path the sender holds moves with the work. A path somebody else
        holds is recorded as contested rather than taken away from them: only
        a holder can release a lock, and refusing the whole handoff over one
        overlap would make handing work over expensive enough to skip.
        """
        root = project_root()
        result_of = hand_off(
            RepositoryPeers(root),
            store(),
            title,
            open_questions=list(open_question),
            to=to,
            text=text,
            established=validated_results(list(result or [])),
            not_again=list(not_again or []),
            tasks=list(task or []),
            paths=list(path or []),
            watch=list(watch or []),
            root=root,
        )
        typer.echo(f"{result_of.handoff.id}: {result_of.handoff.title}")
        if result_of.to:
            typer.echo(f"  handed to {result_of.to}")
        for moved in result_of.transferred:
            typer.echo(f"  transferred {moved}")
        for taken in result_of.locked:
            typer.echo(f"  locked {taken}")
        for disputed in result_of.contested:
            typer.echo(
                f"  contested {disputed} — somebody else holds it, both recorded"
            )
        if result_of.woken:
            typer.echo("  woken")
        if result_of.note:
            typer.echo(f"  {result_of.note}")

    @app.command("brief")
    def brief_cmd(
        node_id: Annotated[str, typer.Argument(help="The handoff to render, by id")],
        for_whom: Annotated[
            str,
            typer.Option("--for", help="peer, detached, or person"),
        ] = "peer",
    ) -> None:
        """Write one handoff out for whoever is going to read it.

        The same record either way. A peer resolves the ids, an agent with no
        repository gets what a peer would look up spelled out, and a person
        gets a file — so the substance never depends on who asked.
        """
        held = store()
        # Both reads are typed rather than resolved against the whole class
        # list, because this command is about one type: an id naming something
        # else is not a handoff, which is the answer rather than a narrowing.
        found = next((node for node in held.read(Handoff) if node.id == node_id), None)
        if found is None:
            refuse(
                "is the id of no handoff in this repository",
                what=node_id,
                steps=[
                    step(
                        "see the handoffs",
                        devtools("ledger", "list", "--kind", kind_of(Handoff)),
                    )
                ],
            )
        carried = {task.id: task for task in held.read(Task)}
        moved = [
            carried[edge.target]
            for edge in held.out_of(found.id)
            if edge.kind == "coordination:transfers" and edge.target in carried
        ]
        changed = held.moved_since(found.at, found.watch)

        def watched_line(spelling: str) -> str:
            node = held.resolve(spelling, classes)
            if node is None:
                return f"- `{spelling}` — no such node"
            where = held.standing(node, classes)
            moved_note = " — **moved since this handoff**" if node.id in changed else ""
            return f"- `{spelling}` — {where.label}: {where.reason}{moved_note}"

        typer.echo(
            render_brief(
                found,
                moved,
                validated_audience(for_whom),
                watched=[watched_line(spelling) for spelling in found.watch],
            )
        )

    @app.command("mine")
    def mine_cmd(
        holder: Annotated[
            str, typer.Option("--holder", help="Whose list to render")
        ] = USER_HOLDER,
    ) -> None:
        """Print one holder's outstanding tasks, grouped by what they cost.

        The person's list by default, because that is the one somebody reads
        on a machine that cannot ask the log.
        """
        typer.echo(render(store(), holder))

    @app.command("done")
    def done_cmd(
        node_id: Annotated[
            str, typer.Argument(help="The task or handoff to close, by id")
        ],
    ) -> None:
        """Mark one piece of work finished, by recording it again as done.

        A task its holder has finished, or a handoff its receiver has: both
        are work, and the kinds that are records instead say so rather than
        pretending to close. Nothing is overwritten — what it looked like
        stays in the log and the reading takes the latest, so who closed it
        and when are both there.
        """
        held = store()
        found = held.resolve(node_id, classes)
        closed = found.completed() if found is not None else None
        if found is None or closed is None:
            refuse(
                "names a record, which is not work that can be done"
                if found is not None
                else "is the id of no node in this repository",
                what=node_id,
            )
        held.amend(closed)
        typer.echo(f"{found.id}: done")

    @app.command("migrate")
    def migrate_cmd(
        kinds: Annotated[
            list[str] | None,
            typer.Argument(
                help="Which declared kinds to settle; every misplaced record where none is named"
            ),
        ] = None,
    ) -> None:
        """Copy a kind's records into the journal its placement now declares.

        A kind's placement decides the journal at the moment a record is
        appended, so moving a kind after records exist leaves every earlier
        one where it was: the fold still reads them and nothing is lost, but
        the committed half no longer carries the kind a diff is meant to show.
        This copies those records, and the blobs they attach, into the journal
        the mapping now declares — the edges at them too, since an edge is
        committed only where both of its ends are.

        The source lines stay where they are. This log is only ever appended
        to, a read folds the two copies into one and takes the declared
        journal's, and the committed half is merged by a driver that restores
        a line any branch still holds. Running it again copies nothing.
        """
        named = list(kinds or [])
        for kind in named:
            if kind not in node_kinds and kind not in edge_kinds:
                refuse(
                    "is not a declared kind",
                    what=kind,
                    steps=[
                        step(
                            "see the kinds this project declares",
                            devtools("ledger", "types"),
                        )
                    ],
                    code=2,
                )
        held = store()
        moved = migrate(held, named or None)
        if not moved.copied and not moved.settled:
            typer.echo("Every record sits in the journal its kinds declare.")
            return
        typer.echo(f"kinds moved: {', '.join(moved.kinds())}")
        for placement in held.roots:
            landed = [each for each in moved.copied if each.declared == placement]
            if landed:
                typer.echo(
                    f"{len(landed)} record(s) copied to {held.journal(placement)}"
                )
        if moved.settled:
            typer.echo(f"{len(moved.settled)} record(s) were already there")
        if moved.blobs():
            typer.echo(f"{len(moved.blobs())} blob(s) copied beside them")
        for digest in moved.missing():
            typer.echo(f"no half of the store holds the blob {digest}")

    @app.command("index-notes")
    def index_notes_cmd() -> None:
        """Record the runs already under notes/ that the log points at nothing of.

        Recording starts when a recorder is wired, so a tree written before
        that holds runs the ledger says nothing about. This walks it: one
        closed session per session directory under `notes/traces/` and per
        launch directory under `notes/harness/`, with the journal pinned as
        the tree holds it and the outcome read off that journal's last
        record, and one output per result document under a session, about
        that session. A directory the log already points at is passed over,
        so running it again records nothing.
        """
        recorder = session_recorder(project_root(), author(), classes, layout)
        if recorder is None:
            refuse(
                "this project does not index its runs: observability:session and"
                " observability:output are not among its declared kinds"
            )
        swept = index_notes(recorder)
        typer.echo(
            f"{len(swept.sessions())} session(s) and {len(swept.outputs())} output(s)"
            f" recorded; {len(swept.known())} already indexed"
        )
        for directory in swept.refused():
            typer.echo(f"the ledger refused the run at {directory}; the log says why")

    @app.command("snapshot")
    def snapshot_cmd(
        branch: Annotated[
            str, typer.Option("--branch", help="Where the copy is committed")
        ] = SNAPSHOT_BRANCH,
    ) -> None:
        """Commit the local half to a branch of its own, for a record worth keeping.

        The local half is live untracked state the rest of the time, which
        is what keeps a node per turn out of the history of the code. This is
        the deliberate act that preserves it, and it is somebody asking rather
        than something happening on every write. The committed half is
        already in git, so it is not copied: the snapshot is the local
        journal and the blobs beside it, and nothing else.
        """
        root = project_root()
        kept = (
            f"  committed kinds are {layout.committed.describe()}: already in git,"
            " not copied"
            if layout.committed is not None
            else ""
        )
        held = layout.local.root(root)
        if not held.exists():
            if kept:
                typer.echo(kept, err=True)
            refuse("this repository has recorded nothing local to snapshot")
        commit = snapshot(
            held,
            shared_git_directory(root),
            branch,
            f"snapshot({branch}): the local half of the ledger as it stands",
        )
        typer.echo(f"{branch} at {commit[:12]}: {held}")
        if kept:
            typer.echo(kept)

    @app.command("explore")
    def explore_cmd(
        export: Annotated[
            Path | None,
            typer.Option(
                "--export", help="Write one self-contained page here instead of serving"
            ),
        ] = None,
        host: Annotated[str, typer.Option(help="Interface to bind")] = "127.0.0.1",
        port: Annotated[int, typer.Option(help="TCP port to bind")] = 8767,
        open_page: Annotated[
            bool, typer.Option("--open/--no-open", help="Open the page in a browser")
        ] = True,
    ) -> None:
        """Open the log in a browser, or write it as one self-contained page.

        Every node listed with its standing read now, narrowed by kind,
        standing or the moment it last moved; one node in full; the DAG drawn.
        Served on the loopback and refusing any other bind, like every local
        surface. `--export` writes the same page with the whole log embedded —
        a memo attachment opened without a server, showing what the log held
        when it was written.
        """
        # Imported where it is used, because the explorer serves a page and
        # that is the `web` extra: a project without it still reads its log
        # through every other command here.
        from lup.ledger.explorer import export_explorer, serve_explorer

        root = project_root()
        if export is not None:
            written = export_explorer(root, classes, relations, export, layout=layout)
            typer.echo(f"written {written}")
            return
        try:
            serve_explorer(
                root, classes, relations, host, port, open_page, layout=layout
            )
        except ValueError as error:
            refuse(str(error), code=2)

    return app
