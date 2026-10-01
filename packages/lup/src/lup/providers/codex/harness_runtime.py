"""Codex CLI evidence, cache verification, and explicit plugin installation."""

import hashlib
import json
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import sh
import tomlkit
from semver import Version
from pydantic import BaseModel, Field

from lup.channels.models import write_atomic
from lup.execution.locks import exclusive
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.codex.app_server import native_command, native_environment
from lup.harness.contracts import CapabilityProbe
from lup.harness.models import CapabilityEvidence
from lup.types import EnvVars


@contextmanager
def codex_home_lock(home: Path) -> Iterator[None]:
    """Serialize owned configuration writes with native plugin publication.

    Held across every read, change and write of a home's ``config.toml``
    lup makes. A repository's contained sessions share one home volume and
    start concurrently -- a launch's probes, a run's workers, a second
    terminal -- each preparing it: two such writes that read the same
    document each rename a copy lacking what the other added.
    """
    with exclusive(home / ".lup-plugin-install.lock"):
        yield


def replace_codex_config(home: Path, text: str) -> None:
    """Replace a home's ``config.toml`` whole, through a file of this writer's own.

    Renamed over the old one rather than rewritten in place, so a Codex
    starting beside the write never reads half of it, and staged under a
    name no other writer shares, since the next writer would truncate a
    shared one while the first is still filling it. Staged beside the file a
    link resolves to, so a person's linked configuration stays linked. The
    mode is kept, 0600 where the file is new, since it can carry a tool
    server's secrets. The last step of a change made under
    :func:`codex_home_lock`.
    """
    settings = (home / "config.toml").resolve()
    mode = settings.stat().st_mode & 0o777 if settings.exists() else 0o600
    write_atomic(settings, text.encode("utf-8"), mode=mode)


class CodexCliEvidence(BaseModel, frozen=True):
    executable: Path
    arguments: list[str] = []
    installed: bool
    output: str = ""


class PluginCacheConfig(BaseModel, frozen=True):
    codex_home: Path = Field(default_factory=lambda: Path.home() / ".codex")
    # Required for explicit shared homes and for a stable installed-cache path.
    marketplace: str
    plugin: str = "lup"
    # None derives an immutable installation revision newer than retained caches.
    # An explicit value selects an already-versioned external fixture.
    version: str | None = None

    def cache_root(self) -> Path:
        """Every retained native revision for this plugin in the selected home."""
        return self.codex_home / "plugins" / "cache" / self.marketplace / self.plugin


class PluginCacheEvidence(BaseModel, frozen=True):
    package_version: str = ""
    source_root: Path
    installed_root: Path
    source_digest: str
    installed_digest: str | None = None
    ready: bool


class InstalledCodexPlugin(BaseModel, frozen=True, extra="ignore"):
    """The fields a native plugin listing uses to identify an enabled revision."""

    plugin_id: str = Field(alias="pluginId")
    version: str
    installed: bool
    enabled: bool

    def selects(self, selector: str, version: str) -> bool:
        """Whether the runtime selected the exact revision being launched."""
        return (
            self.plugin_id == selector
            and self.version == version
            and self.installed
            and self.enabled
        )


class CodexPluginListing(BaseModel, frozen=True, extra="ignore"):
    """Installed plugins reported by the selected native runtime and home."""

    installed: list[InstalledCodexPlugin] = []

    def selects(self, selector: str, version: str) -> bool:
        """Whether a native listing contains the requested enabled revision."""
        return any(plugin.selects(selector, version) for plugin in self.installed)


def digest_directory(root: Path, read_content: Callable[[Path], bytes]) -> str | None:
    """Hash deployable relative paths and modes with caller-normalized bytes."""
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.relative_to(root).parts
        and path.suffix not in {".pyc", ".pyo"}
    )
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(b"x" if path.stat().st_mode & 0o111 else b"-")
        digest.update(b"\0")
        digest.update(read_content(path))
        digest.update(b"\0")
    return digest.hexdigest()


def plugin_content_digest(root: Path) -> str | None:
    """Hash plugin content while treating its cachebuster version as location."""

    def content(path: Path) -> bytes:
        if path.relative_to(root) != Path(".codex-plugin/plugin.json"):
            return path.read_bytes()
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["version"] = ""
        return json.dumps(manifest, sort_keys=True).encode("utf-8")

    return digest_directory(root, content)


def plugin_manifest_version(source_root: Path) -> str:
    """The version segment codex caches this plugin under, from its manifest."""
    manifest = source_root / ".codex-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    match data:
        case {"version": str(version)}:
            return version
    raise ValueError(f"Codex plugin manifest lacks a version: {manifest}")


