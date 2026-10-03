"""Authenticated ChatGPT retention and its analysis-facing artifacts."""

from collections.abc import Sequence
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from lup.types import JsonValue
from lup.devtools.conversation import app as conversation
from lup.devtools.conversation import chatgpt
from lup.devtools.conversation import selection


def sample_payload() -> JsonValue:
    """A selected branch, one discarded branch, and colliding filenames."""
    return {
        "title": "A useful discussion",
        "mapping": {
            "root": {
                "id": "root",
                "parent": None,
                "children": ["user", "branch", "tool"],
                "message": None,
            },
            "user": {
                "id": "user",
                "parent": "root",
                "children": ["assistant"],
                "message": {
                    "id": "message-user",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["Question"]},
                    "metadata": {
                        "attachments": [
                            {
                                "id": "file-one",
                                "name": "../../notes.md",
                                "mime_type": "text/markdown",
                                "source": "local",
                            }
                        ]
                    },
                },
            },
            "assistant": {
                "id": "assistant",
                "parent": "user",
                "children": [],
                "message": {
                    "id": "message-assistant",
                    "author": {"role": "assistant"},
                    "content": {
                        "content_type": "multimodal_text",
                        "parts": [
                            "Answer\n\n[Download report](sandbox:/mnt/data/report.pdf)",
                            {"content_type": "code", "code": "result = 1"},
                        ],
                    },
                    "metadata": {},
                },
            },
            "branch": {
                "id": "branch",
                "parent": "root",
                "children": [],
                "message": {
                    "id": "message-branch",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["Discarded"]},
                    "metadata": {
                        "attachments": [
                            {
                                "id": "file-two",
                                "name": "notes.md",
                                "mime_type": "text/markdown",
                            }
                        ]
                    },
                },
            },
            "tool": {
                "id": "tool",
                "parent": "root",
                "children": [],
                "message": {
                    "id": "message-tool",
                    "author": {"role": "tool"},
                    "content": {"content_type": "execution_output", "parts": []},
                    "metadata": {
                        "attachments": [
                            {
                                "id": "internal-render",
                                "name": "/mnt/data/_renders/page-12.png",
                                "mime_type": "image/png",
                            }
                        ]
                    },
                },
            },
        },
        "current_node": "assistant",
    }


def sample_conversation() -> chatgpt.ChatGPTConversation:
    """The validated form of :func:`sample_payload`."""
    return chatgpt.conversation_from(sample_payload())


@pytest.mark.parametrize(
    ("url", "route", "identifier"),
    [
        ("https://chatgpt.com/c/conversation-1", "conversation", "conversation-1"),
        ("chatgpt.com/g/g-test/c/conversation-2", "conversation", "conversation-2"),
        ("https://chat.openai.com/share/share-1", "share", "share-1"),
    ],
)
def test_reference_recognizes_authenticated_and_shared_urls(
    url: str, route: str, identifier: str
) -> None:
    reference = chatgpt.ConversationReference(value=url)

    assert reference.route() == route
    assert reference.identifier() == identifier


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/c/conversation-1",
        "https://chatgpt.com/",
        "https://chatgpt.com/c/../../escape",
    ],
)
def test_reference_refuses_urls_that_cannot_name_a_safe_delivery(url: str) -> None:
    with pytest.raises(chatgpt.ChatGPTDownloadError):
        chatgpt.ConversationReference(value=url).identifier()


@pytest.mark.asyncio
async def test_private_payload_uses_bearer_from_browser_session() -> None:
    reference = chatgpt.ConversationReference(
        value="https://chatgpt.com/c/conversation-1"
    )
    session_response = AsyncMock(status=200, url="https://chatgpt.com/api/auth/session")
    session_response.json.return_value = {"accessToken": "session-token"}
    payload_response = AsyncMock(
        status=200,
        url="https://chatgpt.com/backend-api/conversation/conversation-1",
    )
    payload_response.json.return_value = sample_payload()
    request = AsyncMock()
    request.get.side_effect = [session_response, payload_response]

    session = await chatgpt.fetch_session(request, reference)
    payload = await chatgpt.fetch_payload(request, reference, session)

    assert payload == sample_payload()
    assert "session-token" not in repr(session)
    assert request.get.await_args_list[1].kwargs["headers"] == {
        "Referer": "https://chatgpt.com/c/conversation-1",
        "Accept": "application/json",
        "Authorization": "Bearer session-token",
    }


