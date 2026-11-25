"""Keep seats running: one supervisor per home, one `bh run <seat>` process per seat, restarted when it stops.

    bh service add <seat>      keep a seat running (the supervisor picks it up within seconds)
    bh service remove <seat>   stop keeping it (its process is stopped)
    bh service status          what is kept, what runs, since when, how often it restarted
    bh service run             the supervisor itself, in the foreground
    bh service install         Windows: a scheduled task that starts the supervisor at logon and every 10 minutes
    bh service uninstall       remove that task
    bh service stop            ask the running supervisor to stop its seats and exit

What is kept lives in $BH_HOME/service/seats.json, read every few seconds, so add and remove need no restart. One
process per seat, so one seat crashing never takes another down. A seat never runs twice: before starting one, the
supervisor looks for any other `bh run` naming it (or `bh run --all`) and leaves it alone while one exists. A seat
that stops comes back after 5 seconds, doubling to 5 minutes, and the wait resets once it has stayed up 10 minutes.
Standard library only. First built by the Glass Reach host session to keep a Reach NPC up across reboots."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

WIN = os.name == "nt"
POLL = 5            # seconds between looks at seats.json and the children
BACKOFF = (5, 300)  # first and longest wait before restarting a seat that stopped
STEADY = 600        # a seat up this long has its backoff reset
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Service:
    def __init__(self, home: Path):
        self.home = home
        self.dir = home / "service"
        self.conf_p, self.state_p = self.dir / "seats.json", self.dir / "state.json"
        self.stop_p, self.lock_p, self.logs = self.dir / "stop.flag", self.dir / "supervisor.lock", self.dir / "logs"

    # ---------- what is kept ----------

    def conf(self) -> dict[str, Any]:
        try:
            return json.loads(self.conf_p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"seats": {}}

    def save_conf(self, c: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.conf_p.with_suffix(".tmp")
        tmp.write_text(json.dumps(c, indent=2), encoding="utf-8")
        os.replace(tmp, self.conf_p)

    def log(self, msg: str) -> None:
        self.logs.mkdir(parents=True, exist_ok=True)
        line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
        with open(self.logs / "supervisor.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if sys.stdout and not Path(sys.executable).name.lower().startswith("pythonw"):
            print(line, flush=True)

    # ---------- processes ----------

    def start(self, name: str, spec: dict[str, Any]) -> subprocess.Popen:
        path = Path(spec.get("log") or self.logs / f"{name}.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "body_harness.cli", "--home", str(self.home), "run", name]
        kw: dict[str, Any] = {"creationflags": NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if WIN \
            else {"start_new_session": True}
        with open(path, "a", encoding="utf-8") as out:
            return subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    env=dict(os.environ, PYTHONIOENCODING="utf-8"), **kw)

    def hold_lock(self) -> Any:
        """One supervisor per home: a lock held for the life of the process. None if another holds it."""
        self.dir.mkdir(parents=True, exist_ok=True)
        f = open(self.lock_p, "a+")
        try:
            if WIN:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return None
        return f

    def supervise(self) -> int:
        lock = self.hold_lock()
        if not lock:
            return 0  # one is running already: the scheduled task's 10-minute run lands here
        self.stop_p.unlink(missing_ok=True)
        kids: dict[str, dict[str, Any]] = {}
        self.log(f"supervisor up, pid {os.getpid()}, home {self.home}")
        while True:
            want = {n: s for n, s in (self.conf().get("seats") or {}).items() if s.get("enabled", True)}
            if self.stop_p.exists():
                for n, k in kids.items():
                    if k.get("p") and k["p"].poll() is None:
                        kill(k["p"].pid)
                        self.log(f"{n}: stopped (supervisor stop)")
                self.stop_p.unlink(missing_ok=True)
                self.state_p.unlink(missing_ok=True)
                self.log("supervisor down")
                return 0
            for n in [n for n in kids if n not in want]:
                k = kids.pop(n)
                if k.get("p") and k["p"].poll() is None:
                    kill(k["p"].pid)
                    self.log(f"{n}: stopped (removed)")
            now, others = time.time(), None
            mine = {k["p"].pid for k in kids.values() if k.get("p")}
            for n, spec in want.items():
                k = kids.setdefault(n, {"p": None, "since": 0, "wait": BACKOFF[0], "next": 0, "starts": 0})
                p = k["p"]
                if p and p.poll() is None:
                    if now - k["since"] > STEADY:
                        k["wait"] = BACKOFF[0]
                    continue
                if p:
                    self.log(f"{n}: exited {p.returncode} after {int(now - k['since'])}s; again in {k['wait']}s")
                    k["p"], k["next"] = None, now + k["wait"]
                    k["wait"] = min(k["wait"] * 2, BACKOFF[1])
                if now < k["next"]:
                    continue
                others = bh_runs() if others is None else others
                # only runs of this home: a run --all of another home (a pilot's) must not hold these seats back
                dup = [pid for pid, home, names in others if pid not in mine and same_home(home, self.home)
                       and (n in names or "--all" in names)]
                if dup:
                    if k.get("dup") != dup:
                        self.log(f"{n}: already run by pid {dup[0]} outside the supervisor; left alone (stop that "
                                 f"one first)")
                    k["dup"], k["next"] = dup, now + 60
                    continue
                k["dup"] = None
                try:
                    k["p"], k["since"] = self.start(n, spec), now
                except OSError as e:
                    self.log(f"{n}: could not start ({e}); again in {k['wait']}s")
                    k["next"], k["wait"] = now + k["wait"], min(k["wait"] * 2, BACKOFF[1])
                    continue
                k["starts"] += 1
                mine.add(k["p"].pid)
                self.log(f"{n}: started, pid {k['p'].pid}")
            self.state_p.write_text(json.dumps({"pid": os.getpid(), "at": now, "seats": {
                n: {"pid": k["p"].pid if k["p"] and k["p"].poll() is None else None, "since": k["since"],
                    "starts": k["starts"], "outside": k.get("dup")} for n, k in kids.items()}}), encoding="utf-8")
            time.sleep(POLL)

    # ---------- the command ----------

    def status(self) -> None:
        try:
            st = json.loads(self.state_p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        live = bool(st) and time.time() - st.get("at", 0) < POLL * 6
        print(f"supervisor: {'up, pid ' + str(st['pid']) if live else 'not running'}")
        for n, spec in (self.conf().get("seats") or {}).items():
            s = (st.get("seats") or {}).get(n, {}) if live else {}
            if not spec.get("enabled", True):
                line = "disabled"
            elif s.get("pid"):
                line = f"running, pid {s['pid']}, up {int((time.time() - s['since']) / 60)} min, starts {s['starts']}"
            elif s.get("outside"):
                line = f"run outside the supervisor by pid {s['outside'][0]}"
            else:
                line = "not running"
            print(f"  {n:14} {line}   log {spec.get('log') or self.logs / (n + '.log')}")

    def task_name(self) -> str:
        default = Path(os.environ.get("BH_HOME") or Path.home() / ".body-harness").resolve()
        return "body-harness seats" if self.home.resolve() == default else f"body-harness seats ({self.home.name})"

    def install(self) -> int:
        if not WIN:
            print("install is for Windows. On Linux or macOS, start `bh --home <home> service run` at login: a "
                  "systemd user unit, a launchd agent, or cron @reboot")
            return 2
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = pyw if pyw.exists() else Path(sys.executable)
        tr = f'"{exe}" -m body_harness.cli --home "{self.home}" service run'
        r = subprocess.run(["schtasks", "/Create", "/F", "/TN", self.task_name(), "/SC", "MINUTE", "/MO", "10",
                            "/TR", tr], capture_output=True, text=True)
        print((r.stdout or r.stderr).strip())
        if r.returncode == 0:
            subprocess.run(["schtasks", "/Run", "/TN", self.task_name()], capture_output=True)
            print(f"task '{self.task_name()}' starts the supervisor now, within 10 minutes of logon, and again if "
                  f"it stops")
        return r.returncode


def default_home() -> Path:
    return Path(os.environ.get("BH_HOME") or Path.home() / ".body-harness")


def same_home(a: Path | None, b: Path) -> bool:
    """Whether a run's --home (None: it named none, so the default) is this home."""
    norm = lambda x: os.path.normcase(os.path.abspath(str(x)))
    return norm(a if a is not None else default_home()) == norm(b)


