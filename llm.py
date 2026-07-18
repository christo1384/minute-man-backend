"""
LLM extraction layer for Minute Man — provider-agnostic. v3: two templates.

extract_minutes(transcript, provider, template) sends the transcript to the
chosen model with the template's system prompt and returns a parsed dict:

  template="safety"  (default — v2.2 behaviour plus incidents/summary):
      { "incidents": [...], "summary": "...",
        "hazards": [...], "actions": [...], "decisions": [...] }

  template="general" (new in v3):
      { "summary": "...", "topics": [...],
        "actions": [...], "decisions": [...] }

Providers:
  - "anthropic" : Claude (needs ANTHROPIC_API_KEY — paid, ~1-2c per transcript)
  - "gemini"    : Google Gemini (needs GEMINI_API_KEY — free tier available at
                  https://aistudio.google.com, no credit card; plenty for
                  toolbox-talk volumes)
  - "openai"    : GPT   (needs OPENAI_API_KEY — paid)
  - "demo"      : no API key required — deterministic canned output for BOTH
                  templates so the whole pipeline (engine, DB save, register,
                  exports) can be tested offline. NOT for production use.

Model IDs are read from env so you can bump them without touching code.
"""

import os
import json
import re

from prompts import PROMPTS_BY_TEMPLATE, SAFETY_SYSTEM_PROMPT

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
    plain = sorted(
        (n for n in flash if re.fullmatch(r"gemini-[0-9.]+-flash", n)),
        key=lambda n: [int(x) for x in re.findall(r"\d+", n)],
        reverse=True,
    )
    for candidate in (plain, [n for n in flash if n.endswith("-latest")], flash, names):
        if candidate:
            return candidate[0]
    raise ValueError("No usable Gemini model found for this API key.")


# Per-template empty shapes — what a request yields when nothing was extracted.
_EMPTY_BY_TEMPLATE = {
    "safety": {"incidents": [], "summary": "", "hazards": [], "actions": [], "decisions": []},
    "general": {"summary": "", "topics": [], "actions": [], "decisions": []},
}


# ---------------------------------------------------------------------------
# JSON parsing — models occasionally wrap JSON in prose or code fences.
# ---------------------------------------------------------------------------
def _parse_json(text: str, template: str = "safety") -> dict:
    """Pull the first JSON object out of a model response, defensively,
    and normalise it to the requested template's shape."""
    empty = {k: (list(v) if isinstance(v, list) else v)
             for k, v in _EMPTY_BY_TEMPLATE[template].items()}
    if not text:
        return empty
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
        return empty
    # normalise shape: only the template's own keys, with safe defaults
    if template == "general":
        return {
            "summary": str(data.get("summary", "") or ""),
            "topics": data.get("topics", []) or [],
            "actions": data.get("actions", []) or [],
            "decisions": data.get("decisions", []) or [],
        }
    return {
        "incidents": data.get("incidents", []) or [],
        "summary": str(data.get("summary", "") or ""),
        "hazards": data.get("hazards", []) or [],
        "actions": data.get("actions", []) or [],
        "decisions": data.get("decisions", []) or [],
    }


# ---------------------------------------------------------------------------
# Provider calls — each takes the transcript and the template's system prompt.
# ---------------------------------------------------------------------------
def _call_anthropic(transcript: str, system_prompt: str) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": transcript}],
    )
    return "".join(block.text for block in msg.content if block.type == "text")