@pytest.mark.asyncio
async def test_public_snapshot_does_not_request_an_authenticated_session() -> None:
    request = AsyncMock()
    reference = chatgpt.ConversationReference(value="https://chatgpt.com/share/share-1")

    session = await chatgpt.fetch_session(request, reference)

    assert session is None
    request.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_session_without_access_token_requires_login() -> None:
    response = AsyncMock(status=200, url="https://chatgpt.com/api/auth/session")
    response.json.return_value = {"user": {"name": "Ada"}}
    request = AsyncMock()
    request.get.return_value = response
    reference = chatgpt.ConversationReference(
        value="https://chatgpt.com/c/conversation-1"
    )

    with pytest.raises(chatgpt.ChatGPTAuthenticationRequired, match="access token"):
        await chatgpt.fetch_session(request, reference)


@pytest.mark.asyncio
async def test_authenticated_404_reports_an_unavailable_conversation() -> None:
    response = AsyncMock(
        status=404,
        url="https://chatgpt.com/backend-api/conversation/conversation-1",
    )
    request = AsyncMock()
    request.get.return_value = response
    reference = chatgpt.ConversationReference(
        value="https://chatgpt.com/c/conversation-1"
    )
    session = chatgpt.ChatGPTSession.model_validate({"accessToken": "session-token"})

    with pytest.raises(chatgpt.ChatGPTDownloadError, match="404") as captured:
        await chatgpt.fetch_payload(request, reference, session)

    assert type(captured.value) is chatgpt.ChatGPTDownloadError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("download_url", "expected_headers"),
    [
        (
            "https://chatgpt.com/backend-api/files/file-one/download",
            {
                "Referer": "https://chatgpt.com/c/one",
                "Authorization": "Bearer session-token",
            },
        ),
        ("https://files.oaiusercontent.com/file.bin", {}),
    ],
)
async def test_attachment_bearer_is_scoped_to_chatgpt(
    download_url: str, expected_headers: dict[str, str]
) -> None:
    response = AsyncMock(
        status=200,
        headers={"content-type": "application/octet-stream"},
    )
    response.body.return_value = b"retained file"
    request = AsyncMock()
    request.get.return_value = response
    attachment = chatgpt.ChatGPTAttachment.model_validate(
        {"id": "file-one", "name": "notes.md", "download_url": download_url}
    )
    session = chatgpt.ChatGPTSession.model_validate({"accessToken": "session-token"})
    reference = chatgpt.ConversationReference(value="https://chatgpt.com/c/one")

    fetched = await chatgpt.fetch_attachment(request, reference, attachment, session)

    assert fetched.body == b"retained file"
    request.get.assert_awaited_once_with(
        download_url,
        headers=expected_headers,
        fail_on_status_code=False,
    )


@pytest.mark.asyncio
async def test_sandbox_link_uses_the_interpreter_download_endpoint() -> None:
    envelope = AsyncMock(
        status=200,
        url="https://chatgpt.com/backend-api/conversation/conversation-1/interpreter/download",
        headers={"content-type": "application/json"},
    )
    envelope.json.return_value = {
        "download_url": "https://files.oaiusercontent.com/report.pdf"
    }
    download = AsyncMock(
        status=200,
        headers={"content-type": "application/pdf"},
    )
    download.body.return_value = b"generated report"
    request = AsyncMock()
    request.get.side_effect = [envelope, download]
    reference = chatgpt.ConversationReference(
        value="https://chatgpt.com/c/conversation-1"
    )
    session = chatgpt.ChatGPTSession.model_validate({"accessToken": "session-token"})
    attachment = sample_conversation().attachments()[1]

    fetched = await chatgpt.fetch_attachment(request, reference, attachment, session)

    assert fetched.body == b"generated report"
    first = request.get.await_args_list[0]
    assert first.args == (
        "https://chatgpt.com/backend-api/conversation/conversation-1/interpreter/download",
    )
    assert first.kwargs["params"] == {
        "message_id": "message-assistant",
        "sandbox_path": "/mnt/data/report.pdf",
    }


