"""What a repository path is for, and which gates that answer relaxes.

The lattice judges an action by what it does. A role adds the missing half:
what the thing acted upon is *for*. Production code carries the conventions
because other code reads it; tests and retained data carry none of them because
their subjects are behaviour and evidence rather than their own source shape;
scratch carries nothing at all because every file there is disposable by
construction.

Roles inside the repository are declared by the application, never the kernel
— that path vocabulary belongs to whoever laid out the tree. Two scratch roots
the kernel knows unaided, both because they sit outside every worktree where no
repo-relative declaration could reach them: the session scratchpad, whose
spelling the harness fixes, and the machine's temporary root that holds it,
where nothing is reviewed and nothing is meant to last.
"""

import fnmatch
import posixpath
from pathlib import PurePosixPath
from typing import Literal

from .decision import SUBSTITUTION_SENTINEL
from .rows import PathRoleKind, PathRoleName, PathRoleRow, DisplacedTargetRow
from .syntax import VerbatimText, expands, verbatim_piece

# lup: ignore[library-default] — the native runtimes' own plugin directory names
GENERATED_PLUGIN_ROOTS = (".claude/plugins", ".codex/plugins")
# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
GENERATED_PLUGIN_REFUSAL = (
    "this edits a generated plugin tree, which is compiled from source and"
    " already loaded"
)
# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
GENERATED_PLUGIN_RECOVERY = (
    "Edit the policy source, run `lup-devtools harness generate all`, then ask"
    " the user to restart claude or codex so the change takes effect."
)


# lup: ignore[constant-declaration] — the words this gate says, in a kernel
# compiled hermetically into a bare dispatcher that takes no arguments
FOREIGN_REPOSITORY_REFERRAL = (
    "this file belongs to a different repository, which this project's rules"
    " do not cover"
)
# lup: ignore[constant-declaration] — the words this gate says, in a kernel
# compiled hermetically into a bare dispatcher that takes no arguments
FOREIGN_REPOSITORY_RECOVERY = (
    "That repository's conventions, size budget and gates are its own, and"
    " this project's rule checker is not applying any of them. Edit it as that"
    " repository would want it, not as this one would."
)
"""What a foreign-repository edit is told, in place of a convention refusal.

The sentence has to say two things at once and be believed on both. That the
edit may proceed once a human approves it, and — the part that was actually
costing something — that the rules it is *not* being judged by were never
about it, so the way through is not to satisfy them. A refusal naming a lup
rule teaches an agent to restyle somebody else's code until the rule stops
firing, which is exactly what happened.
"""


def spells_its_path(word: str) -> bool:
    """Whether a word names exactly the path it spells.

    A word carrying an unexpanded parameter, a substitution, or a tilde names
    a different file at run time than the one written down, so every answer
    derived from reading it is an answer about a path that may never exist.

    Both readers of that fact need it and were deriving it apart. The
    redirection rule refused the create-versus-overwrite relaxation to such a
    word, while :func:`path_role` matched its declared patterns against it as
    though every component were a directory name. They disagreed about
    ``$W/tmp/f.py``, and the role won: ``**/tmp`` absorbed the ``$W`` and
    called the whole path scratch, which allowed ``rm -rf $W/tmp`` unprompted
    on the strength of a component saying nothing about where ``$W``
    resolves.

    A :class:`~lup.policy.kernel.syntax.VerbatimText` spells its path
    whatever it holds: `'a$b'` is a file named with a dollar sign.
    """
    if isinstance(word, VerbatimText):
        return True
    return not any(marker in word for marker in ("$", "~", "`", SUBSTITUTION_SENTINEL))


