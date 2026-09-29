"""Which word of an interpreter invocation is the program it runs.

The criterion is one sentence -- a script file runs, and nothing that leaves
no reviewable artifact behind does -- and all of the difficulty is in the
grammar: an option's value is never the script, a cluster carries every
letter in it, and an option nobody listed is unread rather than guessed at.
"""

import shlex

import pytest

from lup.policy.kernel.programs import ProgramKind, read_program


@pytest.mark.parametrize(
    ("command", "kind", "subject"),
    [
        ("bash tmp/x.sh", "script", "tmp/x.sh"),
        ("bash -o pipefail tmp/x.sh", "script", "tmp/x.sh"),
        ("bash +o posix tmp/x.sh", "script", "tmp/x.sh"),
        ("bash -eo pipefail tmp/x.sh", "script", "tmp/x.sh"),
        ("bash -x tmp/x.sh -c ignored", "script", "tmp/x.sh"),
        ("bash -- -named.sh", "script", "-named.sh"),
        ("bash -ec 'x'", "inline", "-ec"),
        ("bash -s arg", "inline", "-s"),
        ("bash /dev/fd/3", "inline", "/dev/fd/3"),
        ("bash -o", "unread", "-o"),
        ("bash -Q tmp/x.sh", "unread", "-Q"),
        ("bash '$SCRIPT'", "unread", "$SCRIPT"),
        ("bash -x", "bare", ""),
        ("node --import=./hooks.js tmp/x.js", "script", "tmp/x.js"),
        ("node --max-old-space-size=4096 tmp/x.js", "script", "tmp/x.js"),
        (
            "node --import=data:text/javascript,x tmp/x.js",
            "inline",
            "--import=data:text/javascript,x",
        ),
        ("node --no-warnings tmp/x.js", "script", "tmp/x.js"),
        ("node --run build", "bare", ""),
        ("bun tmp/x.ts", "script", "tmp/x.ts"),
        ("bun x.mjs", "script", "x.mjs"),
        ("bun install", "subcommand", "install"),
        ("bun --smol run dev", "subcommand", "run"),
        ("deno run -r=https://deno.land tmp/x.ts", "script", "run tmp/x.ts"),
        ("deno eval 'x'", "inline", "eval"),
        ("deno fmt", "subcommand", "fmt"),
        ("deno run", "bare", "run"),
        ("python -Wignore tmp/x.py", "script", "tmp/x.py"),
        ("python -OO tmp/x.py", "script", "tmp/x.py"),
        ("python -m examples.x", "module", "examples.x"),
        ("python -Bm http.server", "unread", "-Bm"),
        ("python -Sc 'x'", "inline", "-Sc"),
        ("perl -w x.pl", "unread", "-w"),
        ("perl -e 'x'", "inline", "-e"),
        ("bash --version", "informational", "--version"),
        ("bash -h", "bare", ""),
        ("python3 -V", "informational", "-V"),
        ("python -VV", "informational", "-VV"),
        ("python --help-all", "informational", "--help-all"),
        ("node -v", "informational", "-v"),
        ("bun --revision", "informational", "--revision"),
        ("deno --version", "informational", "--version"),
        ("deno -V", "informational", "-V"),
        ("perl -v", "informational", "-v"),
        ("bash --version -c ls", "inline", "-c"),
        ("python3 -V tmp/x.py", "script", "tmp/x.py"),
    ],
)
def test_the_program_is_where_the_options_stop(
    command: str, kind: ProgramKind, subject: str
) -> None:
    reading = read_program(shlex.split(command))
    assert (reading["kind"], reading["subject"]) == (kind, subject)
