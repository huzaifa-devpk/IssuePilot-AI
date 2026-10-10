"""Thin GitHub REST client: issue, repo tree, raw file contents."""
import os
import re

import httpx

URL_RE = re.compile(r"github\.com/([^/\s]+)/([^/\s#?]+)/(?:issues|pull)/(\d+)")
SKIP_DIRS = {"node_modules", "dist", "build", ".git", "vendor", "__pycache__", ".github", "docs-site", "target"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".lock", ".min.js", ".map", ".woff", ".woff2",
            ".ttf", ".pdf", ".zip", ".gz", ".mp4", ".jar", ".class", ".pyc", ".snap", ".csv"}
API = "https://api.github.com"


def parse_issue_url(url: str) -> tuple[str, str, int]:
    m = URL_RE.search(url or "")
    if not m:
        raise ValueError("Enter a link like https://github.com/owner/repo/issues/123")
    return m.group(1), m.group(2).removesuffix(".git"), int(m.group(3))


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "IssuePilot-AI"}
    if tok := os.getenv("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {tok}"
    return h


def _useful(path: str) -> bool:
    parts = path.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return False
    return not any(path.lower().endswith(e) for e in SKIP_EXT)


async def fetch_issue(client: httpx.AsyncClient, owner: str, repo: str, number: int) -> dict:
    r = await client.get(f"{API}/repos/{owner}/{repo}/issues/{number}", headers=_headers())
    r.raise_for_status()
    d = r.json()
    return {
        "title": d.get("title", ""),
        "body": d.get("body") or "",
        "labels": [l["name"] for l in d.get("labels", [])],
        "state": d.get("state"),
        "url": d.get("html_url"),
    }


async def fetch_tree(client: httpx.AsyncClient, owner: str, repo: str) -> tuple[str, list[str], bool]:
    r = await client.get(f"{API}/repos/{owner}/{repo}", headers=_headers())
    r.raise_for_status()
    branch = r.json()["default_branch"]
    r = await client.get(f"{API}/repos/{owner}/{repo}/git/trees/{branch}", params={"recursive": "1"}, headers=_headers())
    r.raise_for_status()
    d = r.json()
    paths = [t["path"] for t in d.get("tree", []) if t["type"] == "blob" and _useful(t["path"])]
    return branch, paths, bool(d.get("truncated"))


async def fetch_file(client: httpx.AsyncClient, owner: str, repo: str, branch: str, path: str, limit: int = 24000) -> str:
    """Raw content (does not consume the REST API rate limit). Returns '' on any failure."""
    try:
        r = await client.get(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}")
        return r.text[:limit] if r.status_code == 200 else ""
    except httpx.HTTPError:
        return ""
