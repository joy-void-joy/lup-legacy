# lup: ignore[empty-collection, import-re, re-call, set-shape]
"""Single importable source of truth for the codebase's rule set.

The line rules are declared here. The generated hermetic edit policy denies an
edit whose added lines match one of them unless the line carries a
`# lup: ignore`. Harness generation projects these rows into its
dependency-free runtime; a test asserts that projection stays identical, and
the `lup-devtools dev check --antipatterns` auditor consumes them to scan the
whole tree after the fact (catching lines that slipped in past the hook and
`# lup: ignore` markers that no longer guard anything). The project rules and
the composition rule are declared beside the audits that decide them and
gathered into the same :class:`RuleSet` below, so one set holds every rule a
project is judged by, whichever surface decides it.

Each rule carries a stable kebab-case ``id``. A directive names rules
pyright-style — `# lup: ignore[dict-get]` silences only ``dict-get`` on that
line, `# lup: ignore[a, b]` a list — so one open site opts out of one rule
without blinding the others. The bare `# lup: ignore` stays valid (it silences
every rule) and the auditor surfaces it as "untyped", an advisory rather than a
blocker, so each one is visible until it names the rules it means.

The set is a syntax-aware linter pass, not a raw grep: every rule declares the
syntactic ``context`` it inspects, and source is masked once (via
`lup.harness.codescan.common.LineProjections` — Python through its tokenizer,
the TypeScript family through the kernel's span scan) so "code" rules scan
lines with string literals and comments blanked while "comment" directive
rules see comments intact. Every Python rule selects its violations from the
tree; the regex it also carries is the fallback for source no tree can be had
from, and the sole detector for the TypeScript table, which this grammar is
not for. Neither
gate reaches for a lint engine to do it: ruff has no plugin API, and the ones
that do — flake8, pylint, semgrep — could not run inside the hook.

A matcher reads the tree, which both gates have, so both judge a line the
same way — a rule the kernel keeps flagging while the audit calls its marker
spurious is a change one gate demands and the other refuses. What no tree
settles is what a bare receiver *is*: `lup.harness.codescan.grammar` resolves the
declaration behind a selected site, so `dict-get` can distinguish a mapping
from an HTTP client. That needs a type checker, which both gates reach — the
audit through an oracle it holds, the hook through the resolver its
dispatcher runs over the text it is about to write. It returns
`Refutation` rows this module drops — and reports the
surviving directives for. Regex alone remains where a rule is text-shaped. The
`lup.harness.codescan.registry` index and the generated `docs/rules.md` reference list
this family beside the boundary, spelling, and architecture rules.

Most messages read the same to everyone, so most of the table is a literal. A
rule that has to name a native tool asks the runtime for the words instead:
`pdf-extraction` reaches `NativeSpellings.read_document`, and
:func:`rule_set_for` compiles the table once per plugin so a runtime
with no such tool ships the rule without one named.

Each entry pairs a stable id and a compiled regex with the message the hook and
auditor show. Beyond `pydantic` and the harness seam that spells those words,
this module imports only the standard library (directly and through
`lup.harness.codescan.common`) so the auditor can load it cheaply;
`# lup:` marker detection stays in `lup.harness.codescan.markers`, and the shared scan
core — ignore matching, comment-column tokenization, the masked line
projections, the line cursor — in `lup.harness.codescan.common`, which this set's
consumers and the auditor import directly.
"""

import re

from pydantic import BaseModel, Field

from lup.harness.codescan.boundaries import (
    ASSEMBLY_RULE,
    CONSTANT_DECLARATION_RULE,
    FRONT_DOOR_RULE,
    KERNEL_IMPORTS_RULE,
    LIBRARY_DEFAULT_RULE,
    NATIVE_SPELLING_RULE,
    SEAM_RULE,
)
from lup.harness.codescan.capabilities import ABSTRACT_DECLARATION_RULE, CAPABILITY_RULE
from lup.harness.codescan.references import STALE_REFERENCE_RULE
from lup.harness.codescan.dispatch import DISPATCH_RULE
from lup.harness.codescan.narrowing import CHAIN_RULE
from lup.harness.codescan.portable import PORTABLE_RULE, CompositionRule
from lup.harness.codescan.project import ProjectRule
from lup.harness.codescan.common import (
    AntiPattern,
    LineProjections,
    PythonContext,
    Matcher,
    Refutation,
    RuleExample,
    RuleSelection,
    RULE_CONTEXTS,
    RuleContext,
    TypeFamily,
    file_level_ignore,
    ignore_rule_ids,
)
from lup.harness.contracts import Spelling, Unsupported
from lup.policy.kernel.edit import (
    HISTORICAL_VOICE_RE,
    MARKDOWN_SUFFIXES,
    historical_voice_sites,
    namedtuple_sites,
    noqa_sites,
    pyright_ignore_sites,
    type_ignore_sites,
    all_export_sites,
    bare_except_sites,
    except_baseexception_sites,
    global_statement_sites,
    elif_chain_sites,
    wildcard_guard_sites,
    model_config_sites,
    private_class_sites,
    private_function_sites,
    private_variable_sites,
    any_type_sites,
    bare_basemodel_sites,
    bare_object_sites,
    frozenset_shape_sites,
    set_shape_sites,
    dict_str_object_sites,
    dict_str_payload_sites,
    generic_base_sites,
    typing_generics_sites,
    typing_union_sites,
    cast_sites,
    eval_exec_sites,
    os_environ_sites,
    os_file_ops_sites,
    os_path_sites,
    os_shell_sites,
    re_call_sites,
    string_replace_sites,
    string_split_sites,
    string_strip_sites,
    suppress_sites,
    utcnow_sites,
    argparse_sites,
    dataclass_sites,
    import_re_sites,
    pdf_extraction_sites,
    rich_progress_sites,
    subprocess_sites,
    suppress_import_sites,
    continues_comment_block,
    lines_of,
    python_tree,
    default_factory_sites,
    derived_interpolation_sites,
    dict_get_sites,
    empty_collection_sites,
    silent_truncation_sites,
    suppression_placement,
    suppression_reaches,
    tuple_shape_sites,
)
from lup.policy.kernel.typescript import TYPESCRIPT_SUFFIXES


MAPPING_FAMILY = TypeFamily(
    name="mapping",
    classes=[
        "dict",
        "Mapping",
        "MutableMapping",
        "MappingProxyType",
        "UserDict",
        "Counter",
        "OrderedDict",
        "defaultdict",
        "ChainMap",
    ],
)
"""Declarations that make a `.get` receiver a keyed lookup rather than a client.

A `TypedDict` is deliberately absent. It is a mapping at runtime, and reading
an optional key out of one with `.get` is how the language says to — but it
is also the modelling `dict-get` exists to ask for, so a site that resolves
to one has already done what the rule wanted and is refuted by saying so.
"""

TEXT_FAMILY = TypeFamily(
    name="text",
    classes=["str", "bytes", "bytearray", "UserString"],
)
"""Declarations that make a `.replace` receiver text rather than something else.

The rule is about substituting one piece of text for another inside a string.
The tree already tells that from the rename wearing the same name, by arity —
a bound `Path.replace` takes only the destination. What arity cannot reach is
a two-argument `replace` on a value that is not text at all: a dataframe
filling missing values, a datetime rebuilt with a field changed. Those spell
the rule's shape exactly and are not its subject, and only the receiver's own
declaration says so.
"""


def historical_voice(
    examples: list[RuleExample], matcher: Matcher | None = None
) -> AntiPattern:
    """The prose rule, as each language's table carries it.

    One rule read three ways: Python's comments and docstrings, the
    TypeScript family's comments, a Markdown page's text. Built here once so
    the phrases, the message and the roles cannot differ between the tables,
    and each table supplies only what is its own — examples in its syntax, and
    the tree selector Python alone has.
    """
    return AntiPattern(
        id="historical-voice",
        remedy="say what the code is; how it came to be belongs in the commit message",
        pattern=HISTORICAL_VOICE_RE,
        matcher=matcher,
        examples=examples,
        message=(
            "Prose — a comment, a docstring, a page — says what the code is, "
            "not how it came to be: a phrase about a prior state, a fix told "
            "by the defect it removed, or an issue number standing in for the "
            "reason dates the moment it is read rather than the moment it was "
            "written. Say what holds and why; the change belongs in the commit "
            "message, and a decision worth keeping belongs where it is looked "
            "up. Another project's issue that is the reason a workaround "
            "exists is cited as `owner/repo#123`, and a phrase quoted as an "
            "example goes in backticks"
        ),
        context="prose",
        roles=["production", "test"],
    )


