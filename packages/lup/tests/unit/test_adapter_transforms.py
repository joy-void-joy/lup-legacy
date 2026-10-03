"""Provider-owned profile, endpoint, and native event-decoding transforms.

The decoding half names every vendor shape either hook decoder recognizes.
``parse_claude_before_tool`` and ``parse_codex_before_tool`` narrow a hook
payload into an operation; ``ClaudeEventDecoder.decode`` and
``CodexEventDecoder.decode`` narrow that operation into the vocabulary a policy
judges. A shape that silently stops matching — a renamed vendor field, a
``case`` arm reordered above one it now shadows, a ``str()`` guard that no
longer holds — falls through to the fallback and is reported as an unknown
tool, which is the failure these decoders exist to make loud.

So the roster is derived rather than restated. The payload cases are pinned
against the arms read back out of each parser, and the operation cases against
the members of ``ClaudeOperation`` and ``CodexOperation``. An arm or a union
member added without a case fails this module instead of passing unnoticed.
"""

import ast
import inspect
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeAliasType, get_args

import pytest
from pydantic import AnyHttpUrl, BaseModel, SecretStr

from lup.providers.claude import ClaudeCompatibleEndpoint
from lup.providers.claude.config import (
    ClaudeCompatibilityTransform,
    ClaudeProfileRegistry,
    ClaudeProfileResolver,
    ClaudeProfileSelection,
    claude_profile_selector,
)
from lup.providers.claude.native import (
    ClaudeBeforeToolEvent,
    ClaudeEditBatchOperation,
    ClaudeEditOperation,
    ClaudeEventDecoder,
    ClaudeFetchOperation,
    ClaudeHookPayload,
    ClaudeOperation,
    ClaudeSearchOperation,
    ClaudeShellOperation,
    ClaudeUnknownOperation,
    ClaudeWriteOperation,
    parse_claude_before_tool,
)
from lup.providers.claude import Claude
from lup.providers.codex import CodexCompatibleEndpoint
from lup.providers.codex.config import (
    CodexCompatibilityTransform,
    CodexProfileRegistry,
    CodexProfileSelection,
    codex_profile_selector,
)
from lup.providers.codex.native import (
    CodexBeforeToolEvent,
    CodexEventDecoder,
    CodexFetchOperation,
    CodexFileChange,
    CodexFileChangeOperation,
    CodexHookPayload,
    CodexOperation,
    CodexSearchOperation,
    CodexShellOperation,
    CodexUnknownOperation,
    parse_codex_before_tool,
)
from lup.providers.codex import Codex
from lup.policy.models import (
    BeforeTool,
    EditBatch,
    EditChange,
    FetchUrl,
    SearchWeb,
    ShellCommand,
    ToolIdentity,
    UnknownTool,
)
from lup.providers.config import ProfileSelector
from lup.providers.routing import (
    ExactModelMatcher,
    ModelRoute,
    ModelRouter,
    PrefixModelMatcher,
)
from lup.types import CustomModel, JsonObject


class DecoderArm(BaseModel, frozen=True):
    """One ``case`` arm of a vendor-facing decoder, named by what it accepts."""

    discriminators: list[str]
    """Every vendor string the arm pins — a method, a tool name, an item type."""

    label: str
    """What it pins plus the keys it needs; unique among one decoder's arms."""


def key_name(key: ast.expr | None) -> str:
    """The literal name one mapping-pattern entry is keyed under."""
    match key:
        case ast.Constant(value=str(name)):
            return name
        case _:
            return "?"


def entry_label(key: ast.expr | None, value: ast.pattern) -> str:
    """One mapping entry as ``key``, or ``key=literal`` where the arm pins it."""
    match value:
        case ast.MatchValue(value=ast.Constant(value=str(literal))):
            return f"{key_name(key)}={literal}"
        case _:
            return key_name(key)