def parse_run(cmd: str) -> tuple[Path | None, list[str]] | None:
    """A command line's `bh [--home H] run ...`: (its home or None, the seats it names and --all), or None if it is
    not a bh run. The home is read from the line as written, so a quoted path with spaces survives."""
    words = cmd.replace('"', " ").split()
    bh = next((i for i, w in enumerate(words) if Path(w).stem.lower() == "bh" or w == "body_harness.cli"), None)
    if bh is None or "run" not in words[bh + 1:] or "service" in words[bh + 1:]:
        return None
    m = re.search(r'--home(?:=|\s+)(?:"([^"]+)"|\'([^\']+)\'|(\S+))', cmd)
    home = Path(next(g for g in m.groups() if g)) if m else None
    rest = words[words.index("run", bh + 1) + 1:]
    return home, [x for x in rest if not x.startswith("-") or x == "--all"]


def bh_runs() -> list[tuple[int, Path | None, list[str]]]:
    """Every running `bh run ...` on this machine (bh, bh.exe or python -m body_harness.cli): [(pid, home, names)]."""
    if WIN:
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'body_harness|bh(\\.exe)?' } | "
              "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout
    else:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True).stdout
        out = "\n".join(ln.strip().replace(" ", "\t", 1) for ln in out.splitlines())
    rows = []
    for line in out.splitlines():
        if "\t" not in line:
            continue
        pid, cmd = line.split("\t", 1)
        run = parse_run(cmd)
        if run:
            rows.append((int(pid), *run))
    return rows


