---
name: analyze
description: Retain a ChatGPT or Claude conversation and answer from its files
---

# Analyze a Retained Conversation

Retain one complete ChatGPT or Claude conversation and everything its conversation API supplies, checkpoint the exact input, and only then answer from the retained files. Browser login is personal state under `.lup/`; conversation content is repository data under `tmp/`. Never answer by skimming the live page or from a paraphrase supplied beside the link.

## Input

**Arguments**: the arguments supplied with this skill invocation

The first token is one of:

- an authenticated `https://chatgpt.com/c/<id>` conversation or public `/share/<id>` snapshot;
- an authenticated `https://claude.ai/chat/<id>` conversation or public `/share/<id>` snapshot.

Everything after an optional ` -- ` is the question to answer from it.

If the URL is absent, Ask the user directly, offering concrete options, and wait for the answer: which ChatGPT or Claude conversation URL to analyze. A URL is the identity of the source; guessing one would bind the answer to nothing.

## Step 1 — Retain the source before reading it

Run:

```bash
uv run lup-devtools conversation chatgpt "<url>"
```

For a `claude.ai` URL, run the sibling provider command instead:

```bash
uv run lup-devtools conversation claude "<url>"
```

Either command takes several URLs at once and retains them under one checkpoint, reporting each URL that it could not reach and refusing at the end. Suffix a URL with `:<artifact>` — `"<url>:report.pdf"` — to retain that one file instead of every attachment the conversation declares, naming it by the filename it is retained under or by its provider file id. The manifest records the selector, so a narrowed delivery cannot be read as a complete one, and the files an earlier pass already downloaded are carried forward.

An ordinary authenticated URL uses the operator's persistent browser session through a headless browser. If that session is missing or expired, the command refuses and names the matching `uv run lup-devtools conversation setup <provider>` command. Do not run that setup command implicitly during analysis: it intentionally opens a visible login window. Ask the operator to complete the explicit setup action, then retry retention. Never ask for a cookie, token, password, or exported credential in chat, and never require the user to turn an ordinary conversation into a public share.

The download is successful only when the command reports a path under `tmp/conversations/<provider>/<id>/`. That is the default output root; pass `--output <directory>` when the retained input belongs elsewhere. The command checkpoints a destination only when it is inside the repository and Git does not ignore it. It never force-adds ignored data. It writes one closed delivery:

- `manifest.json` — source identity, counts, and every attachment digest;
- `conversation.json` — the untouched service payload;
- `conversation.md` — the conversation rendered with speaker tags;
- `attachments/<safe-name>` — ChatGPT's user-visible uploads and sandbox-linked outputs, with ` (2)`-style suffixes only when visible names collide;
- `attachments/<file-id>/<safe-name>` — the exact attachment extraction Claude placed in its conversation payload.

ChatGPT retention excludes tool-only working files and fails before replacement or commit when any user-visible file cannot be fetched. Claude's web payload carries extracted file content rather than a binary download URL; its manifest labels that representation explicitly and records provider-reported file and image counts. Do not make either representation sound stronger than it is. A missing file, an image present only in the raw payload, or an empty Claude extraction may be the premise the conversation discusses.

## Step 2 — Read the retained delivery, completely

Read `manifest.json` first. Confirm every path it names exists, and use its counts and representation fields to notice an incomplete read. Then read all of `conversation.md`, all relevant material in `conversation.json` — including ChatGPT branches when the question depends on how the discussion evolved — and every file under `attachments/`.

The Markdown is a convenient view, not a substitute for the raw payload. An unfamiliar message block is deliberately preserved as JSON rather than dropped, and the complete mapping remains in `conversation.json`. Follow attachment pointers to the bytes on disk. If this runtime cannot open an attachment's format, say which file remains unread and ask how the user wants it converted; do not silently reason without it.

Treat conversation assertions as assertions. ChatGPT or Claude saying a theorem holds, a program ran, or a source says something is not independent evidence. Attachments may supply that evidence, but only what you actually inspect counts.

## Step 3 — Answer the invoking user

If a question followed ` -- `, answer that exact question. Otherwise synthesize what the retained conversation and attachments establish, what remains unsupported, and the most useful next conclusion or action. Do not continue the downloaded conversation as though you were one of its participants unless the user explicitly asks for that.

Ground consequential statements in clickable repo-relative paths to the retained transcript, raw payload, or attachment. Distinguish clearly between:

- what the user in the downloaded conversation supplied;
- what a ChatGPT or Claude response claimed;
- what an attachment mechanically shows;
- what you infer after reading them.

End with the retained path and, when one was created, the checkpoint commit so the answer can be reproduced from the exact same input.
