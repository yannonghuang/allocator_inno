"""
Planning copilot: interpret natural-language requirements into planning config (e.g. get_preferred_variants).
Uses LLM when OPENAI_API_KEY is set; falls back to rule-based parsing otherwise or on LLM failure.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# Type for config we expose (variant_selection.multiple = False => single best, True/omit => all feasible)
ConfigDict = dict[str, Any]


def _rule_based_parse(message: str, current_config: ConfigDict) -> tuple[str, ConfigDict | None]:
    """Parse user message with rules; return (reply, config_update or None). Conversational tone."""
    t = message.strip().lower()
    vs = current_config.get("variant_selection") or {}
    multi = vs.get("multiple")

    if not t:
        return (
            "You can tell me how you’d like planning to behave—for example prefer a single best variant per demand, "
            "or split demand across all feasible variants. You can also ask to see the current settings.",
            None,
        )

    if re.search(r"show|current|what('s| is)? (my )?config|settings|config", t):
        parts = []
        if multi is False:
            parts.append("**single best variant**")
        else:
            top_n_val = vs.get("top_n")
            if top_n_val is not None and int(top_n_val) >= 1:
                parts.append(f"**top {int(top_n_val)} variants** (split demand among them)")
            else:
                parts.append("**all feasible variants** (equal split)")
        sw = vs.get("score_weights") or {}
        if isinstance(sw, dict):
            p, i, c = sw.get("purchase"), sw.get("inventory_consumed"), sw.get("commit_time")
            if p and not c and not i:
                parts.append("ranked by **least additional supply**")
            elif i and not c and not p:
                parts.append("ranked by **most existing inventory used**")
            elif c and not i and not p:
                parts.append("ranked by **earliest commit time**")
            elif any(sw.get(k) for k in ("commit_time", "inventory_consumed", "purchase")):
                parts.append("with custom **score weights**")
        desc = ", ".join(parts) if parts else "default"
        return (
            f"Right now we’re using {desc}. If you’d like to switch, just say so—e.g. “use single variant” or “split across all”.",
            None,
        )

    # Split among top N variants (e.g. "split among top 2 variants that use the least additional supplies")
    top_n_match = re.search(r"top\s+(\d+)\s+variants?", t)
    if re.search(r"split|among|divide", t) and top_n_match:
        n_val = int(top_n_match.group(1))
        n_val = max(1, min(n_val, 99))
        if re.search(r"least (additional |new )?(supply|supplies|purchase)|minimum additional|minim(ize|ise) (additional |new )?(supply|supplies|purchase)", t):
            return (
                f"Splitting demand among the **top {n_val}** variants ranked by **least additional supply**—the {n_val} variants that need the least new purchase will share the demand equally. Re-run plan to apply.",
                {"variant_selection": {**vs, "multiple": True, "top_n": n_val, "score_weights": {"commit_time": 0, "inventory_consumed": 0, "purchase": 1}}},
            )
        return (
            f"Splitting demand among the **top {n_val}** variants (by current score). Each will get an equal share. Re-run plan to apply.",
            {"variant_selection": {**vs, "multiple": True, "top_n": n_val}},
        )

    # Minimize additional supplies / new purchase = weight on minimizing purchase (single variant)
    if re.search(
        r"minimum additional (supply|supplies)|minim(ize|ise) (additional |new )?(supply|supplies|purchase|buy)|"
        r"least (additional |new )?(supply|supplies|purchase)|use minimum additional",
        t,
    ):
        return (
            "Changing the **score weight distribution** so that **minimizing new purchase** gets all the weight—we’ll prefer variants that need the least additional supply, at the expense of fulfillment time and inventory use. Re-run plan to apply.",
            {"variant_selection": {**vs, "multiple": False, "score_weights": {"commit_time": 0, "inventory_consumed": 0, "purchase": 1}}},
        )

    # Prefer existing inventory / use what we have / minimize buy / most inventory
    if re.search(
        r"inventor(y|ies)|existing (stock|inventory|supply)|use (what we have|existing|current)|"
        r"most (existing |current )?inventory|minim(ize|ise) (buy|purchase)|least (buy|purchase)|"
        r"consume (more |existing )?inventory|prefer (existing |current )?stock",
        t,
    ):
        return (
            "Changing the **score weight distribution** so that **inventory consumption** gets all the weight—at the expense of fulfillment time. "
            "The planner will prefer variants that use the most existing inventory; commit times may be later. "
            "Re-run plan to apply.",
            {"variant_selection": {**vs, "multiple": False, "score_weights": {"commit_time": 0, "inventory_consumed": 1, "purchase": 0}}},
        )

    if re.search(r"single|one variant|only one|best variant|use one", t):
        return (
            "Got it—I’ll use **single best variant**. The planner will pick one option per demand using earliest commit, "
            "most inventory consumed, and least new purchase. Re-run plan to apply.",
            {"variant_selection": {**vs, "multiple": False}},
        )

    if re.search(r"all variants|split|multiple variants|every variant|equal split|divide (across|among)", t):
        return (
            "Using **all feasible variants** with an equal split—demand is divided across every feasible option "
            "(integer quantities when demand is integer). Re-run plan to apply.",
            {"variant_selection": {**vs, "multiple": True}},
        )

    if re.search(r"reset|default|clear", t):
        return (
            "Reset to default: **all feasible variants** (equal split). Re-run plan to apply.",
            {"variant_selection": {"multiple": True}},
        )

    return (
        "I’m not sure I caught that. I can set whether we use a **single best variant** per demand (earliest commit, "
        "most existing inventory used, least purchase) or **split** demand across all feasible variants. What would you like?",
        None,
    )


def _llm_parse(message: str, current_config: ConfigDict, history: list[dict]) -> tuple[str, ConfigDict | None] | None:
    """Call OpenAI to interpret intent. Returns (reply, config_update) or None on failure."""
    try:
        import openai
    except ImportError:
        logger.debug("Planning copilot: openai not installed, using rule-based fallback")
        return None
    api_key = settings.openai_api_key
    if not api_key:
        logger.info("Planning copilot: OPENAI_API_KEY not set on backend, using rule-based fallback. Set OPENAI_API_KEY in the backend environment (e.g. in docker-compose for the backend service) to enable the LLM.")
        return None
    client = openai.OpenAI(api_key=api_key)
    system = """You are a friendly planning configuration assistant. You help users configure how the planning engine chooses BOM/variants when multiple alternatives exist. Be conversational and natural—not robotic or rigid. Acknowledge what they said and respond in a warm, helpful way.

