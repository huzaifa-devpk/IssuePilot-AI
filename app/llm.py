"""LLM providers. Every provider must serve a model of at most 8B parameters (hackathon rule)."""
import json
import os
import re

import httpx

SYSTEM = ("You are IssuePilot, a patient open-source maintainer helping a beginner start on a GitHub issue. "
          "Use simple words. Only mention files from the provided list. Reply with ONE JSON object and nothing else.")

SCHEMA = ('{"summary": "2-3 plain sentences", "requirements": ["what must change / expected behaviour"], '
          '"why_files": {"<exact path from list>": "one sentence on why it matters"}, '
          '"implementation_plan": ["short imperative step"], "testing_checklist": ["something to verify"]}')


def provider_info() -> dict:
    return {
        "default": os.getenv("LLM_PROVIDER", "ollama"),
        "ollama": {"model": os.getenv("OLLAMA_MODEL", "qwen3:4b"), "ready": True},
        "groq": {"model": os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"), "ready": bool(os.getenv("GROQ_API_KEY"))},
        "openrouter": {"model": os.getenv("OPENROUTER_MODEL", ""),
                       "ready": bool(os.getenv("OPENROUTER_API_KEY") and os.getenv("OPENROUTER_MODEL"))},
    }


def build_prompt(issue: dict, files: list[dict]) -> str:
    blocks = "\n\n".join(f"### {f['path']}\n```\n{f['snippet']}\n```" for f in files)
    return (f"ISSUE TITLE: {issue['title']}\nLABELS: {', '.join(issue['labels']) or 'none'}\n"
            f"ISSUE BODY:\n{issue['body'][:2500]}\n\nCANDIDATE FILES (ranked, most relevant first):\n{blocks}\n\n"
            f"Return JSON with exactly this shape:\n{SCHEMA}\n"
            "Give 3-6 plan steps and 3-5 checklist items. Never invent file paths.")


def _extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            return json.loads(m.group(0))
        raise


def _strs(v) -> list[str]:
    return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []


def normalise(raw: dict, paths: list[str]) -> dict:
    why = raw.get("why_files") if isinstance(raw.get("why_files"), dict) else {}
    return {
        "summary": str(raw.get("summary", "")).strip(),
        "requirements": _strs(raw.get("requirements")),
        "why_files": {p: str(why.get(p, "")).strip() for p in paths},
        "implementation_plan": _strs(raw.get("implementation_plan")),
        "testing_checklist": _strs(raw.get("testing_checklist")),
    }


class LLMError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


async def _ollama(client: httpx.AsyncClient, prompt: str, model: str) -> str:
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    body = {
        "model": model, "stream": False, "format": "json", "think": False, "keep_alive": "10m",
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        "options": {"temperature": 0.2, "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "4096")), "num_predict": 900},
    }
    try:
        r = await client.post(f"{host}/api/chat", json=body, timeout=600)
        if r.status_code == 400 and "think" in r.text:  # older Ollama without the `think` option
            body.pop("think")
            r = await client.post(f"{host}/api/chat", json=body, timeout=600)
    except httpx.ConnectError:
        raise LLMError(f"Cannot reach Ollama at {host}. Start it with `ollama serve`, then `ollama pull {model}`.")
    if r.status_code == 404:
        raise LLMError(f"Model '{model}' is not installed. Run `ollama pull {model}`.")
    if r.status_code >= 400:
        raise LLMError(f"Ollama error {r.status_code}: {r.text[:200]}")
    return r.json()["message"]["content"]


async def _openai_compatible(client, prompt, model, url, key, name) -> str:
    if not key or not model:
        raise LLMError(f"{name} is not configured. Set its API key and model in .env.", 400)
    body = {"model": model, "temperature": 0.2, "max_tokens": 900, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}
    hdr = {"Authorization": f"Bearer {key}"}
    r = await client.post(url, json=body, headers=hdr, timeout=120)
    if r.status_code == 400 and "response_format" in r.text:
        body.pop("response_format")
        r = await client.post(url, json=body, headers=hdr, timeout=120)
    if r.status_code == 429:
        raise LLMError(f"{name} rate limit hit. Wait a minute and retry.", 429)
    if r.status_code >= 400:
        raise LLMError(f"{name} error {r.status_code}: {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"]


async def generate(client: httpx.AsyncClient, issue: dict, files: list[dict], provider: str, model: str | None) -> dict:
    prompt = build_prompt(issue, files)
    info = provider_info()
    if provider not in ("ollama", "groq", "openrouter"):
        raise LLMError(f"Unknown provider '{provider}'.", 400)
    model = model or info[provider]["model"]
    if provider == "ollama":
        text = await _ollama(client, prompt, model)
    elif provider == "groq":
        text = await _openai_compatible(client, prompt, model, "https://api.groq.com/openai/v1/chat/completions",
                                        os.getenv("GROQ_API_KEY"), "Groq")
    else:
        text = await _openai_compatible(client, prompt, model, "https://openrouter.ai/api/v1/chat/completions",
                                        os.getenv("OPENROUTER_API_KEY"), "OpenRouter")
    try:
        data = _extract_json(text)
    except (json.JSONDecodeError, ValueError):
        data = {"summary": text.strip()[:600]}  # keep the raw answer visible instead of failing the demo
    report = normalise(data, [f["path"] for f in files])
    report["_meta"] = {"provider": provider, "model": model}
    return report
