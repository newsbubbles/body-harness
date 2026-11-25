"""Build the body-harness GitHub page: one static page per language from src/page.html and i18n/<lang>.json.

    python site/build.py [out_dir]      (default: site/_site)

English is the base: a key missing from a translation falls back to the English text. No dependencies.
"""
import hashlib
import html
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).parent
SITE_URL = "https://newsbubbles.github.io/body-harness/"
# code: (html lang, path under the site root); English sits at the root
LANGS = {
    "en": ("en", ""),
    "es": ("es", "es/"),
    "de": ("de", "de/"),
    "fr": ("fr", "fr/"),
    "ro": ("ro", "ro/"),
    "ja": ("ja", "ja/"),
    "zh": ("zh-Hans", "zh/"),
    "ar": ("ar", "ar/"),
}
FONTS = "family=Spectral:wght@500;600;700&family=Public+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500"
EXTRA_FONTS = {
    "ja": "&family=Noto+Sans+JP:wght@400;500;700&family=Noto+Serif+JP:wght@600;700",
    "zh": "&family=Noto+Sans+SC:wght@400;500;700&family=Noto+Serif+SC:wght@600;700",
    "ar": "&family=Noto+Sans+Arabic:wght@400;500;700&family=Noto+Naskh+Arabic:wght@600;700",
}


def merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        return {k: merge(base[k], over[k]) if k in over else base[k] for k in base} | {k: v for k, v in over.items() if k not in base}
    return over if over not in (None, "") or base in (None, "") else base


def missing(base, over, at=""):
    """Keys English has that a translation lacks, so a half-done translation is visible at build time."""
    if not isinstance(base, dict):
        return []
    if not isinstance(over, dict):
        return [at or "(root)"]
    return [m for k in base for m in (missing(base[k], over[k], f"{at}{k}.") if k in over else [at + k])]


def lookup(d, dotted):
    for part in dotted.split("."):
        d = d[part]
    return d


def attr(s):
    return html.escape(re.sub(r"<[^>]+>", "", s), quote=True)