PORTABLE_PYTHON_ANTI_PATTERNS: list[AntiPattern] = [
    AntiPattern(
        id="any-type",
        pattern=re.compile(r"\bAny\b"),
        matcher=Matcher(select=any_type_sites),
        examples=[
            RuleExample(
                code="def handle(payload: Any) -> None: ...", verdict="flagged"
            ),
            RuleExample(code="from typing import Any", verdict="flagged"),
            RuleExample(
                code="def handle(payload: Payload) -> None: ...", verdict="cleared"
            ),
            RuleExample(
                code="failed = any(row.failed for row in rows)", verdict="cleared"
            ),
        ],
        message="Never use Any — use specific types, TypedDict, or BaseModel",
    ),
    AntiPattern(
        id="type-ignore",
        pattern=re.compile(r"#\s*type:\s*ignore"),
        matcher=Matcher(select=type_ignore_sites),
        examples=[
            RuleExample(
                code="total = count + label  # type: ignore", verdict="flagged"
            ),
            RuleExample(
                code="total = count + label  # type: ignore[operator]",
                verdict="flagged",
            ),
            RuleExample(
                code="total = count + int(label)  # the operands agree",
                verdict="cleared",
            ),
            RuleExample(
                code="total = count + int(label)  # never # type: ignore it away",
                verdict="cleared",
            ),
        ],
        message="Never use # type: ignore — fix the type error properly",
        context="comment",
    ),
    AntiPattern(
        id="pyright-ignore",
        pattern=re.compile(r"#\s*pyright:\s*ignore"),
        matcher=Matcher(select=pyright_ignore_sites),
        examples=[
            RuleExample(
                code="total = count + label  # pyright: ignore", verdict="flagged"
            ),
            RuleExample(
                code="total = count + label  # pyright: ignore[reportOperatorIssue]",
                verdict="flagged",
            ),
            RuleExample(
                code="total = count + int(label)  # the operands agree",
                verdict="cleared",
            ),
        ],
        message="Never use # pyright: ignore — fix the type error properly",
        context="comment",
    ),
    AntiPattern(
        id="noqa",
        pattern=re.compile(r"#\s*noqa\b"),
        matcher=Matcher(select=noqa_sites),
        examples=[
            RuleExample(code="import json  # noqa", verdict="flagged"),
            RuleExample(
                code="summary = describe(run)  # noqa: E501", verdict="flagged"
            ),
            RuleExample(code="summary = describe(run)", verdict="cleared"),
            RuleExample(
                code='code = "# noqa is a forbidden shape, not a suppression"',
                verdict="cleared",
            ),
        ],
        message="Never use # noqa — fix the lint issue properly",
        context="comment",
    ),
    historical_voice(
        examples=[
            RuleExample(
                code="# the home a launch selects, previously read from the environment",
                verdict="flagged",
            ),
            RuleExample(
                code="# the receipt this replaced is gone; see #436 for why",
                verdict="flagged",
            ),
            RuleExample(
                code="# it used to filter, and the integration branch lost",
                verdict="flagged",
            ),
            RuleExample(
                code="# the old fallback wrote nothing and said nothing",
                verdict="flagged",
            ),
            RuleExample(
                code="# kept public during the capability migration",
                verdict="flagged",
            ),
            RuleExample(
                code="# until now nothing outside a session saw inside it",
                verdict="flagged",
            ),
            RuleExample(
                code="# the hole this closes: a write nothing read",
                verdict="flagged",
            ),
            RuleExample(
                code="# the home a launch selects",
                verdict="cleared",
            ),
            RuleExample(
                code='colour = "#264F78"  # the terminal selection background',
                verdict="cleared",
            ),
            RuleExample(
                code="# the key used to select a home",
                verdict="cleared",
            ),
            RuleExample(
                code="# sent twice, since the vendor drops it (openai/codex#21639)",
                verdict="cleared",
            ),
            RuleExample(
                code="# a sentence saying `previously` is what this refuses",
                verdict="cleared",
            ),
        ],
        matcher=Matcher(select=historical_voice_sites),
    ),
    AntiPattern(
        id="generic-base",
        matcher=Matcher(select=generic_base_sites),
        strength="strong",
        pattern=re.compile(r"\bGeneric\["),
        examples=[
            RuleExample(code="class Box(Generic[T]): ...", verdict="flagged"),
            RuleExample(code="class Box[T]: ...", verdict="cleared"),
        ],
        message="Use Python 3.12+ class[T] syntax instead of Generic[T]",
    ),
    AntiPattern(
        id="typing-union",
        matcher=Matcher(select=typing_union_sites),
        strength="strong",
        pattern=re.compile(r"\b(?:Optional|Union)\["),
        examples=[
            RuleExample(
                code="def find(name: str) -> Optional[User]: ...", verdict="flagged"
            ),
            RuleExample(
                code="def find(name: str) -> Union[User, Team]: ...", verdict="flagged"
            ),
            RuleExample(
                code="def find(name: str) -> User | None: ...", verdict="cleared"
            ),
        ],
        message="Use PEP 604 unions — X | None instead of Optional, X | Y instead of Union",
    ),
    AntiPattern(
        id="typing-generics",
        matcher=Matcher(select=typing_generics_sites),
        strength="strong",
        pattern=re.compile(r"\b(?:List|Dict|Tuple|Set)\["),
        examples=[
            RuleExample(
                code="def labels(rows: List[str]) -> None: ...", verdict="flagged"
            ),
            RuleExample(
                code="def labels(rows: list[str]) -> None: ...", verdict="cleared"
            ),
            # An alias with no builtin to lower-case is not one of these.
            RuleExample(
                code="def labels(rows: Sequence[str]) -> None: ...", verdict="cleared"
            ),
        ],
        message="Use lowercase builtin generics — list, dict, tuple, set — "
        "instead of the capitalized typing aliases",
    ),
    AntiPattern(
        id="all-export",
        pattern=re.compile(r"__all__\s*[=:]"),
        matcher=Matcher(select=all_export_sites),
        examples=[
            RuleExample(
                code='__all__ = ["Session", "SessionOpener"]', verdict="flagged"
            ),
            RuleExample(code='__all__ += ["SessionOpener"]', verdict="flagged"),
            RuleExample(
                code='PUBLIC_NAMES = ["Session", "SessionOpener"]', verdict="cleared"
            ),
            # Reading somebody else's export list binds nothing here.
            RuleExample(code="exported = module.__all__", verdict="cleared"),
        ],
        message="No __all__ — import directly from the defining module",
    ),
    AntiPattern(
        id="dict-str-object",
        remedy="use a TypedDict or BaseModel, `JsonObject` for data, or `Namespace` for live objects, from `lup.types`",
        pattern=re.compile(r"\b(?:dict|Mapping)\[\s*str\s*,\s*object\s*\]"),
        matcher=Matcher(select=dict_str_object_sites),
        examples=[
            RuleExample(
                code="def load(row: dict[str, object]) -> None: ...", verdict="flagged"
            ),
            RuleExample(
                code="def load(row: SessionRow) -> None: ...", verdict="cleared"
            ),
        ],
        message="dict[str, object] and Mapping[str, object] say nothing about either "
        "half — a TypedDict or BaseModel carries the fields where they are closed, "
        "JsonObject from lup.types carries data whose schema lives elsewhere, and "
        "Namespace from lup.types carries the one thing `object` is honest for: names "
        "bound to live Python objects rather than to data",
    ),
    AntiPattern(
        # Flags a string-keyed dict/Mapping only when the VALUE is a scalar/
        # payload type (str, int, float, bool, bytes, complex, or a union that
        # opens with one). Concrete class and callable value types are left
        # alone: `dict[str, Agent]`, `dict[str, LupMcpTool]`, `dict[str,
        # Callable[...]]` are registries/routers whose open, data-driven key
        # set IS the point. The smell is a CLOSED, enumerable key set with a
        # scalar value (config-shaped) — that wants a BaseModel or
        # dict[Literal[...], V]; JsonValue stays the escape for arbitrary JSON.
        id="dict-str-payload",
        remedy="name the keys: a BaseModel, `dict[Literal[...], V]`, an id model as the key, or `StringMap` or `EnvVars` from `lup.types`",
        pattern=re.compile(
            r"\b(?:dict|Mapping|MutableMapping)\[\s*str\s*,"
            r"\s*(?:str|int|float|bool|bytes|complex)\b"
        ),
        matcher=Matcher(select=dict_str_payload_sites),
        examples=[
            RuleExample(
                code="def render(fields: dict[str, str]) -> None: ...",
                verdict="flagged",
            ),
            # A union reaching a scalar is the same open map of scalars.
            RuleExample(
                code="def render(fields: dict[str, str | None]) -> None: ...",
                verdict="flagged",
            ),
            RuleExample(
                code="def render(fields: SessionFields) -> None: ...", verdict="cleared"
            ),
            # A registry: the value is a concrete class, and the open key set
            # keyed by external data is the whole point.
            RuleExample(
                code="def render(fields: dict[str, Agent]) -> None: ...",
                verdict="cleared",
            ),
            RuleExample(
                code="def render(fields: dict[SessionId, str]) -> None: ...",
                verdict="cleared",
            ),
        ],
        message="String-keyed dict with a scalar value hides what the keys are — the "
        "comment naming them is the shape the type could not carry. Four shapes carry "
        "it instead, and one of them almost always fits: a BaseModel or "
        "dict[Literal[...], V] where the field set is closed and enumerable; a frozen "
        "id model as the key (dict[SessionId, str]) where the keys are identities this "
        "code mints; a declared route list behind a router (CommentRouter in "
        "lup.formats.banner, ModelRouter in lup.providers.routing) where they are a "
        "dispatch table; and EnvVars or StringMap from lup.types where the names are "
        "owned outside this repository. Concrete class/callable value types "
        "(dict[str, Agent]) are already accepted and JsonValue covers arbitrary JSON. "
        "Where the keys are open and none of those names them, "
        "`# lup: ignore[dict-str-payload]` carries the reason they are open",
    ),
    AntiPattern(
        # Flags `.get("literal")` — a field name the author knew and the type
        # does not carry. A key computed at runtime is a lookup into a map
        # whose keys are data, never a hidden schema, and takes no directive.
        id="dict-get",
        remedy="model the payload as a BaseModel or TypedDict and read the field",
        pattern=re.compile(r"\.get\s*\("),
        matcher=Matcher(select=dict_get_sites),
        family=MAPPING_FAMILY,
        refinement=(
            "The audit resolves what each site's receiver is and keeps the "
            "finding only where that class is in the mapping family. An HTTP "
            "client, an SDK object, a vendor type carrying a `get` of its own "
            "are each refuted, and so is a receiver nothing can be shown "
            "about — an unannotated parameter, a value the checker infers "
            "nothing for — because a rule about mappings has nothing to say "
            "about a value nobody can show is one, and a denial nobody can "
            "substantiate leaves a directive as the only way past it. The two "
            "are refuted differently: a receiver resolved outside the family "
            "makes a marker there spurious, while one nothing can be shown "
            "about leaves a marker standing — what a checker failed to learn "
            "is no evidence against it, and a hook knowing less than the sweep "
            "has to come out unresolved rather than spurious, or the two would "
            "trade one marker back and forth. A "
            "`TypedDict` resolves to its own class and is refuted there: it "
            "is already the modelling this rule asks for. What the tree "
            "settles it settles alone, so a key computed at runtime, a route "
            "decorator, and a call on an imported module never become sites "
            "and never reach a checker. Where no checker answers at all every "
            "finding keeps its unresolved verdict and the gate asks instead "
            "of refusing."
        ),
        examples=[
            RuleExample(code='name = payload.get("name")', verdict="flagged"),
            # Reached *through* a module rather than on one, so still a keyed
            # lookup — which is why the tree rules out the bare name only.
            RuleExample(code='token = os.environ.get("LUP_TOKEN")', verdict="flagged"),
            RuleExample(code="name = payload.name", verdict="cleared"),
            # A key computed at runtime: the map's keys are data, so there is
            # no schema to model and nothing being hidden by not modelling it.
            RuleExample(code="held = sessions.get(actor)", verdict="cleared"),
            RuleExample(code="import httpx; reply = httpx.get(url)", verdict="cleared"),
            RuleExample(
                code='@app.get("/runs")\ndef runs() -> list[Run]: ...',
                verdict="cleared",
            ),
            # No tree says what an imported class is, so the hook flags this
            # and the sweep takes it back once the receiver resolves.
            RuleExample(code='reply = client.get("url")', verdict="refuted"),
        ],
        message='`.get("literal")` on a dict-shaped payload hides the schema — the '
        "field name is in the call and nowhere in the type, so model it and read the "
        "fields (BaseModel/TypedDict). Nothing else is this rule's subject and none "
        "of it takes a directive: a key computed at runtime is a lookup into a map "
        "whose keys are data, a receiver the checker resolves outside the mapping "
        "family is refuted by the audit, and a `TypedDict` is already the "
        "modelling this asks for — `.get` is how an optional key is read out of "
        "one. A marker at any of those is reported spurious. A receiver the "
        "checker cannot resolve at all is refuted too, and a marker there stands. "
        "Where the literal is genuinely one key of an open dict, add "
        "`# lup: ignore[dict-get]`",
    ),
    AntiPattern(
        # A value a declaration carries, spliced into the text of a file this
        # repository generates. The per-format constructors take a document
        # instead, which is emitted and parsed back before the file exists.
        id="derived-interpolation",
        remedy="build the body as a document and hand it to `Artifact.in_yaml`, `in_markdown`, `in_toml` or `in_json`",
        pattern=re.compile(
            r"\b(?:body|content)\s*=\s*"
            r"(?:f[\"']|[\"'][^\"']*[\"']\s*\.\s*(?:format|join)\b)"
        ),
        matcher=Matcher(select=derived_interpolation_sites),
        examples=[
            RuleExample(
                code='Artifact.generated(path=p, body=f"name: {skill.name}\\n", '
                "semantic_id=i, banner=b)",
                verdict="flagged",
            ),
            RuleExample(
                code='Artifact(path=p, content="\\n".join(rows), semantic_id=i)',
                verdict="flagged",
            ),
            RuleExample(
                code="Artifact.in_yaml(path=p, document=document, semantic_id=i, "
                "banner=b)",
                verdict="cleared",
            ),
            # A piece fixed where it is written — a command the banner already
            # names, an exit status — reads the same in every generated file.
            RuleExample(
                code='Artifact.generated(path=p, body=f"exit {REFUSAL_STATUS}\\n", '
                "semantic_id=i, banner=b)",
                verdict="cleared",
            ),
            # Whitespace is not a container: this is the whole page, ended.
            RuleExample(
                code='Artifact.generated(path=p, body=page + "\\n", '
                "semantic_id=i, banner=b)",
                verdict="cleared",
            ),
            # The subject is the file's body; a path is validated by the
            # artifact itself, which refuses one escaping its root.
            RuleExample(
                code='Artifact(path=Path(f"agents/{name}.toml"), content=text, '
                "semantic_id=i)",
                verdict="cleared",
            ),
        ],
        message="A value derived from a declaration enters a generated file as a "
        "value, not spliced into its text: a colon, a newline or a quote in it ends "
        "the container it lands in, and the file stays plausible until something "
        "parses it. Build the body as a document and hand it to the constructor "
        "for its format — `Artifact.in_yaml` with a `YamlDocument`, "
        "`Artifact.in_markdown` with a `MarkdownDocument`, `Artifact.in_toml`, "
        "`Artifact.in_json` — each emitted by a library and parsed back before the "
        "file exists. A piece fixed where it is written, a literal or a name "
        "spelled as a constant, is not derived and is not flagged. A format with "
        "no document yet takes `# lup: ignore[derived-interpolation]` naming the "
        "format",
    ),
    AntiPattern(
        id="bare-object",
        pattern=re.compile(r"(?:(?<!\w)(?!_)\w+\s*:|->)\s*object\b"),
        matcher=Matcher(select=bare_object_sites),
        examples=[
            RuleExample(
                code="def store(value: object) -> None: ...", verdict="flagged"
            ),
            RuleExample(
                code="def store(value: SessionId) -> None: ...", verdict="cleared"
            ),
            # The annotation is the subject, so constructing a sentinel is not.
            RuleExample(code="MISSING = object()", verdict="cleared"),
        ],
        message="Bare `object` says nothing about the value — use a concrete type, "
        "TypedDict, or BaseModel, and narrow at untyped boundaries",
    ),
    AntiPattern(
        id="bare-basemodel",
        pattern=re.compile(r"(?:(?<!\[)\b\w+\s*:|->)\s*BaseModel\b(?!\s*[\]|])"),
        matcher=Matcher(select=bare_basemodel_sites),
        examples=[
            RuleExample(
                code="def render(part: BaseModel) -> str: ...", verdict="flagged"
            ),
            RuleExample(
                code="def render(part: TextPart | ImagePart) -> str: ...",
                verdict="cleared",
            ),
            # Inheriting it is the shape this steers toward, not away from.
            RuleExample(
                code="class Session(BaseModel):\n    name: str", verdict="cleared"
            ),
        ],
        message="A parameter or return annotated exactly BaseModel accepts any model — "
        "name the concrete union of models or make the function generic",
    ),
    AntiPattern(
        id="tuple-shape",
        remedy="name the positions with a BaseModel, or a TypedDict where no model can go",
        strength="strong",
        pattern=re.compile(r"\btuple\["),
        matcher=Matcher(select=tuple_shape_sites),
        examples=[
            RuleExample(
                code="def span(text: str) -> tuple[int, int]: ...", verdict="flagged"
            ),
            RuleExample(code="def span(text: str) -> Span: ...", verdict="cleared"),
            RuleExample(
                code="def spans(text: str) -> tuple[Span, ...]: ...", verdict="cleared"
            ),
        ],
        message="A fixed-arity `tuple[...]` hides what each position means — name the "
        "fields with a BaseModel. Fall back to a TypedDict only where a model cannot go: "
        "the hermetic kernel, which has no pydantic, or a field that must stay the caller's "
        "own object, which validation would copy. `tuple[X, ...]` is a sequence and never "
        "trips this",
    ),
    AntiPattern(
        # Mirrors tuple-shape for frozenset: every declared frozenset annotation
        # or constructed constant. A fixed name set constant wants a dict or a
        # purpose-built structure; an immutable-default-argument use is the one
        # legitimate site — `# lup: ignore[frozenset-shape]` marks it.
        id="frozenset-shape",
        remedy="use a dict, or a purpose-built structure",
        pattern=re.compile(r"\bfrozenset\b"),
        matcher=Matcher(select=frozenset_shape_sites),
        examples=[
            RuleExample(
                code='ROLES: frozenset[str] = frozenset({"worker"})', verdict="flagged"
            ),
            RuleExample(
                code='ROLES: dict[str, Role] = {"worker": WORKER}', verdict="cleared"
            ),
        ],
        message="A declared `frozenset[...]` shape or constant collapses structure a "
        "`dict[...]` keeps — each member is a bare name, and whatever it keyed has nowhere "
        "left to live. Use a dict, frozen once 3.15 ships `frozendict`, or a purpose-built "
        "structure. For a genuinely immutable default argument add "
        "`# lup: ignore[frozenset-shape]`",
    ),
    AntiPattern(
        # A declared or constructed set — `set(...)`/`set[...]` (no space
        # before the bracket; ruff never formats one in) or a `: set`/`-> set`
        # annotation. The dot lookbehind keeps `.set()` method calls out, and
        # prose about "set" never carries the bracket or annotation shape.
        # `frozenset` is caught by frozenset-shape and never trips this, since
        # its "set" is not a standalone word.
        id="set-shape",
        remedy="use a dict where the members key something, or a `list[BaseModel]`",
        pattern=re.compile(r"(?<!\.)\bset[\[(]|(?::|->)\s*set\b"),
        matcher=Matcher(select=set_shape_sites),
        examples=[
            RuleExample(
                code="def known(names: set[str]) -> None: ...", verdict="flagged"
            ),
            RuleExample(
                code="def known(names: dict[str, Role]) -> None: ...", verdict="cleared"
            ),
            # The rule's own remedy: reach for membership locally instead of
            # declaring the set as the interface.
            RuleExample(code="seen = {row.name for row in rows}", verdict="cleared"),
        ],
        message="A declared `set` collapses structure a `dict[...]` keeps — a bare set "
        "of strings is a record that lost its other fields, so whatever each member "
        "keyed has nowhere left to live. Use a dict when the members key something, or "
        "a `list[BaseModel]` when each carries more than its own name. Reach for "
        "membership on a local set comprehension "
        "instead of declaring the set as the interface. For a genuinely "
        "set-shaped value add `# lup: ignore[set-shape]`",
    ),
    AntiPattern(
        # The matcher clears every factory that does work a literal cannot say,
        # so what reaches a verdict is the empty collection the annotation
        # already names. A pydantic field is an annotated class declaration,
        # which `empty_collection_exempt_lines` clears, so no line trips both:
        # this rule owns the factory spelling and that one owns the seed.
        id="default-factory",
        remedy="write the default as a literal, `items: list[B] = []`",
        pattern=re.compile(r"\bdefault_factory\s*="),
        matcher=Matcher(select=default_factory_sites),
        examples=[
            RuleExample(
                code="class Run(BaseModel):\n    steps: list[Step] = Field(default_factory=list)",
                verdict="flagged",
            ),
            RuleExample(
                code="class Run(BaseModel):\n    steps: list[Step] = []",
                verdict="cleared",
            ),
            # A factory doing work no literal expresses is not a site at all.
            RuleExample(
                code="class Run(BaseModel):\n    run_id: str = Field(default_factory=new_run_id)",
                verdict="cleared",
            ),
        ],
        message="`Field(default_factory=list)` states in a factory what the annotation "
        "already declares — write the default as a literal, `items: list[B] = []`, which "
        "pydantic copies per instance. A factory that does real work (reads another "
        "declaration, stamps a value, builds a model) is cleared by both gates on its "
        "own, and a marker there is reported spurious",
    ),
    AntiPattern(
        # The matcher exempts deliberate defaults — __init__ state, call
        # kwargs, annotated module and class declarations — so what reaches a
        # verdict is the build-then-append seed. The annotated class
        # declaration among those is a pydantic field, whose factory spelling
        # default-factory owns. The lookbehind keeps `==`/`!=`/`<=`/`>=` out.
        id="empty-collection",
        remedy="build the collection with a comprehension, or yield the items from a nested function",
        pattern=re.compile(r"(?<![=!<>])=\s*(?:\{\}|\[\]|set\(\))"),
        matcher=Matcher(select=empty_collection_sites),
        examples=[
            RuleExample(
                code="names = []\nfor row in rows:\n    names.append(row.name)",
                verdict="flagged",
            ),
            RuleExample(code="names = [row.name for row in rows]", verdict="cleared"),
            # An annotated class declaration is a pydantic field, which the
            # matcher exempts — default-factory owns that spelling instead.
            RuleExample(
                code="class Run(BaseModel):\n    steps: list[Step] = []",
                verdict="cleared",
            ),
        ],
        message="Empty-collection literals (`= {}`, `= []`, `= set()`) usually seed an "
        "append/mutate loop — build the collection with a comprehension, or, when the "
        "loop carries control flow a comprehension cannot, `yield` the items from a "
        "nested function and let its caller collect them. Add "
        "`# lup: ignore[empty-collection]` only for a fold neither expresses",
    ),
    AntiPattern(
        id="cast",
        pattern=re.compile(r"\bcast\s*\("),
        matcher=Matcher(select=cast_sites),
        examples=[
            RuleExample(code="value = cast(str, raw)", verdict="flagged"),
            RuleExample(
                code='value = raw if isinstance(raw, str) else ""', verdict="cleared"
            ),
            # A `cast` method belongs to somebody else's API, not to `typing`.
            RuleExample(code="values = column.cast(int)", verdict="cleared"),
        ],
        message="`cast(...)` is a code smell — narrow with isinstance or a type guard, "
        "or fix the annotation so the cast is unnecessary",
    ),
    AntiPattern(
        id="import-re",
        remedy="parse with the format's own API: `json`, `pathlib`, `urllib.parse`, `xml.etree`, `datetime`",
        pattern=re.compile(r"\bimport\s+re\b|\bfrom\s+re\s+import\b"),
        matcher=Matcher(select=import_re_sites),
        examples=[
            RuleExample(code="import re", verdict="flagged"),
            RuleExample(code="from re import compile", verdict="flagged"),
            RuleExample(code="import json", verdict="cleared"),
        ],
        message="`import re` / `from re import` is a code smell — parse structured data with "
        "its own API instead: JSON -> json.loads, paths -> pathlib.Path, URLs -> urllib.parse, "
        "XML/HTML -> xml.etree.ElementTree / lxml, dates -> datetime",
    ),
    AntiPattern(
        id="re-call",
        pattern=re.compile(
            r"\bre\.(compile|search|match|fullmatch|sub|findall|split)\s*\("
        ),
        matcher=Matcher(select=re_call_sites),
        examples=[
            RuleExample(code="host = re.search(pattern, url)", verdict="flagged"),
            RuleExample(code="host = urlparse(url).netloc", verdict="cleared"),
            # A compiled pattern's own method is not the module entry point.
            RuleExample(code="found = matcher.search(text)", verdict="cleared"),
        ],
        message="Avoid regex for structured data — reach for its parser instead: "
        "JSON -> json.loads, paths -> pathlib.Path, URLs -> urllib.parse, "
        "XML/HTML -> xml.etree.ElementTree / lxml, dates -> datetime",
    ),
    AntiPattern(
        # `.replace` on `os`, `Path`, or a `*path` receiver is pathlib/os's
        # atomic file rename, not string surgery — the lookbehinds keep the
        # codebase's path-named receivers out of the net. os.replace as an atomic
        # rename is steered toward pathlib by os-file-ops instead, so the two
        # rules stay coherent: this one is only about string surgery.
        id="string-replace",
        pattern=re.compile(r"(?<!\bos)(?<![Pp]ath)\.replace\s*\("),
        matcher=Matcher(select=string_replace_sites),
        family=TEXT_FAMILY,
        refinement=(
            "The hook decides this one by arity, which tells text surgery "
            "from the file rename wearing the same name and reaches no "
            "further. The audit resolves what each site's receiver is and "
            "keeps the finding only where that class is text. A dataframe "
            "filling missing values, a datetime rebuilt with one field "
            "changed, a vendor object carrying a `replace` of its own all "
            "spell this rule's shape and none of them is its subject, so "
            "each is refuted — as is a receiver nothing can be shown about, "
            "since a rule about strings has nothing to say about a value "
            "nobody can show is one. Where no checker answers, the arity "
            "verdict stands alone and the gate asks rather than refusing."
        ),
        examples=[
            RuleExample(code='name = source.replace(".py", ".pyi")', verdict="flagged"),
            RuleExample(
                code='name = Path(source).with_suffix(".pyi")', verdict="cleared"
            ),
            # The two the tree settles by arity: a bound rename takes only the
            # destination, and the unbound spelling is named outright.
            RuleExample(code="Path(source).replace(destination)", verdict="cleared"),
            RuleExample(code="os.replace(source, destination)", verdict="cleared"),
            # What arity cannot reach: a two-argument `replace` on something
            # that is not text at all, which only a resolved receiver tells
            # from the string surgery this rule is about.
            RuleExample(
                code="frame = frame.replace(missing, default)", verdict="refuted"
            ),
        ],
        message="Avoid .replace() for structured data — edit it through its parser instead "
        "(pathlib.Path for paths, urllib.parse for URLs, json for JSON)",
    ),
    AntiPattern(
        # Only separator-form `.split(sep)` is flagged — `.rsplit` and
        # `.partition`/`.rpartition` included, so the variants are not a dodge
        # (partition always takes a separator, so it always trips). A separator
        # implies structure with a real parser alternative (csv, pathlib,
        # urllib, json, datetime). Argument-less `.split()` is whitespace
        # tokenization of free text, for which no parser exists — the negative
        # lookahead exempts it.
        id="string-split",
        pattern=re.compile(r"\.r?split\s*\((?!\s*\))|\.r?partition\s*\("),
        matcher=Matcher(select=string_split_sites),
        examples=[
            RuleExample(code='host = url.split("/")[2]', verdict="flagged"),
            # Partition always takes a separator, so the variant is no dodge.
            RuleExample(code='key, _, value = line.partition(":")', verdict="flagged"),
            RuleExample(code="host = urlparse(url).netloc", verdict="cleared"),
            # Argless splitting is whitespace tokenizing, which has no parser.
            RuleExample(code="words = sentence.split()", verdict="cleared"),
        ],
        message="Avoid .split(sep)/.rsplit/.partition for structured data — parse it "
        "instead (urllib.parse for URLs, pathlib.Path for paths, json for JSON, "
        "datetime for dates)",
    ),
    AntiPattern(
        # Only separator-form `.strip(chars)` is flagged — `.lstrip`/`.rstrip`
        # included, so the variants are not a dodge. Naming the characters to
        # strip implies field extraction from structured text; argless
        # stripping is whitespace framing, for which no parser exists — the
        # negative lookahead exempts it, mirroring string-split's argless rule.
        id="string-strip",
        pattern=re.compile(r"\.[lr]?strip\s*\((?!\s*\))"),
        matcher=Matcher(select=string_strip_sites),
        examples=[
            RuleExample(code='name = field.strip("<>")', verdict="flagged"),
            # Argless stripping is whitespace framing, which has no parser.
            RuleExample(code="name = field.strip()", verdict="cleared"),
        ],
        message="Avoid .strip(chars)/.lstrip/.rstrip for structured data — parse it "
        "instead (urllib.parse for URLs, pathlib.Path for paths, json for JSON, "
        "datetime for dates)",
    ),
    AntiPattern(
        # A literal or a SCREAMING_CASE constant is a bound decided in the
        # code; a lowercase name is one the caller passed, which makes
        # `results[:limit]` a request rather than a truncation and keeps it out
        # of the net. Single digits go with it: `rest[:1]` and `data[:2]` are
        # parser bounds, never a cut artifact. What is left is a size somebody
        # chose for content, which is the shape this names — the matcher clears
        # the digests, splits, and sniffs that also wear it.
        id="silent-truncation",
        remedy="emit the whole value; where a hard limit forces a cut, save the full copy and point at it",
        pattern=re.compile(r"\[\s*:\s*(?:\d[\d_]*\d|[A-Z][A-Z0-9_]{2,})\s*\]"),
        matcher=Matcher(select=silent_truncation_sites),
        examples=[
            RuleExample(code="preview = body[:200]", verdict="flagged"),
            RuleExample(code="preview = body[:SNIPPET_LENGTH]", verdict="flagged"),
            RuleExample(code="preview = body", verdict="cleared"),
            # A lowercase name is a bound the caller passed, so this is a
            # request rather than a cut; a small literal is a parser bound.
            RuleExample(code="page = rows[:limit]", verdict="cleared"),
            RuleExample(code="head = rest[:2]", verdict="cleared"),
        ],
        message="Slicing a prefix discards the rest with nothing said, and a cut artifact "
        "looks exactly like a complete one. Emit the whole value: the container grows to "
        "fit what it holds, not the reverse. Cut only for a hard limit a document format "
        "or a function contract imposes — never for printing space, log volume, or ease of "
        "reading — and where a cut is forced, save the full copy and point at it from what "
        "survives. On a bound that is genuinely one of those, add "
        "`# lup: ignore[silent-truncation]` naming which",
    ),
    AntiPattern(
        id="bare-except",
        pattern=re.compile(r"\bexcept\s*:"),
        matcher=Matcher(select=bare_except_sites),
        examples=[
            RuleExample(code="try:\n    load()\nexcept:\n    pass", verdict="flagged"),
            RuleExample(
                code="try:\n    load()\nexcept FileNotFoundError:\n    raise",
                verdict="cleared",
            ),
        ],
        message="Bare `except:` catches SystemExit/KeyboardInterrupt — name the exception",
    ),
    AntiPattern(
        id="except-baseexception",
        pattern=re.compile(r"\bexcept\s+BaseException\b"),
        matcher=Matcher(select=except_baseexception_sites),
        examples=[
            RuleExample(
                code="try:\n    load()\nexcept BaseException:\n    raise",
                verdict="flagged",
            ),
            RuleExample(
                code="try:\n    load()\nexcept Exception:\n    raise", verdict="cleared"
            ),
        ],
        message="except BaseException catches KeyboardInterrupt — use Exception or narrower",
    ),
    AntiPattern(
        id="suppress",
        pattern=re.compile(r"\bcontextlib\.suppress\b"),
        matcher=Matcher(select=suppress_sites),
        examples=[
            RuleExample(
                code="with contextlib.suppress(KeyError):\n    load()",
                verdict="flagged",
            ),
            RuleExample(
                code="try:\n    load()\nexcept KeyError:\n    logger.warning(...)",
                verdict="cleared",
            ),
            # The bare name is `suppress-import`'s subject, not this one's.
            RuleExample(code="with suppress(KeyError):\n    load()", verdict="cleared"),
        ],
        message="contextlib.suppress silently swallows exceptions — log, handle, or re-raise",
    ),
    AntiPattern(
        id="suppress-import",
        pattern=re.compile(r"\bfrom\s+contextlib\s+import\b.*\bsuppress\b"),
        matcher=Matcher(select=suppress_import_sites),
        examples=[
            RuleExample(code="from contextlib import suppress", verdict="flagged"),
            RuleExample(
                code="from contextlib import asynccontextmanager", verdict="cleared"
            ),
            # The qualified spelling is `suppress`'s subject at its use site.
            RuleExample(code="import contextlib", verdict="cleared"),
        ],
        message="contextlib.suppress silently swallows exceptions — log, handle, or re-raise",
    ),
    AntiPattern(
        id="dataclass",
        pattern=re.compile(
            r"@dataclass|\bimport\s+dataclasses\b|\bfrom\s+dataclasses\s+import\b"
        ),
        matcher=Matcher(select=dataclass_sites),
        examples=[
            RuleExample(code="from dataclasses import dataclass", verdict="flagged"),
            RuleExample(
                code="@dataclass\nclass Span:\n    start: int", verdict="flagged"
            ),
            RuleExample(code="from pydantic import BaseModel", verdict="cleared"),
        ],
        message="Use Pydantic BaseModel (or TypedDict) instead of dataclasses",
    ),
    AntiPattern(
        # Same dodge as dataclass: a typed record that ducks pydantic
        # validation. Catches the typing.NamedTuple class form and the
        # collections.namedtuple factory alike.
        id="namedtuple",
        pattern=re.compile(r"\bNamedTuple\b|\bnamedtuple\b"),
        matcher=Matcher(select=namedtuple_sites),
        examples=[
            RuleExample(
                code="class Span(NamedTuple):\n    start: int", verdict="flagged"
            ),
            RuleExample(
                code='Span = namedtuple("Span", "start end")', verdict="flagged"
            ),
            RuleExample(
                code="class Span(BaseModel):\n    start: int", verdict="cleared"
            ),
        ],
        message="Use Pydantic BaseModel (or TypedDict) instead of NamedTuple/namedtuple",
    ),
    AntiPattern(
        # Anchored at statement level so a `model_config` mention in a call's
        # keyword or a payload key is not a declaration. Prose cannot reach it
        # at all: a "code" rule reads token-masked source, so the identifier
        # inside a docstring or comment is already blank by the time it matches.
        id="model-config",
        remedy="declare the configuration as class keywords: `class A(BaseModel, frozen=True)`",
        pattern=re.compile(r"^\s*model_config\s*[:=]"),
        matcher=Matcher(select=model_config_sites),
        examples=[
            RuleExample(
                code="class Run(BaseModel):\n    model_config = ConfigDict(frozen=True)",
                verdict="flagged",
            ),
            RuleExample(
                code="class Run(BaseModel, frozen=True):\n    name: str",
                verdict="cleared",
            ),
            # A class body is what makes the name pydantic's configuration, so
            # the same word as a call keyword is an ordinary argument.
            RuleExample(
                code="session = Session(model_config=config)", verdict="cleared"
            ),
        ],
        message="Declare pydantic configuration as class keywords — "
        "class A(BaseModel, frozen=True, extra='forbid') — instead of assigning "
        "model_config, so the configuration reads in the header beside the class "
        "it configures. Every key carries over under its own name; a shared "
        "ConfigDict alias inlines into each header rather than being imported",
    ),
    AntiPattern(
        id="subprocess",
        pattern=re.compile(r"\bimport\s+subprocess\b|\bfrom\s+subprocess\s+import\b"),
        matcher=Matcher(select=subprocess_sites),
        examples=[
            RuleExample(code="import subprocess", verdict="flagged"),
            RuleExample(code="import sh", verdict="cleared"),
        ],
        message="Use the `sh` library instead of subprocess",
    ),
    AntiPattern(
        id="os-shell",
        pattern=re.compile(r"\bos\.(?:system|popen|exec[lv]\w*)\s*\("),
        matcher=Matcher(select=os_shell_sites),
        examples=[
            RuleExample(code='os.system("git status")', verdict="flagged"),
            RuleExample(code="sh.git.status()", verdict="cleared"),
            # A method of that name on somebody's object is not the os call.
            RuleExample(code="runner.system(command)", verdict="cleared"),
        ],
        message="Use the `sh` library instead of os.system()/os.popen()/os.exec*()",
    ),
    AntiPattern(
        id="argparse",
        pattern=re.compile(r"\bimport\s+argparse\b|\bfrom\s+argparse\s+import\b"),
        matcher=Matcher(select=argparse_sites),
        examples=[
            RuleExample(code="import argparse", verdict="flagged"),
            RuleExample(code="import typer", verdict="cleared"),
        ],
        message="Use `typer` instead of argparse",
    ),
    AntiPattern(
        id="rich-progress",
        pattern=re.compile(r"\brich\.progress\b|\bfrom\s+rich\.progress\s+import\b"),
        matcher=Matcher(select=rich_progress_sites),
        examples=[
            RuleExample(code="from rich.progress import track", verdict="flagged"),
            RuleExample(code="from tqdm import tqdm", verdict="cleared"),
            # Rich itself is fine; it is the progress bar that is not.
            RuleExample(code="from rich.console import Console", verdict="cleared"),
        ],
        message="Use `tqdm` instead of rich progress bars",
    ),
    AntiPattern(
        id="os-path",
        pattern=re.compile(r"\bos\.path\b"),
        matcher=Matcher(select=os_path_sites),
        examples=[
            RuleExample(code="parent = os.path.dirname(source)", verdict="flagged"),
            RuleExample(code="parent = Path(source).parent", verdict="cleared"),
            RuleExample(code="import os", verdict="cleared"),
        ],
        message="Use pathlib.Path instead of os.path",
    ),
    AntiPattern(
        # A scoped list of os file/dir operations that all have a pathlib.Path
        # equivalent (iterdir, mkdir, unlink, rename, replace, stat, ...).
        # Config access (os.environ/os.getenv) and process launching (os.system/
        # os.exec*/os.popen) are covered by os-environ and os-shell instead.
        id="os-file-ops",
        pattern=re.compile(
            r"\bos\.(?:getcwd|chdir|listdir|scandir|walk|mkdir|makedirs|rmdir|"
            r"removedirs|remove|unlink|rename|renames|replace|link|symlink|"
            r"readlink|stat|lstat|chmod|chown)\s*\("
        ),
        matcher=Matcher(select=os_file_ops_sites),
        examples=[
            RuleExample(code="os.mkdir(destination)", verdict="flagged"),
            RuleExample(code="Path(destination).mkdir()", verdict="cleared"),
            # An os call `pathlib` has no equivalent for is outside the list.
            RuleExample(code="owner = os.getpid()", verdict="cleared"),
        ],
        message="Use pathlib.Path for file/dir operations instead of os.* "
        "(Path.iterdir/mkdir/unlink/rename/replace/stat/...)",
    ),
    AntiPattern(
        id="os-environ",
        pattern=re.compile(r"\bos\.(?:environ|getenv)\b"),
        matcher=Matcher(select=os_environ_sites),
        examples=[
            RuleExample(code='token = os.environ["LUP_TOKEN"]', verdict="flagged"),
            RuleExample(code='token = os.getenv("LUP_TOKEN")', verdict="flagged"),
            RuleExample(code="token = settings.token", verdict="cleared"),
        ],
        message="Read configuration through pydantic-settings, not os.environ/os.getenv",
    ),
    AntiPattern(
        id="eval-exec",
        pattern=re.compile(r"(?<![.\w])(?:eval|exec)\s*\("),
        matcher=Matcher(select=eval_exec_sites),
        examples=[
            RuleExample(code="value = eval(source)", verdict="flagged"),
            RuleExample(code="value = ast.literal_eval(source)", verdict="cleared"),
            # A method of that name is somebody else's API — a database
            # cursor, a template engine — never the builtin this refuses.
            RuleExample(code="rows = cursor.exec(statement)", verdict="cleared"),
        ],
        message="Never use eval()/exec() — parse the data (ast.literal_eval for "
        "literals) or dispatch explicitly",
    ),
    AntiPattern(
        id="utcnow",
        matcher=Matcher(select=utcnow_sites),
        strength="strong",
        pattern=re.compile(r"\butcnow\s*\("),
        examples=[
            RuleExample(code="stamp = datetime.utcnow()", verdict="flagged"),
            RuleExample(code="stamp = datetime.now(timezone.utc)", verdict="cleared"),
        ],
        message="datetime.utcnow() is naive and deprecated — use datetime.now(timezone.utc)",
    ),
    AntiPattern(
        id="global-statement",
        pattern=re.compile(r"^global\s+\w"),
        matcher=Matcher(select=global_statement_sites),
        examples=[
            RuleExample(
                code="def install(session):\n    global CURRENT\n    CURRENT = session",
                verdict="flagged",
            ),
            RuleExample(
                code="def install(session):\n    STATE.current = session",
                verdict="cleared",
            ),
            # `nonlocal` closes over an enclosing scope rather than publishing
            # into the module's, so it is not what this refuses.
            RuleExample(
                code="def outer():\n    total = 0\n\n    def inner():\n        nonlocal total",
                verdict="cleared",
            ),
        ],
        message="No `global` statements — mutate a module-level holder object or pass "
        "state explicitly",
    ),
    AntiPattern(
        id="private-function",
        pattern=re.compile(r"\bdef\s+_[a-zA-Z]"),
        matcher=Matcher(select=private_function_sites),
        examples=[
            RuleExample(code="def _resolve(name: str) -> str: ...", verdict="flagged"),
            RuleExample(code="def resolve(name: str) -> str: ...", verdict="cleared"),
            RuleExample(code="def __enter__(self) -> Self: ...", verdict="cleared"),
        ],
        message="No `_` prefix on functions/methods — nothing is private (nest inside caller if needed)",
    ),
    AntiPattern(
        id="private-class",
        pattern=re.compile(r"\bclass\s+_[A-Z]"),
        matcher=Matcher(select=private_class_sites),
        examples=[
            RuleExample(code="class _Cache: ...", verdict="flagged"),
            RuleExample(code="class Cache: ...", verdict="cleared"),
        ],
        message="No `_` prefix on classes — nothing is private",
    ),
    AntiPattern(
        id="private-variable",
        pattern=re.compile(r"^_[a-zA-Z]\w*\s*(?::[^=]*)?=(?!=)(?!.*,\s*$)"),
        matcher=Matcher(select=private_variable_sites),
        examples=[
            RuleExample(code="_CACHE = Cache()", verdict="flagged"),
            RuleExample(code="CACHE = Cache()", verdict="cleared"),
            # Module level is the scope a name is published from, so a local
            # is nobody's interface and a tuple unpacking declares no one name.
            RuleExample(
                code="def read(rows):\n    _first = rows[0]", verdict="cleared"
            ),
            RuleExample(code="_head, tail = rows", verdict="cleared"),
        ],
        message="No `_` prefix on variables/constants — nothing is private "
        "(unused `_` function parameters are exempt)",
    ),
    AntiPattern(
        id="elif-chain",
        remedy="write it as a `match` on the subject the arms decide on",
        pattern=re.compile(r"^\s*elif\b"),
        matcher=Matcher(select=elif_chain_sites),
        examples=[
            RuleExample(
                code='if kind == "a":\n    a()\nelif kind == "b":\n    b()',
                verdict="flagged",
            ),
            # A lone `if` in an `else` is the same tree, spelled deeper.
            RuleExample(
                code='if kind == "a":\n    a()\nelse:\n    if kind == "b":\n        b()',
                verdict="flagged",
            ),
            RuleExample(
                code='match kind:\n    case "a":\n        a()\n    case "b":\n        b()',
                verdict="cleared",
            ),
            # Two ways is a boolean, and guard clauses that return are each
            # their own statement rather than arms of one chain.
            RuleExample(
                code='if kind == "a":\n    a()\nelse:\n    b()', verdict="cleared"
            ),
            RuleExample(
                code=(
                    "if seconds < 60:\n    return 's'\n"
                    "if seconds < 3600:\n    return 'm'\nreturn 'h'"
                ),
                verdict="cleared",
            ),
        ],
        message=(
            "A chain of three or more ways is a `match` written long: name the "
            "subject the arms decide on and write each arm as a pattern of it — "
            "a literal or dotted name per value, a class pattern with the fields "
            "it reads, a tuple of the fields, a sequence pattern over the split "
            "line, the variant of a union — with a guard on a pattern that binds "
            "what the guard reads. An arm with no pattern to write, an ordered "
            "comparison among them, becomes a guard clause that returns, each "
            "its own statement. Where the chain is the honest shape, "
            "`# lup: ignore[elif-chain]` carries why"
        ),
    ),
    AntiPattern(
        id="wildcard-guard",
        remedy="bind what the guard reads in the pattern, or write guard clauses that return",
        pattern=re.compile(r"^\s*case\s+(?:_|[A-Za-z_]\w*)\s+if\b"),
        matcher=Matcher(select=wildcard_guard_sites),
        examples=[
            RuleExample(
                code=(
                    "match seconds:\n    case _ if seconds < 60:\n"
                    '        return "s"\n    case _:\n        return "m"'
                ),
                verdict="flagged",
            ),
            # A bare capture binds the subject under another name and no more.
            RuleExample(
                code=(
                    "match suffix:\n    case suffix if suffix in PYTHON:\n"
                    "        return 1\n    case _:\n        return 0"
                ),
                verdict="flagged",
            ),
            # A guard on a pattern that binds what the guard reads is the
            # honest kind, and an unguarded wildcard is the fallthrough.
            RuleExample(
                code=(
                    "match lease:\n    case Lease(applied=True):\n"
                    '        return "merged"\n'
                    "    case Lease(reason=str() as reason) if reason:\n"
                    '        return reason\n    case _:\n        return "clean"'
                ),
                verdict="cleared",
            ),
            RuleExample(
                code=(
                    "match (before, view):\n"
                    "    case (None, PeerView(running=True)):\n        arrive()\n"
                    "    case (PeerView(doing=was), PeerView(doing=now)) if was != now:\n"
                    "        redescribe()"
                ),
                verdict="cleared",
            ),
        ],
        message=(
            "A guard on a wildcard — `case _ if …`, `case name if …` — decides on "
            "the guard alone, which is the chain in a `match` costume. Bind what "
            "the guard reads in the pattern instead: a class pattern with the "
            "field, a tuple of the fields, a literal. Where no pattern exists, "
            "write the decision as guard clauses that return; a guarded "
            "fallthrough after real arms nests its `if` inside `case _:`. Where "
            "the guard is the honest shape, `# lup: ignore[wildcard-guard]` "
            "carries why"
        ),
    ),
]
"""Python rules whose message reads the same whatever runtime is shown it."""