def cachebusted_plugin_version(source_root: Path, content_digest: str) -> str:
    """Name the initial content revision before a home retains older revisions."""
    base = Version.parse(plugin_manifest_version(source_root)).replace(build=None)
    return f"{base}+codex.{content_digest}"


def selected_plugin_version(
    source_root: Path,
    content_digest: str,
    config: PluginCacheConfig,
) -> str:
    """Select immutable content with a strictly increasing native release.

    Codex chooses the highest cached version, independent of marketplace
    registration. Semantic release ordering is shared with the native loader;
    build metadata ordering differs, so tied releases publish a higher patch.
    Only a unique highest revision can be reused. The authored package version
    remains in source; a staged manifest names this installation revision.
    """
    versions: dict[Path, Version] = {}
    unparsed: list[Path] = []
    cache = config.cache_root()
    for path in cache.iterdir() if cache.is_dir() else []:
        if not path.is_dir() or not all(
            character.isascii() and (character.isalnum() or character in ".+_-")
            for character in path.name
        ):
            continue
        try:
            versions[path] = Version.parse(path.name)
        except ValueError:
            unparsed.append(path)
    base = Version.parse(plugin_manifest_version(source_root)).replace(build=None)
    latest = max(versions.values(), default=base)
    highest = [path for path, version in versions.items() if version == latest]
    match config.version, highest:
        case str(candidate), _:
            requested = Version.parse(candidate)
            if any(
                version > requested or (version == requested and path.name != candidate)
                for path, version in versions.items()
            ):
                raise RuntimeError(
                    "Explicit Codex plugin revision is shadowed by retained cache versions. Use automatic installation revisions or select a clean Codex home; live caches are preserved."
                )
        case None, [path] if latest >= base and (
            plugin_content_digest(path) == content_digest
            or latest.build == f"codex.{content_digest}"
        ):
            candidate = path.name
        case None, _:
            release = max(base, latest.bump_patch()) if versions else base
            candidate = str(release.replace(build=f"codex.{content_digest}"))
    for path in unparsed:
        if path.name == "local" or path.name > candidate:
            raise RuntimeError(
                f"Native Codex cache revision {path} overrides semantic revisions. Select a clean Codex home; live cache paths cannot be removed or overwritten."
            )
    return candidate


def plugin_cache_evidence(
    source_root: Path, config: PluginCacheConfig
) -> PluginCacheEvidence:
    """Compare the committed package to the separately installed cached copy."""
    source = plugin_content_digest(source_root)
    if source is None:
        raise FileNotFoundError(f"Codex plugin source does not exist: {source_root}")
    version = selected_plugin_version(source_root, source, config)
    installed_root = (
        config.codex_home
        / "plugins"
        / "cache"
        / config.marketplace
        / config.plugin
        / version
    )
    installed = plugin_content_digest(installed_root)
    return PluginCacheEvidence(
        package_version=plugin_manifest_version(source_root),
        source_root=source_root,
        installed_root=installed_root,
        source_digest=source,
        installed_digest=installed,
        ready=installed == source,
    )


def revision_snapshot(source_root: Path, revision: str, parent: Path) -> Path:
    """A directory holding one installed revision's exact content, and nothing else.

    What a home holds under ``revision`` is the source with its manifest's
    version set to that name, as :func:`stage_cachebusted_marketplace`
    stages it, so the same is written here from the source on the host.
    Mounted over the plugin's whole cache in a home, it is the one revision
    Codex finds there.

    An automatic revision names the content it was installed from, and a
    name for other content is refused: the source moved between the
    preparation and this, or the preparation reported something it did not
    install. The directory is named for the revision and the content
    together, reused whole where present -- once its content is read back as
    that name's, since a directory outside the container is still one
    something on the host could have changed -- and written aside then
    renamed into place, so an interrupted write is never mounted and a
    session reading an older snapshot never has it rewritten under it.
    """
    digest = plugin_content_digest(source_root)
    if digest is None:
        raise FileNotFoundError(f"Codex plugin source does not exist: {source_root}")
    if Path(revision).name != revision or Version.parse(revision).build != (
        f"codex.{digest}"
    ):
        raise ValueError(
            f"Codex revision {revision!r} does not name the plugin content at "
            f"{source_root}. The source changed while the launch prepared it; "
            "launch again."
        )
    named = hashlib.sha256(f"{revision}\n{digest}".encode()).hexdigest()
    target = parent / named[:16]
    if target.is_dir():
        if plugin_content_digest(target / revision) != digest:
            raise ValueError(
                f"The held Codex revision at {target} no longer holds the content "
                "it was written with. Remove it on the host and launch again."
            )
        return target
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".staging-", dir=parent) as area:
        staging = Path(area) / "snapshot"
        copied = staging / revision
        shutil.copytree(
            source_root, copied, ignore=shutil.ignore_patterns("__pycache__")
        )
        manifest = copied / ".codex-plugin" / "plugin.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["version"] = revision
        manifest.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            staging.rename(target)
        except OSError:
            # Another launch wrote the same content first; this copy goes
            # with the staging area.
            if not target.is_dir():
                raise
    return target


