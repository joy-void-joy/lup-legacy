"""Authenticated Claude retention adapted from forumboard's conversation path."""

from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lup.types import JsonValue
from lup.devtools import roster
from lup.devtools.conversation import app as conversation
from lup.devtools.conversation import claude as claude_conversation
from lup.devtools.conversation import selection


def sample_payload() -> JsonValue:
    """A conversation carrying tool blocks and colliding attachment names."""
    return {
        "uuid": "conversation-1",
        "name": "Research discussion",
        "chat_messages": [
            {
                "uuid": "message-user",
                "sender": "human",
                "content": "Please inspect these.",
                "file_count": 2,
                "attachments": [
                    {
                        "id": "file-one",
                        "file_name": "../../notes.md",
                        "file_type": "text/markdown",
                        "file_size": 100,
                        "extracted_content": "first attachment",
                    },
                    {
                        "id": "file-two",
                        "file_name": "notes.md",
                        "file_type": "text/markdown",
                        "file_size": 200,
                        "extracted_content": "second attachment",
                    },
                ],
            },
            {
                "uuid": "message-assistant",
                "sender": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "search",
                        "integration_name": "drive",
                        "input": {"query": "theorem"},
                    },
                    {
                        "type": "tool_result",
                        "content": [{"type": "text", "text": "result body"}],
                        "display_content": {"count": 1},
                    },
                    {
                        "type": "knowledge",
                        "title": "A source",
                        "url": "https://example.test/source",
                        "metadata": {"site_name": "Example"},
                    },
                    {
                        "type": "local_resource",
                        "name": "proof.lean",
                        "file_path": "/project/proof.lean",
                    },
                    {"type": "json_block", "json_block": {"proved": True}},
                ],
                "image_count": 1,
            },
        ],
    }


def sample_conversation() -> claude_conversation.ConversationPayload:
    """The validated form of :func:`sample_payload`."""
    return claude_conversation.conversation_from(sample_payload())


@pytest.mark.parametrize(
    ("url", "route", "identifier"),
    [
        ("https://claude.ai/chat/conversation-1", "conversation", "conversation-1"),
        ("claude.ai/share/share-1", "share", "share-1"),
    ],
)
def test_reference_recognizes_authenticated_and_shared_urls(
    url: str, route: str, identifier: str
) -> None:
    reference = claude_conversation.ConversationReference(value=url)

    assert reference.route() == route
    assert reference.identifier() == identifier


def test_renderer_keeps_tool_results_sources_resources_and_gap_markers() -> None:
    reference = claude_conversation.ConversationReference(
        value="https://claude.ai/chat/conversation-1"
    )

    conversation_payload = sample_conversation()
    rendered = claude_conversation.render_conversation(
        reference, conversation_payload, conversation_payload.retained_paths()
    )

    assert "<user>\nPlease inspect these." in rendered
    assert "[tool call: search via drive]" in rendered
    assert '"query": "theorem"' in rendered
    assert "result body" in rendered
    assert '"count": 1' in rendered
    assert "https://example.test/source — Example" in rendered
    assert "[file: proof.lean — /project/proof.lean]" in rendered
    assert '"proved": true' in rendered
    assert "Claude reports 1 image(s)" in rendered
    assert "attachments/file-one/notes.md" in rendered


def test_attachments_with_the_same_name_are_collision_safe() -> None:
    attachments = sample_conversation().attachments()

    assert [item.relative_path().as_posix() for item in attachments] == [
        "attachments/file-one/notes.md",
        "attachments/file-two/notes.md",
    ]


