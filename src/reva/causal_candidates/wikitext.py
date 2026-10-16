"""Minimal, offset-preserving wikitext renderer for sentence extraction (agent CAUSCAND).

Not a MediaWiki parser. It turns an article's wikitext into rendered paragraphs (plain text) while
keeping, for every rendered character, the position of the wikitext character it came from, and the
wikilinks (``[[target|anchor]]``) as spans of the rendered text. That is all the causal-candidate
extractor needs: a sentence (rendered, for reading), its exact wikitext slice (verbatim source), and the
article's *own* wikilinks inside it (the only way an argument is ever linked to an entity).

What is removed: comments, ``<ref>`` (paired and self-closing), ``<math>``/``<gallery>``/``<timeline>``
/... blocks, tables (``{| ... |}``), files and categories (``[[File:...]]``, ``[[Fichier:...]]``,
``[[Catégorie:...]]``), templates (``{{...}}``) except a few inline ones rendered as text without links
(dates, ``lang``, ``nobr``, ``convert``, ``s``/``-s`` centuries, ordinals, ...), bold/italic quotes,
HTML tags (inner text kept). Unknown templates inside running text are dropped and counted per sentence
(``templates_dropped``) so the reader knows the rendering may miss words; the verbatim wikitext slice is
always kept next to the rendering.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

NON_ARTICLE_NS = {
    # namespaces whose [[...]] are not wikilinks to articles (en + fr), lower-case
    "file",
    "image",
    "fichier",
    "category",
    "catégorie",
    "categorie",
    "media",
    "média",
    "template",
    "modèle",
    "help",
    "aide",
    "wikipedia",
    "wikipédia",
    "wp",
    "portal",
    "portail",
    "user",
    "utilisateur",
    "talk",
    "discussion",
    "special",
    "spécial",
    "wikt",
    "wiktionary",
    "wiktionnaire",
    "s",
    "q",
    "n",
    "b",
    "v",
    "voy",
    "commons",
    "c",
    "d",
    "wikidata",
    "mw",
    "meta",
    "m",
    "species",
    "w",
    "project",
    "projet",
    "draft",
    "brouillon",
    "module",
    "mediawiki",
    "annexe",
    "appendix",
    "référence",
}

DROP_BLOCK_TAGS = (
    "math",
    "gallery",
    "timeline",
    "syntaxhighlight",
    "source",
    "score",
    "graph",
    "mapframe",
    "templatedata",
    "imagemap",
    "chem",
    "ce",
    "hiero",
    "poem",
    "nowiki",
    "pre",
    "references",
    "categorytree",
    "inputbox",
    "noinclude",
)

SKIP_SECTIONS = {
    "references",
    "notes",
    "see also",
    "further reading",
    "external links",
    "bibliography",
    "sources",
    "citations",
    "footnotes",
    "works cited",
    "notes and references",
    "primary sources",
    "secondary sources",
    "notes et références",
    "références",
    "voir aussi",
    "bibliographie",
    "liens externes",
    "articles connexes",
    "annexes",
    "filmographie",
    "dans la culture populaire",
    "in popular culture",
    "gallery",
    "galerie",
    "liste",
    "chronologie",
    "timeline",
}


@dataclass(frozen=True)
class Link:
    """A wikilink of the article: ``target`` as written (before redirects), ``anchor`` as displayed."""

    target: str
    anchor: str
    start: int  # rendered offsets (within the paragraph text)
    end: int
    src_start: int  # wikitext offsets of the whole ``[[...]]`` (+ link trail)
    src_end: int


@dataclass
class Paragraph:
    text: str
    section: str
    links: list[Link]
    src: list[int]  # rendered index -> wikitext offset (len(text) + 1 entries; last = end)
    in_list: bool = False
    dropped_at: list[int] = field(default_factory=list)  # rendered offsets where a template was dropped

    def src_span(self, a: int, b: int) -> tuple[int, int]:
        """Wikitext span of rendered ``[a, b)``, widened to whole ``[[...]]`` of the links it touches."""
        s0 = self.src[a]
        s1 = self.src[b - 1] + 1 if b > a else s0
        for lk in self.links:
            if lk.start < b and lk.end > a:
                s0, s1 = min(s0, lk.src_start), max(s1, lk.src_end)
        return s0, s1

    def links_in(self, a: int, b: int) -> list[Link]:
        return [lk for lk in self.links if lk.start >= a and lk.end <= b]

    def dropped_between(self, a: int, b: int) -> int:
        return bisect_right(self.dropped_at, b) - bisect_left(self.dropped_at, a)


# ----------------------------------------------------------------------------- template rendering

FR_MONTHS = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


def _split_args(body: str) -> list[str]:
    """Split a template body on top-level ``|`` (nested ``{{}}`` and ``[[]]`` kept together)."""
    out, depth_t, depth_l, cur, i = [], 0, 0, [], 0
    while i < len(body):
        two = body[i : i + 2]
        if two == "{{":
            depth_t += 1
            cur.append(two)
            i += 2
            continue
        if two == "}}" and depth_t:
            depth_t -= 1
            cur.append(two)
            i += 2
            continue
        if two == "[[":
            depth_l += 1
            cur.append(two)
            i += 2
            continue
        if two == "]]" and depth_l:
            depth_l -= 1
            cur.append(two)
            i += 2
            continue
        c = body[i]
        if c == "|" and not depth_t and not depth_l:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def plain(text: str) -> str:
    """Wikitext fragment -> plain text (templates dropped, links -> anchors, quotes removed)."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", text, flags=re.S | re.I)
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\{\{[^{}]*\}\}", lambda m: render_template(m.group(0)[2:-2]) or "", text)
    text = re.sub(r"\[\[([^\[\]|]*)\|([^\[\]]*)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\[\]|]*)\]\]", r"\1", text)
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _positional(args: list[str]) -> list[str]:
    return [a.strip() for a in args[1:] if not re.match(r"^\s*[\w -]+\s*=", a)]