What the system supports:
- variant_selection.multiple: false = single best variant; true or omitted = split demand across variants.
- variant_selection.top_n: optional number (e.g. 2). When set with split mode, use only the top N variants (by score_weights), and split demand equally among those N. So "split among top 2 variants that use the least additional supplies" => multiple: true, top_n: 2, score_weights: {"purchase": 1, "commit_time": 0, "inventory_consumed": 0}.
- variant_selection.score_weights: optional { "commit_time", "inventory_consumed", "purchase" } — numbers that are normalized to sum to 1. They control how we rank variants: **weights are not fixed; they are set dynamically from the user's request.** Default is balanced (commit_time ~0.4, inventory_consumed ~0.35, purchase ~0.25). If the user says "use the most existing inventories" or "prioritize existing inventory", they mean: **change the weight distribution** so that inventory consumption gets more, most, or all of the weight — **at the expense of fulfillment time** (commit_time weight goes down). Set score_weights to e.g. {"commit_time": 0, "inventory_consumed": 1, "purchase": 0}. For "earliest delivery" or "fastest", put weight on commit_time. For "minimize new purchases", "minimum additional supplies", "least additional supply", put weight on purchase: score_weights {"commit_time": 0, "inventory_consumed": 0, "purchase": 1}. When the user says "split among top 2" (or top N) "variants that use the least additional supplies", set multiple: true, top_n: 2 (or N), and score_weights for purchase so we split demand among the top N variants ranked by least purchase.

Respond with valid JSON only, no markdown code fences. Two keys:
- "reply": string (required). Your reply. Sound human: acknowledge their intent, explain that you're changing the weight distribution (or variant mode) and what that means (e.g. "inventory gets all the weight, so we prefer options that use the most existing stock; fulfillment may be later"). Mention re-run plan if you changed something.
- "config_update": object (optional). Include when the user clearly wants a change. Use {"variant_selection": {"multiple": false, "score_weights": {...}}} or similar. For "use the most existing inventories" use score_weights: {"commit_time": 0, "inventory_consumed": 1, "purchase": 0}.

Never give a stiff list of exact phrases; respond naturally."""
    messages = [{"role": "system", "content": system}]
    messages.append({"role": "user", "content": f"Current config: {json.dumps(current_config)}"})
    for h in history[-10:]:
        messages.append({"role": h["role"], "content": h.get("text", h.get("content", ""))})
    messages.append({"role": "user", "content": message})
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.2,
            max_tokens=500,
        )
        text = (resp.choices[0].message.content or "").strip()
        # Remove markdown code blocks if present
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        data = json.loads(text)
        reply = data.get("reply") or "I didn’t quite get that. I can set **single best variant** (earliest commit, use more existing inventory, less new purchase) or **split** demand across all feasible variants—which do you prefer?"
        config_update = data.get("config_update")
        if isinstance(config_update, dict) and "variant_selection" in config_update:
            vs_up = config_update["variant_selection"]
            if isinstance(vs_up, dict) and ("multiple" in vs_up or "score_weights" in vs_up or "top_n" in vs_up):
                return (reply, config_update)
        return (reply, None)
    except Exception as e:
        logger.warning("Planning copilot: LLM call failed (%s), using rule-based fallback", e)
        return None


def planning_copilot_reply(
    message: str,
    current_config: ConfigDict,
    history: list[dict[str, str]] | None = None,
) -> tuple[str, ConfigDict | None]:
    """
    Interpret user message into a reply and optional config update.
    Uses LLM when available; otherwise rule-based. Returns (reply, config_update or None).
    """
    history = history or []
    llm_result = _llm_parse(message, current_config, history)
    if llm_result is not None:
        return llm_result
    return _rule_based_parse(message, current_config)