def test_selected_branch_is_rendered_without_losing_unknown_content() -> None:
    reference = chatgpt.ConversationReference(
        value="https://chatgpt.com/c/conversation-1"
    )

    rendered = chatgpt.render_conversation(reference, sample_conversation())

    assert "<user>\nQuestion" in rendered
    assert "<assistant>\nAnswer" in rendered
    assert "result = 1" in rendered
    assert "Discarded" not in rendered
    assert "attachments/notes.md" in rendered
    assert "attachments/report.pdf" in rendered
    assert "complete branched payload" in rendered


def test_visible_files_are_flat_named_and_collision_safe() -> None:
    attachments = sample_conversation().attachments()

    assert [item.stored_name() for item in attachments] == [
        "notes.md",
        "report.pdf",
        "notes.md",
    ]
    assert [
        path.as_posix() for path in chatgpt.attachment_paths(attachments).values()
    ] == [
        "attachments/notes.md",
        "attachments/report.pdf",
        "attachments/notes (2).md",
    ]


def test_visible_file_paths_cannot_escape_the_attachments_directory() -> None:
    attachment = chatgpt.ChatGPTAttachment.model_validate(
        {"id": "file-one", "name": ".."}
    )

    with pytest.raises(chatgpt.ChatGPTDownloadError, match="parent"):
        attachment.relative_path()


def test_sandbox_file_paths_cannot_traverse_out_of_mnt_data() -> None:
    attachment = chatgpt.ChatGPTAttachment(
        representation="sandbox",
        message_id="message-one",
        sandbox_path="/mnt/data/../secret.txt",
    )

    with pytest.raises(chatgpt.ChatGPTDownloadError, match="malformed"):
        attachment.identity()


def fetched_attachments() -> list[chatgpt.FetchedAttachment]:
    """Downloaded bytes for both declarations in the sample payload."""
    return [
        chatgpt.FetchedAttachment(declaration=declaration, body=body)
        for declaration, body in zip(
            sample_conversation().attachments(),
            (b"first attachment", b"generated report", b"second attachment"),
            strict=True,
        )
    ]


def test_delivery_keeps_raw_mapping_rendering_files_and_digests(tmp_path: Path) -> None:
    reference = chatgpt.ConversationReference(
        value="https://chatgpt.com/c/conversation-1"
    )
    destination = chatgpt.write_delivery(
        tmp_path,
        reference,
        sample_payload(),
        sample_conversation(),
        fetched_attachments(),
    )

    assert (destination / "conversation.json").is_file()
    assert "Question" in (destination / "conversation.md").read_text()
    assert (
        destination / "attachments" / "notes.md"
    ).read_bytes() == b"first attachment"
    assert (
        destination / "attachments" / "report.pdf"
    ).read_bytes() == b"generated report"
    assert (
        destination / "attachments" / "notes (2).md"
    ).read_bytes() == b"second attachment"
    manifest = chatgpt.DownloadManifest.model_validate_json(
        (destination / "manifest.json").read_text()
    )
    assert manifest.mapping_node_count == 5
    assert manifest.active_message_count == 2
    assert [record.representation for record in manifest.attachments] == [
        "metadata",
        "sandbox",
        "metadata",
    ]
    assert [record.size_bytes for record in manifest.attachments] == [
        16,
        16,
        17,
    ]
    assert all(len(record.sha256) == 64 for record in manifest.attachments)


def test_delivery_replaces_a_prior_complete_snapshot(tmp_path: Path) -> None:
    reference = chatgpt.ConversationReference(
        value="https://chatgpt.com/c/conversation-1"
    )
    destination = chatgpt.write_delivery(
        tmp_path,
        reference,
        sample_payload(),
        sample_conversation(),
        fetched_attachments(),
    )
    (destination / "stale.txt").write_text("old")

    rewritten = chatgpt.write_delivery(
        tmp_path,
        reference,
        sample_payload(),
        sample_conversation(),
        fetched_attachments(),
    )

    assert rewritten == destination
    assert not (rewritten / "stale.txt").exists()


