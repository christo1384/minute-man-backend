"""
LLM extraction layer for Minute Man — provider-agnostic.

extract_minutes(transcript, provider) sends the transcript to the chosen model
with MINUTE_MAN_SYSTEM_PROMPT and returns a parsed dict:
    { "hazards": [...], "actions": [...], "decisions": [...] }

Providers:
  - "anthropic" : Claude (needs ANTHROPIC_API_KEY)
  - "openai"    : GPT   (needs OPENAI_API_KEY)
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
    "openai": _call_openai,
    "demo": _call_demo,
}


def extract_minutes(transcript: str, provider: str | None = None) -> dict:
    """Extract structured minutes from a transcript using the chosen provider."""
    provider = (provider or os.getenv("DEFAULT_PROVIDER", "anthropic")).lower()
    caller = _PROVIDERS.get(provider)
    if caller is None:
        raise ValueError(f"Unknown provider '{provider}'. Use anthropic, openai, or demo.")
    raw = caller(transcript)
    return _parse_json(raw)
