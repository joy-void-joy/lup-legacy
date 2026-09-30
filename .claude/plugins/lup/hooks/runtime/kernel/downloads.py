# lup: ignore[empty-collection]
# The dependency-free runtime deliberately uses primitive rows and stdlib scanners.
"""What a downloader invocation reads, sends, and lands on disk.

`curl` and `wget` are two spellings of reaching an origin, and what a person
weighs is the same whichever is spelled: which origin is read, whether
anything is sent to it, and where the response lands. So both are read into
those three answers, through each tool's own option grammar, and judged by
the same rows -- the fetch scopes for the origin, a question for anything
sent, and the destination policy for every file the response is written to.

An option the grammar does not list leaves the invocation unread, because a
downloader's options are exactly what move those answers: a config file, a
cookie jar, a recursive crawl, a file name the server chooses.
"""

import posixpath
import urllib.parse
from typing import TypedDict

from .programs import OptionGrammar, ReadOption, read_options


class DownloaderGrammar(OptionGrammar):
    """How one downloader's command line says what it reads, sends and writes."""

    sends: list[str]
    """Options whose value is a request body, which is what makes a call an upload."""

    method: list[str]
    """Options naming the request method."""

    document: list[str]
    """Options naming the file the response itself lands at."""

    logs: list[str]
    """Options naming another file the tool writes beside the response."""

    remote_name: list[str]
    """Options landing each response at its own URL's file name."""

    directory: list[str]
    """Options naming the directory a response saved under its own name lands in."""

    saves: bool
    """Whether a response lands at its own name when no option says where."""

    unsaved: list[str]
    """Options under which no response is saved at all."""

    formats: list[str]
    """Options whose value is a format the tool prints once the transfer ends.

    Printed to the standard output, which is what makes one ordinary: curl's
    `-w '%{http_code}'` is how a probe asks whether a service answered. The
    same format can send what it prints to a file (`%output{…}`) or be read
    from one (`@file`), and either leaves the invocation unread."""


class DownloadReading(TypedDict):
    """What one invocation reads, sends, and writes, as far as it could be read."""

    urls: list[str]
    method: str
    sends: str
    """The option carrying a request body, or empty where none is sent."""

    targets: list[str]
    """Every file the invocation writes, relative to where it runs."""

    unread: str
    """The word this reading could not read, or empty where every word was."""


def downloader(
    valued: tuple[str, ...] = (),
    flags: tuple[str, ...] = (),
    sends: tuple[str, ...] = (),
    method: tuple[str, ...] = (),
    document: tuple[str, ...] = (),
    logs: tuple[str, ...] = (),
    remote_name: tuple[str, ...] = (),
    directory: tuple[str, ...] = (),
    saves: bool = False,
    unsaved: tuple[str, ...] = (),
    formats: tuple[str, ...] = (),
) -> DownloaderGrammar:
    """One downloader grammar, whose every role-bearing option is also read.

    The roles are listed apart from the option lists so each reads as what it
    does, and joined into them here so no role can name an option the reader
    would then refuse as unlisted.
    """
    return DownloaderGrammar(
        valued=[*valued, *sends, *method, *document, *logs, *directory, *formats],
        flags=[*flags, *remote_name, *unsaved],
        families=[],
        open_attached=False,
        attached=[],
        sends=list(sends),
        method=list(method),
        document=list(document),
        logs=list(logs),
        remote_name=list(remote_name),
        directory=list(directory),
        saves=saves,
        unsaved=list(unsaved),
        formats=list(formats),
    )


