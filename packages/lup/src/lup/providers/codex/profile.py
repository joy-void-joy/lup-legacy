"""Move an account's settings into another home without moving its installed state."""

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Self

import tomlkit
from pydantic import BaseModel, Field, TypeAdapter
from tomlkit.exceptions import ParseError

from lup.providers.codex.app_server import CodexAppServer
from lup.providers.codex.home import SEED_RECORD, seeded_codex_settings
from lup.providers.codex.harness_runtime import codex_home_lock
from lup.types import JsonObject, JsonValue


class CodexConfigurationOrigin(BaseModel, extra="ignore"):
    """Native discriminator naming the owner of one configuration layer."""

    type: str


class CodexConfigurationLayer(BaseModel, extra="ignore"):
    """The native user layer after Codex resolves its path-valued settings."""

    name: CodexConfigurationOrigin
    config: JsonObject


class CodexConfigurationRead(BaseModel, extra="ignore"):
    """Only native layers are transported; defaults and project state stay local."""

    layers: list[CodexConfigurationLayer]


class CodexHookSettings(BaseModel, extra="allow"):
    """Hook events are extensible; persisted trust is owned by the destination."""

    state: JsonObject | None = None

    def personal(self) -> JsonObject:
        """Keep event declarations while omitting persisted hook trust."""
        return TypeAdapter[JsonObject](JsonObject).validate_python(
            self.model_dump(exclude={"state"})
        )


class CodexConfigurationState(BaseModel, extra="ignore"):
    """Only the nested owned state needs decoding from an open native config."""

    hooks: CodexHookSettings | None = None


class CodexAccountSettings(BaseModel, frozen=True):
    """The account's own settings, carried into a contained session's home without trust.

    A home inside the container cannot be derived from the account's the way
    a host home is, so the account's settings are read on the host and
    installed into it as it opens: every setting the person keeps, and none
    of what the account's own home installed or trusted.
    """

    source_home: Path
    source_user_home: Path = Field(default_factory=Path.home)
    settings: JsonObject = Field(repr=False)

    @classmethod
    def capture(cls, home: Path) -> Self:
        """Snapshot the account's settings without exposing parse values."""

        def read(path: Path) -> JsonObject:
            document = tomlkit.parse(path.read_text(encoding="utf-8"))
            return TypeAdapter[JsonObject](JsonObject).validate_python(
                document.unwrap()
            )

        try:
            base = home / "config.toml"
            snapshot = cls(
                source_home=home.resolve(),
                settings=read(base) if base.is_file() else {},
            )
            return snapshot.model_copy(
                update={"settings": snapshot.personal_settings()}
            )
        except (OSError, ValueError, ParseError):
            raise ValueError(
                "Cannot read the account's Codex settings; check its TOML."
            ) from None

    def personal_settings(self, enforce_policy: bool = False) -> JsonObject:
        """Leave native installation/trust local and retain required policy hooks."""
        settings = self.model_copy(deep=True).settings
        for key in ("marketplaces", "plugins", "projects"):
            settings.pop(key, None)
        state = CodexConfigurationState.model_validate(settings)
        if state.hooks is not None:
            settings["hooks"] = state.hooks.personal()
        if enforce_policy:
            features = settings.setdefault("features", {})
            if not isinstance(features, dict):
                raise ValueError("Codex features must be a settings table")
            features["hooks"] = True
        return settings

    async def normalized(
        self, staging: Path, enforce_policy: bool = False
    ) -> JsonObject:
        """Let the native parser identify paths, then retain their source-home origin."""
        settings = self.personal_settings(enforce_policy)
        # Codex reads these dependencies during configuration loading, before
        # config/read can return the path-normalized user layer.
        for key in (
            "model_instructions_file",
            "model_catalog_json",
            "experimental_compact_prompt_file",
        ):
            value = settings.get(key)
            if isinstance(value, str):
                path = Path(value)
                if path.is_relative_to("~"):
                    path = self.source_user_home / path.relative_to("~")
                settings[key] = str(
                    path if path.is_absolute() else self.source_home / path
                )
        (staging / "config.toml").write_text(tomlkit.dumps(settings), encoding="utf-8")
        server = CodexAppServer(
            Path("codex"),
            environment={
                "CODEX_HOME": str(staging),
                "HOME": str(self.source_user_home),
            },
        )
        try:
            async with asyncio.timeout(20):
                await server.start()
                reported = CodexConfigurationRead.model_validate(
                    await server.request(
                        "config/read", {"includeLayers": True, "cwd": str(staging)}
                    )
                )
        finally:
            await server.close()

        def located(value: JsonValue) -> JsonValue:
            match value:
                case dict():
                    return {key: located(item) for key, item in value.items()}
                case list():
                    return [located(item) for item in value]
                case str() if Path(value).is_relative_to(staging):
                    return str(self.source_home / Path(value).relative_to(staging))
                case _:
                    return value

        for layer in reported.layers:
            if layer.name.type == "user":
                return {key: located(value) for key, value in layer.config.items()}
        raise ValueError("Codex reported no user configuration layer")

    def install(self, home: Path, enforce_policy: bool = False) -> None:
        """Replace the home's settings with these, keeping what it installed and trusted.

        Replaced at every launch, and merged three ways against what the last
        launch installed, so a session still running in this home keeps a
        setting it changed that the person did not.
        """
        try:
            home.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=".lup-settings-", dir=home
            ) as temporary:
                settings = asyncio.run(self.normalized(Path(temporary), enforce_policy))
                destination = home / "config.toml"
                with codex_home_lock(home):
                    existing = (
                        TypeAdapter[JsonObject](JsonObject).validate_python(
                            tomlkit.parse(
                                destination.read_text(encoding="utf-8")
                            ).unwrap()
                        )
                        if destination.exists()
                        else {}
                    )
                    # The record is what the next launch's merge
                    # measures from.
                    record = home / SEED_RECORD
                    seeded = seeded_codex_settings(
                        destination.read_text(encoding="utf-8")
                        if destination.exists()
                        else None,
                        record.read_text(encoding="utf-8")
                        if record.is_file()
                        else None,
                        settings,
                    )
                    record.parent.mkdir(parents=True, exist_ok=True)
                    record.write_text(json.dumps(settings), encoding="utf-8")
                    settings = dict(seeded.settings)
                    for key in ("marketplaces", "plugins", "projects"):
                        if key in existing:
                            settings[key] = existing[key]
                    state = CodexConfigurationState.model_validate(existing)
                    if state.hooks is not None and state.hooks.state is not None:
                        selected_hooks = settings.setdefault("hooks", {})
                        if not isinstance(selected_hooks, dict):
                            raise ValueError("Codex hooks must be a settings table")
                        selected_hooks["state"] = state.hooks.state
                    if existing == settings and destination.exists():
                        return
                    prepared = Path(temporary) / "prepared.config.toml"
                    prepared.touch(mode=0o600)
                    prepared.write_text(tomlkit.dumps(settings), encoding="utf-8")
                    prepared.replace(destination)
        except Exception:
            # Native configuration errors can quote arbitrary values, including secrets.
            raise ValueError(
                "Cannot prepare the account's Codex settings; they were not logged. "
                "Check its TOML and use absolute, accessible paths for referenced files; "
                "contained launches require those paths to be mounted."
            ) from None
