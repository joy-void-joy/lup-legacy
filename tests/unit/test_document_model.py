"""A generated document holds values, and a value cannot break its container.

What this is against is the shape it replaced: a workflow and a frontmatter
block built by formatting text, where a description carrying a colon, a
command carrying a newline or a key a reader takes for a boolean lands in the
file as structure rather than as the value it was. The file stays plausible,
and what notices is whatever parses it next.

The claim the nodes make is narrow and checkable: whatever a document renders,
parsing it back yields the data the nodes declare. Everything below is that
claim put to the values most likely to break it.
"""

from pathlib import Path

import yaml
from tomlkit import TOMLDocument

from lup.formats.markdown import MarkdownDocument, Prose
from lup.formats.toml import edited_manifest
from lup.formats.yaml import (
    YamlDocument,
    YamlEntry,
    YamlFlow,
    YamlItem,
    YamlList,
    YamlMap,
    YamlScalar,
    scalars,
)


def mapping(pairs: dict[str, str]) -> YamlDocument:
    """One flat document, the shape a frontmatter block is."""
    return YamlDocument(root=YamlMap(entries=scalars(pairs)))


def test_a_value_that_would_end_its_mapping_is_written_as_a_value() -> None:
    """The hazard, one value at a time: each reads back as what it was.

    Each of these ended the mapping it was interpolated into, and the two
    that did not — `yes` and `on` — changed type instead, which is worse for
    being invisible: a description reading `yes` became the boolean True.
    """
    hazards = {
        "colon": "a: b",
        "hash": "trailing # comment",
        "star": "* leading star",
        "quote": "it's quoted",
        "truthy": "yes",
        "bracket": "[not a list]",
        "brace": "{not a map}",
        "dashes": "--- not a document",
    }
    # Declared through an entry rather than through `scalars`, which reads an
    # empty value as a field nobody filled and writes no key for it.
    empty = YamlEntry(key="empty", value=YamlScalar(value=""))
    document = YamlDocument(
        root=YamlMap(entries=[*scalars(hazards), empty]),
    )

    parsed = yaml.safe_load(document.text())

    assert parsed == {**hazards, "empty": ""}


def test_a_key_both_readers_disagree_about_is_quoted() -> None:
    """`on:` is the key every workflow opens with, and a boolean to pyyaml.

    ruamel writes YAML 1.2 and pyyaml reads 1.1, so an unquoted `on` is a
    string to the emitter and `True` to the reader. Quoting settles it for
    both, which is why the document is checked against the stricter one.
    """
    document = YamlDocument(
        root=YamlMap(entries=[YamlEntry(key="on", value=YamlScalar(value="push"))])
    )

    assert "'on':" in document.text()
    assert yaml.safe_load(document.text()) == {"on": "push"}


def test_a_multi_line_value_keeps_its_lines_and_its_indentation() -> None:
    """A script inside a step is a literal block, not a folded quotation."""
    document = mapping({"run": "first --thing\nsecond --thing\n"})

    assert "run: |\n" in document.text()
    assert yaml.safe_load(document.text()) == {"run": "first --thing\nsecond --thing\n"}


def test_a_comment_stands_above_the_entry_it_explains() -> None:
    """The reason a document is declared rather than dumped.

    A generated file is read, and no emitter keeps a comment through a plain
    dump — so the entry carries it, and a blank line where a long mapping
    wants one.
    """
    document = YamlDocument(
        root=YamlMap(
            entries=[
                YamlEntry(key="name", value=YamlScalar(value="Publish")),
                YamlEntry(
                    key="jobs",
                    spaced=True,
                    comment="why this job exists\nsaid in two lines",
                    value=YamlMap(entries=scalars({"runs-on": "ubuntu-latest"})),
                ),
            ]
        )
    )

    assert document.text() == (
        "name: Publish\n"
        "\n"
        "# why this job exists\n"
        "# said in two lines\n"
        "jobs:\n"
        "  runs-on: ubuntu-latest\n"
    )


def test_a_nested_comment_is_indented_with_the_key_it_stands_above() -> None:
    """Placed by column, which is why a node is told where it stands."""
    document = YamlDocument(
        root=YamlMap(
            entries=[
                YamlEntry(
                    key="jobs",
                    value=YamlMap(
                        entries=[
                            YamlEntry(
                                key="permissions",
                                comment="what stands in for a stored token",
                                value=YamlMap(entries=scalars({"id-token": "write"})),
                            )
                        ]
                    ),
                )
            ]
        )
    )

    assert document.text() == (
        "jobs:\n"
        "  # what stands in for a stored token\n"
        "  permissions:\n"
        "    id-token: write\n"
    )


def test_a_comment_stands_above_the_item_it_explains() -> None:
    """A sequence item is explained at its dash, not inside the mapping it holds."""
    steps = YamlList(
        items=[
            YamlItem(value=YamlMap(entries=scalars({"run": "uv sync"}))),
            YamlItem(
                comment="why this step exists",
                value=YamlMap(entries=scalars({"name": "Step", "run": "true"})),
            ),
        ]
    )
    document = YamlDocument(
        root=YamlMap(
            entries=[
                YamlEntry(
                    key="jobs",
                    value=YamlMap(entries=[YamlEntry(key="steps", value=steps)]),
                )
            ]
        )
    )

    assert document.text() == (
        "jobs:\n"
        "  steps:\n"
        "    - run: uv sync\n"
        "    # why this step exists\n"
        "    - name: Step\n"
        "      run: 'true'\n"
    )


