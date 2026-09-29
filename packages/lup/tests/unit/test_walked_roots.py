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
from lup.policy.kernel.walks import walked_roots
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
    ],
)
def test_a_recursive_reader_walks_the_roots_its_grammar_names(
    command: str, roots: list[str]
) -> None:
    assert [root["path"] for root in walked_roots(command.split())] == roots


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
