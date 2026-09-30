"""Typed TextMate themes provisioned in Lup-owned Codex homes."""

import plistlib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from lup.channels.models import write_atomic
from lup.harness.models import NativeName


type ThemeColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-F]{6}$")]
type ThemeFontStyle = Literal["bold", "italic", "underline"]


class TextMateStyle(BaseModel, frozen=True, populate_by_name=True):
    """One validated TextMate settings dictionary."""

    background: ThemeColor | None = None
    caret: ThemeColor | None = None
    font_style: ThemeFontStyle | None = Field(
        default=None,
        validation_alias="fontStyle",
        serialization_alias="fontStyle",
    )
    foreground: ThemeColor | None = None
    invisibles: ThemeColor | None = None
    line_highlight: ThemeColor | None = Field(
        default=None,
        validation_alias="lineHighlight",
        serialization_alias="lineHighlight",
    )
    selection: ThemeColor | None = None


class TextMateRule(BaseModel, frozen=True):
    """A TextMate scope selector and the style it applies."""

    name: str | None = None
    scope: str | None = None
    settings: TextMateStyle


class TextMateThemeDocument(BaseModel, frozen=True, populate_by_name=True):
    """The property-list document Codex's TextMate loader reads."""

    name: str
    semantic_class: str = Field(
        validation_alias="semanticClass",
        serialization_alias="semanticClass",
    )
    author: str
    settings: list[TextMateRule]


class CodexTheme(BaseModel, frozen=True):
    """A named custom theme that can materialize itself in a Codex home."""

    slug: NativeName
    document: TextMateThemeDocument

    def render(self) -> str:
        """Render a deterministic XML property list accepted by Codex."""
        payload = self.document.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False).decode(
            "utf-8"
        )

    def write(self, codex_home: Path) -> Path:
        """Materialize this theme without changing the home's selection.

        Renamed into place, since every start preparing a shared home writes
        it again while a Codex beside it may be reading it.
        """
        target = codex_home / "themes" / f"{self.slug}.tmTheme"
        write_atomic(target, self.render().encode("utf-8"))
        return target