DOWNLOADER_GRAMMARS: dict[str, DownloaderGrammar] = {
    "curl": downloader(
        valued=(
            "-H",
            "--header",
            "-m",
            "--max-time",
            "--connect-timeout",
            "--retry",
            "--retry-delay",
            "--retry-max-time",
            "--max-redirs",
            "-A",
            "--user-agent",
            "-e",
            "--referer",
            "-r",
            "--range",
        ),
        flags=(
            "-s",
            "--silent",
            "-S",
            "--show-error",
            "-f",
            "--fail",
            "--fail-with-body",
            "-i",
            "--include",
            "-I",
            "--head",
            "-v",
            "--verbose",
            "-L",
            "--location",
            "--compressed",
            "--no-progress-meter",
            "-#",
            "--progress-bar",
            "-g",
            "--globoff",
            "-4",
            "-6",
        ),
        sends=(
            "-d",
            "--data",
            "--data-ascii",
            "--data-binary",
            "--data-raw",
            "--data-urlencode",
            "--json",
            "-F",
            "--form",
            "--form-string",
            "-T",
            "--upload-file",
        ),
        method=("-X", "--request"),
        document=("-o", "--output"),
        logs=("-D", "--dump-header"),
        remote_name=("-O", "--remote-name", "--remote-name-all"),
        formats=("-w", "--write-out"),
    ),
    "wget": downloader(
        valued=(
            "-t",
            "--tries",
            "-T",
            "--timeout",
            "-w",
            "--wait",
            "--waitretry",
            "-U",
            "--user-agent",
            "--header",
            "--referer",
            "--limit-rate",
            "--progress",
        ),
        flags=(
            "-q",
            "--quiet",
            "-nv",
            "--no-verbose",
            "-v",
            "--verbose",
            "-c",
            "--continue",
            "-N",
            "--timestamping",
            "-nc",
            "--no-clobber",
            "--show-progress",
            "-S",
            "--server-response",
            "-4",
            "--inet4-only",
            "-6",
            "--inet6-only",
        ),
        sends=("--post-data", "--post-file", "--body-data", "--body-file"),
        method=("--method",),
        document=("-O", "--output-document"),
        logs=("-o", "--output-file", "-a", "--append-output"),
        directory=("-P", "--directory-prefix"),
        saves=True,
        unsaved=("--spider",),
    ),
}
"""Each downloader's grammar, keyed by executable name.

Each spelling is the tool's own. A `wget` response lands at its own name
unless an option says otherwise, where `curl` prints it; `-` names the
standard output rather than a file."""


def remote_file_name(url: str, fallback: str) -> str:
    """The name a response saved under its own URL lands at.

    The URL's last path segment, which is what both tools take; one ending in
    a slash names a directory, where `wget` writes ``index.html`` and `curl`
    writes nothing.
    """
    try:
        path = urllib.parse.urlsplit(url if "://" in url else f"http://{url}").path
    except ValueError:
        return fallback
    return posixpath.basename(path) or fallback


def read_download(
    words: list[str],
    grammars: dict[str, DownloaderGrammar] = DOWNLOADER_GRAMMARS,
) -> DownloadReading:
    """Read what one downloader invocation reads, sends, and writes.

    Every word that is not an option is a URL, which is how both tools read
    their operands; `--` makes every word after it one. A reading that meets
    an option it cannot read stops there and names it, rather than guessing
    where the next operand starts.
    """
    rules = grammars[posixpath.basename(words[0])]
    options: list[ReadOption] = []
    urls: list[str] = []
    unread = ""
    position = 1
    while position < len(words):
        word = words[position]
        if word == "--":
            urls.extend(words[position + 1 :])
            break
        if len(word) > 1 and word.startswith("-"):
            read = read_options(word, words[position + 1 :], rules)
            if read is None:
                unread = word
                break
            options.extend(read["options"])
            position += read["width"]
            continue
        urls.append(word)
        position += 1

    def values(role: list[str]) -> list[str]:
        return [
            option["value"] or ""
            for option in options
            if option["name"] in role and option["value"] != "-"
        ]

    names = [option["name"] for option in options]
    documents = values(rules["document"])
    directories = values(rules["directory"])
    saved = any(name in rules["remote_name"] for name in names) or (
        rules["saves"]
        and not any(name in rules["unsaved"] for name in names)
        and not any(name in rules["document"] for name in names)
    )
    derived = [
        posixpath.join(directories[-1] if directories else "", name)
        for url in urls
        for name in [remote_file_name(url, "index.html" if rules["saves"] else "")]
        if name
    ]
    methods = [
        option["value"] or "" for option in options if option["name"] in rules["method"]
    ]
    # A format that names a file -- one it writes, or one it is read from -- is
    # a write or a format nobody here read, so the invocation is unread there.
    written_format = next(
        (
            option["name"]
            for option in options
            if option["name"] in rules["formats"]
            and (
                "%output{" in (option["value"] or "")
                or (option["value"] or "").startswith("@")
            )
        ),
        "",
    )
    return DownloadReading(
        urls=urls,
        method=methods[-1] if methods else "",
        sends=next((name for name in names if name in rules["sends"]), ""),
        targets=[*documents, *values(rules["logs"]), *(derived if saved else [])],
        unread=unread or written_format,
    )


def download_targets(words: list[str]) -> list[str]:
    """Every file one downloader invocation writes, for the host to measure.

    The same reading the classifier judges, taken by the host so the files it
    reports as present and tracked are the files the verdict is about.
    """
    if not words or posixpath.basename(words[0]) not in DOWNLOADER_GRAMMARS:
        return []
    return read_download(words)["targets"]