def _named(args: list[str]) -> dict[str, str]:
    out = {}
    for a in args[1:]:
        m = re.match(r"^\s*([\w -]+?)\s*=(.*)$", a, flags=re.S)
        if m:
            out[m.group(1).strip().lower()] = m.group(2).strip()
    return out


def render_template(body: str) -> str | None:
    """Inline text for a handful of templates that carry running text; ``None`` = drop."""
    args = _split_args(body)
    name = args[0].strip().lower().replace("_", " ")
    pos = _positional(args)
    named = _named(args)
    if name in ("lang", "langue", "lang-fr", "transl", "transliteration") or name.startswith("lang-"):
        if name.startswith("lang-") and pos:
            return plain(pos[0])
        return plain(pos[1]) if len(pos) >= 2 else (plain(pos[0]) if pos else None)
    if name in (
        "nobr",
        "nowrap",
        "nobreak",
        "small",
        "smaller",
        "abbr",
        "sc",
        "smallcaps",
        "small caps",
        "petites capitales",
        "pc",
        "romain",
        "sic",
        "nb",
        "nobold",
        "noitalic",
        "lien",
        "nom",
        "w",
        "ill",
        "interlanguage link",
        "illm",
        "ill-wd",
        "lien web interne",
    ):
        if name in ("lien", "ill", "illm", "interlanguage link", "ill-wd"):
            txt = named.get("texte") or named.get("fr") or named.get("lt") or (pos[0] if pos else "")
            return plain(txt) or None
        return plain(pos[0]) if pos else None
    if name in ("date", "date-", "date de naissance", "date de décès", "date-trier") and pos:
        return " ".join(plain(p) for p in pos[:3] if p and not p.startswith("dans"))
    if name in ("s", "siècle", "-s", "s-", "sp", "sp-", "-sp", "millénaire", "-millénaire", "s2", "s2-"):
        if pos:
            r = plain(pos[0]) + "e"
            if pos[0].upper() == "I":
                r = "Ier"
            return r if name.endswith("-") or name.startswith("-") else r + (" siècle" if name[0] == "s" else "")
    if name in ("er", "e", "re", "ers", "res", "ème", "eme", "1er", "1re", "nd", "nde", "è"):
        return {"1er": "1er", "1re": "1re"}.get(name, name)
    if name in ("ier", "iie", "iiie", "ive", "ve", "xe") or re.fullmatch(r"[ivxlc]+e", name):
        return name.upper()[:-1] + "e"
    if name in ("nombre", "nb", "unité", "formatnum", "num", "nombre/unité") and pos:
        return " ".join(plain(p) for p in pos[:2])
    if name.startswith("formatnum:"):
        return body.split(":", 1)[1]
    if name in ("convert", "cvt") and pos:
        return " ".join(plain(p) for p in pos[:2])
    if name in ("circa", "c.", "c"):
        return "c. " + (plain(pos[0]) if pos else "")
    if name in ("spaces", "space", "nbsp", "thinsp", "sp", "esp", "espace", "~", "0"):
        return " "
    if name in ("snd", "spaced ndash", "ndash", "–", "mdash", "spaced en dash", "spnd"):
        return " – "
    if name in ("as of",) and pos:
        return "As of " + " ".join(pos[:3])
    if name in ("citation", "quote", "cquote", "guil", "guillemets", "« »") and pos:
        return "« " + plain(pos[0]) + " »" if name in ("citation", "guil", "guillemets", "« »") else plain(pos[0])
    if name in ("ndash", "-", "!", "=", "'", "' \"", "\" '"):
        return {"ndash": "–", "-": "-", "!": "|", "=": "=", "'": "'"}.get(name, "")
    if name in ("us$", "usd", "us dollar", "$", "£", "€", "gbp", "inrconvert") and pos:
        return {"gbp": "£", "£": "£", "€": "€"}.get(name, "US$") + plain(pos[0])
    if name in ("death date and age", "birth date", "death date", "start date", "end date", "birth date and age"):
        return " ".join(p for p in pos[:3] if p.isdigit())
    if name in ("avjc", "av. j.-c.", "av j-c", "-", "bc", "bce"):
        return "av. J.-C." if name.startswith("av") else "BC"
    return None


