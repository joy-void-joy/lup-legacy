"""Which words a recursive reader walks, read off each utility's own grammar.

A key or a login is refused to every word that names it, and a recursive
reader names none: `grep -r password ~` spells a home and reads the keys in
it. The host says whether anything withheld lies beneath a walked root, since
only a filesystem can; what is pinned here is which roots those are, and that
the kernel refuses a walk the host found reaching one.
"""

import pytest

from lup.policy.kernel.rows import RefusedPathRow, WithheldWalkRow
from lup.policy.kernel.shell import decide_shell
from lup.policy.kernel.walks import reads_names, skipped_file, walked_roots
from lup.policy.refused_paths import credential_files
from lup.policy.shell_rules import erase_shell_rules
from lup.policy.vocabulary import default_vocabulary

ROWS: list[RefusedPathRow] = [credential_files().erased()]


@pytest.mark.parametrize(
    ("command", "roots"),
    [
        ("grep -r password ~", ["~"]),
        ("grep -rn password ~ /etc", ["~", "/etc"]),
        ("grep -R password", ["."]),
        ("grep --recursive -e password src tests", ["src", "tests"]),
        ("grep -d recurse password ~", ["~"]),
        ("grep password ~/notes.txt", []),
        ("rg password ~", ["~"]),
        ("rg password", ["."]),
        ("rg --files ~", []),
        ("tar czf tmp/out.tgz ~", ["~"]),
        ("tar -czf tmp/out.tgz -C / home", ["/home"]),
        ("tar --create --file=tmp/out.tar ~", ["~"]),
        ("tar xzf tmp/out.tgz", []),
        ("tar --no-recursion -cf tmp/out.tar ~", []),
        ("zip -r tmp/out.zip ~", ["~"]),
        ("zip tmp/out.zip notes.txt", []),
        ("cp -r ~ tmp/home", ["~"]),
        ("cp -a src tmp/src", ["src"]),
        ("cp notes.txt tmp/notes.txt", []),
        ("rsync -a ~/ tmp/home/", ["~/"]),
        ("scp -r ~ host:backup", ["~"]),
        ("diff -r ~ tmp/home", ["~", "tmp/home"]),
        ("ls -R ~", []),
        ("find ~ -name id_rsa", []),
        ("du -sh ~", []),
        # A payload handed every name find yields reads each of them.
        ("find ~ -exec cat {} +", ["~"]),
        ("find -L ~ /etc -type f -exec grep -l x {} ;", ["~", "/etc"]),
        ("find ~ -execdir cat {} ;", ["~"]),
        ("find -exec cat {} +", ["."]),
        ("find ~ -exec echo done ;", []),
    ],
)
def test_a_recursive_reader_walks_the_roots_its_grammar_names(
    command: str, roots: list[str]
) -> None:
    assert [root["path"] for root in walked_roots(command.split())] == roots


@pytest.mark.parametrize(
    ("command", "roots"),
    [
        ("find ~ -name id_rsa", ["~"]),
        ("ls -R ~", ["~"]),
        ("ls ~", []),
        ("du -a ~", ["~"]),
        ("tree ~ /etc", ["~", "/etc"]),
        ("fd id_rsa ~", ["~"]),
        ("rg --files ~", ["~"]),
    ],
)
def test_a_listing_read_through_a_pipeline_walks_what_it_lists(
    command: str, roots: list[str]
) -> None:
    """A name is not the secret, until something downstream reads each one."""
    assert [
        root["path"] for root in walked_roots(command.split(), listed=True)
    ] == roots


@pytest.mark.parametrize(
    ("command", "consumed"),
    [
        ("xargs cat", True),
        ("xargs -0 grep -l token", True),
        ("read -r f", True),
        ("xargs", False),
        ("cat", False),
    ],
)
def test_names_read_from_input_are_handed_to_a_program(
    command: str, consumed: bool
) -> None:
    assert reads_names(command.split()) is consumed


@pytest.mark.parametrize(
    ("command", "hidden"),
    [
        ("grep -r password ~", True),
        ("rg password ~", False),
        ("rg --hidden password ~", True),
        ("rg -uu password ~", True),
        ("rg -u -u password ~", True),
        ("rg -u password ~", False),
    ],
)
def test_a_walk_reads_dot_names_unless_its_tool_skips_them(
    command: str, hidden: bool
) -> None:
    (root,) = walked_roots(command.split())

    assert root["hidden"] is hidden


@pytest.mark.parametrize(
    ("command", "excluded", "skipped"),
    [
        ("grep -r --exclude-dir=.lup x .", [".lup"], []),
        (
            "grep -r --exclude-dir .lup --exclude-dir=node_modules x .",
            [".lup", "node_modules"],
            [],
        ),
        ("grep -r --exclude=.env.local x .", [], [".env.local"]),
        ("grep -r x .", [], []),
    ],
)
def test_a_walk_leaves_out_what_its_tool_is_told_to(
    command: str, excluded: list[str], skipped: list[str]
) -> None:
    """A directory and a file are left out apart: `--exclude=.lup` still descends."""
    (root,) = walked_roots(command.split())

    assert root["path"] == "."
    assert sorted(root["excluded"]) == sorted(excluded)
    assert root["skipped"] == skipped


def test_a_walk_the_host_found_reaching_a_key_is_refused() -> None:
    rules = erase_shell_rules(default_vocabulary())
    walks = [WithheldWalkRow(root="~", found="/home/u/.ssh/id_rsa")]

    refused = decide_shell(
        "grep -r password ~", rules, refused_paths=ROWS, withheld_walks=walks
    )
    unfound = decide_shell("grep -r password ~", rules, refused_paths=ROWS)

    assert refused.effect == "deny"
    assert "/home/u/.ssh/id_rsa" in refused.reason
    assert unfound.effect == "allow"


@pytest.mark.parametrize(
    ("command", "yielded"),
    [
        ("find . -name *.py -exec grep -l x {} +", ["*.py"]),
        ("find . -name *.py -o -name *.json -exec cat {} +", []),
        ("find . -iname *.MD -type f -exec cat {} +", ["*.MD"]),
        ("find ~ -exec cat {} +", []),
    ],
)
def test_a_find_yields_only_the_names_its_plain_tests_admit(
    command: str, yielded: list[str]
) -> None:
    """`find . -name '*.py' | xargs grep` never hands its reader a login."""
    (root,) = walked_roots(command.split())

    assert root["yielded"] == yielded
    assert skipped_file("auth.json", root) is bool(yielded)
