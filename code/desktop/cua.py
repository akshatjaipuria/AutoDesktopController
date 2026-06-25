import json
import subprocess
import time
import shutil
from typing import Any

# Find cua-driver in PATH, fallback to native shell resolution
CUA = shutil.which("cua-driver") or shutil.which("cua-driver.exe") or "cua-driver"

class CuaError(RuntimeError):
    pass

def call_cua(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    proc = subprocess.run(
        [CUA, "call", tool, json.dumps(args)],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise CuaError(f"{tool} failed: {proc.stderr.strip()}")
    out = proc.stdout.strip()
    return json.loads(out) if out.startswith("{") else {"raw": out}

def ensure_daemon() -> None:
    status = subprocess.run([CUA, "status"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if "is running" not in status.stdout:
        subprocess.Popen([CUA, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