def pattern_discriminators(pattern: ast.pattern | None) -> list[str]:
    """Every vendor string one pattern pins, in the order it names them."""
    match pattern:
        case ast.MatchValue(value=ast.Constant(value=str(literal))):
            return [literal]
        case ast.MatchSequence(patterns=[head, *_]):
            return pattern_discriminators(head)
        case ast.MatchMapping(keys=keys, patterns=values):
            return [
                literal
                for key, value in zip(keys, values, strict=True)
                if key_name(key) == "type"
                for literal in pattern_discriminators(value)
            ]
        case ast.MatchOr(patterns=alternatives):
            return [
                literal
                for alternative in alternatives
                for literal in pattern_discriminators(alternative)
            ]
        case ast.MatchAs(pattern=inner):
            return pattern_discriminators(inner)
        case _:
            return []


def pattern_label(pattern: ast.pattern | None) -> str:
    """One arm spelled out: what it pins, and the payload keys it requires."""
    match pattern:
        case ast.MatchValue(value=ast.Constant(value=str(literal))):
            return literal
        case ast.MatchSequence(patterns=parts):
            return "".join(pattern_label(part) for part in parts)
        case ast.MatchMapping(keys=keys, patterns=values):
            entries = ", ".join(
                entry_label(key, value) for key, value in zip(keys, values, strict=True)
            )
            return f"({entries})"
        case ast.MatchOr(patterns=alternatives):
            return " | ".join(pattern_label(part) for part in alternatives)
        case ast.MatchClass(cls=ast.Name(id=name)):
            return name
        case ast.MatchClass(cls=ast.Attribute(attr=name)):
            return name
        case ast.MatchAs(pattern=inner) if inner is not None:
            return pattern_label(inner)
        case _:
            return "_"


def decoder_arms(decoder: Callable[..., object]) -> list[DecoderArm]:
    """Every ``case`` arm of one decoder's match statement, in source order.

    A decoder that narrows a vendor payload has no union of ours to enumerate:
    its roster is the literal strings its arms pin, and a hand-kept list of
    them goes quiet the day it stops matching. Reading the arms back out of the
    decoder's own source is what makes the roster impossible to fall behind.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(decoder)))

    def statements() -> Iterator[ast.Match]:
        for node in ast.walk(tree):
            match node:
                case ast.Match():
                    yield node

    return [
        DecoderArm(
            discriminators=pattern_discriminators(case.pattern),
            label=pattern_label(case.pattern),
        )
        for case in next(statements()).cases
    ]


def arm_labels(decoder: Callable[..., object]) -> list[str]:
    """Each arm's label — the roster a per-shape suite has to name in full."""
    return [arm.label for arm in decoder_arms(decoder)]


def union_members(alias: TypeAliasType) -> list[str]:
    """Every model one ``type X = A | B`` alias names, by class name."""
    return [member.__name__ for member in get_args(alias.__value__)]


def test_claude_profile_precedence_and_immutability(tmp_path: Path) -> None:
    registry = ClaudeProfileRegistry(
        profiles={
            "active": ClaudeProfileSelection(config_directory=tmp_path / "active"),
            "explicit": ClaudeProfileSelection(config_directory=tmp_path / "explicit"),
        },
        active="active",
        default=ClaudeProfileSelection(config_directory=tmp_path / "default"),
    )
    resolver = ClaudeProfileResolver(registry)
    original = Claude(model=CustomModel(id="claude"), environment={"KEEP": "1"})

    active = resolver.resolve(None).apply(original)
    explicit = resolver.resolve("explicit").apply(original)

    assert active.environment["CLAUDE_CONFIG_DIR"] == str(tmp_path / "active")
    assert explicit.environment["CLAUDE_CONFIG_DIR"] == str(tmp_path / "explicit")
    assert explicit.environment["KEEP"] == "1"
    assert "CLAUDE_CONFIG_DIR" not in original.environment
    with pytest.raises(KeyError, match="unknown Claude profile"):
        resolver.resolve("missing")