def test_delivery_keeps_raw_payload_attachment_extracts_and_digests(
    tmp_path: Path,
) -> None:
    reference = claude_conversation.ConversationReference(
        value="https://claude.ai/chat/conversation-1"
    )
    destination = claude_conversation.write_delivery(
        tmp_path, reference, sample_payload(), sample_conversation()
    )

    assert destination == (
        tmp_path / "tmp" / "conversations" / "claude" / "conversation-1"
    )
    assert (destination / "conversation.json").is_file()
    assert "Please inspect" in (destination / "conversation.md").read_text()
    assert (
        destination / "attachments" / "file-one" / "notes.md"
    ).read_text() == "first attachment"
    manifest = claude_conversation.DownloadManifest.model_validate_json(
        (destination / "manifest.json").read_text()
    )
    assert manifest.provider == "claude"
    assert manifest.reported_file_count == 2
    assert manifest.reported_image_count == 1
    assert [record.representation for record in manifest.attachments] == [
        "claude_api_extracted_text",
        "claude_api_extracted_text",
    ]
    assert all(len(record.sha256) == 64 for record in manifest.attachments)


def test_delivery_can_be_placed_under_a_caller_selected_output(tmp_path: Path) -> None:
    reference = claude_conversation.ConversationReference(
        value="https://claude.ai/chat/conversation-1"
    )

    destination = claude_conversation.write_delivery(
        tmp_path,
        reference,
        sample_payload(),
        sample_conversation(),
        tmp_path / "retained",
    )

    assert destination == tmp_path / "retained" / "claude" / "conversation-1"


def test_provider_commands_are_nested_under_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkpoints: list[list[Path]] = []

    async def retain(
        requests: Sequence[selection.RetentionRequest],
        root: Path,
        state_directory: Path,
        output: Path,
    ) -> list[selection.RetentionAttempt]:
        attempts: list[selection.RetentionAttempt] = []
        for request in requests:
            reference = claude_conversation.ConversationReference(value=request.url)
            destination = output / "claude" / reference.identifier()
            destination.mkdir(parents=True)
            attempts.append(
                selection.RetentionAttempt(request=request, destination=destination)
            )
        return attempts

    monkeypatch.setattr(conversation, "retain_claude", retain)
    monkeypatch.setattr(conversation, "project_root", lambda: tmp_path)
    monkeypatch.setattr(
        conversation,
        "checkpoint_delivery",
        lambda _root, destinations, *, provider: checkpoints.append(destinations),
    )

    result = CliRunner().invoke(
        conversation.app,
        [
            "claude",
            "https://claude.ai/chat/conversation-1",
            "--output",
            str(tmp_path / "retained"),
        ],
    )

    assert result.exit_code == 0
    assert checkpoints == [[tmp_path / "retained" / "claude" / "conversation-1"]]
    assert "retained/claude/conversation-1" in result.stdout
    assert "conversation" in [spec.name for spec in roster.LIBRARY_SPECS]
    assert "chatgpt" not in [spec.name for spec in roster.LIBRARY_SPECS]


def test_a_selector_retains_only_the_named_extraction(tmp_path: Path) -> None:
    reference = claude_conversation.ConversationReference(
        value="https://claude.ai/chat/conversation-1"
    )

    destination = claude_conversation.write_delivery(
        tmp_path,
        reference,
        sample_payload(),
        sample_conversation(),
        tmp_path / "retained",
        "file-two",
    )

    assert (destination / "attachments" / "file-two" / "notes.md").is_file()
    assert not (destination / "attachments" / "file-one").exists()
    transcript = (destination / "conversation.md").read_text()
    assert "attachments/file-two/notes.md" in transcript
    assert "notes.md → not retained" in transcript
    manifest = claude_conversation.DownloadManifest.model_validate_json(
        (destination / "manifest.json").read_text()
    )
    assert manifest.selected_artifact == "file-two"
    assert manifest.declared_attachment_count == 2
    assert [record.id for record in manifest.attachments] == ["file-two"]


def test_an_unknown_selector_names_what_the_conversation_offers() -> None:
    with pytest.raises(claude_conversation.ClaudeDownloadError, match="notes.md"):
        claude_conversation.selected_attachment(
            sample_conversation().attachments(), "absent.md"
        )


def test_an_ambiguous_selector_asks_for_the_file_id() -> None:
    with pytest.raises(claude_conversation.ClaudeDownloadError, match="file id"):
        claude_conversation.selected_attachment(
            sample_conversation().attachments(), "notes.md"
        )
