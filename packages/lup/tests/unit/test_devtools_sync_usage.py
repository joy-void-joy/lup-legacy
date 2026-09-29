"""Behavior tests for reading what projects built on a package import from it.

What has to hold: only files git tracks count, so a virtual environment
inside a checkout is never read as use; a `from` import names what it takes
and a plain one names its module; and the module-by-module reading says
which projects reach each module and with which names.
"""

from pathlib import Path

import sh

from lup.devtools.sync_usage import usage_in, usage_report


def checkout(root: Path, files: dict[str, str]) -> Path:
    """A git checkout holding these files, all tracked but one environment."""
    root.mkdir(parents=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git = sh.Command("git").bake("-C", str(root), _tty_out=False)
    git("init", "-b", "main")
    (root / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    git("add", ".")
    return root


def test_only_tracked_files_count(tmp_path: Path) -> None:
    root = checkout(
        tmp_path / "adopter",
        {
            "src/app.py": "from lup.tools.mcp import lup_tool, Toolset\nimport lup.types\n",
            ".venv/lib/lup/copy.py": "from lup.gone import Everything\n",
            "src/other.py": "import json\nfrom lupine import nothing\n",
        },
    )

    usage = usage_in("adopter", root, "lup")

    assert [(use.module, use.names) for use in usage.modules] == [
        ("lup.tools.mcp", ["Toolset", "lup_tool"]),
        ("lup.types", []),
    ]


def test_each_module_says_who_reaches_it_and_with_what(tmp_path: Path) -> None:
    first = usage_in(
        "first",
        checkout(tmp_path / "first", {"a.py": "from lup.tools.mcp import lup_tool\n"}),
        "lup",
    )
    second = usage_in(
        "second",
        checkout(
            tmp_path / "second",
            {"b.py": "from lup.tools.mcp import Toolset\nfrom lup import Claude\n"},
        ),
        "lup",
    )

    report = usage_report([first, second], ["unread"], "lup")

    assert [(reach.module, reach.projects, reach.names) for reach in report.reach] == [
        ("lup", ["second"], ["Claude"]),
        ("lup.tools.mcp", ["first", "second"], ["Toolset", "lup_tool"]),
    ]
    assert report.unlocated == ["unread"]