def _call_gemini(transcript: str, system_prompt: str) -> str:
    """Google Gemini via plain REST (no SDK needed — stdlib only).

    Free-tier friendly: get a key at https://aistudio.google.com (no card).
    Model is read from GEMINI_MODEL; bump the env var if Google retires the
    model — no code change needed.
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
            "system_instruction": {"parts": [{"text": system_prompt}]},
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


def _call_openai(transcript: str, system_prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": transcript},
        ],
    )
    return resp.choices[0].message.content


# ---------------------------------------------------------------------------
# Demo (keyless) extractor — deterministic, offline. For plumbing tests only.
# v3: returns realistic canned output for BOTH templates.
# ---------------------------------------------------------------------------
def _demo_safety(transcript: str) -> str:
    t = transcript.lower()
    incidents, hazards, actions, decisions = [], [], [], []

    # --- incidents (past events reviewed) ---
    if "nearly went over" in t or "near-fall" in t or "near miss" in t or "nearly" in t:
        incidents.append({
            "description": "Near-falls reported on the slippery ground before start of work",
            "severity": "near miss",
            "outcome": "Reviewed at the talk — revealed the slippery-ground hazard; area to be roped off and surfaced (see hazards).",
        })
    if "off sick" in t:
        incidents.append({
            "description": "A worker was off sick after cold/wet exposure last week",
            "severity": "not stated",
            "outcome": "Reviewed — proper wet-weather gear to be issued from stores (see hazards).",
        })
    if "pulled the tag" in t or "tag was pulled" in t or ("tag" in t and "kept using" in t):
        incidents.append({
            "description": "Isolation tag removed from the bench grinder and the machine used anyway",
            "severity": "near miss",
            "outcome": "Reviewed as a serious breach — grinder re-tagged, all crew briefed; guard repair raised (see hazards).",
        })

    # --- hazards / actions / decisions (v2.2 keyword logic, unchanged) ---
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
    if not incidents:
        # Keep the demo realistic without inventing: a generic reviewed-nothing marker
        # would break the "never invent" rule, so incidents stays empty — but for the
        # canned demo transcripts above at least one keyword always matches.
        pass

    # --- summary (built only from the structured content above) ---
    bits = []
    if incidents:
        bits.append(f"The talk reviewed {len(incidents)} recent incident{'s' if len(incidents) != 1 else ''}.")
    bits.append(f"{len(hazards)} hazard{'s' if len(hazards) != 1 else ''} and the agreed controls were discussed.")
    if actions:
        bits.append(f"{len(actions)} action{'s were' if len(actions) != 1 else ' was'} assigned with timeframes.")
    if decisions:
        bits.append(f"{len(decisions)} decision{'s were' if len(decisions) != 1 else ' was'} recorded.")
    bits.append("A responsible person must review and sign off this record.")
    summary = " ".join(bits)

    return json.dumps({
        "incidents": incidents,
        "summary": summary,
        "hazards": hazards,
        "actions": actions,
        "decisions": decisions,
    })


def _demo_general(transcript: str) -> str:
    t = transcript.lower()
    topics, actions, decisions = [], [], []

    if "steel" in t or "delivery" in t or "programme" in t or "schedule" in t:
        topics.append("Programme / delivery dates")
        decisions.append("Revised delivery date accepted; programme updated accordingly.")
    if "budget" in t or "cost" in t or "invoice" in t or "quote" in t:
        topics.append("Budget and costs")
        actions.append({"who": "Unassigned — needs an owner", "what": "Circulate the updated cost summary", "by_when": "Friday"})
    if "client" in t or "variation" in t:
        topics.append("Client / variations")
        actions.append({"who": "Unassigned — needs an owner", "what": "Confirm the variation scope with the client in writing", "by_when": "This week"})
    if "hire" in t or "recruit" in t or "staff" in t or "apprentice" in t:
        topics.append("Staffing")
        decisions.append("Proceed with advertising the open role.")
    if "workshop" in t or "capacity" in t or "machine" in t:
        topics.append("Workshop capacity")

    if not topics:
        topics.append("General discussion")
        actions.append({"who": "Unassigned — needs an owner",
                        "what": "Review the transcript manually and record actions",
                        "by_when": "Not specified — needs a date"})

    para1 = ("The meeting covered " + ", ".join(topics[:-1]) + (" and " if len(topics) > 1 else "")
             + topics[-1] + ".")
    para2 = []
    if decisions:
        para2.append(f"{len(decisions)} decision{'s were' if len(decisions) != 1 else ' was'} made.")
    if actions:
        para2.append(f"{len(actions)} action{'s were' if len(actions) != 1 else ' was'} assigned for follow-up.")
    summary = para1 + ("\n\n" + " ".join(para2) if para2 else "")

    return json.dumps({
        "summary": summary,
        "topics": topics,
        "actions": actions,
        "decisions": decisions,
    })


def _call_demo(transcript: str, system_prompt: str) -> str:
    # The demo provider keys off the prompt it was handed — same routing the
    # real providers get, zero API keys needed.
    if system_prompt is PROMPTS_BY_TEMPLATE["general"]:
        return _demo_general(transcript)
    return _demo_safety(transcript)


_PROVIDERS = {
    "anthropic": _call_anthropic,
    "gemini": _call_gemini,
    "openai": _call_openai,
    "demo": _call_demo,
}


def _demo_custom_additions(result: dict, template_spec: dict) -> dict:
    """v5: the keyless demo provider honours an uploaded template's custom
    columns and sections with deterministic canned values, so the whole
    template pipeline is testable with zero API keys."""
    from template_engine import spec_custom_parts, _snake

    per_core, customs = spec_custom_parts(template_spec)
    for kind in ("hazards", "actions"):
        cols = per_core.get(kind)
        if cols:
            for row in result.get(kind, []):
                row["custom"] = {c["key"]: f"Demo value for {c['label']}" for c in cols}
    if customs:
        result["custom_sections"] = {
            _snake(s.get("title", "custom")): [
                {c["key"]: f"Demo {c['label']}" for c in s.get("columns", [])}
            ]
            for s in customs
        }
    return result


def extract_minutes(transcript: str, provider: str | None = None,
                    template: str = "safety",
                    template_spec: dict | None = None) -> dict:
    """Extract structured minutes from a transcript using the chosen provider
    and template ("safety" — the default, matching v2.2 — or "general").

    v5: when `template_spec` (an uploaded template's TemplateSpec) is given,
    the prompt is assembled dynamically — curated core rules + the template's
    custom fields/sections behind the injection guard — and the parsed result
    carries per-row `custom` objects and a top-level `custom_sections`."""
    template = (template or "safety").lower()
    if template not in PROMPTS_BY_TEMPLATE:
        raise ValueError(f"Unknown template '{template}'. Use safety or general.")
    provider = (provider or os.getenv("DEFAULT_PROVIDER", "anthropic")).lower()
    caller = _PROVIDERS.get(provider)
    if caller is None:
        raise ValueError(f"Unknown provider '{provider}'. Use anthropic, gemini, openai, or demo.")

    if template_spec:
        from template_engine import build_prompt_for_template

        prompt = build_prompt_for_template(template_spec)
        has_hazards = any(s.get("kind") == "hazards"
                          for s in template_spec.get("sections", []))
        base_template = "safety" if has_hazards else "general"
        if provider == "demo":
            raw = _demo_safety(transcript) if has_hazards else _demo_general(transcript)
            result = _parse_json(raw, base_template)
            return _demo_custom_additions(result, template_spec)
        raw = caller(transcript, prompt)
        result = _parse_json(raw, base_template)
        # keep whatever custom content the model returned (defensively)
        try:
            data = json.loads(raw) if isinstance(raw, str) else {}
        except Exception:
            data = {}
        if isinstance(data, dict) and isinstance(data.get("custom_sections"), dict):
            result["custom_sections"] = data["custom_sections"]
        return result

    raw = caller(transcript, PROMPTS_BY_TEMPLATE[template])
    return _parse_json(raw, template)
