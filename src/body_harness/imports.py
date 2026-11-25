"""A seat's import folder: files and folders its operator gives it (notes, digests, memories from before this harness
ran the body), searchable with `records`. Linked by default, so nothing is duplicated and nothing moves.

`bh import SEAT PATH` links PATH into seats/SEAT/import/: a folder as a symlink, or a junction where Windows refuses
symlinks; a file as a symlink, or a pointer file (NAME.bhlink, the target's path in JSON) where it cannot be linked.
--copy copies instead, --unzip unpacks an archive. Links the operator made there by hand are followed too."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

FOLDER = "import"
POINTER = ".bhlink"
MAX_BYTES = 2_000_000
# never read through a link, whatever the folder holds: a grant's token or a secret
SECRET = re.compile(r"(^door\.json$|\.invite\.json$|^\.env)", re.I)
LIMIT = 20


def _link_dir(src: Path, dst: Path) -> str:
    try:
        os.symlink(src, dst, target_is_directory=True)
        return "symlink"
    except OSError:
        if sys.platform != "win32":
            raise
    import _winapi  # a junction needs no privilege on Windows and works across drives
    _winapi.CreateJunction(str(src), str(dst))
    return "junction"


def add(seat_dir: Path, src: str, mode: str = "link", name: str = "") -> str:
    """Link, copy or unzip src into the seat's import folder. Returns one line on what was done."""
    s = Path(src).expanduser().resolve()
    if not s.exists():
        raise ValueError(f"no such file or folder: {src}")
    root = seat_dir / FOLDER
    root.mkdir(parents=True, exist_ok=True)
    base = name or (s.stem if mode == "unzip" else s.name)
    dst = root / base
    if dst.exists() or dst.is_symlink() or (root / (base + POINTER)).exists():
        raise ValueError(f"the import folder already has {base}: bh import SEAT --remove {base} first, or --name")
    if mode == "unzip":
        if not zipfile.is_zipfile(s):
            raise ValueError(f"{s.name} is not a zip archive")
        with zipfile.ZipFile(s) as z:
            bad = [n for n in z.namelist() if n.startswith(("/", "\\")) or ".." in Path(n).parts]
            if bad:
                raise ValueError(f"{s.name} has paths that leave its folder ({bad[0]}): not unpacked")
            z.extractall(dst)
            return f"unpacked {len(z.namelist())} entries into {FOLDER}/{base}"
    if mode == "copy":
        if s.is_dir():
            shutil.copytree(s, dst)
        else:
            shutil.copy2(s, dst)
        return f"copied {s.name} into {FOLDER}/{base}"
    if s.is_dir():
        return f"linked {FOLDER}/{base} to the folder ({_link_dir(s, dst)})"
    try:
        os.symlink(s, dst)
        return f"linked {FOLDER}/{base} to the file (symlink)"
    except OSError:
        (root / (base + POINTER)).write_text(json.dumps({"target": str(s)}), encoding="utf-8")
        return f"linked {FOLDER}/{base} to the file (a pointer: this system refuses file links here)"


def _is_link(p: Path) -> bool:
    return p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())


def remove(seat_dir: Path, name: str) -> str:
    """Unlink an import (the target is never touched), or put a copied one aside in import-removed/."""
    root = seat_dir / FOLDER
    p, ptr = root / name, root / (name + POINTER)
    if ptr.exists() and not p.exists():
        p = ptr
    if not (p.exists() or p.is_symlink()):
        raise ValueError(f"no import {name}: bh import SEAT --list")
    if _is_link(p):
        # unlink, never rmtree: removing a junction's contents would delete the operator's own files
        os.rmdir(p) if p.is_dir() and not p.is_symlink() else p.unlink()
        return f"unlinked {name}; what it pointed at is untouched"
    gone = seat_dir / "import-removed"
    gone.mkdir(exist_ok=True)
    p.rename(gone / f"{p.name}.{int(time.time())}")
    return f"put {name} aside in import-removed/ (nothing deleted)"