# lup: ignore[constant-declaration] — the bare noun a rule's own sentence uses,
# declared with the rule rather than chosen per caller
DOCUMENT_IN_HAND = "the file"
"""How the document rule names the file for a runtime to place in its sentence.

A rule speaks about no particular path, so what a runtime interpolates here is
the bare noun; whatever it says about handing a document over whole is the
runtime's own sentence to make.
"""

NO_RUNTIME_READER = Unsupported(
    reason="this table was compiled for no runtime, so none of them is speaking here"
)
"""The reader the neutral table carries: the repository audit and the rule
reference are read by people rather than by a runtime, and inventing a tool
name for them would be the platform leak the seam exists to prevent."""


def pdf_extraction_rule(document_reader: Spelling) -> AntiPattern:
    """The rule against PDF text extractors, completed by one runtime's reader.

    The extractor's failure is silent: a scanned or image-only page yields an
    empty string, which reads downstream as an empty document rather than as
    an extraction that did not happen. Handing the file whole to the runtime's
    own reader has no such mode.

    Which tool that is belongs to the runtime, and this rule ships into every
    plugin tree — so the sentence is asked for rather than written here, and a
    runtime with nothing that takes a document contributes none, leaving the
    failure mode stated and no tool named.
    """
    return AntiPattern(
        id="pdf-extraction",
        remedy="read the document whole instead of extracting its text",
        pattern=re.compile(
            r"\b(?:import|from)\s+"
            r"(?:fitz|pymupdf|pypdf|PyPDF2|PyPDF4|pdfplumber|pdfminer|pypdfium2)\b"
        ),
        matcher=Matcher(select=pdf_extraction_sites),
        examples=[
            RuleExample(code="import pypdf", verdict="flagged"),
            RuleExample(code="from pdfminer import high_level", verdict="flagged"),
            # A writer is not an extractor: nothing here comes back empty from
            # a scanned page, because nothing here reads one.
            RuleExample(code="import pdfkit", verdict="cleared"),
        ],
        message=" ".join(
            words
            for words in (
                "A PDF text extractor comes back empty from a scanned or image-only "
                "page, and an empty string reads as an empty document rather than as "
                "an extraction that failed — read the document whole instead of "
                "pulling text out of it.",
                document_reader.in_prose(),
            )
            if words
        ),
    )


