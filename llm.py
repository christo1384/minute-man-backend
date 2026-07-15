"""
LLM extraction layer for Minute Man — provider-agnostic.

extract_minutes(transcript, provider) sends the transcript to the chosen model
with MINUTE_MAN_SYSTEM_PROMPT and returns a parsed dict:
    { "hazards": [...], "actions": [...], "decisions": [...] }

Providers:
  - "anthropic" : Claude (needs ANTHROPIC_API_KEY — paid, ~1-2c per transcript)
  - "gemini"    : Google Gemini (needs GEMINI_API_KEY — free tier available at
                  https://aistudio.google.com, no credit card; plenty for
                  toolbox-talk volumes)
  - "openai"    : GPT   (needs OPENAI_API_KEY — paid)
  - "demo"      : no API key required — a simple keyword-based extractor so the
                  whole pipeline can be run and tested offline before you add a
                  real key. NOT for production use.

Model IDs are read from env so you can bump them without touching code.
"""

import os
import json
import re

from prompts import MINUTE_MAN_SYSTEM_PROMPT

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
_gemini_model_cache: str | None = None  # discovered working model, kept for the process lifetime


def _discover_gemini_model(key: str) -> str:
    """Ask Google which models this key can use and pick the best flash one.

    Runs only if the configured model 404s (Google retires model IDs over
    time). Keeps the app working with zero config changes.
    """
    import urllib.request

    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=100",
        headers={"x-goog-api-key": key},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    names = [
        m["name"].removeprefix("models/")
        for m in data.get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]
    flash = [n for n in names if "flash" in n]
    # Prefer plain "gemini-N-flash" (newest first), then -latest aliases, then any flash.
    import re
    plain = sorted(
        (n for n in flash if re.fullmatch(r"gemini-[0-9.]+-flash", n)),
        key=lambda n: [int(x) for x in re.findall(r"\d+", n)],
        reverse=True,
    )
    for candidate in (plain, [n for n in flash if n.endswith("-latest")], flash, names):
        if candidate:
            return candidate[0]
    raise ValueError("No usable Gemini model found for this API key.")

_EMPTY = {"hazards": [], "actions": [], "decisions": []}


# ---------------------------------------------------------------------------
# JSON parsing — models occasionally wrap JSON in prose or code fences.
# ---------------------------------------------------------------------------
def _parse_json(text: str) -> dict:
    """Pull the first JSON object out of a model response, defensively."""
    if not text:
        return dict(_EMPTY)
    # strip ```json ... ``` fences if present
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text
    # else grab the outermost braces
    if not fenced:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return dict(_EMPTY)
    # normalise shape
    return {
        "hazards": data.get("hazards", []) or [],
        "actions": data.get("actions", []) or [],
        "decisions": data.get("decisions", []) or [],
    }


# ---------------------------------------------------------------------------
# Provider calls
# ---------------------------------------------------------------------------
def _call_anthropic(transcript: str) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2000,
        system=MINUTE_MAN_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": transcript}],
    )
    return "".join(block.text for block in msg.content if block.type == "text")


