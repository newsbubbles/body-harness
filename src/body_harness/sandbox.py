"""The scratchpad: Python the agent writes, run in its own process with the seat's workspace as its whole world.

An audit hook in the child refuses the network, other processes, native code (ctypes), and any file under the
harness's home outside the workspace and the apps folder, so code a character writes (or was talked into writing)
cannot read its door token or another seat's notes. It is a guard rail, not a VM: run untrusted operators' seats
in a container if that matters to you.

Apps are the same runner in another mode: `apps/<name>.py` declares `TOOL = {...}` (a name, a description and JSON
schema parameters) and `def run(args): ...`, and becomes a tool the seat can find and call."""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

RUNNER = r'''
import sys, os, json
home, ws, apps, mode, target = sys.argv[1:6]
norm = lambda p: os.path.normcase(os.path.realpath(p))
HOME, WS, APPS = norm(home), norm(ws), norm(apps)
def inside(p, root):
    return p == root or p.startswith(root + os.sep)
def ok_path(p, write):
    try:
        p = norm(p)
    except Exception:
        return False
    if inside(p, WS) or inside(p, APPS):
        return True
    if write or inside(p, HOME):
        return False
    return True
DENY = ("socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.startfile", "os.fork",
        "winreg.", "ctypes.", "urllib.", "http.client", "ftplib.", "smtplib.", "webbrowser.")
def hook(ev, args):
    if ev == "open":
        path, mode = args[0], (args[1] or "r")
        if isinstance(path, int):
            return
        if not ok_path(path, any(c in str(mode) for c in "wax+")):
            raise PermissionError(f"the sandbox keeps to the workspace and apps: {path}")
    elif ev in ("os.remove", "os.rename", "os.rmdir", "os.mkdir", "shutil.rmtree", "shutil.move", "os.chmod",
                "os.truncate", "os.link", "os.symlink"):
        for a in args:
            if isinstance(a, (str, bytes, os.PathLike)) and not ok_path(a, True):
                raise PermissionError(f"the sandbox writes only in the workspace and apps: {a}")
    elif ev in ("os.listdir", "os.scandir", "glob.glob"):
        a = args[0] if args else "."
        if isinstance(a, (str, bytes, os.PathLike)) and not ok_path(a, False):
            raise PermissionError(f"the sandbox reads only the workspace and apps under the harness home: {a}")
    elif ev == "import" and args and str(args[0]).split(".")[0] in ("ctypes", "_ctypes", "cffi", "winreg", "_winapi"):
        raise PermissionError("the sandbox has no native calls")
    elif ev.startswith(DENY):
        raise PermissionError(f"the sandbox has no network and starts no processes ({ev})")
src = open(target, encoding="utf-8").read() if mode == "code" else None
sys.addaudithook(hook)
os.chdir(WS)
sys.path[:0] = [WS, APPS]
if mode == "code":
    g = {"__name__": "__main__"}
    exec(compile(src, "scratch.py", "exec"), g)
else:
    import importlib.util
    spec = importlib.util.spec_from_file_location("app_" + os.path.basename(target)[:-3], target)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    args = json.loads(sys.stdin.read() or "{}")
    r = m.run(args)
    print(r if isinstance(r, str) else json.dumps(r, default=str))
'''

OUT_MAX = 6000


async def _run(home: Path, ws: Path, apps: Path, mode: str, target: Path, stdin: str, timeout: float) -> dict[str, Any]:
    runner = ws.parent / "cache" / "_runner.py"
    runner.parent.mkdir(exist_ok=True)
    if not runner.exists() or runner.read_text(encoding="utf-8") != RUNNER:
        runner.write_text(RUNNER, encoding="utf-8")
    env = {"PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""), "PYTHONIOENCODING": "utf-8",
           "BH_SCENE": str(ws / "scene.json"), "BH_DATA": str(apps / (target.stem + ".data"))}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-X", "utf8", str(runner), str(home), str(ws), str(apps), mode, str(target),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        cwd=str(ws))
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin.encode()), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "reason": f"stopped after {timeout:.0f}s"}
    o, e = out.decode(errors="replace"), err.decode(errors="replace")
    res: dict[str, Any] = {"ok": proc.returncode == 0, "stdout": o[-OUT_MAX:]}
    if e.strip():
        res["stderr"] = e[-2500:]
    return res


async def run_python(home: Path, seat_dir: Path, code: str, timeout: float = 30) -> dict[str, Any]:
    ws, apps = seat_dir / "workspace", seat_dir / "apps"
    f = seat_dir / "cache" / "_scratch.py"
    f.write_text(code, encoding="utf-8")
    return await _run(home, ws, apps, "code", f, "", timeout)


def app_specs(apps: Path) -> dict[str, dict[str, Any]]:
    """Read each app's TOOL literal without running it."""
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(apps.glob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "TOOL" for t in node.targets):
                try:
                    spec = ast.literal_eval(node.value)
                except ValueError:
                    break
                if isinstance(spec, dict) and spec.get("description"):
                    spec.setdefault("name", f.stem)
                    spec.setdefault("parameters", {"type": "object", "properties": {}})
                    spec["file"] = f.name
                    out["app_" + f.stem] = spec
                break
    return out


async def run_app(home: Path, seat_dir: Path, file: str, args: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
    apps = seat_dir / "apps"
    (apps / (Path(file).stem + ".data")).mkdir(exist_ok=True)
    return await _run(home, seat_dir / "workspace", apps, "app", apps / file, json.dumps(args), timeout)


def write_file(seat_dir: Path, path: str, text: str) -> dict[str, Any]:
    """Write under workspace/ or apps/ only."""
    p = Path(path)
    if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] not in ("workspace", "apps"):
        return {"ok": False, "reason": "write under workspace/ or apps/, as a relative path (apps/mood.py)"}
    full = seat_dir / p
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(text, encoding="utf-8")
    return {"ok": True, "wrote": str(p), "chars": len(text)}
