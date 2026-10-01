"""Which of a person's Claude Code settings follow them into a container, and back.

A contained session runs in its repository's own config volume, so nothing
the person set in their own home reaches it unless a launch carries it, and
nothing the session changes reaches them unless the launch brings it back.
In, everything the account holds (:mod:`lup.providers.claude.home_seed`).
Back, only what is the person's to keep, decided here per key over every key
the installed CLI takes (:mod:`lup.providers.claude.settings_keys`, compiled
from what the CLI's program declares). Every key is one of three:

``returns``
    A display, input or behaviour preference — the theme, the editor mode,
    verbosity, timestamps. A session's change goes back: a portable key to the
    person's lup config, where it reaches both runtimes, anything else to the
    account's own settings, or its configuration document for the few
    preferences only that document holds.
``withheld``
    Anything that runs code, widens or narrows what may run, or belongs to an
    organization or an installation — hooks, permissions, ``env``, helpers
    and status-line commands, MCP servers, plugins and marketplaces, sandbox,
    login forcing, memory — and the document's own housekeeping. Never
    carried out of a container, whatever a session wrote.
``session``
    The model and how hard it thinks. A session's ``/model`` and ``/effort``
    stay in that session; the next one starts from what the lup config
    declares.

A key this module does not name is withheld: a key a newer CLI added is one
nobody has decided about, and the gate says so until somebody does.

Measured on Claude Code 2.1.282, in a pseudo-terminal against a private
config home: ``/theme`` and ``/config``'s editor mode are written to
``settings.json`` (``{"theme": "light"}``, ``{"editorMode": "vim"}``) and the
configuration document is left without either; ``/model`` writes ``model``
and ``/effort`` writes ``modelSettings.<model>.effortLevel`` to the same file.
With a theme in both, the picker marks the settings file's; with it in the
document alone, the document's — the CLI reads a settings value first and
falls back to the document's copy, which its own source calls legacy. A theme
seeded into ``settings.json`` is left in place at startup.
"""

from typing import get_args

from lup.providers.claude.settings_keys import ClaudeDocumentKey, ClaudeSettingKey
from lup.providers.settings_schema import SettingFlow