def test_delivery_can_be_placed_under_a_caller_selected_output(tmp_path: Path) -> None:
    reference = chatgpt.ConversationReference(
        value="https://chatgpt.com/c/conversation-1"
    )

    destination = chatgpt.write_delivery(
        tmp_path,
        reference,
        sample_payload(),
        sample_conversation(),
        fetched_attachments(),
        tmp_path / "retained",
    )

    assert destination == tmp_path / "retained" / "chatgpt" / "conversation-1"


def test_delivery_refuses_to_look_complete_when_a_file_is_missing(
    tmp_path: Path,
) -> None:
    reference = chatgpt.ConversationReference(
        value="https://chatgpt.com/c/conversation-1"
    )

    with pytest.raises(chatgpt.ChatGPTDownloadError):
        chatgpt.write_delivery(
            tmp_path,
            reference,
            sample_payload(),
            sample_conversation(),
            fetched_attachments()[:1],
        )


def retaining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refused: str = ""
) -> list[list[Path]]:
    """Wire the CLI onto a retention that fails only the refused selector."""
    checkpoints: list[list[Path]] = []

    async def retain(
        requests: Sequence[selection.RetentionRequest],
        root: Path,
        state_directory: Path,
        output: Path,
    ) -> list[selection.RetentionAttempt]:
        attempts: list[selection.RetentionAttempt] = []
        for request in requests:
            if refused and request.artifact == refused:
                attempts.append(
                    selection.RetentionAttempt(
                        request=request, error="no such artifact"
                    )
                )
                continue
            reference = chatgpt.ConversationReference(value=request.url)
            destination = output / "chatgpt" / reference.identifier()
            destination.mkdir(parents=True, exist_ok=True)
            attempts.append(
                selection.RetentionAttempt(request=request, destination=destination)
            )
        return attempts

    monkeypatch.setattr(conversation, "retain_chatgpt", retain)
    monkeypatch.setattr(conversation, "project_root", lambda: tmp_path)
    monkeypatch.setattr(
        conversation,
        "checkpoint_delivery",
        lambda _root, destinations, *, provider: checkpoints.append(destinations),
    )
    return checkpoints


def test_download_command_checkpoints_only_after_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoints = retaining(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        conversation.app,
        [
            "chatgpt",
            "https://chatgpt.com/c/conversation-1",
            "--output",
            str(tmp_path / "retained"),
        ],
    )

    assert result.exit_code == 0
    assert checkpoints == [[tmp_path / "retained" / "chatgpt" / "conversation-1"]]
    assert "retained/chatgpt/conversation-1" in result.stdout


def test_several_urls_are_retained_under_one_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoints = retaining(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        conversation.app,
        [
            "chatgpt",
            "https://chatgpt.com/c/conversation-1",
            "https://chatgpt.com/c/conversation-2",
            "--output",
            str(tmp_path / "retained"),
        ],
    )

    assert result.exit_code == 0
    assert checkpoints == [
        [
            tmp_path / "retained" / "chatgpt" / "conversation-1",
            tmp_path / "retained" / "chatgpt" / "conversation-2",
        ]
    ]


def test_one_refused_url_keeps_the_rest_and_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoints = retaining(tmp_path, monkeypatch, refused="absent.zip")

    result = CliRunner().invoke(
        conversation.app,
        [
            "chatgpt",
            "https://chatgpt.com/c/conversation-1:absent.zip",
            "https://chatgpt.com/c/conversation-2",
            "--output",
            str(tmp_path / "retained"),
        ],
    )

    assert result.exit_code == 1
    assert checkpoints == [[tmp_path / "retained" / "chatgpt" / "conversation-2"]]
    assert "conversation-1:absent.zip" in result.output