def test_a_comment_inside_an_item_is_indented_with_the_item_s_keys() -> None:
    """An item's keys start past its dash, and so does a comment above one."""
    step = YamlMap(
        entries=[
            YamlEntry(key="name", value=YamlScalar(value="Step")),
            YamlEntry(key="run", comment="why it runs", value=YamlScalar(value="x")),
        ]
    )
    document = YamlDocument(
        root=YamlMap(
            entries=[
                YamlEntry(key="steps", value=YamlList(items=[YamlItem(value=step)]))
            ]
        )
    )

    assert document.text() == (
        "steps:\n  - name: Step\n    # why it runs\n    run: x\n"
    )


def test_a_sequence_of_mappings_is_written_one_dashed_item_per_line() -> None:
    """What a workflow's steps are, and the layout every reader expects."""
    document = YamlDocument(
        root=YamlMap(
            entries=[
                YamlEntry(
                    key="steps",
                    value=YamlList(
                        items=[
                            YamlItem(
                                value=YamlMap(
                                    entries=scalars({"uses": "actions/checkout@v4"})
                                )
                            ),
                            YamlItem(
                                value=YamlMap(entries=scalars({"run": "uv sync"}))
                            ),
                        ]
                    ),
                )
            ]
        )
    )

    assert document.text() == (
        "steps:\n  - uses: actions/checkout@v4\n  - run: uv sync\n"
    )


def test_an_inline_sequence_stays_on_the_line_that_names_it() -> None:
    """A short list reads better inline, and is quoted in flow context."""
    document = YamlDocument(
        root=YamlMap(
            entries=[
                YamlEntry(key="branches", value=YamlFlow(items=["main", "dev"])),
                YamlEntry(key="tags", value=YamlFlow(items=["v*", "a, b"])),
            ]
        )
    )

    assert "branches: [main, dev]" in document.text()
    assert yaml.safe_load(document.text()) == {
        "branches": ["main", "dev"],
        "tags": ["v*", "a, b"],
    }


def test_an_entry_with_nothing_to_say_writes_no_key() -> None:
    """A declared default nobody filled is absent, not empty.

    An empty string is a value: a step whose `working-directory` is `""` runs
    in a directory named that, which is not the same as the step not naming
    one. `None` and `False` are kept, being something said.
    """
    document = YamlDocument(
        root=YamlMap(
            entries=[
                *scalars({"name": "step", "working-directory": "", "quiet": False}),
                YamlEntry(key="pull_request", value=YamlScalar(value=None)),
            ]
        )
    )

    assert yaml.safe_load(document.text()) == {
        "name": "step",
        "quiet": False,
        "pull_request": None,
    }


def test_a_mapping_cannot_declare_one_key_twice() -> None:
    """The second would silently win, and the document would still parse."""
    try:
        YamlMap(entries=scalars({"a": "one"}) + scalars({"a": "two"}))
    except ValueError as refused:
        assert "one key twice" in str(refused)
    else:
        raise AssertionError("a repeated key was accepted")


def test_frontmatter_is_fenced_above_the_prose_it_introduces() -> None:
    """The two containers a skill file is, and where one ends."""
    document = MarkdownDocument(
        frontmatter=mapping({"description": "does: a thing"}),
        blocks=[Prose(text="# Heading\n\nAuthored prose, carried as written.")],
    )

    assert document.text() == (
        "---\n"
        "description: 'does: a thing'\n"
        "---\n"
        "\n"
        "# Heading\n"
        "\n"
        "Authored prose, carried as written.\n"
    )


def test_prose_is_the_hole_the_model_keeps() -> None:
    """Authored Markdown passes through: nothing in it is derived."""
    written = "A pipe | a `backtick`, and a --- line.\n"

    assert Prose(text=written).render() == written


AUTHORED = """\
[project]
name = "thing"   # aligned the way its author likes
# why the version sits here
version = "1.4.2"
"""


def test_an_edited_manifest_keeps_what_the_change_did_not_touch(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(AUTHORED, encoding="utf-8")

    def moved(document: TOMLDocument) -> str:
        document["project"]["version"] = "1.5.0"
        return "moved"

    assert edited_manifest(manifest, moved) == "moved"
    assert manifest.read_text(encoding="utf-8") == (
        '[project]\nname = "thing"   # aligned the way its author likes\n'
        '# why the version sits here\nversion = "1.5.0"\n'
    )


def test_an_edited_manifest_is_left_alone_on_a_dry_run(tmp_path: Path) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(AUTHORED, encoding="utf-8")

    def moved(document: TOMLDocument) -> None:
        document["project"]["version"] = "1.5.0"

    edited_manifest(manifest, moved, write=False)

    assert manifest.read_text(encoding="utf-8") == AUTHORED