def python_anti_patterns(
    document_reader: Spelling,
    portable: list[AntiPattern] = PORTABLE_PYTHON_ANTI_PATTERNS,
) -> list[AntiPattern]:
    """The Python table one runtime is shown, in its own words where it has them.

    The portable rows are this library's reading of the conventions rather than
    anything Python settles, so a project holding itself to a different set
    passes its own — the same reason :class:`RuleSet` takes its tables
    instead of naming them.
    """
    return [*portable, pdf_extraction_rule(document_reader)]


PYTHON_ANTI_PATTERNS: list[AntiPattern] = python_anti_patterns(NO_RUNTIME_READER)
"""Anti-patterns checked against added lines of `.py` files (mirrors the hook)."""


TS_ANTI_PATTERNS: list[AntiPattern] = [
    AntiPattern(
        id="as-any",
        pattern=re.compile(r"\bas\s+any\b"),
        examples=[
            RuleExample(code="const session = raw as any;", verdict="flagged"),
            RuleExample(
                code="const session = raw as SessionPayload;", verdict="cleared"
            ),
            RuleExample(
                code="// the cast read `raw as any` before the guard existed",
                verdict="cleared",
            ),
        ],
        message="Never use `as any` — use proper types or type guards",
    ),
    AntiPattern(
        id="as-unknown",
        pattern=re.compile(r"\bas\s+unknown\b"),
        examples=[
            RuleExample(code="const session = raw as unknown;", verdict="flagged"),
            RuleExample(
                code="const session = isSession(raw) ? raw : null;", verdict="cleared"
            ),
        ],
        message="Never use `as unknown` — use type guards or proper types",
    ),
    AntiPattern(
        id="any-annotation",
        pattern=re.compile(r":\s*any\b"),
        examples=[
            RuleExample(code="function load(payload: any) {}", verdict="flagged"),
            RuleExample(code="const session: any = raw;", verdict="flagged"),
            RuleExample(
                code="function load(payload: SessionPayload) {}", verdict="cleared"
            ),
            RuleExample(code="function load(payload: unknown) {}", verdict="cleared"),
            RuleExample(
                code="/** Whether a node answers a search: any of its readable fields carries the words. */",
                verdict="cleared",
            ),
            RuleExample(code="// TODO: any of these", verdict="cleared"),
            RuleExample(code='const label: string = ": any";', verdict="cleared"),
        ],
        message="Never use `any` type annotation — use specific types, generics, or `unknown`",
    ),
    AntiPattern(
        id="any-assertion",
        pattern=re.compile(r"<any>"),
        examples=[
            RuleExample(code="const session = <any>raw;", verdict="flagged"),
            RuleExample(code="const session = <SessionPayload>raw;", verdict="cleared"),
            RuleExample(
                code='const note = "a <any> cast hides the shape";', verdict="cleared"
            ),
        ],
        message="Never use `<any>` type assertion — use proper types",
    ),
    AntiPattern(
        id="ts-ignore",
        pattern=re.compile(r"@ts-ignore"),
        examples=[
            RuleExample(code="// @ts-ignore", verdict="flagged"),
            RuleExample(
                code="// the parameter's type admits this call",
                verdict="cleared",
            ),
        ],
        message="Never use @ts-ignore — fix the type error properly",
        context="comment",
    ),
    AntiPattern(
        id="ts-expect-error",
        pattern=re.compile(r"@ts-expect-error"),
        examples=[
            RuleExample(code="// @ts-expect-error", verdict="flagged"),
            RuleExample(code="// the overload covers this call", verdict="cleared"),
        ],
        message="Never use @ts-expect-error — fix the type error properly",
        context="comment",
    ),
    AntiPattern(
        id="ts-nocheck",
        pattern=re.compile(r"@ts-nocheck"),
        examples=[
            RuleExample(code="// @ts-nocheck", verdict="flagged"),
            RuleExample(
                code="// every export in this module is typed", verdict="cleared"
            ),
        ],
        message="Never use @ts-nocheck — fix the type errors in the file",
        context="comment",
    ),
    AntiPattern(
        id="eslint-disable",
        pattern=re.compile(r"//\s*eslint-disable"),
        examples=[
            RuleExample(
                code="// eslint-disable-next-line no-console", verdict="flagged"
            ),
            RuleExample(
                code="// the console call routes through the logger", verdict="cleared"
            ),
        ],
        message="Never use eslint-disable — fix the lint issue properly",
        context="comment",
    ),
    AntiPattern(
        id="eslint-disable-block",
        pattern=re.compile(r"/\*\s*eslint-disable"),
        examples=[
            RuleExample(code="/* eslint-disable no-console */", verdict="flagged"),
            RuleExample(
                code="/* the rule is satisfied, not switched off */", verdict="cleared"
            ),
        ],
        message="Never use eslint-disable — fix the lint issue properly",
        context="comment",
    ),
    AntiPattern(
        id="tslint-disable",
        pattern=re.compile(r"//\s*tslint:disable"),
        examples=[
            RuleExample(code="// tslint:disable:no-console", verdict="flagged"),
            RuleExample(
                code="// eslint reports this finding, not tslint", verdict="cleared"
            ),
        ],
        message="Never use tslint:disable — migrate to eslint and fix the issue",
        context="comment",
    ),
    AntiPattern(
        id="non-null-assertion",
        pattern=re.compile(r"[\w\)\]]!\."),
        examples=[
            RuleExample(code="const name = session!.name;", verdict="flagged"),
            RuleExample(code='const name = session?.name ?? "";', verdict="cleared"),
            # A negation is not an assertion: the rule is about the postfix.
            RuleExample(code="if (!session.name) { return; }", verdict="cleared"),
        ],
        message="Postfix `!.` non-null assertion hides a possible null/undefined — "
        "narrow the type or handle the missing case",
    ),
    AntiPattern(
        id="var-declaration",
        strength="strong",
        pattern=re.compile(r"\bvar\s+[A-Za-z_$]"),
        examples=[
            RuleExample(code="var count = 0;", verdict="flagged"),
            RuleExample(code="const count = 0;", verdict="cleared"),
            RuleExample(code="const variance = spread(rows);", verdict="cleared"),
        ],
        message="Use `const` or `let` instead of `var` — var is function-scoped and hoisted",
    ),
    AntiPattern(
        id="function-object-type",
        pattern=re.compile(r":\s*(?:Function|Object)\b"),
        examples=[
            RuleExample(code="let handler: Function;", verdict="flagged"),
            RuleExample(
                code="let handler: (event: SessionEvent) => void;", verdict="cleared"
            ),
        ],
        message="Never use `Function` or `Object` as a type — declare the call "
        "signature or the object shape",
    ),
    AntiPattern(
        id="console-log",
        pattern=re.compile(r"\bconsole\.log\s*\("),
        examples=[
            RuleExample(code="console.log(session);", verdict="flagged"),
            RuleExample(code="logger.debug(session);", verdict="cleared"),
        ],
        message="console.log is a debug leftover — remove it or route through a logger",
    ),
    historical_voice(
        examples=[
            RuleExample(
                code="// the home a launch selects, previously read from the environment",
                verdict="flagged",
            ),
            RuleExample(
                code="/* the record this replaced is gone; see #436 for why */",
                verdict="flagged",
            ),
            RuleExample(code="// the home a launch selects", verdict="cleared"),
            RuleExample(code='const said = "previously";', verdict="cleared"),
        ],
    ),
]
"""Anti-patterns checked against added lines of TypeScript/JavaScript files."""

