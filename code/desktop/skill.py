import time
import sys
import subprocess

from schemas import AgentResult, NodeSpec
from .cua import call_cua, ensure_daemon
from .driver import DesktopDriver

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

            driver = DesktopDriver(session=self.session)

            # 2. Goal Decomposition
            subgoals = await driver.decompose_goal(goal, app_name)
            
            trace = [{"phase": "Goal Decomposition", "subgoals": subgoals}]
            actions_taken = []
            total_turns = 0

            # 3. Action Sequencing (Scan-Act-Verify loop)
            for subgoal in subgoals:
                success, turns_taken, err = await driver.execute_subgoal(
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
