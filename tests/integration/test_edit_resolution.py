"""The edit gate's route to a checker, end to end.

The unit tests pin each half against a fake: the kernel asks when nothing
resolved a receiver and admits the line when something did, and resolution
refutes a declaration outside the family. What neither can show is that the
host half actually reaches a checker and comes back with an answer the kernel
can read — a chain of a subprocess, a CLI, a language server and a JSON
contract, every link of which is silent when it breaks. A gate that always
answered "nothing resolved" would pass every unit test in the suite and ask
about every `.get(` an agent ever writes.
"""

from pathlib import Path

import pytest

from lup.devtools.dev.pyright_oracle import langserver_path
from lup.policy.assets.host import resolved_refutations
from lup.workspace.paths import project_root

pytestmark = pytest.mark.skipif(
    langserver_path() is None, reason="pyright-langserver is not installed"
)

RESOLUTION_COMMAND = ["lup-devtools", "dev", "refutations"]
"""The bare name this project really declares, resolved the way the gate does.

Spelled `.venv/bin/lup-devtools` before, which is `uv`'s default layout and
not where this project's environment always is: `UV_PROJECT_ENVIRONMENT`
redirects it. So the path named nothing wherever that redirect points — and
:func:`resolved_refutations` reports a resolver that is not installed as the
same silence as none declared, which is an answer this test asserts about
elsewhere. It passed by proving nothing about the chain it exists to prove.

The bare name is also what :func:`~lup.policy.assets.host.declared_program`
is for, so declaring it here exercises that resolution rather than routing
around it.
"""

PROPOSED = (
    "import httpx\n"
    "\n"
    "\n"
    "def read(client: httpx.Client) -> httpx.Response:\n"
    '    return client.get("url")\n'
)
"""A receiver only a checker can settle: an HTTP client, outside the family.

Two shapes would not do, both because the tree rules them out on its own and
no checker is ever asked. A module-qualified `httpx.get` is one. A key
computed at runtime is the other: `client.get(url)` reads a map whose keys
are data, which is not what this rule is about. Stating each shape once is
the point, and is also why neither can stand in for the case tested here.
"""

REFUTED_LINE = 5
"""Where `client.get("url")` sits in the proposed content above."""


def unwritten(root: Path) -> str:
    """A path inside the checkout that nothing has ever written.

    The point of the buffer: the content is judged before it exists, so the
    file it belongs to need not. What the path decides is import resolution
    and the module's own name, and those follow from where it *would* be.
    """
    return str(root / "packages" / "lup" / "src" / "lup" / "codescan" / "unwritten.py")


def test_the_host_resolves_a_receiver_the_gate_could_not() -> None:
    root = project_root()

    refuted = resolved_refutations(unwritten(root), PROPOSED, RESOLUTION_COMMAND)

    assert refuted == {"refuted": {"dict-get": [REFUTED_LINE]}, "unresolved": {}}
    assert not Path(unwritten(root)).exists(), "resolving wrote the file"


def test_a_mapping_receiver_comes_back_unrefuted() -> None:
    """Empty is an answer: a checker looked and the rule stands.

    This and a checker that never ran have to arrive differently, because one
    is evidence the gate should deny on and the other is why it must ask.
    """
    root = project_root()
    proposed = 'payload: dict[str, str] = {}\nvalue = payload.get("name")\n'

    refuted = resolved_refutations(unwritten(root), proposed, RESOLUTION_COMMAND)

    assert refuted == {"refuted": {}, "unresolved": {}}


def test_a_framework_header_map_resolves_as_the_mapping_it_is() -> None:
    """Starlette's `Headers` is a `typing.Mapping`, and the checker says so.

    The receiver: `request.headers.get("literal")`, where `request` is
    annotated `Request`. The sweep resolves it into the family and demands a
    directive, so a hook reporting it refuted — because its own checker
    answers nothing — would delete the very marker this answer requires.
    Pinned here so the resolved verdict is the one both gates are built on.
    """
    root = project_root()
    proposed = (
        "from starlette.requests import Request\n"
        "\n"
        "\n"
        "def resume_of(request: Request) -> str:\n"
        '    return request.headers.get("last-event-id", "")\n'
    )

    refuted = resolved_refutations(unwritten(root), proposed, RESOLUTION_COMMAND)

    assert refuted == {"refuted": {}, "unresolved": {}}


def test_no_declared_resolver_is_not_an_empty_refutation() -> None:
    """Nothing looked, so the gate has to ask rather than refuse."""
    root = project_root()

    assert resolved_refutations(unwritten(root), PROPOSED, []) is None


def test_a_resolver_that_is_not_installed_reports_nothing_resolved() -> None:
    """A declared path that is not there is the same silence as none declared."""
    root = project_root()

    assert (
        resolved_refutations(unwritten(root), PROPOSED, ["nowhere/lup-devtools"])
        is None
    )
