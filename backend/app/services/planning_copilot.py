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
        ms = current_config.get("method_selection") or {}
        if ms.get("multiple") is True:
            parts.append("**method equal split** (demand divided across all feasible methods)")
        elif ms.get("elaborate") is True:
            parts.append("**elaborate method selection** (methods scored by commit/inventory/purchase, same weights as variants)")
        else:
            parts.append("**simple method selection** (methods by preference only)")
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

    # Earliest commit time / fastest = single best with full weight on commit_time
    if re.search(
        r"earliest commit|earliest (delivery|fulfillment|time)|fastest|select.*(alternative|option|variant).*earliest|"
        r"prefer.*earliest|commit time.*(earliest|first)|minim(ize|ise) (commit )?time",
        t,
    ):
        return (
            "Using **single best** with **earliest commit time** as the only criterion—the planner will pick the alternative (variant or method when scored) that commits soonest. Re-run plan to apply.",
            {"variant_selection": {**vs, "multiple": False, "score_weights": {"commit_time": 1, "inventory_consumed": 0, "purchase": 0}}},
        )

    if re.search(r"single|one variant|only one|best variant|use one", t):
        return (
            "Got it—I’ll use **single best variant**. The planner will pick one option per demand using earliest commit, "
            "most inventory consumed, and least new purchase. Re-run plan to apply.",
            {"variant_selection": {**vs, "multiple": False}},
        )

    if re.search(
        r"all variants|split|multiple variants|every variant|equal split|divide (across|among)|"
        r"equally distribute|distribute equally|(demand )?among multiple alternatives|multiple alternatives.*(equal|distribute)|"
        r"equal(ly)? (split|distribute)|alternatives if present|treat multiple alternatives equally|workload",
        t,
    ):
        return (
            "Treating multiple alternatives equally for **variants** and **methods**: variants use an equal split across all feasible options (integer quantities when demand is integer); "
            "methods use an equal split across all feasible methods (make/move/buy) when multiple can fulfill a demand. Re-run plan to apply.",
            {"variant_selection": {**vs, "multiple": True}, "method_selection": {**(current_config.get("method_selection") or {}), "multiple": True}},
        )

    if re.search(r"reset|default|clear", t):
        return (
            "Reset to default: **all feasible variants** (equal split). Re-run plan to apply.",
            {"variant_selection": {"multiple": True}},
        )

    # Method selection: elaborate (score by commit/inventory/purchase) vs simple (preference only)
    ms = current_config.get("method_selection") or {}
    if re.search(
        r"elaborate method|score methods?|method selection.*score|methods? by (commit|inventory|purchase)|"
        r"turn on elaborate|use elaborate method|enable elaborate",
        t,
    ):
        return (
            "Turning on **elaborate method selection**. Methods (make/move/buy) will be scored by the same criteria as variants: "
            "earliest commit time, most inventory consumed, least new purchase—using the same score weights you set for variant selection. "
            "Re-run plan to apply (this mode is slower).",
            {"method_selection": {**ms, "elaborate": True}},
        )
    if re.search(
        r"simple method|preference only|methods? by preference|turn off elaborate|disable elaborate|"
        r"use simple method|prefer methods? by preference",
        t,
    ):
        return (
            "Using **simple method selection** (preference only). The planner will pick make/move/buy by the preference number only, "
            "not by scoring. Re-run plan to apply.",
            {"method_selection": {**ms, "elaborate": False}},
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
    system = """You are a friendly planning configuration assistant. Users express their requirements in many different ways. Your job is to infer their intent from whatever wording they use—do not expect or require specific phrases. Be conversational and natural.

Intent → config mapping (interpret any phrasing that conveys the same intent). The same policy applies to both **variants** (BOM/recipe alternatives) and **methods** (make/move/buy): when the user wants to treat multiple alternatives equally, apply to both.

1) **Treat multiple alternatives equally / split demand across options / distribute workload / use all options when multiple exist** (for both variants and methods)
   → variant_selection: { "multiple": true }, method_selection: { "multiple": true }. (Variants: demand split equally across all feasible variants; integer qty when demand is integer. Methods: demand split equally across all feasible methods make/move/buy.)
   In your reply, say you're applying equal split to **both** variants and methods.

2) **Use only one best option per demand** (e.g. single best variant, pick one, prefer one)
   → variant_selection: { "multiple": false }. (Engine picks one by: earliest commit, most inventory used, least purchase.)

3) **Prioritize existing inventory / use what we have / consume more stock** (even if delivery is later)
   → variant_selection: { "multiple": false, "score_weights": { "commit_time": 0, "inventory_consumed": 1, "purchase": 0 } }.

4) **Minimize new purchases / least additional supply / avoid new buy**
   → variant_selection: { "multiple": false, "score_weights": { "commit_time": 0, "inventory_consumed": 0, "purchase": 1 } }.

5) **Earliest delivery / fastest commit**
   → variant_selection: { "multiple": false, "score_weights": { "commit_time": 1, "inventory_consumed": 0, "purchase": 0 } }.

6) **Split among top N variants** (e.g. top 2, top 3), optionally **by least supply / by inventory**
   → variant_selection: { "multiple": true, "top_n": N }. Add "score_weights" only if they specify a criterion (e.g. least supply => purchase: 1).

7) **Score methods (make/move/buy) by same criteria as variants** (slower run)
   → method_selection: { "elaborate": true }. **Use preference only for methods** → method_selection: { "elaborate": false }.

Valid config_update keys: variant_selection (object with optional multiple, top_n, score_weights), method_selection (object with optional elaborate, multiple). score_weights: optional { "commit_time", "inventory_consumed", "purchase" } (numbers, normalized to sum 1).

Respond with valid JSON only, no markdown code fences:
- "reply": string (required). Acknowledge their intent in their words and say what you set. Mention re-run plan if you changed config.
- "config_update": object (optional). Include when you inferred a clear intent. Merge intent with current config where sensible (e.g. set only the keys that change).

Interpret freely: e.g. "equally distribute among alternatives if present", "treat multiple alternatives equally", "handle workload", "when there are multiple ways do X", "split across options", "use all options" → all map to (1). Always mention both variants and methods in your reply for (1). Never say you only handle specific phrases."""
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
        if isinstance(config_update, dict):
            if "variant_selection" in config_update:
                vs_up = config_update["variant_selection"]
                if isinstance(vs_up, dict) and ("multiple" in vs_up or "score_weights" in vs_up or "top_n" in vs_up):
                    return (reply, config_update)
            if "method_selection" in config_update:
                ms_up = config_update["method_selection"]
                if isinstance(ms_up, dict) and ("elaborate" in ms_up or "multiple" in ms_up):
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