def _call_gemini(transcript: str) -> str:
    """Google Gemini via plain REST (no SDK needed — stdlib only).

    Free-tier friendly: get a key at https://aistudio.google.com (no card).
    Model is read from GEMINI_MODEL (default gemini-2.5-flash); bump the env
    var if Google retires the model — no code change needed.
    """
    import urllib.request
    import urllib.error

    global _gemini_model_cache
    key = os.environ["GEMINI_API_KEY"]  # KeyError -> 400 "missing key" upstream

    def _post(model: str):
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        )
        body = json.dumps({
            "system_instruction": {"parts": [{"text": MINUTE_MAN_SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": transcript}]}],
            # Ask Gemini for raw JSON so _parse_json has nothing to strip.
            "generationConfig": {"response_mime_type": "application/json"},
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = _post(_gemini_model_cache or GEMINI_MODEL)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # Configured model retired by Google — discover a current one and retry.
            _gemini_model_cache = _discover_gemini_model(key)
            try:
                data = _post(_gemini_model_cache)
            except urllib.error.HTTPError as exc2:
                detail = exc2.read().decode("utf-8", "replace")[:300]
                raise ValueError(f"Gemini API error {exc2.code}: {detail}") from exc2
        else:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ValueError(f"Gemini API error {exc.code}: {detail}") from exc
    try:
        parts = data["candidates"][0]["content"]["parts"]
        # Newer Gemini models can return several parts, including "thought"
        # (reasoning) parts — keep only real answer text and join the rest.
        text = "".join(
            p.get("text", "")
            for p in parts
            if isinstance(p, dict) and not p.get("thought")
        )
        if not text.strip():  # everything was thoughts? fall back to all text
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if not text.strip():
            raise ValueError(f"Gemini returned no text. Raw: {str(data)[:400]}")
        return text
    except (KeyError, IndexError) as exc:
        raise ValueError(f"Unexpected Gemini response shape: {str(data)[:400]}") from exc


def _call_openai(transcript: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": MINUTE_MAN_SYSTEM_PROMPT},
            {"role": "user", "content": transcript},
        ],
    )
    return resp.choices[0].message.content


# ---------------------------------------------------------------------------
# Demo (keyless) extractor — deterministic, offline. For plumbing tests only.
# ---------------------------------------------------------------------------
def _call_demo(transcript: str) -> str:
    t = transcript.lower()
    hazards, actions, decisions = [], [], []

    if "mud" in t or "slippery" in t or "crushed metal" in t:
        hazards.append({
            "hazard": "Slippery muddy ground after overnight rain (near-falls reported)",
            "control": "Rope off worst area and lay crushed metal; seal the main walkway first",
            "control_tier": "3. Isolation",
            "compliance_note": "Barrier + surface treatment before relying on care (HSWA safe access).",
        })
        actions.append({"who": "Unassigned — needs an owner", "what": "Lay crushed metal on the worst slip areas", "by_when": "Today"})
        decisions.append("Rope off and metal the muddy area today.")
    if "hi-vis" in t or "raincoat" in t or "wet weather" in t:
        hazards.append({
            "hazard": "Cold / wet exposure with only hi-vis vests",
            "control": "Issue proper wet-weather gear from stores",
            "control_tier": "6. PPE",
            "compliance_note": "PPE is the last line of defence — confirm stores are stocked.",
        })
    if "hot work" in t or "fuel tank" in t or "welding" in t:
        hazards.append({
            "hazard": "Hot work — welding on an old fuel tank (fire / explosion)",
            "control": "Signed hot-work permit; dedicated fire watch with extinguisher; 30-min after-check",
            "control_tier": "5. Administrative Controls",
            "compliance_note": "Verify tank is purged / gas-free before striking an arc (not stated — confirm).",
        })
        decisions.append("Hot work proceeds only under the signed permit with fire watch in place.")
    if "grinder" in t or "guard" in t or "tag" in t:
        hazards.append({
            "hazard": "Bench grinder with a loose guard — tag-out removed and machine used anyway",
            "control": "Machine tagged out; no use until maintenance repairs the guard",
            "control_tier": "3. Isolation",
            "compliance_note": "Defeating an isolation tag is a serious breach — brief all crew and contractors.",
        })
        actions.append({"who": "Unassigned — needs an owner", "what": "Get maintenance to repair the bench-grinder guard", "by_when": "Today"})
        decisions.append("Grinder stays tagged out until the guard is repaired.")

    if not hazards:
        hazards.append({
            "hazard": "No specific hazard auto-detected by the demo extractor",
            "control": "Review the transcript manually and add hazards",
            "control_tier": "5. Administrative Controls",
            "compliance_note": "Demo provider only — add a real API key for full extraction.",
        })

    return json.dumps({"hazards": hazards, "actions": actions, "decisions": decisions})


_PROVIDERS = {
    "anthropic": _call_anthropic,
    "gemini": _call_gemini,
    "openai": _call_openai,
    "demo": _call_demo,
}


def extract_minutes(transcript: str, provider: str | None = None) -> dict:
    """Extract structured minutes from a transcript using the chosen provider."""
    provider = (provider or os.getenv("DEFAULT_PROVIDER", "anthropic")).lower()
    caller = _PROVIDERS.get(provider)
    if caller is None:
        raise ValueError(f"Unknown provider '{provider}'. Use anthropic, gemini, openai, or demo.")
    raw = caller(transcript)
    return _parse_json(raw)
