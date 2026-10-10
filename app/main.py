import asyncio
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

load_dotenv()
from . import github, llm, scoring  # noqa: E402  (after load_dotenv so env is populated)

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
CANDIDATES = 12   # files downloaded for full scoring
TOP_N = 5         # files shown to the user
LLM_FILES = 4     # files whose snippets go to the model (keeps prompts small for 3-4B models)

app = FastAPI(title="IssuePilot AI")


class AnalyzeRequest(BaseModel):
    url: str
    provider: str | None = None
    model: str | None = None


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/api/config")
def config():
    return llm.provider_info()


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    t0 = time.time()
    try:
        owner, repo, number = github.parse_issue_url(req.url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            issue, (branch, paths, truncated) = await asyncio.gather(
                github.fetch_issue(client, owner, repo, number), github.fetch_tree(client, owner, repo))
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            msg = {404: "Issue or repository not found (is it public?).",
                   403: "GitHub rate limit reached. Add GITHUB_TOKEN to .env and retry.",
                   401: "GITHUB_TOKEN in .env is invalid."}.get(code, f"GitHub error {code}.")
            raise HTTPException(502 if code != 404 else 404, msg)
        except httpx.HTTPError:
            raise HTTPException(502, "Could not reach GitHub. Check your internet connection.")

        sig = scoring.extract_signals(issue["title"], issue["body"])
        # Stage 1: cheap path-only pass over the whole tree to choose what to download
        shortlist = sorted(paths, key=lambda p: scoring.prelim_score(p, sig), reverse=True)[:CANDIDATES]
        # Stage 2: download shortlisted files, compute the full K/N/C/R score
        contents = await asyncio.gather(*(github.fetch_file(client, owner, repo, branch, p) for p in shortlist))
        scored = [scoring.full_score(p, c, sig) | {"_content": c} for p, c in zip(shortlist, contents)]
        scored.sort(key=lambda s: s["score"], reverse=True)
        top = scored[:TOP_N]
        for s in top:
            s["snippet"] = scoring.best_snippet(s["_content"], sig)

        provider = req.provider or llm.provider_info()["default"]
        try:
            report = await llm.generate(client, issue, top[:LLM_FILES], provider, req.model)
        except llm.LLMError as e:
            raise HTTPException(e.status, str(e))

    meta = report.pop("_meta")
    files = [{"path": s["path"], "score": s["score"], "parts": s["parts"], "why": report["why_files"].get(s["path"], "")}
             for s in top]
    return {
        "issue": {k: issue[k] for k in ("title", "labels", "state", "url")},
        "repo": f"{owner}/{repo}", "tree_size": len(paths), "tree_truncated": truncated,
        "signals": {"keywords": list(sig["keywords"])[:8], "identifiers": sig["identifiers"][:8]},
        "report": {k: report[k] for k in ("summary", "requirements", "implementation_plan", "testing_checklist")},
        "files": files, "weights": scoring.WEIGHTS, "meta": meta | {"seconds": round(time.time() - t0, 1)},
    }