def entries(seat_dir: Path) -> list[dict[str, Any]]:
    root = seat_dir / FOLDER
    out = []
    for p in sorted(root.iterdir()) if root.is_dir() else []:
        if p.name.endswith(POINTER):
            out.append({"name": p.name[:-len(POINTER)], "kind": "pointer", "to": _pointer(p)})
        elif _is_link(p):
            out.append({"name": p.name, "kind": "link", "to": os.path.realpath(p)})
        else:
            out.append({"name": p.name, "kind": "folder" if p.is_dir() else "file"})
    return out


def _pointer(p: Path) -> str:
    try:
        return str(json.loads(p.read_text(encoding="utf-8")).get("target") or "")
    except (OSError, ValueError):
        return ""


def files(seat_dir: Path) -> dict[str, Path]:
    """Every readable text file the import folder reaches, by its path as the seat sees it, following links (with
    a guard against loops). Secrets and the seat's own files are never in it."""
    root = seat_dir / FOLDER
    out: dict[str, Path] = {}
    if not root.is_dir():
        return out
    seat = os.path.realpath(seat_dir)
    seen: set[str] = set()

    def keep(rel: str, p: Path) -> None:
        real = os.path.realpath(p)
        if SECRET.search(p.name) or (real.startswith(seat + os.sep) and
                                     not real.startswith(os.path.realpath(root) + os.sep)):
            return
        try:
            if p.stat().st_size > MAX_BYTES or b"\0" in p.read_bytes()[:4096]:
                return
        except OSError:
            return
        out[rel] = p

    def walk(base: Path, prefix: str) -> None:
        real = os.path.realpath(base)
        if real in seen:
            return
        seen.add(real)
        try:
            kids = sorted(base.iterdir())
        except OSError:
            return
        for k in kids:
            rel = f"{prefix}{k.name}"
            if k.name.endswith(POINTER):
                t = Path(_pointer(k))
                rel = rel[:-len(POINTER)]
                if t.is_dir():
                    walk(t, rel + "/")
                elif t.is_file():
                    keep(rel, t)
            elif k.is_dir():
                walk(k, rel + "/")
            elif k.is_file():
                keep(rel, k)

    walk(root, "")
    return out


def records(seat_dir: Path, args: dict[str, Any]) -> str:
    """The seat's tool: match= a regex over every line of every file, or file= one file to read (from_line, lines)."""
    fs = files(seat_dir)
    if not fs:
        return "Nothing has been imported for you: your operator puts files in your seat with bh import."
    f, match = str(args.get("file") or "").strip(), str(args.get("match") or "").strip()
    if f:
        p = fs.get(f) or next((v for k, v in fs.items() if k.endswith("/" + f) or k == f.lstrip("/")), None)
        if p is None:
            near = [k for k in fs if f.lower() in k.lower()][:10]
            return f"no file {f}." + (f" Close: {', '.join(near)}" if near else " records() lists them")
        text = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(args.get("from_line") or 1))
        n = max(1, min(int(args.get("lines") or 120), 400))
        body = "\n".join(f"{i}: {t}" for i, t in enumerate(text[start - 1:start - 1 + n], start))
        more = f"\n(lines {start}-{min(start + n - 1, len(text))} of {len(text)}; from_line= for more)" \
            if len(text) > start + n - 1 or start > 1 else ""
        return f"{f}\n{body}{more}"
    if not match:
        lines = [f"{k} ({max(1, v.stat().st_size // 1000)} KB)" for k, v in list(fs.items())[:80]]
        return f"{len(fs)} files imported for you:\n" + "\n".join(lines) + \
            ("\n..." if len(fs) > 80 else "") + "\nmatch= searches them all; file= reads one."
    try:
        rx = re.compile(match, re.I)
    except re.error as e:
        return f"match= is not a regex Python reads ({e}); plain words work too"
    limit = max(1, min(int(args.get("limit") or LIMIT), 60))
    hits, total = [], 0
    for k, p in fs.items():
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if rx.search(line):
                total += 1
                if len(hits) < limit:
                    hits.append(f"{k}:{i}: {line.strip()[:240]}")
    if not hits:
        return f"nothing in your {len(fs)} imported files matches {match!r}"
    tail = f"\n({total - len(hits)} more; narrow match= or raise limit=)" if total > len(hits) else ""
    return f"{total} lines match {match!r}:\n" + "\n".join(hits) + tail
