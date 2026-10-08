"""Fixed UI and client logging settings offered through Sage choices.

The values are executable WoW slash commands, not user/model-provided text.
Keep this allowlist intentionally narrow and reversible.
"""

CHAT_VISIBILITY_COMMANDS = {
    "hide_chat_overlay": "/run ChatFrame1:Hide()",
    "restore_chat_overlay": "/run ChatFrame1:Show()",
    "enable_combat_log": "/run LoggingCombat(true)",
}
