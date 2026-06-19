The Computer skill automates native desktop applications using the OS accessibility
tree (macOS AX, Windows UIA, Linux AT-SPI). It walks a multi-layer cascade mirroring
the Browser skill: deterministic metadata extraction, accessibility tree traversal,
and visual set-of-marks escalation.

The execution bypasses standard LLM tool dispatch. Instead, the `desktop/skill.py`
wrapper autonomously drives a rigid Scan-Act-Verify loop:
1. Scan: Capture the `get_window_state` tree.
2. Act: Prompt Gemini 3.1 Flash-Lite to select the next `element_index` or action.
3. Verify: Re-scan to ensure the application state has progressed.

Inputs: `metadata.app` (required, the exact name of the application to launch or
target) and `metadata.goal` (required, free-text description of the task to
accomplish).
Output: `AgentResult` populated with the sequence of actions taken or an error.
Use when the user requests interactions with local applications, settings, or
desktop files that cannot be handled via standard terminal commands.