# lup: ignore[constant-declaration] — a decision per key of the CLI's own
# schema, made once here and checked against every release by the gate
SETTINGS_FLOWS: dict[ClaudeSettingKey, SettingFlow] = {
    # What the prompt and the transcript look like, and how input is taken.
    "theme": "returns",
    "editorMode": "returns",
    "keybindingFlavor": "returns",
    "vimInsertModeRemaps": "returns",
    "verbose": "returns",
    "outputStyle": "returns",
    "viewMode": "returns",
    "language": "returns",
    "tui": "returns",
    "voice": "returns",
    "prefersReducedMotion": "returns",
    "timeFormat": "returns",
    "timeZone": "returns",
    "showTurnDuration": "returns",
    "showMessageTimestamps": "returns",
    "showThinkingSummaries": "returns",
    "spinnerTipsEnabled": "returns",
    "spinnerVerbs": "returns",
    "syntaxHighlightingDisabled": "returns",
    "maxProseWidth": "returns",
    "terminalTitleFromRename": "returns",
    "terminalProgressBarEnabled": "returns",
    "autoScrollEnabled": "returns",
    "wheelScrollAccelerationEnabled": "returns",
    "todoFeatureEnabled": "returns",
    "prUrlTemplate": "returns",
    "footerLinksRegexes": "returns",
    "bashEditDiffEnabled": "returns",
    "respectGitignore": "returns",
    "emojiCompletionEnabled": "returns",
    "promptSuggestionEnabled": "returns",
    "awaySummaryEnabled": "returns",
    "showClearContextOnPlanAccept": "returns",
    "feedbackSurveyRate": "returns",
    "feedbackDrafts": "returns",
    "modelPicker": "returns",
    # How a session behaves, none of it running anything or reaching further.
    "autoCompactEnabled": "returns",
    "autoCompactWindow": "returns",
    "precomputeCompactionEnabled": "returns",
    "autoContinueAtUsageLimit": "returns",
    "switchModelsOnFlag": "returns",
    "fastModePerSessionOptIn": "returns",
    "fileCheckpointingEnabled": "returns",
    "respondToBashCommands": "returns",
    "bashOutputMaxChars": "returns",
    "taskOutputMaxChars": "returns",
    "skillListingMaxDescChars": "returns",
    "skillListingBudgetFraction": "returns",
    "promptCacheTtl": "returns",
    "subagentPromptCacheTtl": "returns",
    "askUserQuestionTimeout": "returns",
    "dialogExpiry": "returns",
    "workflowSizeGuideline": "returns",
    "workflowKeywordTriggerEnabled": "returns",
    "attribution": "returns",
    "includeCoAuthoredBy": "returns",
    "includeGitInstructions": "returns",
    "doneMeansMerged": "returns",
    "totalTokensReminder": "returns",
    "totalTokensReminderBudget": "returns",
    "totalTokensReminderAfterUserTurn": "returns",
    "breakReminder": "returns",
    "quietHours": "returns",
    "cleanupPeriodDays": "returns",
    "desktopSessionCleanupPeriodDays": "returns",
    "preferredNotifChannel": "returns",
    "inputNeededNotifEnabled": "returns",
    "agentPushNotifEnabled": "returns",
    # The model and how hard it thinks: the session's own, never the person's.
    "model": "session",
    "fallbackModel": "session",
    "advisorModel": "session",
    "effortLevel": "session",
    "modelSettings": "session",
    "ultracode": "session",
    "fastMode": "session",
    "alwaysThinkingEnabled": "session",
    # Programs a setting runs, and the shell they run in.
    "apiKeyHelper": "withheld",
    "proxyAuthHelper": "withheld",
    "awsCredentialExport": "withheld",
    "awsAuthRefresh": "withheld",
    "gcpAuthRefresh": "withheld",
    "otelHeadersHelper": "withheld",
    "processWrapper": "withheld",
    "policyHelper": "withheld",
    "policyHelpers": "withheld",
    "fileSuggestion": "withheld",
    "statusLine": "withheld",
    "subagentStatusLine": "withheld",
    "spellcheck": "withheld",
    "defaultShell": "withheld",
    "teammateMode": "withheld",
    "daemonColdStart": "withheld",
    "hooks": "withheld",
    "disableAllHooks": "withheld",
    "allowManagedHooksOnly": "withheld",
    "allowedHttpHookUrls": "withheld",
    "httpHookAllowedEnvVars": "withheld",
    "env": "withheld",
    # What a session may do, and what it may reach.
    "permissions": "withheld",
    "sandbox": "withheld",
    "skipWebFetchPreflight": "withheld",
    "skipDangerousModePermissionPrompt": "withheld",
    "skipWorkflowUsageWarning": "withheld",
    "disableAutoMode": "withheld",
    "modelProposedGoals": "withheld",
    "agent": "withheld",
    "worktree": "withheld",
    "plansDirectory": "withheld",
    "disableSkillShellExecution": "withheld",
    "skillOverrides": "withheld",
    "disableBundledSkills": "withheld",
    "syncClaudeAiSkills": "withheld",
    "disableAgentView": "withheld",
    "disableWorkflows": "withheld",
    "enableWorkflows": "withheld",
    "disableArtifact": "withheld",
    "enableArtifact": "withheld",
    "remoteControlAtStartup": "withheld",
    "remoteControl": "withheld",
    "disableRemoteControl": "withheld",
    "isolatePeerMachines": "withheld",
    "crossSessionInbound": "withheld",
    "autoUploadSessions": "withheld",
    "remote": "withheld",
    "remoteTools": "withheld",
    "sshConfigs": "withheld",
    "channelsEnabled": "withheld",
    "allowedChannelPlugins": "withheld",
    # MCP servers, plugins and the marketplaces they come from: the repository's.
    "enableAllProjectMcpServers": "withheld",
    "enabledMcpjsonServers": "withheld",
    "disabledMcpjsonServers": "withheld",
    "disableClaudeAiConnectors": "withheld",
    "managedMcpServers": "withheld",
    "allowedMcpServers": "withheld",
    "deniedMcpServers": "withheld",
    "allowManagedMcpServersOnly": "withheld",
    "allowAllClaudeAiMcps": "withheld",
    "allowClaudeInChromeWithManagedMcp": "withheld",
    "enabledPlugins": "withheld",
    "prependPlugins": "withheld",
    "appendPlugins": "withheld",
    "pluginConfigs": "withheld",
    "syncClaudeAiPlugins": "withheld",
    "extraKnownMarketplaces": "withheld",
    "additionalMarketplaces": "withheld",
    "strictKnownMarketplaces": "withheld",
    "allowedMarketplaces": "withheld",
    "blockedMarketplaces": "withheld",
    "disableCommandPluginSources": "withheld",
    "disableSideloadFlags": "withheld",
    "pluginSuggestionMarketplaces": "withheld",
    "strictPluginOnlyCustomization": "withheld",
    "pluginTrustMessage": "withheld",
    # Memory: the repository's, never the person's.
    "autoMemoryEnabled": "withheld",
    "autoMemoryDirectory": "withheld",
    "autoDreamEnabled": "withheld",
    "claudeMd": "withheld",
    "claudeMdExcludes": "withheld",
    # Login, models an organization allows, and where requests are routed.
    "forceLoginMethod": "withheld",
    "forceLoginGatewayUrl": "withheld",
    "forceLoginOrgUUID": "withheld",
    "gatewayInternalNetworks": "withheld",
    "availableModels": "withheld",
    "enforceAvailableModels": "withheld",
    "modelOverrides": "withheld",
    "modelPricing": "withheld",
    "maxEffortLevel": "withheld",
    # Where settings come from, and the installation itself.
    "$schema": "withheld",
    "wslInheritsWindowsSettings": "withheld",
    "parentSettingsBehavior": "withheld",
    "managedSourcesBehavior": "withheld",
    "forceRemoteSettingsRefresh": "withheld",
    "allowManagedPermissionRulesOnly": "withheld",
    "autoUpdatesChannel": "withheld",
    "minimumVersion": "withheld",
    "requiredMinimumVersion": "withheld",
    "requiredMaximumVersion": "withheld",
    "companyAnnouncements": "withheld",
    "spinnerTipsOverride": "withheld",
}
"""Every key of Claude Code's settings schema, and where a session's change to it goes."""