def stage_cachebusted_marketplace(
    source_root: Path,
    cwd: Path,
    config: PluginCacheConfig,
    version: str,
) -> Path:
    """Materialize one immutable marketplace source with a cachebusted manifest."""
    relative_source = source_root.resolve().relative_to(cwd.resolve())
    destination = (
        config.codex_home / "plugins" / "sources" / config.marketplace / version
    )
    if destination.is_dir():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(
        prefix=".staging-", dir=destination.parent
    ) as temporary_text:
        temporary = Path(temporary_text)
        marketplace = temporary / ".agents" / "plugins" / "marketplace.json"
        marketplace.parent.mkdir(parents=True)
        shutil.copy2(cwd / ".agents" / "plugins" / "marketplace.json", marketplace)
        staged_source = temporary / relative_source
        staged_source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_root, staged_source)
        manifest = staged_source / ".codex-plugin" / "plugin.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["version"] = version
        manifest.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            temporary.replace(destination)
        except FileExistsError:
            return destination
    return destination


class CodexCapabilityProbe(CapabilityProbe[CodexCliEvidence]):
    """Probe exactly one named Codex CLI capability through its native command."""

    def __init__(
        self,
        capability: str = "codex-cli",
        arguments: list[str] | None = None,
        executable: Path = Path("codex"),
    ) -> None:
        self.capability = capability
        self.arguments = list(arguments or ["--version"])
        self.executable = executable

    def probe(self) -> CapabilityEvidence[CodexCliEvidence]:
        try:
            command = sh.Command(str(self.executable))
            output = str(command(*self.arguments))
        except (sh.CommandNotFound, sh.ErrorReturnCode):
            evidence = CodexCliEvidence(
                executable=self.executable,
                arguments=self.arguments,
                installed=False,
            )
            return CapabilityEvidence(
                capability=self.capability,
                supported=False,
                evidence=evidence,
                version="missing",
            )
        try:
            version = (
                output if self.arguments == ["--version"] else str(command("--version"))
            )
        except (sh.CommandNotFound, sh.ErrorReturnCode):
            version = "unknown"
        evidence = CodexCliEvidence(
            executable=self.executable,
            arguments=self.arguments,
            installed=True,
            output=output,
        )
        return CapabilityEvidence(
            capability=self.capability,
            supported=True,
            evidence=evidence,
            version=version.strip(),
        )


def codex_capability_probes(
    executable: Path = Path("codex"),
) -> list[CodexCapabilityProbe]:
    """Compose independent probes without a provider-wide support table."""
    return [
        CodexCapabilityProbe("codex-cli", ["--version"], executable),
        CodexCapabilityProbe("app-server", ["app-server", "--help"], executable),
        CodexCapabilityProbe("plugins", ["plugin", "--help"], executable),
        CodexCapabilityProbe(
            "hooks",
            ["--enable", "hooks", "features", "list"],
            executable,
        ),
    ]