def blocks(t, code, lang_map):
    """The page parts that are lists or need building, keyed by their {{@name}} in the template."""
    _, path = LANGS[code]
    root = "../" if path else ""
    chapters = "".join(
        f'<li class="chapter"><p class="when">{c["when"]}</p><div class="stack"><h3>{c["title"]}</h3>'
        + "".join(f'<p class="prose">{p}</p>' for p in c["body"]) + "</div></li>"
        for c in t["story"]["chapters"])
    cards = lambda items: "".join(
        f'<div class="guard"><h4>{g["h"]}</h4><p>{g["p"]}</p>' + (f'<p class="case">{g["case"]}</p>' if g.get("case") else "") + "</div>"
        for g in items)
    num = ' class="num"'
    model_th = "".join(f'<th{num if i in (1, 2, 3, 4) else ""}>{h}</th>' for i, h in enumerate(t["numbers"]["models"]["th"]))
    claims = "".join(
        f'<tr><td>{r["c"]}</td><td>{r["m"]}</td><td><span class="pill p-{r["v"]}">{t["claims"]["verdicts"][r["v"]]}</span></td></tr>'
        for r in t["claims"]["rows"])
    compare = "".join(f'<tr><td><b>{r["a"]}</b></td><td>{r["b"]}</td><td>{r["c"]}</td></tr>' for r in t["compare"]["rows"])
    note = t["footer"].get("translation") or ""
    chart = {
        "savings": {k: t["numbers"]["savings"][k] for k in ("phases", "tip")},
        "models": {k: t["numbers"]["models"][k] for k in ("slept_legend", "slept_tip_before", "slept_tip_after", "not_run", "cost_tip", "notes")},
        "jev": {"names": t["numbers"]["small"]["jev_names"], "tip": t["numbers"]["small"]["jev_tip"]},
    }
    return {
        "root": root,
        "html_lang": LANGS[code][0],
        "self": path,
        "canonical": SITE_URL + path,
        "alternates": "\n".join(
            f'<link rel="alternate" hreflang="{LANGS[c][0]}" href="{SITE_URL + LANGS[c][1]}">' for c in LANGS)
            + f'\n<link rel="alternate" hreflang="x-default" href="{SITE_URL}">',
        "fonts": "https://fonts.googleapis.com/css2?" + FONTS + EXTRA_FONTS.get(code, "") + "&display=swap",
        "lang_options": "".join(
            f'<option value="{c}" lang="{LANGS[c][0]}"{" selected" if c == code else ""}>{lang_map[c]["name"]}</option>' for c in LANGS),
        "stats": "".join(f'<div class="stat"><b>{s["v"]}</b><span>{s["t"]}</span></div>' for s in t["stats"]),
        "chapters": chapters,
        "guards": cards(t["turn"]["guards"]),
        "ideas": cards(t["ideas"]["items"]),
        "steps": "".join(f"<li>{s}</li>" for s in t["imagination"]["steps"]),
        "dojo": "".join(f"<li>{s}</li>" for s in t["imagination"]["dojo"]),
        "model_th": model_th,
        "claims_th": "".join(f"<th>{h}</th>" for h in t["claims"]["th"]),
        "claims": claims,
        "compare_th": "".join(f"<th>{h}</th>" for h in t["compare"]["th"]),
        "compare": compare,
        "sources": "".join(f"<li>{s}</li>" for s in t["sources"]["items"]),
        "translation_note": f'<p class="small muted">{note}</p>' if note else "",
        "chart_strings": json.dumps(chart, ensure_ascii=False).replace("</", "<\\/"),
        "lang_map": json.dumps({c: {"path": LANGS[c][1], "dir": lang_map[c]["dir"], "suggest": lang_map[c]["suggest"]}
                                for c in LANGS}, ensure_ascii=False).replace("</", "<\\/"),
    }


def render(template, t, built):
    def sub(m):
        key = m.group(1)
        if key.startswith("@"):
            return built[key[1:]]
        value = lookup(t, key)
        # head tags take plain text; the body keeps the strings' inline markup
        return attr(value) if key.startswith("meta.") or key in ("nav.languages", "nav.sections") or key.endswith("aria") else value
    return re.sub(r"\{\{\s*([@\w.]+)\s*\}\}", sub, template)


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "_site"
    template = (HERE / "src" / "page.html").read_text(encoding="utf-8")
    en = json.loads((HERE / "i18n" / "en.json").read_text(encoding="utf-8"))
    texts = {}
    for code in LANGS:
        f = HERE / "i18n" / f"{code}.json"
        if code == "en" or not f.exists():
            texts[code] = en
            continue
        own = json.loads(f.read_text(encoding="utf-8"))
        gaps = [g for g in missing(en, own) if g != "footer.translation"]
        if gaps:
            print(f"{code}: {len(gaps)} keys fall back to English: {', '.join(gaps[:6])}")
        texts[code] = merge(en, own)
    texts["en"] = en
    # Pages lets browsers keep site.css/site.js for 10 minutes; a content hash in the link makes a new build load at once
    v = hashlib.sha256(b"".join((HERE / "src" / a).read_bytes() for a in ("site.css", "site.js"))).hexdigest()[:10]
    for code, (_, path) in LANGS.items():
        t = texts[code]
        page = render(template, t, {**blocks(t, code, texts), "v": v})
        left = re.findall(r"\{\{[^}]*\}\}", page)
        if left:
            raise SystemExit(f"{code}: unfilled {left[:5]}")
        dest = out / path / "index.html"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(page, encoding="utf-8", newline="\n")
    for asset in ("site.css", "site.js", "icon.svg"):
        shutil.copyfile(HERE / "src" / asset, out / asset)
    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"built {len(LANGS)} languages into {out}")


if __name__ == "__main__":
    main()
