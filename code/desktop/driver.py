import asyncio
import json
import time
from typing import Any

from gateway import LLM
from .cua import call_cua

class DesktopDriver:
    NAME = "computer"

    def __init__(self, session: str | None = None):
        self.session = session

    async def decompose_goal(self, goal: str, app_name: str) -> list[str]:
        prompt = f"""Break this goal down into simple, discrete subgoals for {app_name}.
Goal: {goal}

IMPORTANT: Define your subgoals as STATES to achieve (e.g. "The text is entered", "The final result is extracted"), NOT as blind actions (e.g. "Press a button").
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

    async def execute_subgoal(self, subgoal: str, app_name: str, pid: int, wid: str, actions_taken: list, trace: list) -> tuple[bool, int, str]:
        attempts = 0
        turns = 0
        
        while attempts < 10:
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

CRITICAL: Evaluate the accessibility tree. If the Current Subgoal has ALREADY been achieved, you MUST output "action": "done" to move on.

CRITICAL: You must output EXACTLY ONE JSON object. Do NOT output multiple steps or multiple JSON blocks at once. You are in a strict loop: take ONE action, and you will see the updated screen on the next turn.

Choose how to act using ONE of the following formats (JSON only):
- Layer 1 (Extract / Done): {{"layer": "1", "action": "done", "reason": "Goal already achieved, or Extracted result: X"}}
- Layer 2a (Deterministic typing - PREFERRED for entering numbers/text/hotkeys): {{"layer": "2a", "action": "type_text", "text": "value_to_type", "expected_postcondition": "Screen updates"}}
- Layer 2b (AX Tree click - Use only if typing fails): {{"layer": "2b", "action": "click", "element_index": N, "expected_postcondition": "Button depresses"}}
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