class CodexPluginInstaller:
    """Install only missing/stale local packages through the official CLI."""

    def __init__(
        self,
        config: PluginCacheConfig,
        executable: Path = Path("codex"),
        environment: EnvVars | None = None,
    ) -> None:
        self.config = config
        self.executable = executable
        self.environment = dict(environment or {})

    def plugin_environment(self) -> EnvVars:
        """Environment shared by every Codex plugin lifecycle command."""
        self.config.codex_home.mkdir(parents=True, exist_ok=True)
        return {
            **native_environment(self.environment),
            **CODEX_LOGIN.environment(self.config.codex_home),
        }

    def ensure(
        self, source_root: Path, cwd: Path, force: bool = False
    ) -> PluginCacheEvidence:
        """Stage with the native CLI, then publish without pruning live revisions."""
        with codex_home_lock(self.config.codex_home):
            before = plugin_cache_evidence(source_root, self.config)
            if before.ready and self.registered(before) and not force:
                return before
            if before.installed_root.exists() and not before.ready:
                raise RuntimeError(
                    f"Installed immutable Codex revision is corrupt: {before.installed_root}. "
                    "Select a clean Codex home; a live revision cannot be overwritten."
                )
            marketplace_root = stage_cachebusted_marketplace(
                source_root, cwd, self.config, before.installed_root.name
            )
            with TemporaryDirectory(
                prefix=".plugin-install-", dir=self.config.codex_home
            ) as temporary_text:
                staged = self.config.model_copy(
                    update={
                        "codex_home": Path(temporary_text),
                        "version": before.installed_root.name,
                    }
                )
                environment = {
                    **self.plugin_environment(),
                    **CODEX_LOGIN.environment(staged.codex_home),
                }
                command = native_command(self.executable, environment)
                command(
                    "plugin",
                    "marketplace",
                    "add",
                    str(marketplace_root),
                    _cwd=str(cwd),
                    _env=environment,
                )
                command(
                    "plugin",
                    "add",
                    f"{self.config.plugin}@{self.config.marketplace}",
                    "--json",
                    _cwd=str(cwd),
                    _env=environment,
                )
                self.publish(source_root, staged)
            return plugin_cache_evidence(source_root, self.config)

    def registered(self, evidence: PluginCacheEvidence) -> bool:
        """The native home must select this revision, not merely contain its files."""
        settings = self.config.codex_home / "config.toml"
        if not settings.exists():
            return False
        document = tomlkit.parse(settings.read_text(encoding="utf-8"))
        try:
            marketplace = document["marketplaces"][self.config.marketplace]
            plugin = document["plugins"][
                f"{self.config.plugin}@{self.config.marketplace}"
            ]
            source = marketplace["source"]
            expected = (
                self.config.codex_home
                / "plugins"
                / "sources"
                / self.config.marketplace
                / evidence.installed_root.name
            )
            return (
                plugin["enabled"] is True
                and marketplace["source_type"] == "local"
                and isinstance(source, str)
                and Path(source) == expected
            )
        except (KeyError, TypeError):
            return False

    def verify(self, evidence: PluginCacheEvidence, cwd: Path) -> None:
        """Refuse a cache the native runtime does not discover as enabled."""
        environment = self.plugin_environment()
        reported = native_command(self.executable, environment)(
            "plugin",
            "list",
            "--json",
            "--marketplace",
            self.config.marketplace,
            _cwd=str(cwd),
            _env=environment,
        )
        listing = CodexPluginListing.model_validate_json(str(reported))
        selector = f"{self.config.plugin}@{self.config.marketplace}"
        if not listing.selects(selector, evidence.installed_root.name):
            raise RuntimeError(
                f"Codex does not discover {selector} at {evidence.installed_root} "
                f"as installed and enabled in {self.config.codex_home}"
            )

    def publish(self, source_root: Path, staged: PluginCacheConfig) -> None:
        """Retain every live path and merge only this native installation's state."""
        installed = plugin_cache_evidence(source_root, staged)
        if not installed.ready:
            raise RuntimeError(
                "Codex reported installation success but cached plugin digest differs"
            )
        destination = plugin_cache_evidence(source_root, self.config).installed_root
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            with TemporaryDirectory(prefix=".publish-", dir=destination.parent) as area:
                revision = Path(area) / "revision"
                shutil.copytree(installed.installed_root, revision)
                revision.replace(destination)
        settings = self.config.codex_home / "config.toml"
        document = (
            tomlkit.parse(settings.read_text(encoding="utf-8"))
            if settings.exists()
            else tomlkit.document()
        )
        native = tomlkit.parse(
            (staged.codex_home / "config.toml").read_text(encoding="utf-8")
        )
        for key, name in (
            ("marketplaces", self.config.marketplace),
            ("plugins", f"{self.config.plugin}@{self.config.marketplace}"),
        ):
            entries = document.setdefault(key, tomlkit.table(is_super_table=True))
            entries[name] = native[key][name]
        replace_codex_config(self.config.codex_home, tomlkit.dumps(document))

    def remove(self, cwd: Path) -> None:
        """Explicitly remove this plugin and its configured marketplace."""
        environment = self.plugin_environment()
        selector = f"{self.config.plugin}@{self.config.marketplace}"
        native_command(self.executable, environment)(
            "plugin",
            "remove",
            selector,
            _cwd=str(cwd),
            _env=environment,
            _ok_code=[0, 1],
        )
        native_command(self.executable, environment)(
            "plugin",
            "marketplace",
            "remove",
            self.config.marketplace,
            _cwd=str(cwd),
            _env=environment,
            _ok_code=[0, 1],
        )
