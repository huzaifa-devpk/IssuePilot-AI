"""Custom relevance scoring (no CRUD, no embeddings).

Score(file) = w1*K + w2*N + w3*C + w4*R, each component normalised to 0..1
  K  keyword match   - weighted issue keywords found in the file's content
  N  name/path match - weighted issue keywords found in the file path (basename counts extra)
  C  code context    - exact identifiers from the issue (`backticked`, camelCase, snake_case) found in content
  R  issue reference - the issue explicitly names the file, its basename, or its directory
"""
import math
import re
from collections import Counter

WEIGHTS = {"K": 0.25, "N": 0.30, "C": 0.25, "R": 0.20}

STOP = set("""the and for with that this from have has not but are was were will would should could can into when then than
them they their there here what which while where about after before also just only some any all each other more most
such use used using uses get gets got set sets add adds added make makes need needs want wants like see issue issues
bug fix fixed please thanks thank error errors problem expected actual steps reproduce behavior behaviour current
currently instead does doesn work works working file files code line lines one two new old via per its""".split())

PATH_RE = re.compile(r"[\w./-]*[\w-]+\.[A-Za-z]{1,5}\b|[\w-]+/[\w./-]+")
IDENT_RE = re.compile(r"`([^`\n]{2,60})`|\b([a-z]+[A-Z]\w+|[A-Z][a-z]+[A-Z]\w+|[a-z]+_[a-z_0-9]+)\b")


def split_ident(s: str) -> list[str]:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    return [t.lower() for t in re.split(r"[^A-Za-z0-9]+", s) if len(t) > 2 and t.lower() not in STOP]


def extract_signals(title: str, body: str) -> dict:
    kw: Counter = Counter()
    for t in split_ident(title):
        kw[t] += 3
    for t in split_ident(body[:6000]):
        kw[t] += 1
    keywords = dict(kw.most_common(15))
    idents = set()
    for m in IDENT_RE.finditer(f"{title}\n{body[:6000]}"):
        ident = (m.group(1) or m.group(2) or "").strip()
        if len(ident) > 3 and " " not in ident:
            idents.add(ident)
    refs = {m.group(0).lower().strip("./") for m in PATH_RE.finditer(f"{title}\n{body[:6000]}")}
    return {"keywords": keywords, "identifiers": sorted(idents)[:20], "refs": sorted(refs)}


def name_score(path: str, keywords: dict) -> float:
    if not keywords:
        return 0.0
    base = split_ident(path.rsplit("/", 1)[-1].rsplit(".", 1)[0])
    rest = split_ident(path.rsplit("/", 1)[0]) if "/" in path else []
    total = sum(keywords.values())
    hit = sum(w * 1.5 for k, w in keywords.items() if k in base) + sum(w for k, w in keywords.items() if k in rest and k not in base)
    return min(1.0, hit / (total * 0.35))


def ref_score(path: str, refs: list) -> float:
    p = path.lower()
    base = p.rsplit("/", 1)[-1]
    best = 0.0
    for r in refs:
        if p == r or p.endswith("/" + r):
            return 1.0
        if "/" not in r and base == r:
            best = max(best, 0.9)
        elif "/" in r and p.startswith(r.rstrip("/") + "/"):
            best = max(best, 0.5)
    return best


def keyword_score(content: str, keywords: dict) -> float:
    if not content or not keywords:
        return 0.0
    toks = Counter(split_ident(content))
    total = sum(keywords.values())
    hit = sum(w * min(1.0, math.log1p(toks[k]) / math.log1p(5)) for k, w in keywords.items())
    return min(1.0, hit / total)


def context_score(content: str, identifiers: list) -> float:
    if not content or not identifiers:
        return 0.0
    found = sum(1 for i in identifiers if i in content)
    return min(1.0, found / min(len(identifiers), 6))


def prelim_score(path: str, sig: dict) -> float:
    """Cheap pass over the whole tree (no file content yet) to pick which files to download."""
    return 0.6 * name_score(path, sig["keywords"]) + 0.4 * ref_score(path, sig["refs"])


def full_score(path: str, content: str, sig: dict) -> dict:
    c = {
        "K": keyword_score(content, sig["keywords"]),
        "N": name_score(path, sig["keywords"]),
        "C": context_score(content, sig["identifiers"]),
        "R": ref_score(path, sig["refs"]),
    }
    total = sum(WEIGHTS[k] * v for k, v in c.items())
    return {"path": path, "score": round(total, 4), "parts": {k: round(WEIGHTS[k] * v, 4) for k, v in c.items()}}


def best_snippet(content: str, sig: dict, max_chars: int = 1100) -> str:
    """Window of lines around the densest keyword/identifier hit, so small models see relevant code."""
    lines = content.splitlines()
    if len(content) <= max_chars:
        return content
    needles = list(sig["identifiers"]) + list(sig["keywords"])
    scores = [sum(1 for n in needles if n.lower() in ln.lower()) for ln in lines]
    centre = max(range(len(lines)), key=lambda i: scores[i]) if lines else 0
    lo, hi = max(0, centre - 12), min(len(lines), centre + 18)
    return "\n".join(lines[lo:hi])[:max_chars]