@pytest.mark.parametrize(
    ("supplied", "url", "artifact"),
    [
        ("https://chatgpt.com/c/one", "https://chatgpt.com/c/one", ""),
        ("chatgpt.com/c/one", "chatgpt.com/c/one", ""),
        ("https://chatgpt.com:443/c/one", "https://chatgpt.com:443/c/one", ""),
        (
            "https://chatgpt.com/c/one:bundle.zip",
            "https://chatgpt.com/c/one",
            "bundle.zip",
        ),
        ("chatgpt.com/c/one:report (2).md", "chatgpt.com/c/one", "report (2).md"),
    ],
)
def test_a_trailing_selector_is_read_off_the_url(
    supplied: str, url: str, artifact: str
) -> None:
    request = selection.RetentionRequest.parse(supplied)

    assert request.url == url
    assert request.artifact == artifact


def test_a_selector_retains_only_the_named_artifact(tmp_path: Path) -> None:
    reference = chatgpt.ConversationReference(
        value="https://chatgpt.com/c/conversation-1"
    )
    report = next(
        item
        for item in fetched_attachments()
        if item.declaration.stored_name() == "report.pdf"
    )

    destination = chatgpt.write_delivery(
        tmp_path,
        reference,
        sample_payload(),
        sample_conversation(),
        [report],
        tmp_path / "retained",
        "report.pdf",
    )

    assert (destination / "attachments" / "report.pdf").is_file()
    assert not (destination / "attachments" / "notes.md").exists()
    transcript = (destination / "conversation.md").read_text()
    assert "attachments/report.pdf" in transcript
    assert "notes.md → not retained" in transcript
    manifest = chatgpt.DownloadManifest.model_validate_json(
        (destination / "manifest.json").read_text()
    )
    assert manifest.selected_artifact == "report.pdf"
    assert manifest.declared_attachment_count == 3
    assert [record.name for record in manifest.attachments] == ["report.pdf"]


def test_a_second_selection_keeps_what_the_first_already_paid_for(
    tmp_path: Path,
) -> None:
    reference = chatgpt.ConversationReference(
        value="https://chatgpt.com/c/conversation-1"
    )
    fetched = fetched_attachments()
    report = next(
        item for item in fetched if item.declaration.stored_name() == "report.pdf"
    )
    chatgpt.write_delivery(
        tmp_path,
        reference,
        sample_payload(),
        sample_conversation(),
        [report],
        tmp_path / "retained",
        "report.pdf",
    )

    carried = chatgpt.carried_attachments(
        chatgpt.delivery_directory(tmp_path, reference, tmp_path / "retained"),
        sample_conversation().attachments(),
    )

    assert [item.declaration.stored_name() for item in carried] == ["report.pdf"]
    assert [item.body for item in carried] == [report.body]


def test_a_rotted_prior_attachment_is_not_carried_forward(tmp_path: Path) -> None:
    reference = chatgpt.ConversationReference(
        value="https://chatgpt.com/c/conversation-1"
    )
    destination = chatgpt.write_delivery(
        tmp_path,
        reference,
        sample_payload(),
        sample_conversation(),
        fetched_attachments(),
        tmp_path / "retained",
    )
    (destination / "attachments" / "report.pdf").write_bytes(b"tampered")

    carried = chatgpt.carried_attachments(
        destination, sample_conversation().attachments()
    )

    assert "report.pdf" not in [item.declaration.stored_name() for item in carried]


def test_a_selector_matches_the_collision_suffixed_filename() -> None:
    attachments = sample_conversation().attachments()

    assert (
        chatgpt.selected_attachment(attachments, "notes (2).md").identifier
        == "file-two"
    )


def test_an_unknown_selector_names_what_the_conversation_offers() -> None:
    with pytest.raises(chatgpt.ChatGPTDownloadError, match="report.pdf"):
        chatgpt.selected_attachment(sample_conversation().attachments(), "absent.zip")


def test_attachment_redirects_stay_on_openai_controlled_origins() -> None:
    assert (
        chatgpt.allowed_download_url("https://files.oaiusercontent.com/file.bin")
        == "https://files.oaiusercontent.com/file.bin"
    )
    with pytest.raises(chatgpt.ChatGPTDownloadError):
        chatgpt.allowed_download_url("https://example.com/file.bin")