# ----------------------------------------------------------------------------- masking


def _find_matching(text: str, i: int, open_: str, close: str) -> int:
    """Index just past the ``close`` matching the ``open_`` at ``i`` (nested); ``-1`` if unterminated."""
    depth, j, n = 0, i, len(text)
    lo, lc = len(open_), len(close)
    while j < n:
        if text.startswith(open_, j):
            depth += 1
            j += lo
            continue
        if text.startswith(close, j):
            depth -= 1
            j += lc
            if depth == 0:
                return j
            continue
        j += 1
    return -1


class _Masked:
    """Per-source-character output strings: '' = removed, longer strings = inserted renderings."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.out: list[str] = list(text)
        self.dropped: list[int] = []  # source offsets where a template was dropped

    def blank(self, a: int, b: int) -> None:
        for k in range(max(a, 0), min(b, len(self.out))):
            self.out[k] = ""

    def replace(self, a: int, b: int, s: str) -> None:
        self.blank(a, b)
        if a < len(self.out):
            self.out[a] = s


_TAG_BLOCK_RE = re.compile(r"<(" + "|".join(DROP_BLOCK_TAGS) + r")\b[^>]*?(?:/>|>.*?</\1\s*>)", re.S | re.I)
_REF_RE = re.compile(r"<ref\b[^>]*?/>|<ref\b[^>]*>.*?</ref\s*>", re.S | re.I)
_COMMENT_RE = re.compile(r"<!--.*?(?:-->|$)", re.S)
_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*\b[^<>]*?/?>")
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_QUOTES_RE = re.compile(r"'{2,5}")
_EXT_RE = re.compile(r"\[(?:https?:)?//[^\s\]]+(\s+[^\]]*)?\]")
_LINK_RE = re.compile(r"\[\[([^\[\]|\n]+)(?:\|((?:[^\[\]]|\[(?!\[)|\](?!\]))*))?\]\]([^\W\d_]*)")
_MAGIC_RE = re.compile(r"__[A-ZÉ]+__")


def mask(wikitext: str) -> _Masked:
    m = _Masked(wikitext)
    t = wikitext
    for rx in (_COMMENT_RE, _TAG_BLOCK_RE, _REF_RE, _MAGIC_RE):
        for x in rx.finditer(t):
            m.blank(x.start(), x.end())
    blanked = "".join(c if c else "\x00" for c in m.out)  # offsets preserved
    # templates (top level; nested ones go with their parent)
    i = 0
    while True:
        i = blanked.find("{{", i)
        if i < 0:
            break
        j = _find_matching(blanked, i, "{{", "}}")
        if j < 0:
            m.blank(i, len(blanked))
            break
        body = blanked[i + 2 : j - 2].replace("\x00", "")
        r = render_template(body) if body.count("\n") <= 2 else None
        if r:
            m.replace(i, j, r)
        else:
            m.blank(i, j)
            m.dropped.append(i)
        blanked = blanked[:i] + "\x00" * (j - i) + blanked[j:]
        i = j
    # tables {| ... |}
    i = 0
    while True:
        i = blanked.find("{|", i)
        if i < 0:
            break
        j = _find_matching(blanked, i, "{|", "|}")
        j = len(blanked) if j < 0 else j
        m.blank(i, j)
        blanked = blanked[:i] + "\x00" * (j - i) + blanked[j:]
        i = j
    # files / categories / interwiki: [[ns:...]] with nested brackets
    i = 0
    while True:
        i = blanked.find("[[", i)
        if i < 0:
            break
        head = blanked[i + 2 : i + 40].lstrip(":")
        ns = head.split(":", 1)[0].strip().lower() if ":" in head[:30] else ""
        if ns and (ns in NON_ARTICLE_NS or re.fullmatch(r"[a-z]{2,3}(-[a-z]+)?", ns)):
            j = _find_matching(blanked, i, "[[", "]]")
            j = len(blanked) if j < 0 else j
            m.blank(i, j)
            blanked = blanked[:i] + "\x00" * (j - i) + blanked[j:]
            i = j
        else:
            i += 2
    for x in _EXT_RE.finditer(blanked):  # [url label] -> label
        label = x.group(1)
        if label:
            m.blank(x.start(), x.start(1) + len(label) - len(label.lstrip()))
            m.blank(x.end() - 1, x.end())
        else:
            m.blank(x.start(), x.end())
    for x in _BR_RE.finditer(blanked):
        m.replace(x.start(), x.end(), " ")
    for x in _TAG_RE.finditer(blanked):
        if "\x00" not in x.group(0):
            m.blank(x.start(), x.end())
    for x in _QUOTES_RE.finditer(blanked):
        m.blank(x.start(), x.end())
    return m


# ----------------------------------------------------------------------------- paragraphs


def _norm_target(t: str) -> str:
    t = t.replace("_", " ").strip()
    t = re.sub(r"\s+", " ", t)
    t = t.split("#", 1)[0].strip()
    return t[:1].upper() + t[1:] if t else t


def paragraphs(wikitext: str, *, skip_sections: set[str] = SKIP_SECTIONS) -> list[Paragraph]:
    """Rendered paragraphs (lead = section ``""``) with links and the rendered->wikitext offset map."""
    m = mask(wikitext)
    out_chars = m.out
    text = wikitext
    paras: list[Paragraph] = []
    section = ""
    skip = False
    pos = 0
    lines = text.split("\n")
    dropped = sorted(m.dropped)
    block: list[tuple[int, int, bool]] = []  # (src start, src end, in_list) lines of the current paragraph

    def flush() -> None:
        nonlocal block
        if block and not skip:
            p = _build(m, block, section, dropped)
            if p is not None:
                paras.append(p)
        block = []

    for line in lines:
        a, b = pos, pos + len(line)
        pos = b + 1
        rendered_line = "".join(out_chars[a:b])
        h = re.match(r"^(={2,6})\s*(.*?)\s*\1\s*$", rendered_line.strip())
        if h:
            flush()
            section = re.sub(r"\s+", " ", h.group(2)).strip()
            level = len(h.group(1))
            if level == 2:
                skip = section.lower() in skip_sections
            elif section.lower() in skip_sections:
                skip = True
            continue
        if not rendered_line.strip():
            flush()
            continue
        lm = re.match(r"^[*#:;]+", line)
        if lm:
            flush()
            block = [(a + lm.end(), b, True)]
            flush()
            continue
        block.append((a, b, False))
    flush()
    return paras


def _build(m: _Masked, block: list[tuple[int, int, bool]], section: str, dropped: list[int]) -> Paragraph | None:
    pieces: list[str] = []
    src: list[int] = []
    pending: list[tuple[str, int, int, int]] = []  # (target, anchor src start, link src end, link src start)
    t = m.text
    for k, (a, b, _) in enumerate(block):
        if k:
            pieces.append(" ")
            src.append(a - 1)
        seg = m.out[a:b]
        # a character is "visible source" when masking left it untouched
        visible = "".join(c if c == t[a + i] else "\x00" for i, c in enumerate(seg))
        for x in _LINK_RE.finditer(visible):
            target = x.group(1)
            if "\x00" in target or not target.strip():
                continue
            anchor = x.group(2)
            if anchor is not None and anchor.strip():
                head_end, anchor_a, close_a = x.start(2), x.start(2), x.end(2)
            else:  # [[target]] or pipe trick [[target|]]
                head_end, anchor_a = x.start(1), x.start(1)
                close_a = x.end(1)
                if anchor is not None:
                    seg[x.end(1)] = ""  # the "|"
                    close_a = x.end(1) + 1
            for q in range(x.start(), head_end):
                seg[q] = ""
            if anchor is not None and anchor.strip():
                for q in range(x.start(1), x.start(2)):
                    seg[q] = ""
            seg[close_a] = ""
            seg[close_a + 1] = ""
            pending.append((target, a + anchor_a, a + x.end(), a + x.start()))
        for i, s in enumerate(seg):
            if s:
                pieces.append(s)
                src.extend([a + i] * len(s))
    text = "".join(pieces)
    if not text.strip():
        return None
    links = []
    for target, anchor_src, end_src, start_src in pending:
        r0, r1 = bisect_left(src, anchor_src), bisect_left(src, end_src)
        if r1 > r0:
            links.append(Link(_norm_target(target), text[r0:r1], r0, r1, start_src, end_src))
    src.append((src[-1] + 1) if src else block[0][0])
    drop_r = sorted(bisect_left(src, d) for d in dropped if block[0][0] <= d < block[-1][1])
    return Paragraph(text=text, section=section, links=links, src=src, in_list=block[0][2], dropped_at=drop_r)


# ----------------------------------------------------------------------------- sentences

_ABBR_EN = {
    "mr",
    "mrs",
    "ms",
    "dr",
    "st",
    "jr",
    "sr",
    "vs",
    "c",
    "ca",
    "e.g",
    "i.e",
    "u.s",
    "u.k",
    "gen",
    "lt",
    "col",
    "capt",
    "no",
    "vol",
    "pp",
    "p",
    "fig",
    "approx",
    "est",
    "inc",
    "ltd",
    "co",
    "mt",
    "ft",
    "rev",
    "gov",
    "sen",
    "rep",
    "maj",
    "sgt",
    "adm",
    "cf",
    "al",
    "etc",
    "jan",
    "feb",
    "mar",
    "apr",
    "jun",
    "jul",
    "aug",
    "sep",
    "sept",
    "oct",
    "nov",
    "dec",
    "u.s.s.r",
    "a.d",
    "b.c",
}
_ABBR_FR = {
    "m",
    "mm",
    "mme",
    "mmes",
    "mlle",
    "dr",
    "st",
    "ste",
    "av",
    "apr",
    "j.-c",
    "env",
    "cf",
    "p",
    "pp",
    "vol",
    "chap",
    "art",
    "no",
    "n°",
    "gén",
    "col",
    "cdt",
    "lt",
    "cne",
    "mgr",
    "etc",
    "fig",
    "éd",
    "dir",
    "coll",
    "op",
    "cit",
    "ibid",
    "t",
    "s",
    "vs",
    "sq",
    "jr",
    "c",
    "ca",
    "o",
    "u.s",
}

_SPLIT_RE = re.compile(r"([.!?]+)([\"»”’)\]]*)(\s+)(?=[\"«“(\[]?\s*[A-ZÀ-ÖØ-Þ0-9ÉÈÊÎÔÛÇ])")


def sentences(text: str, lang: str) -> list[tuple[int, int]]:
    """Sentence spans (rendered offsets) of a paragraph. Conservative: abbreviations and initials don't split."""
    abbr = _ABBR_FR if lang == "fr" else _ABBR_EN
    spans, start = [], 0
    for x in _SPLIT_RE.finditer(text):
        if x.group(1) == ".":
            before = text[start : x.start()]
            w = re.search(r"([\w.\-’']+)$", before)
            word = (w.group(1) if w else "").lower().rstrip(".")
            if word in abbr or re.fullmatch(r"[a-zà-ÿ]", word) or re.fullmatch(r"(?:[a-z]\.)+[a-z]?", word):
                continue
            if w and re.fullmatch(r"[A-ZÀ-Þ]", w.group(1)):  # initials "John F. Kennedy"
                continue
        end = x.end(2)
        if text[start:end].strip():
            spans.append((start, end))
        start = x.end(3)
    if text[start:].strip():
        spans.append((start, len(text)))
    out = []
    for a, b in spans:
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if b > a:
            out.append((a, b))
    return out