def test_an_unnamed_claude_profile_leaves_the_home_its_environment_selects() -> None:
    """Naming ``~/.claude`` is not naming nothing, so the default names nothing.

    Under a named ``~/.claude`` Claude Code reads ``~/.claude/.claude.json``,
    and under none the ``~/.claude.json`` beside it, so a default naming the
    directory opened every session on a document the account never wrote.
    Codex's default names no home either."""
    resolver = ClaudeProfileResolver(ClaudeProfileRegistry())
    original = Claude(model=CustomModel(id="claude"), environment={"KEEP": "1"})

    configured = resolver.resolve(None).apply(original)

    assert "CLAUDE_CONFIG_DIR" not in configured.environment
    assert configured.environment["KEEP"] == "1"


def test_profile_selector_resolves_and_applies_to_the_agent(tmp_path: Path) -> None:
    registry = ClaudeProfileRegistry(
        profiles={"work": ClaudeProfileSelection(config_directory=tmp_path / "work")}
    )
    selector = ProfileSelector(ClaudeProfileResolver(registry))
    base = Claude(model=CustomModel(id="claude"), environment={"KEEP": "1"})

    selected = selector.session_factory(base, "work")

    assert selected.environment["CLAUDE_CONFIG_DIR"] == str(tmp_path / "work")
    assert selected.environment["KEEP"] == "1"
    assert "CLAUDE_CONFIG_DIR" not in base.environment


