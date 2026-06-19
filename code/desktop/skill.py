import asyncio
import json
import subprocess
import time
import sys
import shutil
from pathlib import Path
from typing import Any

from schemas import AgentResult, NodeSpec
from gateway import LLM

# Find cua-driver in PATH, fallback to native shell resolution
CUA = shutil.which("cua-driver") or shutil.which("cua-driver.exe") or "cua-driver"

class CuaError(RuntimeError):
    pass

def call_cua(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    proc = subprocess.run(
        [CUA, "call", tool, json.dumps(args)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise CuaError(f"{tool} failed: {proc.stderr.strip()}")
    out = proc.stdout.strip()
    return json.loads(out) if out.startswith("{") else {"raw": out}

def ensure_daemon() -> None:
    status = subprocess.run([CUA, "status"], capture_output=True, text=True)
    if "is running" not in status.stdout:
        subprocess.Popen([CUA, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)

class DesktopSkill:
    NAME = "computer"

    def __init__(self, session: str | None = None):
        self.session = session
        ensure_daemon()

    async def run(self, node: NodeSpec) -> AgentResult:
        app_name = node.metadata.get("app") or (node.inputs[0] if node.inputs else None)
        goal = node.metadata.get("goal", "perform desktop task")
        started = time.time()
        
        if not app_name:
            return self._pack_error("no target app specified", goal, time.time() - started)

        try:
            # 1. Launch & Window Resolution
            pid, wid = self._launch_and_find_window(app_name)
            if not pid or not wid:
                return self._pack_error(f"Failed to launch or find window for {app_name}", goal, time.time() - started)

            # 2. Goal Decomposition
            subgoals = await self._decompose_goal(goal, app_name)
            
            trace = [{"phase": "Goal Decomposition", "subgoals": subgoals}]
            actions_taken = []
            total_turns = 0

            # 3. Action Sequencing (Scan-Act-Verify loop)
            for subgoal in subgoals:
                success, turns_taken, err = await self._execute_subgoal(
                    subgoal, app_name, pid, wid, actions_taken, trace
                )
                total_turns += turns_taken
                
                if not success:
                    return self._pack_error(f"Failed to complete subgoal '{subgoal}': {err}", goal, time.time() - started, trace)

            return self._pack_success(goal, total_turns, trace, time.time() - started)

        except Exception as e:
            return self._pack_error(str(e), goal, time.time() - started, [])

    # --- HELPER METHODS ---

    def _launch_and_find_window(self, app_name: str) -> tuple[int | None, str | None]:
        launch_res = call_cua("launch_app", {"name": app_name})
        pid = launch_res.get("pid")
        if not pid: return None, None

        wid = None
        for _ in range(5):
            time.sleep(1.0)
            windows_res = call_cua("list_windows", {})
            windows = [w for w in windows_res.get("windows", []) 
                       if w.get("pid") == pid or app_name.lower() in w.get("title", "").lower()]
            if windows:
                wid = windows[0].get("window_id")
                pid = windows[0].get("pid")
                break

        # macOS Background Activation Trap
        if wid and sys.platform == "darwin":
            subprocess.run(["osascript", "-e", f'tell application "{app_name}" to activate'])
            time.sleep(0.5)

        return pid, wid

    async def _decompose_goal(self, goal: str, app_name: str) -> list[str]:
        prompt = f"""Break this goal down into simple, discrete subgoals for {app_name}.
Goal: {goal}

If the app is Calculator and the goal is math, use a deterministic sequence (e.g. typing the equation, then reading the result).
Respond ONLY with valid JSON in this format: {{"subgoals": ["Subgoal 1", "Subgoal 2"]}}"""
        
        reply = await asyncio.to_thread(
            LLM().chat, prompt=prompt, agent=self.NAME, session=self.session,
            provider="gemini", max_tokens=300, temperature=0.0
        )
        
        try:
            text = reply.get("text", "").strip()
            if text.startswith("```json"): text = text.strip("`").split("\n", 1)[-1]
            if text.endswith("```"): text = text[:-3]
            return json.loads(text).get("subgoals", [goal])
        except:
            return [goal]

    async def _execute_subgoal(self, subgoal: str, app_name: str, pid: int, wid: str, actions_taken: list, trace: list) -> tuple[bool, int, str]:
        attempts = 0
        turns = 0
        
        while attempts < 3:
            attempts += 1
            turns += 1
            
            # --- PHASE A: SCAN ---
            state = call_cua("get_window_state", {"pid": pid, "window_id": wid, "capture_mode": "ax"})
            if state.get("element_count", 0) == 0:
                trace.append({"turn": turns, "layer": "3", "action": "vision_fallback", "error": "AX tree empty"})
                return False, turns, "Empty AX tree - requires vision fallback"

            truncated_tree = state.get('tree_markdown', '')[:25000]

            # --- PHASE B: ACT (Perception & Routing) ---
            action_prompt = f"""You are driving {app_name}.
Current Subgoal: {subgoal}
Previous actions taken: {json.dumps(actions_taken)}

Current accessibility tree:
{truncated_tree}

Choose how to act using one of the following formats (JSON only):
- Layer 1 (Extract text without clicking): {{"layer": "1", "action": "done", "reason": "Extracted result: X"}}
- Layer 2a (Deterministic hotkey/typing): {{"layer": "2a", "action": "type_text", "text": "850*0.15=", "expected_postcondition": "Screen updates"}}
- Layer 2b (AX Tree click): {{"layer": "2b", "action": "click", "element_index": N, "expected_postcondition": "Button depresses"}}
- Layer 3 (Vision Fallback): {{"layer": "3", "action": "vision_fallback", "reason": "Target not found in tree"}}
"""
            reply = await asyncio.to_thread(
                LLM().chat, prompt=action_prompt, agent=self.NAME, session=self.session,
                provider="gemini", max_tokens=500, temperature=0.0
            )
            
            text = reply.get("text", "").strip()
            if text.startswith("```json"): text = text.strip("`").split("\n", 1)[-1]
            if text.endswith("```"): text = text[:-3]
            
            try:
                action = json.loads(text)
            except json.JSONDecodeError:
                trace.append({"turn": turns, "error": f"Invalid LLM JSON: {text}"})
                continue
            
            layer_used = action.get("layer", "unknown")
            cmd = action.get("action")
            trace_step = {"turn": turns, "subgoal": subgoal, "layer": layer_used, "llm_decision": action}
            
            # --- ROUTING TO THE 4 LAYERS ---
            
            # Layer 3: Vision Fallback (Agent gives up on AX tree)
            if layer_used == "3" or cmd == "vision_fallback":
                trace_step["error"] = "Escalating to vision"
                trace.append(trace_step)
                return False, turns, "Escalated to vision fallback"
                
            # Layer 1: Extract (Agent just reads the tree, no action taken)
            elif layer_used == "1" or cmd == "done":
                trace_step["status"] = "subgoal_completed (extraction)"
                trace.append(trace_step)
                return True, turns, ""
            
            # Build arguments for CUA
            cua_args = {"pid": pid, "window_id": wid}
            if "element_index" in action: cua_args["element_index"] = action["element_index"]
            if "text" in action: cua_args["text"] = action["text"]
            if "key" in action: cua_args["key"] = action["key"]
            
            print(f"  [computer] Turn {turns} | Subgoal: {subgoal} | Layer: {layer_used} | Cmd: {cmd} {cua_args}")
            
            # Layer 2a & 2b: Execution
            if layer_used == "2a":
                # Deterministic hotkey or typing sequence
                call_cua(cmd, cua_args)
            elif layer_used == "2b":
                # AX Tree semantic click
                call_cua(cmd, cua_args)
            else:
                # Fallback execution
                call_cua(cmd, cua_args)
                
            actions_taken.append(action)
            
            # --- PHASE C: VERIFY ---
            time.sleep(0.5)
            call_cua("get_window_state", {"pid": pid, "window_id": wid, "capture_mode": "ax"}) # Re-scan
            
            trace_step["verify"] = "AX Tree refreshed successfully"
            trace.append(trace_step)
            
            # Deterministic sequences usually complete the subgoal instantly
            if layer_used == "2a":
                return True, turns, ""

        return False, turns, "Subgoal retry limit exceeded"

    def _pack_error(self, error: str, goal: str, elapsed: float, trace: list = None) -> AgentResult:
        return AgentResult(
            success=False, agent_name=self.NAME, error=error, elapsed_s=elapsed,
            output={"goal": goal, "error": error, "trace": trace or []}
        )
        
    def _pack_success(self, goal: str, turns: int, trace: list, elapsed: float) -> AgentResult:
        return AgentResult(
            success=True, agent_name=self.NAME, elapsed_s=elapsed,
            output={"goal": goal, "turns": turns, "trace": trace}
        )