def is_generated_plugin_target(word: str) -> bool:
    """Recognize a path confined to a native plugin tree the harness renders.

    Every file there is compiled from typed source, so writing one by hand
    edits a build product: the change is reverted by the next generation and
    never reaches the runtime that already loaded it. The roots stop at
    ``plugins`` because their parents also hold settings, trust state, and
    hand-written skills and commands that no generator can restore.

    The roots are matched as path segments wherever they occur, so an absolute
    spelling and a sibling worktree's tree are recognized too. A relaxation
    may safely decline to resolve those, because declining leaves the ask in
    place; a refusal that only knew the repo-relative spelling would instead
    fail open on the one form that reaches past this worktree.

    Both runtimes' roots are named and both gates read this one answer: which
    tree a write lands in is the same question whichever runtime is running,
    and a refusal knowing one spelling would leave the other open.
    """
    # lup: ignore[string-split] — segment comparison on an already-normalized
    # posix path, which is what the roots are declared as
    segments = posixpath.normpath(word).split("/")
    return any(
        segments[index : index + len(parts)] == parts
        # lup: ignore[string-split] — the declared roots, in the same terms
        for parts in [root.split("/") for root in GENERATED_PLUGIN_ROOTS]
        for index in range(len(segments))
    )


# lup: ignore[library-default] — git's own file names, fixed by git rather than
# chosen for an adopter
GIT_POINTER_NAMES = ("commondir", "gitdir", "config.worktree")
# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
GIT_STATE_REFUSAL = (
    "this rewrites a file git finds its own state through -- a worktree"
    " pointer or a ref -- which host git follows to the repository, the commit"
    " and the configuration it acts on"
)
# lup: ignore[constant-declaration] — refusal wording, declared with its verdict
GIT_STATE_RECOVERY = (
    "Let git write it: `git worktree add`, `move`, `remove` and `prune` keep"
    " the pointers, and `git branch`, `git switch`, `git update-ref` and"
    " `git symbolic-ref` keep the refs. A pointer that is already wrong is"
    " repaired from an operator terminal with `git worktree repair`."
)

type GitState = Literal["pointer", "ref"]
"""The two kinds of file git finds its own state through.

A pointer names where git reads its configuration from, so rewriting one is
choosing that configuration; a ref names a commit, which git's own commands
move all the time and a person may still approve by hand."""


def git_state(word: str) -> GitState | None:
    """Which of git's own state files a path is, if it is one.

    A linked worktree's `.git` file names its administrative entry, and the
    entry's `commondir` and `gitdir` name the shared directory and the way
    back to the checkout; `config.worktree` is read as configuration wherever
    the shared config turns it on. Host git follows each of those to the
    configuration it acts on, so a hand that rewrites one chooses what the
    operator's next git command reads: a `commondir` naming a directory the
    session built hands host git that directory's `core.hooksPath`. Those are
    pointers, and so is an entry of `worktrees/` and the directory holding
    the entries, since renaming or recreating one rewrites every pointer in
    it at once. A ref -- `HEAD`, every `*_HEAD`, `packed-refs`, everything
    under `refs/` -- names a commit instead. git's own commands write every
    one of them consistently, and nothing else needs to.

    Read off the spelling, so it answers for a path no mount could hold: `git
    worktree remove` unlinks exactly these files, and a read-only bind would
    refuse it. A segment that is `.git`, or that ends in `.git` as a bare
    repository's directory does, followed by one of those names. `.git` alone
    is the pointer -- or, in a plain checkout, the repository itself -- while
    a bare `<name>.git` alone is a whole repository rather than anything a
    pointer names, and is left to the verbs that judge a directory.
    """

    def held(beneath: tuple[str, ...]) -> GitState | None:
        """What this run of names under a git directory holds, if git's own."""
        match beneath:
            case ("worktrees",) | ("worktrees", _):
                return "pointer"
            case ("worktrees", _, *inner):
                return held(tuple(inner))
            case ("refs", *_):
                return "ref"
            case (name,) if name in GIT_POINTER_NAMES:
                return "pointer"
            case (name,) if name == "HEAD" or name.endswith("_HEAD"):
                return "ref"
            case ("packed-refs",):
                return "ref"
        return None

    parts = PurePosixPath(posixpath.normpath(word)).parts
    found = [
        "pointer"
        if segment == ".git" and not parts[index + 1 :]
        else held(parts[index + 1 :])
        for index, segment in enumerate(parts)
        if segment.endswith(".git")
    ]
    return "pointer" if "pointer" in found else "ref" if "ref" in found else None


