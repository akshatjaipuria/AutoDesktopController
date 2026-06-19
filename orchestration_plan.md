# AutoDesktopController Orchestration Plan

## 1. Overview
The goal of this orchestration layer is to reliably automate desktop tasks through an agentic model, leveraging `cua-driver` for perception and action, while utilizing the existing `V9` LLM Gateway from Session 9. Unlike web browsing, desktop automation requires dealing with OS-level focus, a11y (accessibility) tree caches, and platform-specific quirks.

To ensure performance and limit token costs, the architecture is designed around a multi-layered escalation strategy: using cheap, local, or deterministic methods wherever possible, and only falling back to expensive vision models as a last resort.

## 2. Core Architecture & Escalation Layers
The framework implements five layers above the base `cua-driver` perception/action primitives:

1. **Goal Decomposition Layer**: Uses a frontier model to break down high-level, natural language goals into ordered, application-specific subgoals.
2. **Perception Interpretation Layer**: The largest cost/quality lever. Instead of feeding massive raw Accessibility (AX) trees to the LLM, we use query filtering, regex extraction, and summarization to refine the markdown representation of the AX tree.
3. **Action Sequencing Layer**: Translates subgoals into execution steps using a strict `scan-act-verify` loop. 
4. **Error Recovery Layer**: Handles missing elements, unexpected modals, OS permission issues, and application crashes by carrying state context across failures.
5. **Vision Fallback Layer (Layer 3)**: Only triggered when the AX tree is empty (for custom renderers like games, Figma) or when elements are strictly visual. Captures screenshots with numbered marks and routes to the V9 Vision endpoint.

### Action Cost Hierarchy
- **Layer 1 (Extract):** Read directly from AX tree, clipboard, or file. Zero LLM cost.
- **Layer 2a (Deterministic):** Predefined hotkey sequences. No LLM cost in the loop.
- **Layer 2b (a11y tree):** Uses a cheap text LLM (like Gemini 3.1 Flash-Lite) to read markdown AX trees and emit JSON actions indexed by element ID. This is the **workhorse** layer.
- **Layer 3 (Vision):** Uses frontier multimodal models. 10x cost of Layer 2b. Use strictly as a last resort.

## 3. The Scan-Act-Verify Execution Loop
The engine executes operations using a strict three-phase loop:

1. **Scan (`get_window_state`)**: Retrieves the UI state and builds an `element_index -> AX node` cache.
2. **Act**: Dispatches clicks, typing, or hotkeys addressing the targeted `element_index`.
3. **Verify (`get_window_state`)**: Re-reads the AX tree to ensure the UI mutated as expected (e.g., target element appeared, title changed).

**Strict Invariants**:
- **Re-scan before action**: An element-indexed action will fail without a prior scan to populate the cache.
- **Turn-scoped tokens**: Every UI change (dialog open, menu pop) invalidates the cache. Re-scan immediately after every state-changing action.

## 4. Handling Desktop Quirks & "Traps"
To prevent silent misbehaviors and hallucination loops, the orchestration implements guardrails for known desktop traps:
- **Permissions Error (`element_count: 0`)**: Detect and immediately raise a `PermissionsError` prompting for TCC/Wayland/UAC grants.
- **macOS Background Launch**: Use AppleScript (`osascript -e 'tell application "App" to activate'`) + 0.5s sleep to force window realization before scanning.
- **Linux Qt Apps**: Inject `QT_ACCESSIBILITY=1` into the environment before launch.
- **Cache Misses**: Triggered by UI reflows; forces a re-scan.
- **The Electron Escape Hatch**: VS Code, Slack, Discord, etc., appear as opaque `AXWebArea` elements. The orchestrator pattern-matches known Electron apps, relaunches them with `--remote-debugging-port`, and drives them natively via Chrome DevTools Protocol (CDP) using the `page` tool.

## 5. Parallel Execution & DAG Strategy

**Verdict: Hybrid (DAG for Goals, Strict Sequential for Native UI)**

Can we use a Directed Acyclic Graph (DAG) for parallel execution? **Yes, but with strict boundaries.**

### Justification AGAINST full Native UI parallelism:
In a standard desktop environment (macOS, Windows, Linux X11), there is only **one** active cursor and **one** foreground window with keyboard focus. If two agents attempt to interact with different native GUI windows simultaneously, they will steal focus from one another, causing clicks to miss, keystrokes to drop, and the `verify` step to instantly fail. 

### Justification FOR DAG in Goal Orchestration:
While the physical native "Action" layer must be globally locked and sequential, a DAG is highly beneficial for the **Goal Decomposition** and **Perception** layers:

1. **Non-UI Background Tasks**: An agent can research documentation (API calls, web scraping), process large text files, or run terminal scripts in parallel while the UI agent drives an application.
2. **CDP/Electron Apps**: If an app is being driven via CDP (`electron_debugging_port`), it does not require active OS focus. We can have one node in the DAG driving VS Code via CDP, while another node drives the native macOS Calculator via a11y tree.
3. **Information Gathering**: Parallel nodes can scan different APIs or files, join their results in the DAG, and pass the context to the UI-driving node.

**Implementation Strategy:**
- Use a DAG to manage high-level subgoals.
- Implement a global `DesktopUI_Mutex`. Any DAG node requiring `cua-driver` native interactions (clicks, keyboard) must acquire the mutex. 
- Nodes using `page` (CDP), Layer 1 Extraction, or external APIs can execute concurrently without the mutex.

## 6. Incremental Integration with Session 9 (DesktopController) Architecture
The desktop orchestration is built strictly as an incremental addition on top of the existing DesktopController framework. We do not reinvent the wheel:
- **Seamless Skill Addition**: The new `computer` skill drops directly into the existing catalogue alongside the `Browser` skill. 
- **Legacy Preservation**: All older DesktopController agents and skills continue to function exactly as they did in Session 9 without any changes (e.g., pure web tasks are handled by Browser). However, the new Desktop Agent handles all desktop apps—including Electron apps via the `page` tool—entirely within its own orchestration, without routing back to the legacy Browser cascade.
- **Minimal Dispatch Overhead**: Integration requires a single incremental branch in the dispatcher (e.g., `if skill.name == "computer":`) to route commands to the desktop orchestrator.
- **Unified Gateway & Tooling**: The existing V9 Gateway handles all LLM and Vision calls. The replay viewer naturally surfaces the action verdicts (e.g., act via `element_index` or escalate), mirroring Browser's `output.path`, and the cost ledger automatically tags these calls under `agent: computer`.