MARKDOWN_ANTI_PATTERNS: list[AntiPattern] = [
    historical_voice(
        examples=[
            RuleExample(
                code="The home a launch selects, previously read from the environment.",
                verdict="flagged",
            ),
            RuleExample(
                code="Measured in #202: two workers blocked on one question.",
                verdict="flagged",
            ),
            RuleExample(code="The home a launch selects.", verdict="cleared"),
            RuleExample(
                code="A sentence saying `previously` is what the rule refuses.",
                verdict="cleared",
            ),
        ],
    ),
]
"""The rules a Markdown page is held to: how its prose is written, and no more.

A page is prose throughout, so a rule about code has nothing to read in one;
the historical-voice rule is the one that does, and a passage is where most
of the prose this repository renders is written.
"""


# lup: ignore[library-default] — Python's own source suffixes
PY_SUFFIXES = (".py", ".pyi")

PROJECT_RULES: list[ProjectRule] = [
    CAPABILITY_RULE,
    ABSTRACT_DECLARATION_RULE,
    DISPATCH_RULE,
    CHAIN_RULE,
    SEAM_RULE,
    ASSEMBLY_RULE,
    FRONT_DOOR_RULE,
    NATIVE_SPELLING_RULE,
    KERNEL_IMPORTS_RULE,
    LIBRARY_DEFAULT_RULE,
    CONSTANT_DECLARATION_RULE,
    STALE_REFERENCE_RULE,
]
"""Every rule this library reads off the whole project.

Each is declared beside the audit that decides it and gathered here once, so
the sweep that runs them, the reference that renders them and the suite that
checks their examples all read one list — a rule declared in its module and
absent here would be a rule nothing runs, which is why the list is the set's
and not the sweep's.
"""