def repository_relative(word: str, checkout: str) -> str:
    """This path as the declarations spell it, where it names a file inside.

    Every reading below is lexical, and every declared root is anchored at the
    repository top, so a role reaches a path only when the path is spelled
    relative to that top. An absolute spelling of the very same file therefore
    matches nothing -- ``tmp/run.log`` is scratch by declaration and
    ``/home/you/project/tmp/run.log`` is not, though they name one file and
    one write.

    That gap is not hypothetical here: a session that cannot move between
    worktrees is told to reach another checkout by absolute path, which is the
    spelling no declaration can see.

    Rewriting is arithmetic on the segments rather than a resolution, so this
    stays a reading of the spelling and makes no filesystem call: what the
    host resolved is a separate fact, and ``..`` is normalized before the
    prefix is taken so nothing climbs out of the checkout and back in under a
    root it was never given.

    ``checkout`` is empty wherever the caller has no root in hand, and then a
    path is returned untouched -- the conservative answer this had before any
    root was passed, rather than a rewrite against a root that was guessed.
    """
    if not checkout or expands(word):
        return word
    normalized = posixpath.normpath(word)
    if not normalized.startswith("/"):
        return word
    top = posixpath.normpath(checkout)
    if normalized == top:
        return "."
    prefix = top if top.endswith("/") else f"{top}/"
    if not normalized.startswith(prefix):
        return word
    return verbatim_piece(word, normalized[len(prefix) :])


def is_session_scratch_target(word: str) -> bool:
    """Recognize a path confined to the session scratchpad.

    ``$TMPDIR`` is the harness-provided scratch root and ``/tmp/claude-*`` its
    host-side spelling, so writes there are scratch by definition. A suffix
    that expands further or climbs out of the root stays unrecognized, and
    so does a quoted `'$TMPDIR/x'`, which names a directory called `$TMPDIR`
    wherever the command runs.
    """
    for prefix in ("$TMPDIR/", "${TMPDIR}/"):
        if word.startswith(prefix) and not isinstance(word, VerbatimText):
            suffix = word[len(prefix) :]
            normalized = posixpath.normpath(suffix)
            return "$" not in suffix and not normalized.startswith(("..", "/"))
    return "$" not in word and posixpath.normpath(word).startswith("/tmp/claude-")


def is_temporary_root_target(word: str) -> bool:
    """Recognize a path under the machine's temporary root.

    A file here is disposable by construction: no review pass walks the
    temporary root, no capture of this checkout holds it, and nothing written
    there is meant to outlive the process that wrote it. That is what
    :func:`path_role` reads it for, and it is the same fact on either side of
    a container boundary, so the role is granted without one.

    Containment still decides a different question, which is why the two
    readings stay separate. Inside a measured container this root is the
    launch's own and disappears with it, so a write here escapes no lease;
    uncontained, the same directory is shared with every other process on the
    machine, and :meth:`SettlementFacts.container_private` is the fact that
    row supplies. The cost of joining them is carried openly: a delete or an
    overwrite here is allowed unprompted, and on a shared host the file it
    replaces may belong to somebody who never saw the question.

    Wider than :func:`is_session_scratch_target`, which the harness mints per
    session and owns at every placement — this one is the whole root, held to
    it by the same two tests: a word carrying an expansion stays
    unrecognized, and a suffix that climbs back out fails the same way —
    ``/tmp/../etc`` normalizes to a path this does not claim.
    """
    return "$" not in word and posixpath.normpath(word).startswith("/tmp/")


