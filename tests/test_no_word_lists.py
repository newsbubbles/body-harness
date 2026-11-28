"""No new word lists in the harness.

The harness reads what a world declares (affordances and their params, schemas, refusals, event kinds) and never
guesses at what a world calls things. A literal list of words, or a regex of alternative words, is how a guess gets
in: one case gets fixed, the next world's wording is missed, and what the model meant is quietly changed.

This test finds, in src/, every collection of two or more word-like string literals (dicts only when kept at module
level as a table) and every string that lists alternative words the way a regex does or matches letters of one alphabet only
([a-z]), and checks each against
tests/word_lists.json. A new one fails, and so does a changed one (a word added to an old list). To pass, it must be
added to word_lists.json with a reason, and that diff is the review. Kinds are "contract" (names the engine or the
harness itself defines: ops, event kinds, tool params, output fields, config keys, Python syntax) or "debt" (a guess
at world content or wording, listed in docs/audit-string-conditionals.md, to be removed). Never add debt.
"""

import ast
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "body_harness"
ALLOWED = ROOT / "tests" / "word_lists.json"
WORD = re.compile(r"^[A-Za-z][A-Za-z_' -]{0,30}$")
# a string that lists words to tell apart, as a regex does: (?:hey|oi|hi), will|next|then, bad arguments|no tool
ALTERNATION = re.compile(r"[A-Za-z]{2,}[A-Za-z' ?]*\|[A-Za-z' ?]*[A-Za-z]{2,}")
# a letter class of one alphabet: text is data in any script, and [a-z] drops "Zoë", "Ægir" and "李明"
ASCII_LETTERS = re.compile(r"\[\^?(?:a-z|A-Z)(?:A-Z|a-z)?")


def _words(node: ast.AST) -> list[str] | None:
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        items = node.elts
    elif isinstance(node, ast.Dict):
        items = [k for k in node.keys if k is not None]
    else:
        return None
    vals = [x.value for x in items if isinstance(x, ast.Constant) and isinstance(x.value, str)]
    if len(vals) >= 2 and len(vals) == len(items) and all(WORD.match(v) for v in vals):
        return vals
    return None


def _strings(node: ast.AST) -> str:
    """The literal text of a string expression, joined across concatenation and f-string parts."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _strings(node.left) + _strings(node.right)
    return ""


def found() -> dict[str, dict[str, str]]:
    """Every word list and word alternation in src: {key: {"file", "line", "sample"}}. The key is the file and a hash
    of the words, so moving a list keeps its key and changing a word does not."""
    out: dict[str, dict[str, str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # a dict built inline is the harness's own output; a dict of words kept at module level is a table
        tables = {id(n.value) for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None}
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        inner = {id(c) for n in ast.walk(tree) if isinstance(n, (ast.JoinedStr, ast.BinOp)) for c in ast.walk(n)
                 if c is not n}  # a part of a joined string is read with the whole
        for node in ast.walk(tree):
            words = None if isinstance(node, ast.Dict) and id(node) not in tables else _words(node)
            if words is None and isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)) and \
                    id(node) not in docs and id(node) not in inner:
                text = _strings(node)
                if text and (ALTERNATION.search(text) or ASCII_LETTERS.search(text)):
                    words = [text]
            if words is None and isinstance(node, ast.Compare) and isinstance(node.ops[0], (ast.In, ast.NotIn)) and \
                    isinstance(node.left, ast.Constant) and isinstance(node.left.value, str) and \
                    WORD.match(node.left.value) and not isinstance(node.comparators[0], (ast.Set, ast.List,
                                                                                         ast.Tuple, ast.Dict)):
                words = [f"'{node.left.value}' in {ast.unparse(node.comparators[0])[:40]}"]  # a word looked for in text
            if words is None:
                continue
            body = "\n".join(sorted(words))
            key = f"{rel}:{hashlib.sha1(body.encode()).hexdigest()[:10]}"
            out[key] = {"file": rel, "line": str(node.lineno), "sample": ", ".join(sorted(words))[:120]}
    return out


def test_every_word_list_in_the_harness_is_reviewed():
    allowed = json.loads(ALLOWED.read_text(encoding="utf-8"))
    new = {k: v for k, v in found().items() if k not in allowed}
    assert not new, (
        "word lists or word alternations not in tests/word_lists.json. The harness reads what the world declares; it "
        "does not guess what worlds call things (AGENTS.md, 'No word lists'). Read the world's declared data instead, "
        "or ask the engine for it. Only a list of the engine's or the harness's own names may be added to "
        "word_lists.json, with a reason, for review:\n" +
        "\n".join(f"  {k}  {v['file']}:{v['line']}  [{v['sample']}]" for k, v in sorted(new.items())))


def test_the_allowlist_has_no_stale_entries():
    allowed = json.loads(ALLOWED.read_text(encoding="utf-8"))
    gone = sorted(set(allowed) - set(found()))
    assert not gone, "remove these from tests/word_lists.json, they are no longer in src: " + ", ".join(gone)


def test_every_allowed_entry_says_why():
    for key, entry in json.loads(ALLOWED.read_text(encoding="utf-8")).items():
        assert entry.get("kind") in ("contract", "debt") and entry.get("why"), key