def kill(pid: int) -> None:
    if WIN:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)
    else:
        try:
            os.killpg(pid, 15)
        except OSError:
            pass


def main(home: Path, cmd: str, names: list[str], log: str | None = None) -> int:
    from .seat import Seat
    svc = Service(home)
    if cmd == "run":
        return svc.supervise()
    if cmd in ("add", "remove"):
        if len(names) != 1:
            print(f"usage: bh service {cmd} <seat>", file=sys.stderr)
            return 2
        n, c = names[0], svc.conf()
        seats = c.setdefault("seats", {})
        if cmd == "add":
            if not Seat(home, n).exists:
                print(f"no seat {n}; bh seats lists them", file=sys.stderr)
                return 2
            seats[n] = {"log": log} if log else {}
        elif seats.pop(n, None) is None:
            print(f"{n} is not kept")
            return 1
        svc.save_conf(c)
        print(f"{n}: {'kept' if cmd == 'add' else 'no longer kept'}; a running supervisor acts within {POLL}s"
              + ("" if svc.state_p.exists() else " (none is running: bh service run, or bh service install)"))
        return 0
    if cmd == "status":
        svc.status()
        return 0
    if cmd == "stop":
        svc.dir.mkdir(parents=True, exist_ok=True)
        svc.stop_p.touch()
        print("asked the supervisor to stop (an installed task starts it again within 10 minutes; uninstall to keep "
              "it down)")
        return 0
    if cmd == "install":
        return svc.install()
    if cmd == "uninstall":
        r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", svc.task_name()], capture_output=True, text=True)
        print((r.stdout or r.stderr).strip())
        return r.returncode
    return 2