def displaced_targets(
    candidates: list[DisplacedTargetRow], rows: list[PathRoleRow]
) -> list[DisplacedTargetRow]:
    """Which resolved targets land under a role other than the one they claim.

    The kernel's half of the answer a symlink makes necessary. The host says
    where a path really lands; this says whether that changes what the path
    is, which is the only part of it a role table can decide.

    A spelling that claims nothing has nothing to lose, so a production path
    is not reported however far it resolves: its write is judged where it
    reads either way, and no relaxation was granted to be taken back. What is
    reported is a grant that would have been read off the wrong file — a
    scratch spelling landing in production, a test root landing outside it.

    Where the two roles agree the link changed nothing this table can see:
    ``tmp/a`` pointing at ``tmp/b`` is scratch either way, and the machine's
    temporary root resolving to its own real name — ``/private/tmp`` on a
    system that spells it that way — is the same root it always was.
    """
    return [
        row
        for row in candidates
        if (claimed := path_role(row["path"], rows)) != "production"
        and path_role(row["lands"], rows) != claimed
    ]


def role_pattern_covers(pattern: str, path: str) -> bool:
    """Whether a declared role pattern reaches a repository-relative path.

    A role names a tree rather than a file, so the pattern is matched against
    the path's leading segments and covers everything below what it names:
    ``tmp`` reaches ``tmp/run/log``, and ``**/__pycache__`` reaches both the
    directory itself and ``src/a/__pycache__/m.pyc``.

    Matching is per segment, so a wildcard cannot cross a ``/`` and silently
    widen a pattern past the directory it names; ``**`` is the one spelling
    that spans segments, standing for any run of them including none. That
    distinction is the whole safety argument for declaring a role by pattern
    at all — ``**/__pycache__`` names every cache directory and nothing else,
    where a substring test would also have claimed ``notes/__pycache__.bak``.

    A literal root carrying glob metacharacters — a directory actually named
    ``run[1]`` — reads as a pattern under this. Nothing in the declarations is
    spelled that way, and a root that ever is escapes the class as ``run[[]1]``.
    """

    def covers(pattern: tuple[str, ...], segments: tuple[str, ...]) -> bool:
        """Whether the pattern matches a leading run of the path's segments."""
        if not pattern:
            return True
        head, rest = pattern[0], pattern[1:]
        if head == "**":
            return any(
                covers(rest, segments[index:]) for index in range(len(segments) + 1)
            )
        if not segments:
            return False
        return fnmatch.fnmatchcase(segments[0], head) and covers(rest, segments[1:])

    return covers(PurePosixPath(pattern).parts, PurePosixPath(path).parts)


def normalized_path(path: str) -> str:
    """Normalize one portable path without resolving against the filesystem."""
    # lup: ignore[string-replace] — a posix parser cannot read a Windows path,
    # so settling the separator convention is what makes the string parseable
    # at all, rather than something the parser below could have done instead
    return posixpath.normpath(path.replace("\\", "/"))


def root_matches(path: str, value: str, kind: PathRoleKind) -> bool:
    """Whether one path sits under a declared directory, however it is declared.

    The one answer both declaration tables need. A protected-path rule and a
    role row ask this of the same two shapes, and asking it in one place is
    what keeps a directory from meaning one thing to the gate that protects it
    and another to the gate that says what it is for.

    ``contains_part`` matches the directory wherever it sits, and excludes the
    system temporary directory, which is a different tree that happens to share
    the name. Wrapping both sides in separators is what makes a segment match a
    segment: ``tmpfoo`` holds the characters and is not the directory.
    """
    portable = normalized_path(path)
    expected = normalized_path(value)
    if kind == "contains_part":
        return f"/{expected}/" in f"/{portable}/" and not portable.startswith(
            f"/{expected}"
        )
    return portable == expected or portable.startswith(expected + "/")


