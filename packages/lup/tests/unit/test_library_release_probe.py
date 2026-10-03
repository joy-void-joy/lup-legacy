"""What `dev library release` reads from the index, candidates told apart.

The index names one version as its latest, and where every version it holds
is a pre-release that one is a release candidate. Reported as "released", it
would be the version a new project is told to pin — a candidate taken by
accident, which is the one thing publishing it as a pre-release is for
preventing. A candidate is reported as what it is, with the opt-in beside it.
"""

from lup.devtools.dev.library import ReleaseIndexDocument


def document(
    latest: str, releases: dict[str, bool] | None = None
) -> ReleaseIndexDocument:
    """An index's answer: its latest version, and each version's yanked state."""
    return ReleaseIndexDocument.model_validate(
        {
            "info": {"version": latest},
            "releases": {
                version: [{"yanked": yanked}]
                for version, yanked in (releases or {}).items()
            },
        }
    )


def test_a_candidate_alone_is_no_release() -> None:
    probe = document("0.5.0rc1", {"0.5.0rc1": False}).probed()
    said = probe.describe()

    assert (probe.version, probe.candidate) == ("", "0.5.0rc1")
    assert said[0] == "no release published yet"
    assert "dev library git --branch <branch>" in said
    assert any("--version 0.5.0rc1" in line and "candidate" in line for line in said)


def test_a_release_is_offered_and_a_newer_candidate_named_beside_it() -> None:
    probe = document("0.4.0", {"0.4.0": False, "0.5.0rc1": False}).probed()
    said = probe.describe()

    assert (probe.version, probe.candidate) == ("0.4.0", "0.5.0rc1")
    assert said[:2] == ["released: 0.4.0", "dev library use published --version 0.4.0"]
    assert any("--version 0.5.0rc1" in line for line in said)


def test_a_candidate_its_release_superseded_is_not_offered() -> None:
    probe = document("0.5.0", {"0.5.0rc1": False, "0.5.0": False}).probed()

    assert (probe.version, probe.candidate) == ("0.5.0", "")
    assert not any("0.5.0rc1" in line for line in probe.describe())


def test_a_yanked_version_is_neither() -> None:
    probe = document("0.4.0", {"0.4.0": False, "0.5.0rc1": True}).probed()

    assert probe.candidate == ""


def test_an_index_listing_no_versions_is_read_from_its_latest() -> None:
    """A private index answering only `info` still tells a candidate apart."""
    assert document("0.5.0rc2").probed().candidate == "0.5.0rc2"
    assert document("0.4.0").probed().version == "0.4.0"