def claude_daltonized_theme() -> CodexTheme:
    """Build Claude Code's truecolor dark colorblind palette for Codex."""
    return CodexTheme(
        slug="claude-daltonized",
        document=TextMateThemeDocument(
            name="Claude Daltonized",
            semantic_class="theme.dark.claude-daltonized",
            author="Claude Code palette port for Codex",
            settings=[
                TextMateRule(
                    settings=TextMateStyle(
                        background="#000000",
                        caret="#99CCFF",
                        foreground="#F8F8F2",
                        invisibles="#505050",
                        line_highlight="#262626",
                        selection="#264F78",
                    )
                ),
                TextMateRule(
                    name="Comments",
                    scope="comment, punctuation.definition.comment",
                    settings=TextMateStyle(
                        font_style="italic",
                        foreground="#75715E",
                    ),
                ),
                TextMateRule(
                    name="Strings",
                    scope="string, punctuation.definition.string",
                    settings=TextMateStyle(foreground="#E6DB74"),
                ),
                TextMateRule(
                    name="String interpolation and escapes",
                    scope=(
                        "constant.character.escape, punctuation.section.embedded, "
                        "variable.interpolation"
                    ),
                    settings=TextMateStyle(foreground="#F8F8F2"),
                ),
                TextMateRule(
                    name="Numbers and constants",
                    scope="constant.numeric, constant.other, support.constant",
                    settings=TextMateStyle(foreground="#BE84FF"),
                ),
                TextMateRule(
                    name="Language constants",
                    scope="constant.language",
                    settings=TextMateStyle(foreground="#BE84FF"),
                ),
                TextMateRule(
                    name="Keywords and operators",
                    scope=("keyword, keyword.control, keyword.operator"),
                    settings=TextMateStyle(foreground="#F92672"),
                ),
                TextMateRule(
                    name="Storage",
                    scope="storage, storage.type, storage.modifier",
                    settings=TextMateStyle(foreground="#66D9EF"),
                ),
                TextMateRule(
                    name="Types and classes",
                    scope=(
                        "entity.name.class, entity.name.type, "
                        "entity.other.inherited-class, support.class, support.type"
                    ),
                    settings=TextMateStyle(foreground="#A6E22E"),
                ),
                TextMateRule(
                    name="Functions and methods",
                    scope="entity.name.function, meta.function-call, support.function",
                    settings=TextMateStyle(foreground="#A6E22E"),
                ),
                TextMateRule(
                    name="Variables and parameters",
                    scope="variable, variable.parameter, variable.other",
                    settings=TextMateStyle(foreground="#FFFFFF"),
                ),
                TextMateRule(
                    name="Properties and attributes",
                    scope=(
                        "entity.other.attribute-name, support.variable.property, "
                        "variable.other.property"
                    ),
                    settings=TextMateStyle(foreground="#FFFFFF"),
                ),
                TextMateRule(
                    name="Tags and selectors",
                    scope=(
                        "entity.name.tag, entity.other.attribute-name.class.css, "
                        "entity.other.attribute-name.id.css, "
                        "entity.other.attribute-name.pseudo-class.css, "
                        "entity.other.attribute-name.pseudo-element.css"
                    ),
                    settings=TextMateStyle(foreground="#A6E22E"),
                ),
                TextMateRule(
                    name="Regular expressions",
                    scope="string.regexp, string.regexp keyword.operator",
                    settings=TextMateStyle(foreground="#E6DB74"),
                ),
                TextMateRule(
                    name="Headings and sections",
                    scope="markup.heading, entity.name.section",
                    settings=TextMateStyle(
                        font_style="bold",
                        foreground="#A6E22E",
                    ),
                ),
                TextMateRule(
                    name="Bold markup",
                    scope="markup.bold",
                    settings=TextMateStyle(
                        font_style="bold",
                        foreground="#F8F8F2",
                    ),
                ),
                TextMateRule(
                    name="Italic markup",
                    scope="markup.italic",
                    settings=TextMateStyle(
                        font_style="italic",
                        foreground="#F8F8F2",
                    ),
                ),
                TextMateRule(
                    name="Links",
                    scope="markup.underline.link, string.other.link",
                    settings=TextMateStyle(
                        font_style="underline",
                        foreground="#99CCFF",
                    ),
                ),
                TextMateRule(
                    name="Quoted markup",
                    scope="markup.quote",
                    settings=TextMateStyle(
                        font_style="italic",
                        foreground="#75715E",
                    ),
                ),
                TextMateRule(
                    name="Inline code",
                    scope="markup.raw, markup.raw.inline",
                    settings=TextMateStyle(foreground="#E6DB74"),
                ),
                TextMateRule(
                    name="Diff additions",
                    scope=("markup.inserted, diff.inserted, meta.diff.header.to-file"),
                    settings=TextMateStyle(
                        background="#001B29",
                        foreground="#51A0C8",
                    ),
                ),
                TextMateRule(
                    name="Diff deletions",
                    scope=("markup.deleted, diff.deleted, meta.diff.header.from-file"),
                    settings=TextMateStyle(
                        background="#3D0100",
                        foreground="#DC5A5A",
                    ),
                ),
                TextMateRule(
                    name="Diff changes",
                    scope="markup.changed, diff.changed",
                    settings=TextMateStyle(foreground="#FFCC00"),
                ),
                TextMateRule(
                    name="Diff metadata",
                    scope=(
                        "meta.diff, meta.diff.header, "
                        "punctuation.definition.from-file, "
                        "punctuation.definition.to-file"
                    ),
                    settings=TextMateStyle(foreground="#75715E"),
                ),
                TextMateRule(
                    name="Invalid and errors",
                    scope="invalid, invalid.illegal, invalid.deprecated",
                    settings=TextMateStyle(foreground="#DC5A5A"),
                ),
            ],
        ),
    )
