"""Writing `.env.local` keeps what a person wrote there.

The file is theirs: a key is updated where it stands, a new one goes after
the rest, and every comment and blank line stays where it was.
"""

from pathlib import Path

import pytest


class TestWriteEnvLocal:
    def test_preserves_comments_and_updates_in_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import lup.devtools.setup as setup

        env_file = tmp_path / ".env.local"
        env_file.write_text(
            "# my secrets\nAPI_KEY=old\n\n# trailing comment\nKEEP=yes\n"
        )
        monkeypatch.setattr(setup, "ENV_LOCAL", env_file)

        setup.write_env_local({"API_KEY": "new", "NEW_KEY": "added"})

        text = env_file.read_text()
        assert "# my secrets" in text
        assert "# trailing comment" in text
        assert "API_KEY=new" in text
        assert "API_KEY=old" not in text
        assert "KEEP=yes" in text
        assert "NEW_KEY=added" in text
        # Existing key updated in place, before the appended new key.
        assert text.index("API_KEY=new") < text.index("NEW_KEY=added")