def test_adapter_selectors_expose_the_resolved_transform(tmp_path: Path) -> None:
    claude = claude_profile_selector(
        ClaudeProfileRegistry(default=ClaudeProfileSelection(config_directory=tmp_path))
    )
    codex = codex_profile_selector(
        CodexProfileRegistry(default=CodexProfileSelection(codex_home=tmp_path))
    )

    claude_config = claude.transform().apply(Claude(model=CustomModel(id="claude")))
    codex_config = codex.transform().apply(
        Codex(model=CustomModel(id="gpt"), cwd=tmp_path)
    )

    assert claude_config.environment["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    assert codex_config.environment["CODEX_HOME"] == str(tmp_path)


def test_claude_compatible_endpoint_owns_auth_and_aliases() -> None:
    original = Claude(model=CustomModel(id="served-model"), environment={"KEEP": "1"})
    transformed = ClaudeCompatibilityTransform(
        ClaudeCompatibleEndpoint(
            base_url=AnyHttpUrl("http://localhost:8000/v1"),
            api_key=SecretStr("secret"),
            auth_style="api_key",
        )
    ).apply(original)

    assert transformed.environment["ANTHROPIC_BASE_URL"] == ("http://localhost:8000/v1")
    assert transformed.environment["ANTHROPIC_API_KEY"] == "secret"
    assert transformed.environment["ANTHROPIC_AUTH_TOKEN"] == ""
    assert transformed.environment["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == ("served-model")
    assert transformed.environment["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "served-model"
    assert "ANTHROPIC_BASE_URL" not in original.environment


def test_codex_compatible_endpoint_uses_structured_provider_config(
    tmp_path: Path,
) -> None:
    original = Codex(model=CustomModel(id="local"), cwd=tmp_path)
    transformed = CodexCompatibilityTransform(
        CodexCompatibleEndpoint(
            identifier="local_provider",
            base_url=AnyHttpUrl("http://localhost:8000/v1"),
            api_key=SecretStr("secret"),
        )
    ).apply(original)

    assert transformed.model_provider == "local_provider"
    assert transformed.environment["LUP_OPENAI_COMPAT_API_KEY"] == "secret"
    assert transformed.provider_config == {
        "model_provider": "local_provider",
        "model_providers": {
            "local_provider": {
                "name": "local_provider",
                "base_url": "http://localhost:8000/v1",
                "env_key": "LUP_OPENAI_COMPAT_API_KEY",
            }
        },
    }
    assert original.provider_config is None


def test_model_router_uses_explicit_recipe_then_first_match() -> None:
    broad = Claude(model=CustomModel(id="broad"))
    exact = Claude(model=CustomModel(id="exact"))
    router = ModelRouter(
        [
            ModelRoute(
                name="broad",
                matcher=PrefixModelMatcher("model-"),
                recipe=lambda: broad,
            ),
            ModelRoute(
                name="exact",
                matcher=ExactModelMatcher("model-special"),
                recipe=lambda: exact,
            ),
        ]
    )

    assert router.resolve("model-special") is broad
    assert router.resolve("unmatched", recipe="exact") is exact
    with pytest.raises(LookupError, match="unknown factory recipe"):
        router.resolve("model-special", recipe="missing")
    with pytest.raises(LookupError, match="no configured route"):
        router.resolve("other")


class HookCase(BaseModel, frozen=True):
    """One native hook payload and the whole ``BeforeTool`` it decodes to."""

    name: str
    arm: str
    """The parser arm this payload reaches, as ``decoder_arms`` labels it."""

    tool_name: str
    tool_input: JsonObject
    expected: BeforeTool


class ClaudeOperationCase(BaseModel, frozen=True):
    """One decoded Claude operation and the ``BeforeTool`` it becomes."""

    name: str
    operation: ClaudeOperation
    expected: BeforeTool


class CodexOperationCase(BaseModel, frozen=True):
    """One decoded Codex operation and the ``BeforeTool`` it becomes."""

    name: str
    operation: CodexOperation
    expected: BeforeTool


CLAUDE_HOOK_CASES = [
    HookCase(
        name="edit-carries-both-sides-of-one-change",
        arm="Edit(file_path, old_string, new_string)",
        tool_name="Edit",
        tool_input={
            "file_path": "/repo/a.py",
            "old_string": "before",
            "new_string": "after",
        },
        expected=BeforeTool(
            tool=EditBatch(
                changes=[
                    EditChange(path=Path("/repo/a.py"), before="before", after="after")
                ]
            ),
            identity=ToolIdentity(original_name="Edit"),
        ),
    ),
    HookCase(
        name="write-to-a-path-the-tree-lacks-is-a-creation",
        arm="Write(file_path, content)",
        tool_name="Write",
        tool_input={"file_path": "/repo/new.py", "content": "body"},
        expected=BeforeTool(
            tool=EditBatch(
                changes=[
                    EditChange(
                        path=Path("/repo/new.py"), after="body", operation="create"
                    )
                ]
            ),
            identity=ToolIdentity(original_name="Write"),
        ),
    ),
    HookCase(
        name="bash-escaping-the-sandbox-is-its-own-arm",
        arm="Bash(command, dangerouslyDisableSandbox)",
        tool_name="Bash",
        tool_input={"command": "uv run pytest", "dangerouslyDisableSandbox": True},
        expected=BeforeTool(
            tool=ShellCommand(command="uv run pytest", unsandboxed=True),
            identity=ToolIdentity(original_name="Bash"),
        ),
    ),
    HookCase(
        name="plain-bash-stays-sandboxed",
        arm="Bash(command)",
        tool_name="Bash",
        tool_input={"command": "uv run pytest"},
        expected=BeforeTool(
            tool=ShellCommand(command="uv run pytest"),
            identity=ToolIdentity(original_name="Bash"),
        ),
    ),
    HookCase(
        # The escape is read as a value and not as a key, so declining it
        # reaches the sandboxed arm rather than the one below it.
        name="bash-declining-the-escape-stays-sandboxed",
        arm="Bash(command)",
        tool_name="Bash",
        tool_input={"command": "uv run pytest", "dangerouslyDisableSandbox": False},
        expected=BeforeTool(
            tool=ShellCommand(command="uv run pytest"),
            identity=ToolIdentity(original_name="Bash"),
        ),
    ),
    HookCase(
        name="web-fetch-carries-a-parsed-url",
        arm="WebFetch(url)",
        tool_name="WebFetch",
        tool_input={"url": "https://docs.example.com/api"},
        expected=BeforeTool(
            tool=FetchUrl(url=AnyHttpUrl("https://docs.example.com/api")),
            identity=ToolIdentity(original_name="WebFetch"),
        ),
    ),
    HookCase(
        name="an-unparseable-fetch-url-falls-back-to-unknown",
        arm="WebFetch(url)",
        tool_name="WebFetch",
        tool_input={"url": "not a url"},
        expected=BeforeTool(
            tool=UnknownTool(
                identity=ToolIdentity(original_name="WebFetch"),
                input={"url": "not a url"},
            ),
            identity=ToolIdentity(original_name="WebFetch"),
        ),
    ),
    HookCase(
        name="web-search-carries-its-query",
        arm="WebSearch(query)",
        tool_name="WebSearch",
        tool_input={"query": "agent sdk hooks"},
        expected=BeforeTool(
            tool=SearchWeb(query="agent sdk hooks"),
            identity=ToolIdentity(original_name="WebSearch"),
        ),
    ),
    HookCase(
        name="an-unnamed-tool-keeps-its-whole-input",
        arm="_",
        tool_name="mcp__notes__write",
        tool_input={"path": "notes.md", "text": "body"},
        expected=BeforeTool(
            tool=UnknownTool(
                identity=ToolIdentity(original_name="mcp__notes__write"),
                input={"path": "notes.md", "text": "body"},
            ),
            identity=ToolIdentity(original_name="mcp__notes__write"),
        ),
    ),
    HookCase(
        # A named tool whose payload lost a field is the drift this suite
        # exists for: it is reported as unrecognized, carrying what arrived.
        name="a-named-tool-missing-a-field-keeps-its-whole-input",
        arm="_",
        tool_name="Edit",
        tool_input={"file_path": "/repo/a.py", "old_string": "before"},
        expected=BeforeTool(
            tool=UnknownTool(
                identity=ToolIdentity(original_name="Edit"),
                input={"file_path": "/repo/a.py", "old_string": "before"},
            ),
            identity=ToolIdentity(original_name="Edit"),
        ),
    ),
]


CODEX_HOOK_CASES = [
    HookCase(
        name="bash-carries-its-command",
        arm="Bash(command)",
        tool_name="Bash",
        tool_input={"command": "uv run pytest"},
        expected=BeforeTool(
            tool=ShellCommand(command="uv run pytest"),
            identity=ToolIdentity(original_name="Bash"),
        ),
    ),
    HookCase(
        name="web-fetch-carries-a-parsed-url",
        arm="web_fetch(url)",
        tool_name="web_fetch",
        tool_input={"url": "https://docs.example.com/api"},
        expected=BeforeTool(
            tool=FetchUrl(url=AnyHttpUrl("https://docs.example.com/api")),
            identity=ToolIdentity(original_name="web_fetch"),
        ),
    ),
    HookCase(
        name="an-unparseable-fetch-url-falls-back-to-unknown",
        arm="web_fetch(url)",
        tool_name="web_fetch",
        tool_input={"url": "not a url"},
        expected=BeforeTool(
            tool=UnknownTool(
                identity=ToolIdentity(original_name="web_fetch"),
                input={"url": "not a url"},
            ),
            identity=ToolIdentity(original_name="web_fetch"),
        ),
    ),
    HookCase(
        name="web-search-carries-its-query",
        arm="web_search(query)",
        tool_name="web_search",
        tool_input={"query": "app-server notifications"},
        expected=BeforeTool(
            tool=SearchWeb(query="app-server notifications"),
            identity=ToolIdentity(original_name="web_search"),
        ),
    ),
    HookCase(
        # The hook seam never sees a parsed patch, so the whole payload stays
        # opaque and is judged as an unknown tool rather than as an edit.
        name="an-opaque-patch-stays-conservative",
        arm="_",
        tool_name="apply_patch",
        tool_input={"patch": "*** Begin Patch\n*** End Patch\n"},
        expected=BeforeTool(
            tool=UnknownTool(
                identity=ToolIdentity(original_name="apply_patch"),
                input={"patch": "*** Begin Patch\n*** End Patch\n"},
            ),
            identity=ToolIdentity(original_name="apply_patch"),
        ),
    ),
]


CLAUDE_OPERATION_CASES = [
    ClaudeOperationCase(
        name="ClaudeEditOperation",
        operation=ClaudeEditOperation(path=Path("a.py"), before="old", after="new"),
        expected=BeforeTool(
            tool=EditBatch(
                changes=[EditChange(path=Path("a.py"), before="old", after="new")]
            ),
            identity=ToolIdentity(original_name="Edit"),
        ),
    ),
    ClaudeOperationCase(
        name="ClaudeWriteOperation",
        operation=ClaudeWriteOperation(path=Path("a.py"), content="body"),
        expected=BeforeTool(
            tool=EditBatch(
                changes=[
                    EditChange(path=Path("a.py"), after="body", operation="create")
                ]
            ),
            identity=ToolIdentity(original_name="Write"),
        ),
    ),
    ClaudeOperationCase(
        # Three native tools collapse onto one semantic batch, and the batch
        # is what a policy judges — a single change and many are the same kind.
        name="ClaudeEditBatchOperation",
        operation=ClaudeEditBatchOperation(
            changes=[
                EditChange(path=Path("a.py"), before="old", after="new"),
                EditChange(path=Path("b.py"), before="left", after="right"),
            ]
        ),
        expected=BeforeTool(
            tool=EditBatch(
                changes=[
                    EditChange(path=Path("a.py"), before="old", after="new"),
                    EditChange(path=Path("b.py"), before="left", after="right"),
                ]
            ),
            identity=ToolIdentity(original_name="Edit"),
        ),
    ),
    ClaudeOperationCase(
        name="ClaudeShellOperation",
        operation=ClaudeShellOperation(
            command="ls", cwd=Path("/repo"), unsandboxed=True
        ),
        expected=BeforeTool(
            tool=ShellCommand(command="ls", cwd=Path("/repo"), unsandboxed=True),
            identity=ToolIdentity(original_name="Bash"),
        ),
    ),
    ClaudeOperationCase(
        name="ClaudeFetchOperation",
        operation=ClaudeFetchOperation(url="https://docs.example.com/api"),
        expected=BeforeTool(
            tool=FetchUrl(url=AnyHttpUrl("https://docs.example.com/api")),
            identity=ToolIdentity(original_name="WebFetch"),
        ),
    ),
    ClaudeOperationCase(
        name="ClaudeFetchOperation-unparseable",
        operation=ClaudeFetchOperation(url="not a url"),
        expected=BeforeTool(
            tool=UnknownTool(
                identity=ToolIdentity(original_name="WebFetch"),
                input={"url": "not a url"},
            ),
            identity=ToolIdentity(original_name="WebFetch"),
        ),
    ),
    ClaudeOperationCase(
        name="ClaudeSearchOperation",
        operation=ClaudeSearchOperation(query="agent sdk hooks"),
        expected=BeforeTool(
            tool=SearchWeb(query="agent sdk hooks"),
            identity=ToolIdentity(original_name="WebSearch"),
        ),
    ),
    ClaudeOperationCase(
        name="ClaudeUnknownOperation",
        operation=ClaudeUnknownOperation(name="Novel", input={"seen": "once"}),
        expected=BeforeTool(
            tool=UnknownTool(
                identity=ToolIdentity(original_name="Novel"), input={"seen": "once"}
            ),
            identity=ToolIdentity(original_name="Novel"),
        ),
    ),
]


CODEX_OPERATION_CASES = [
    CodexOperationCase(
        name="CodexFileChangeOperation",
        operation=CodexFileChangeOperation(
            changes=[CodexFileChange(path=Path("a.py"), before="old", after="new")]
        ),
        expected=BeforeTool(
            tool=EditBatch(
                changes=[EditChange(path=Path("a.py"), before="old", after="new")]
            ),
            identity=ToolIdentity(original_name="apply_patch"),
        ),
    ),
    CodexOperationCase(
        # A creation has no predecessor and a deletion no successor, and both
        # survive the crossing as the missing side rather than as an empty one.
        name="CodexFileChangeOperation-creation",
        operation=CodexFileChangeOperation(
            changes=[CodexFileChange(path=Path("new.py"), after="body")]
        ),
        expected=BeforeTool(
            tool=EditBatch(changes=[EditChange(path=Path("new.py"), after="body")]),
            identity=ToolIdentity(original_name="apply_patch"),
        ),
    ),
    CodexOperationCase(
        name="CodexFileChangeOperation-deletion",
        operation=CodexFileChangeOperation(
            changes=[CodexFileChange(path=Path("gone.py"), before="body")]
        ),
        expected=BeforeTool(
            tool=EditBatch(changes=[EditChange(path=Path("gone.py"), before="body")]),
            identity=ToolIdentity(original_name="apply_patch"),
        ),
    ),
    CodexOperationCase(
        # Codex has no sandbox-escape field to read, so a shell call it decodes
        # is never the unsandboxed kind.
        name="CodexShellOperation",
        operation=CodexShellOperation(command="ls", cwd=Path("/repo")),
        expected=BeforeTool(
            tool=ShellCommand(command="ls", cwd=Path("/repo")),
            identity=ToolIdentity(original_name="Bash"),
        ),
    ),
    CodexOperationCase(
        name="CodexFetchOperation",
        operation=CodexFetchOperation(url="https://docs.example.com/api"),
        expected=BeforeTool(
            tool=FetchUrl(url=AnyHttpUrl("https://docs.example.com/api")),
            identity=ToolIdentity(original_name="web_fetch"),
        ),
    ),
    CodexOperationCase(
        name="CodexFetchOperation-unparseable",
        operation=CodexFetchOperation(url="not a url"),
        expected=BeforeTool(
            tool=UnknownTool(
                identity=ToolIdentity(original_name="web_fetch"),
                input={"url": "not a url"},
            ),
            identity=ToolIdentity(original_name="web_fetch"),
        ),
    ),
    CodexOperationCase(
        name="CodexSearchOperation",
        operation=CodexSearchOperation(query="app-server notifications"),
        expected=BeforeTool(
            tool=SearchWeb(query="app-server notifications"),
            identity=ToolIdentity(original_name="web_search"),
        ),
    ),
    CodexOperationCase(
        name="CodexUnknownOperation",
        operation=CodexUnknownOperation(name="Novel", input={"seen": "once"}),
        expected=BeforeTool(
            tool=UnknownTool(
                identity=ToolIdentity(original_name="Novel"), input={"seen": "once"}
            ),
            identity=ToolIdentity(original_name="Novel"),
        ),
    ),
]


def test_every_claude_hook_arm_is_named_by_a_payload_case() -> None:
    assert sorted({case.arm for case in CLAUDE_HOOK_CASES}) == sorted(
        arm_labels(parse_claude_before_tool)
    )


def test_every_codex_hook_arm_is_named_by_a_payload_case() -> None:
    assert sorted({case.arm for case in CODEX_HOOK_CASES}) == sorted(
        arm_labels(parse_codex_before_tool)
    )


def test_every_claude_operation_model_is_named_by_a_decoded_case() -> None:
    assert sorted(
        {type(case.operation).__name__ for case in CLAUDE_OPERATION_CASES}
    ) == sorted(union_members(ClaudeOperation))


def test_every_codex_operation_model_is_named_by_a_decoded_case() -> None:
    assert sorted(
        {type(case.operation).__name__ for case in CODEX_OPERATION_CASES}
    ) == sorted(union_members(CodexOperation))


def test_the_claude_decoder_answers_each_operation_model_in_one_arm() -> None:
    """The union is the roster on both sides, so neither can grow alone.

    Reading the union alone would let an arm for something outside it pass
    unnoticed, since a case with no member behind it has no case here to
    contradict. Reading the arms alone would miss a member that never gained
    one. Naming both against the union closes each gap with the other.
    """
    assert sorted(arm_labels(ClaudeEventDecoder.decode)) == sorted(
        union_members(ClaudeOperation)
    )


def test_the_codex_decoder_answers_each_operation_model_in_one_arm() -> None:
    assert sorted(arm_labels(CodexEventDecoder.decode)) == sorted(
        union_members(CodexOperation)
    )


@pytest.mark.parametrize(
    "case", CLAUDE_HOOK_CASES, ids=[case.name for case in CLAUDE_HOOK_CASES]
)
def test_each_claude_hook_payload_decodes_to_the_tool_it_names(case: HookCase) -> None:
    payload = ClaudeHookPayload(tool_name=case.tool_name, tool_input=case.tool_input)

    assert (
        ClaudeEventDecoder().decode(parse_claude_before_tool(payload)) == case.expected
    )


@pytest.mark.parametrize(
    "case", CODEX_HOOK_CASES, ids=[case.name for case in CODEX_HOOK_CASES]
)
def test_each_codex_hook_payload_decodes_to_the_tool_it_names(case: HookCase) -> None:
    payload = CodexHookPayload(tool_name=case.tool_name, tool_input=case.tool_input)

    assert CodexEventDecoder().decode(parse_codex_before_tool(payload)) == case.expected


@pytest.mark.parametrize(
    "case", CLAUDE_OPERATION_CASES, ids=[case.name for case in CLAUDE_OPERATION_CASES]
)
def test_each_claude_operation_decodes_to_the_tool_it_names(
    case: ClaudeOperationCase,
) -> None:
    event = ClaudeBeforeToolEvent(operation=case.operation)

    assert ClaudeEventDecoder().decode(event) == case.expected


@pytest.mark.parametrize(
    "case", CODEX_OPERATION_CASES, ids=[case.name for case in CODEX_OPERATION_CASES]
)
def test_each_codex_operation_decodes_to_the_tool_it_names(
    case: CodexOperationCase,
) -> None:
    event = CodexBeforeToolEvent(operation=case.operation)

    assert CodexEventDecoder().decode(event) == case.expected


def test_a_write_over_an_existing_file_carries_the_file_as_its_preimage(
    tmp_path: Path,
) -> None:
    """A whole-file write cannot erase a file's review notes unseen.

    A `Write` states only what the file is about to become, so the decoder is
    the last place that can still see what it is about to stop being. Handed
    no preimage, the marker gate would compare the new text against `""`,
    count no notes lost, and admit the write.

    The generated dispatchers read that document themselves, and this path
    reads it too, so a session composed in process decides as the plugin its
    own declaration generates does.
    """
    target = tmp_path / "noted.py"
    target.write_text("# lup: work somebody owes\nx = 1\n", encoding="utf-8")

    decoded = ClaudeEventDecoder().decode(
        ClaudeBeforeToolEvent(
            operation=ClaudeWriteOperation(path=target, content="x = 2\n")
        )
    )
    batch = decoded.tool
    assert isinstance(batch, EditBatch)
    change = batch.as_documents().changes[0]

    assert change.operation == "overwrite"
    assert change.before == "# lup: work somebody owes\nx = 1\n"


def test_a_write_to_a_missing_path_stays_a_creation_with_no_preimage(
    tmp_path: Path,
) -> None:
    """Nothing to read, and nothing that reading it could protect."""
    decoded = ClaudeEventDecoder().decode(
        ClaudeBeforeToolEvent(
            operation=ClaudeWriteOperation(
                path=tmp_path / "absent.py", content="x = 1\n"
            )
        )
    )
    batch = decoded.tool
    assert isinstance(batch, EditBatch)
    change = batch.as_documents().changes[0]

    assert change.operation == "create"
    assert change.before is None