# lup: ignore[constant-declaration] — a decision per key of the CLI's global
# configuration document, made once here and checked against every release
DOCUMENT_FLOWS: dict[ClaudeDocumentKey, SettingFlow] = {
    # Preferences the settings files also hold, and are read from first.
    "theme": "returns",
    "editorMode": "returns",
    "verbose": "returns",
    "preferredNotifChannel": "returns",
    "autoCompactEnabled": "returns",
    "autoScrollEnabled": "returns",
    "showTurnDuration": "returns",
    "showMessageTimestamps": "returns",
    "todoFeatureEnabled": "returns",
    "fileCheckpointingEnabled": "returns",
    "terminalProgressBarEnabled": "returns",
    "inputNeededNotifEnabled": "returns",
    "agentPushNotifEnabled": "returns",
    "respectGitignore": "returns",
    "workflowSizeGuideline": "returns",
    # Preferences only the document holds.
    "externalEditorContext": "returns",
    "diffTool": "returns",
    "showExpandedTodos": "returns",
    "briefTranscript": "returns",
    "diffSidebarOpen": "returns",
    "messageIdleNotifThresholdMs": "returns",
    "autoConnectIde": "returns",
    "showStatusInTerminalTab": "returns",
    "taskCompleteNotifEnabled": "returns",
    "lspRecommendationDisabled": "returns",
    "lspRecommendationNeverPlugins": "returns",
    "copyFullResponse": "returns",
    "copyOnSelect": "returns",
    "leftArrowOpensAgents": "returns",
    "defaultToAgentsView": "returns",
    "prStatusFooterEnabled": "returns",
    # Programs, access, and the installation.
    "apiKeyHelper": "withheld",
    "env": "withheld",
    "installMethod": "withheld",
    "autoUpdates": "withheld",
    "autoUpdatesProtectedForNative": "withheld",
    "autoInstallIdeExtension": "withheld",
    "claudeInChromeDefaultEnabled": "withheld",
    "remoteControlAtStartup": "withheld",
    "autoUploadSessions": "withheld",
    "autoAddRemoteControlDaemonWorker": "withheld",
    # What the CLI records about its own use, which is the volume's.
    "shiftEnterKeyBindingInstalled": "withheld",
    "hasUsedBackslashReturn": "withheld",
    "tipsHistory": "withheld",
    "hasCompletedClaudeInChromeOnboarding": "withheld",
    "lspRecommendationIgnoredCount": "withheld",
    "remoteDialogSeen": "withheld",
}
"""Every key ``claude config`` accepts for ``.claude.json``, and where a change goes."""

# lup: ignore[constant-declaration] — the settings keys the lup config names in
# its own words, which both directions translate between
PORTABLE: dict[ClaudeSettingKey, tuple[str, ...]] = {
    "theme": ("theme", "claude"),
    "editorMode": ("editor",),
}
"""The settings a lup config holds for every runtime, and where it holds each."""


def unclassified() -> list[str]:
    """Every key the installed release takes which no decision here names.

    The key types are compiled from the committed snapshot, so this answers
    for the release that snapshot was read from; the tables are typed
    against the same keys, so the other direction — a decision naming a key
    the release does not take — is the type checker's to refuse.
    """
    return [
        *(
            key
            for key in get_args(ClaudeSettingKey.__value__)
            if key not in SETTINGS_FLOWS
        ),
        *(
            f"document {key}"
            for key in get_args(ClaudeDocumentKey.__value__)
            if key not in DOCUMENT_FLOWS
        ),
    ]


def setting_flow(key: str) -> SettingFlow:
    """Where a change to one settings key goes; a key nobody decided about stays."""
    decided: list[SettingFlow] = [
        flow for known, flow in SETTINGS_FLOWS.items() if known == key
    ]
    return decided[0] if decided else "withheld"


def document_homed(key: str) -> bool:
    """Whether a returning preference lives in the configuration document alone."""
    return any(
        known == key and flow == "returns" for known, flow in DOCUMENT_FLOWS.items()
    ) and not any(known == key for known in SETTINGS_FLOWS)


def legacy_preference(key: str) -> bool:
    """Whether a returning preference the settings hold also has a document copy."""
    return any(
        known == key and flow == "returns" for known, flow in DOCUMENT_FLOWS.items()
    ) and any(known == key for known in SETTINGS_FLOWS)