def path_role(path: str, rows: list[PathRoleRow]) -> PathRoleName:
    """Classify a repository-relative path by the role its root declares.

    A root is a pattern, so a tree scattered through the repository rather
    than gathered under one prefix — every ``__pycache__``, every ``.bak`` —
    is declarable as what it is. Disposability stays something a project
    states: nothing is scratch for merely being untracked, which is what
    keeps an ignored ``.env.local`` protected unless its project deliberately
    declares it as data.

    Resolution is lexical, so it needs no filesystem call and ``..`` cannot
    climb out of a declared root into a role it was never given. A symlink
    inside a root that points beyond it needs the syscall this will not make,
    and is answered instead by the fact :func:`displaced_targets` reads: the
    host resolves the link, and a landing under another role takes the grant
    back. Nothing here consults that — a role says what a spelling claims,
    which is the question with a lexical answer.

    How far each declaration reaches is the row's own, through
    :func:`role_pattern_covers`: a bare root is anchored at the repository
    top, and a leading ``**/`` recognizes the directory wherever it sits.

    The two absolute scratch roots answer first — the session scratchpad the
    harness mints, and the machine's temporary root around it — because
    reaching the declared roots below would mean passing the guard that keeps
    an absolute path from claiming a role by prefix. A path outside the
    repository is then read only against rows rooted at an absolute path,
    which the host spells for a tree it placed -- another worktree of this
    repository, whose declared scratch is scratch as this checkout's is. Every
    other path outside stays production: a file in some other tree is not
    disposable merely for being elsewhere, while one under ``/tmp`` is
    disposable by what that root is for.
    """
    normalized = verbatim_piece(path, posixpath.normpath(path))
    if is_session_scratch_target(path) or is_temporary_root_target(path):
        return "scratch"
    if normalized.startswith(("/", "../")) or normalized == "..":
        for row in rows:
            if (
                row["root"].startswith("/")
                and spells_its_path(normalized)
                and role_pattern_covers(row["root"], normalized)
            ):
                return row["role"]
        return "production"
    # A declared pattern matches directory names, and an unexpanded word is
    # not one. The scratchpad above is the one opaque spelling with an answer,
    # and it earns it by checking its own suffix rather than by pattern.
    if not spells_its_path(normalized):
        return "production"
    for row in rows:
        if role_pattern_covers(row["root"], normalized):
            return row["role"]
    return "production"


def sibling_scratch_rows(
    trees: list[str], rows: list[PathRoleRow]
) -> list[PathRoleRow]:
    """This checkout's declared scratch, rooted at each other checkout of it.

    Another worktree of the same repository is the same project on another
    branch, so what this one declares disposable is disposable there -- a
    write into its `tmp/` and a delete there are one rule, as they are here.
    Only scratch crosses: the rest of that tree is another checkout's
    production, and a role granting anything there would be reading this
    one's history as if it were that one's. Spelled where each tree stands,
    because an absolute path is how a session reaches one, and the only
    spelling :func:`path_role` reads against a row rooted there.
    """
    return [
        PathRoleRow(root=posixpath.join(tree, row["root"]), role=row["role"])
        for tree in trees
        for row in rows
        if row["role"] == "scratch" and not row["root"].startswith("/")
    ]


def declared_scratch(spelled: str, rows: list[PathRoleRow]) -> bool:
    """Whether a path inside this checkout sits under a root it declares scratch.

    Narrower than :func:`path_role` answering ``"scratch"`` by exactly the two
    roots the kernel knows unaided. The session scratchpad and the machine's
    temporary root are scratch for every checkout and belong to none, so an
    absolute spelling, one climbing out, and one only a run can expand all
    say no. What is left is a repository-relative path under a root this
    project declared, which is the only scratch a checkout can answer for.
    """
    normalized = verbatim_piece(spelled, posixpath.normpath(spelled))
    if normalized.startswith(("/", "../")) or normalized == "..":
        return False
    return spells_its_path(normalized) and path_role(normalized, rows) == "scratch"
