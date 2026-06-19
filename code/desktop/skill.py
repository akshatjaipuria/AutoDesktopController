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
            # 1. Launch App
            launch_res = call_cua("launch_app", {"name": app_name})
            pid = launch_res.get("pid")
            
            if not pid:
                return self._pack_error(f"failed to launch {app_name}", goal, time.time() - started)

            # Wait for app to be ready and get window_id
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

            if not wid:
                return self._pack_error(f"launched {app_name} but found no windows", goal, time.time() - started)

            # macOS Background Activation Trap:
            if sys.platform == "darwin":
                subprocess.run(["osascript", "-e", f'tell application "{app_name}" to activate'])
                time.sleep(0.5)

            # 2. Loop
            max_steps = 12
            turns = 0
            actions_taken = []
            
            for turn in range(max_steps):
                state = call_cua("get_window_state", {"pid": pid, "window_id": wid, "capture_mode": "ax"})
                
                if state.get("element_count", 0) == 0:
                    # Fallback to Vision or fail
                    return self._pack_error("Empty AX tree - requires vision fallback or permissions", goal, time.time() - started)
                
                # LLM Prompt for Layer 2b
                prompt = f"""You are driving a desktop application.
Goal: {goal}
App: {app_name}


Current accessibility tree:
{state.get('tree_markdown', '')[:25000]}

Respond with exactly ONE JSON action from the following formats:
- {{"action": "click", "element_index": N}}
- {{"action": "type_text", "element_index": N, "text": "hello"}}
- {{"action": "press_key", "key": "Return"}}
- {{"action": "done", "reason": "Goal achieved"}}
"""
                reply = await asyncio.to_thread(
                    LLM().chat,
                    prompt=prompt,
                    agent=self.NAME,
                    session=self.session,
                    provider="gemini", # Pin to gemini flash-lite equivalent
                    max_tokens=500,
                    temperature=0.0
                )
                
                # Parse action
                text = reply.get("text", "").strip()
                if text.startswith("```json"):
                    text = text.strip("`").split("\\n", 1)[-1]
                if text.endswith("```"):
                    text = text[:-3]
                
                try:
                    action = json.loads(text)
                except json.JSONDecodeError:
                    return self._pack_error(f"LLM returned invalid JSON: {text}", goal, time.time() - started)
                
                if action.get("action") == "done":
                    return self._pack_success(goal, turns, actions_taken, time.time() - started)
                
                # 3. Act
                cmd = action.get("action")
                cua_args = {"pid": pid, "window_id": wid}
                if "element_index" in action:
                    cua_args["element_index"] = action["element_index"]
                if "text" in action:
                    cua_args["text"] = action["text"]
                if "key" in action:
                    cua_args["key"] = action["key"]
                
                call_cua(cmd, cua_args)
                actions_taken.append(action)
                turns += 1
                time.sleep(0.5)

            return self._pack_error("action budget exceeded", goal, time.time() - started)

        except Exception as e:
            return self._pack_error(str(e), goal, time.time() - started)

    def _pack_error(self, error: str, goal: str, elapsed: float) -> AgentResult:
        return AgentResult(
            success=False,
            agent_name=self.NAME,
            error=error,
            elapsed_s=elapsed,
            output={"goal": goal, "error": error}
        )
        
    def _pack_success(self, goal: str, turns: int, actions: list, elapsed: float) -> AgentResult:
        return AgentResult(
            success=True,
            agent_name=self.NAME,
            elapsed_s=elapsed,
            output={"goal": goal, "turns": turns, "actions": actions}
        )