COMPOSITION_RULES: list[CompositionRule] = [PORTABLE_RULE]
"""Every rule this library judges the assembled harness by, at generation."""

# lup: ignore[frozenset-shape] — name identity membership, read at every line
FOREIGN_RULE_IDS: frozenset[str] = frozenset(rule.id for rule in PROJECT_RULES)
"""Rule ids the project rules own.

A typed ``# lup: ignore[...]`` naming one of these is judged by that rule's
own audit, so this auditor never reports it spurious — while an id no rule
owns still is. Taken off the set rather than named, so a rule declared there
is foreign here the moment it exists.
"""


class RuleSet(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Every rule a project checks, by the surface that decides it.

    ``python``, ``typescript`` and ``markdown`` are the line rules the
    hermetic kernel runs on every edit and the sweep runs again over each
    file; ``project`` the
    rules only the sweep can run, over the whole tree at once; ``composition``
    the rules generation runs over the assembled harness. The rules a project
    holds itself to are its own conventions written down, so the tables this
    library ships are what a caller starts from rather than what it is stuck
    with — one set reaches the edit hook, the whole-file audit, and the
    generated rule reference together, so a project that replaces it replaces
    all three at once.
    """

    python: list[AntiPattern] = Field(default_factory=lambda: PYTHON_ANTI_PATTERNS)
    typescript: list[AntiPattern] = Field(default_factory=lambda: TS_ANTI_PATTERNS)
    markdown: list[AntiPattern] = Field(default_factory=lambda: MARKDOWN_ANTI_PATTERNS)
    project: list[ProjectRule] = Field(default_factory=lambda: PROJECT_RULES)
    composition: list[CompositionRule] = Field(
        default_factory=lambda: COMPOSITION_RULES
    )

    def for_suffix(self, suffix: str) -> list[AntiPattern] | None:
        """The table that applies to a file suffix, or None to skip it.

        Mirrors the hook's split: Python files are checked against the Python
        table, TS/JS-family files against the TS table, Markdown pages
        against the Markdown table, and any other suffix is not scanned (the
        hook gates those three families alone).
        """
        if suffix in PY_SUFFIXES:
            return self.python
        if suffix in TYPESCRIPT_SUFFIXES:
            return self.typescript
        if suffix in MARKDOWN_SUFFIXES:
            return self.markdown
        return None

    def selected(self, selection: RuleSelection) -> "RuleSet":
        """This set with the rules a project retired taken out of every table.

        Narrowing the table rather than filtering findings afterwards is what
        keeps a retired rule from reaching any surface at all: it does not
        fire, it does not render into the compiled plugin, and a directive
        naming it is not graded against a rule that stopped existing here.
        """
        return RuleSet(
            python=[rule for rule in self.python if selection.keeps(rule.id)],
            typescript=[rule for rule in self.typescript if selection.keeps(rule.id)],
            markdown=[rule for rule in self.markdown if selection.keeps(rule.id)],
            project=[rule for rule in self.project if selection.keeps(rule.id)],
            composition=[rule for rule in self.composition if selection.keeps(rule.id)],
        )


def rule_set_for(
    document_reader: Spelling,
    selection: RuleSelection | None = None,
    declared: list[AntiPattern] | None = None,
) -> RuleSet:
    """The tables one native plugin ships, its own reader spelled into them.

    Generation reaches this once per runtime, so the rows compiled into a
    plugin say what that runtime can actually do — and a runtime that declines
    ships the rule with its reason stated and no tool it does not have named.

    ``declared`` carries the shapes a project refuses beyond these, appended
    before the selection runs so a project can retire one of its own the same
    way it retires one of the library's. The two compose in that order because
    a project that added a rule and then thought better of it should not have
    to delete the declaration to stop enforcing it.

    The project's selection narrows the result, so a retired rule is absent
    from the compiled plugin rather than enforced by a hook the sweep stopped
    agreeing with.
    """
    return RuleSet(
        python=[*python_anti_patterns(document_reader), *(declared or [])]
    ).selected(selection or RuleSelection())


# `RuleSet.for_suffix` is the operation; this binds the default table to it.
def patterns_for_suffix(
    suffix: str, rules: RuleSet | None = None
) -> list[AntiPattern] | None:
    """The anti-pattern table one file suffix is checked against."""
    return (rules or RuleSet()).for_suffix(suffix)


def selected_lines(text: str, patterns: list[AntiPattern]) -> dict[str, set[int]]:
    """Which lines each matched rule selects in this text, computed once.

    Where a tree can be had, the selector answers and the pattern is not
    consulted. Where none can — a TypeScript file, a fragment that will not
    parse — what happens next is the rule's own strength, because that is
    what decides the cost of guessing: a soft rule falls back to its pattern,
    where guessing wide costs a directive carrying a reason; a strong rule
    fires nowhere, because no directive may silence one and a verdict the
    tree never confirmed would be a denial with no escape.

    The declaration holds the function here, where the kernel's
    :func:`~lup.policy.kernel.edit.matched_lines` has to resolve a name out
    of a primitive row. Same answer, reached the short way.
    """
    parses = python_tree(text) is not None
    return {
        pattern.id: lines_of(pattern.matcher.select(text)) if parses else set()
        for pattern in patterns
        if pattern.matcher is not None and (parses or pattern.strength == "strong")
    }


def line_hits(
    lines: LineProjections,
    line_no: int,
    patterns: list[AntiPattern],
    selected: dict[str, set[int]] | None = None,
) -> list[AntiPattern]:
    """Every anti-pattern one line trips, each matched in its declared context.

    A rule that selected its lines from the tree is decided by that: the
    grammar already said which lines carry the shape, and re-reading the text
    could only disagree with it. ``selected`` carries no entry for a rule
    whose text would not parse, which is what puts that rule back on its
    pattern.

    Masked source is scanned per rule context: a "code" rule sees string
    literals and comments blanked, so identifiers in prose never match, while
    a "comment" directive rule sees comments intact — a standalone `# noqa`
    line included. Text no grammar masked (a Python fragment that will not
    tokenize, a file of neither family) keeps the conservative whole-line
    scan, skipping blank lines and pure comments that carry no `type:`
    directive — aligned with what the hook would decide.

    There are two contexts and forty-odd rules, so both projections of the
    line are taken once and the table reads whichever one it declared —
    stripping the line per rule made the same two strings forty times over.
    """
    matched = selected or {}

    def trips(pattern: AntiPattern, scanned: dict[RuleContext, str]) -> bool:
        if pattern.id in matched:
            return line_no in matched[pattern.id]
        text = scanned[pattern.context]
        return bool(text) and pattern.pattern.search(text) is not None

    if not lines.tokenized:
        raw = lines.commented[line_no - 1].strip()
        if not raw or (raw.startswith("#") and "type:" not in raw):
            return []
        return [ap for ap in patterns if trips(ap, dict.fromkeys(RULE_CONTEXTS, raw))]
    scanned: dict[RuleContext, str] = {
        context: lines.scan_text(line_no, context) for context in RULE_CONTEXTS
    }
    return [ap for ap in patterns if trips(ap, scanned)]


class AntiPatternFinding(BaseModel):
    """One auditor result about a single line's anti-pattern guarding.

    ``kind`` is:
    - "missing": the line trips a rule with no `# lup: ignore` covering it.
    - "spurious": a `# lup: ignore[id]` (or a bare one) guards a rule the line
      does not trip — a dead directive to delete.
    - "untyped": a bare `# lup: ignore` validly silences the line but names no
      rule; it stays valid, and is surfaced as an advisory rather than a
      blocker until it names the rules it means.

    ``rule_id`` is the rule the finding concerns (empty for a bare marker that
    guards nothing). ``line`` is 1-based.
    """

    kind: str
    line: int
    text: str
    message: str
    rule_id: str = ""


def audit_text(
    text: str,
    patterns: list[AntiPattern],
    refutations: list[Refutation] | None = None,
    typescript: bool = False,
    markdown: bool = False,
    graded: list[str] | None = None,
) -> list[AntiPatternFinding]:
    """Audit one file's current text for per-rule ignore-marker health.

    ``graded`` names the rules whose directives this audit judges in the file,
    every one where it is ``None``. A test is held to the prose rule alone, so
    a directive there for a rule about code, or for another check's own
    marker, is not graded: the edit hook does not judge it either, and
    whether it guards anything is a question about a rule the file is not
    held to.

    ``typescript`` says the text is of that family, read through the
    kernel's span scan rather than the Python tokenizer, and ``markdown``
    that it is a page, prose less the code it quotes — the same split the
    hook makes from the file's suffix, so both gates mask one file one way.

    A bare file-level `# lup: ignore` opts the whole file out (matching the
    hook); the audit reports it as a single advisory "untyped" finding and
    scans nothing else. A typed file-level `# lup: ignore[id]` opts out only
    the named rule file-wide — and when nothing in the file still needs an
    id (no line trips it that an inline directive would not already cover),
    that id reports "spurious" at the directive line, so rule evolution
    cannot leave dead file-wide opt-outs behind. A docstring line is read by
    a prose rule alone: a rule about code finds nothing of its subject in a
    sentence, and a rule about how sentences are written has no reason to
    care whether a comment or a docstring carried one. No directive can open
    inside a string, so a hit in a docstring is rewritten, or opted out of
    file-wide. (The hook reads the same projections wherever the text
    tokenizes, and falls back to scanning the raw line only where it does
    not.) Then, per line and per rule:

    - a tripped rule with no covering ignore -> "missing";
    - a typed `# lup: ignore[id]` naming a rule the line does not trip, or a
      bare ignore on a line that trips nothing -> "spurious";
    - a bare `# lup: ignore` that does silence the line -> "untyped".

    A hit a resolution settled is not a trip at all, so a directive naming
    that rule there reports "spurious" — the audit drives the cleanup of
    markers the refinement made unnecessary. These arrive as `refutations`
    the caller resolved: `lup.harness.codescan.resolution` passes the sites
    whose receiver a type oracle proved outside the rule's family. One it
    refuted without settling — the checker inferred no type for the receiver
    — drops the demand and nothing else: the line is not reported missing,
    and a directive there stands, because a marker deleted on what a checker
    failed to learn is one the next sweep, resolving the receiver, demands
    straight back. With none supplied and no oracle behind them, every
    selected line stands.

    An ignore counts as a guard only where a comment actually starts (per the
    tokenizer), so a docstring or string literal that merely *mentions*
    `# lup: ignore` — and a note whose prose quotes it — guards nothing. Text
    that does not tokenize as Python falls back to a plain substring check.

    Where it may be written is one policy for every rule, line-shaped and
    AST-shaped alike: the line it guards, or standing alone directly above —
    the placement a reason too long for the column budget needs, since a
    comment is the one thing the formatter cannot wrap. A directive is graded
    against the lines it reaches rather than the line it sits on, so the
    overflow placement is read where it applies instead of being reported as
    guarding nothing.
    """
    file_ignore = file_level_ignore(text)
    file_disabled: set[str] = set()
    if file_ignore is not None:
        if file_ignore.rule_ids is None:
            return [  # bare file-level opt-out disables every rule — surface it
                AntiPatternFinding(
                    kind="untyped",
                    line=file_ignore.line,
                    text="# lup: ignore",
                    message="bare file-level `# lup: ignore` opts the whole file out of "
                    "every rule — name the rules it needs: `# lup: ignore[rule, ...]`",
                )
            ]
        file_disabled = file_ignore.rule_ids

    context = (
        PythonContext.parse_typescript(text)
        if typescript
        else PythonContext.parse_markdown()
        if markdown
        else PythonContext.parse(text)
    )
    refuted = {
        (refutation.rule_id, refutation.line)
        for refutation in refutations or []
        if refutation.settled
    }
    # Refuted without being settled: the checker inferred no type for the
    # receiver. These hits stay in `hits_by_line` so a directive written over
    # one is read as reached rather than dead, and are never reported missing.
    unresolved = {
        (refutation.rule_id, refutation.line)
        for refutation in refutations or []
        if not refutation.settled
    }

    file_ignore_line = file_ignore.line if file_ignore is not None else 0
    original_lines = text.splitlines()
    projections = LineProjections.parse(text, typescript, markdown)
    selected = selected_lines(text, patterns)

    def written_directive(line_no: int) -> re.Match[str] | None:
        """The directive actually written on one line, if a comment opens it.

        The whole-file opt-out is not one of these. It stands alone like the
        overflow placement does, so reading it as one would quietly make it
        the guard for whatever line happens to follow the header.
        """
        if line_no < 1 or line_no > len(original_lines) or line_no == file_ignore_line:
            return None
        match = context.suppression_at(line_no, original_lines[line_no - 1])
        if match is None or not context.comment_at(line_no, match.start()):
            return None
        return match

    def guarding_directive(line_no: int) -> re.Match[str] | None:
        """The directive covering `line_no`, wherever the one policy puts it.

        The same placement every project-wide rule accepts, so a marker that
        reads correctly against an AST rule reads correctly here. A directive
        found above is the overflow placement: the line it guards had no room
        left for the reason that justifies it, and a reason worth reading
        often needs more than one line of its own.

        Every line up to the head of the comment block, because that is what
        `suppression_reaches` accepts and this only asks it where to look. A
        fixed pair of candidates is complete only where the policy is capped
        at the line directly above, and this one reaches further.

        The head of the block is also where the search stops. A line that
        does not continue the block ends every reach from above it, so
        reading further can only re-derive the same nothing — and reading
        further is reading to the top of the file, once per audited line,
        which is the whole file walked once per line it contains.
        """
        inline = written_directive(line_no)
        if inline is not None:
            return inline
        for candidate in range(line_no - 1, 0, -1):
            match = written_directive(candidate)
            if match is not None and suppression_reaches(
                original_lines, candidate, line_no
            ):
                return match
            if not continues_comment_block(original_lines[candidate - 1]):
                return None
        return None

    def guarded_lines(line_no: int) -> list[int]:
        """Every audited line the directive written on `line_no` reaches.

        The mirror of :func:`guarding_directive`: a directive is judged
        against the lines it actually covers, so one standing above its
        violation is read there rather than reported as guarding nothing where
        it sits.

        Asked of every audited line rather than the next one, so the two
        directions agree. Were they to disagree, one marker would be reported
        spurious here and its violation missing there — the failure this pair
        exists to prevent, produced by the pair itself.
        """
        return [
            candidate
            for candidate in sorted(hits_by_line)
            if candidate >= line_no
            and suppression_reaches(original_lines, line_no, candidate)
        ]

    hits_by_line = {
        number: [
            ap
            for ap in line_hits(projections, number, patterns, selected)
            if (ap.id, number) not in refuted
            and (ap.context == "prose" or number not in context.docstring_lines)
        ]
        for number in range(1, len(original_lines) + 1)
        # The file-level directive line is not itself audited.
        if number != file_ignore_line
    }

    def quoted(line_no: int) -> str:
        """The line a finding shows, whole.

        A diagnostic is the last place to elide: the reader is here because
        something is wrong with this line, and the end of it — where a
        directive is written — is what a cut takes first.
        """
        return original_lines[line_no - 1].strip()

    file_live: set[str] = set()
    findings: list[AntiPatternFinding] = []
    for index, hits in hits_by_line.items():
        directive = guarding_directive(index)
        covered_ids = ignore_rule_ids(directive) if directive is not None else None
        for ap in hits:
            # Nothing shown about the receiver: the rule demands no directive
            # here and this audit reports none missing. The hit itself stays,
            # so a directive over it keeps a file-level opt-out live below and
            # is graded as reached rather than dead further down.
            open_question = (ap.id, index) in unresolved
            if ap.strength == "strong" and not open_question:
                # No directive reaches this one. A soft rule's suppression is a
                # reasoned exception the audit then grades; a strong rule has a
                # replacement that is right every time, so the same comment
                # would only record a decision to keep the defect.
                findings.append(
                    AntiPatternFinding(
                        kind="missing",
                        line=index,
                        text=quoted(index),
                        message=f"{ap.message} (no suppression: write the replacement)",
                        rule_id=ap.id,
                    )
                )
                continue
            if ap.id in file_disabled:
                # Live only when the file-level directive is the sole silencer;
                # an inline-covered hit does not keep the file-wide id alive.
                if directive is None or not (
                    covered_ids is None or ap.id in covered_ids
                ):
                    file_live.add(ap.id)
                continue
            if directive is not None and (covered_ids is None or ap.id in covered_ids):
                continue
            if open_question:
                continue
            findings.append(
                AntiPatternFinding(
                    kind="missing",
                    line=index,
                    text=quoted(index),
                    message=f"{ap.message} — suppress on {suppression_placement(index)}",
                    rule_id=ap.id,
                )
            )

    # Every directive is graded where it is written, against the lines it
    # reaches rather than the one it sits on. Those differ for exactly the
    # overflow placement, and conflating them is what reported a marker
    # standing above its violation as guarding nothing.
    for index in range(1, len(original_lines) + 1):
        directive = written_directive(index)
        if directive is None:
            continue
        named = ignore_rule_ids(directive)
        guarded = [
            ap for line_no in guarded_lines(index) for ap in hits_by_line[line_no]
        ]
        reached = {ap.id for ap in guarded}
        silenced = {
            ap.id
            for ap in guarded
            if ap.strength != "strong" and ap.id not in file_disabled
        }
        if named is not None:
            findings.extend(
                AntiPatternFinding(
                    kind="spurious",
                    line=index,
                    text=quoted(index),
                    message=f"`# lup: ignore[{rid}]` guards a line that does not trip `{rid}` — remove it",
                    rule_id=rid,
                )
                for rid in sorted(
                    (named if graded is None else named & set(graded))
                    - reached
                    - FOREIGN_RULE_IDS
                )
            )
            continue
        covered = sorted(rid for rid in reached if rid not in file_disabled)
        if silenced:
            findings.append(
                AntiPatternFinding(
                    kind="untyped",
                    line=index,
                    text=quoted(index),
                    message="`# lup: ignore` is untyped — name the rule(s) it silences: "
                    f"`# lup: ignore[{', '.join(covered)}]`",
                    rule_id=covered[0] if covered else "",
                )
            )
        else:
            findings.append(
                AntiPatternFinding(
                    kind="spurious",
                    line=index,
                    text=quoted(index),
                    message="`# lup: ignore` guards a line that matches no anti-pattern — remove it",
                )
            )

    if file_ignore is not None:
        directive_text = text.splitlines()[file_ignore.line - 1].strip()
        for rid in sorted(
            (file_disabled if graded is None else file_disabled & set(graded))
            - file_live
            - FOREIGN_RULE_IDS
        ):
            findings.append(
                AntiPatternFinding(
                    kind="spurious",
                    line=file_ignore.line,
                    text=directive_text,
                    message=f"file-level `# lup: ignore[{rid}]` names a rule nothing "
                    "in the file needs — remove it",
                    rule_id=rid,
                )
            )
    return findings
