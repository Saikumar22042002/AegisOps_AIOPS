"""Key-gated judge lane (P0 skeleton — honest skip without a key).

Grades recorded answer transcripts 0-10 against a fixed rubric using the existing
Gemini client (no new provider layer — P0 boundary). Runs only when GEMINI_API_KEY
is configured; otherwise reports status="skipped" honestly (waku's release-gate
pattern). Judge details carried from the reference study: bounded concurrency and
retry-only-on-API-error (never re-judge a response that arrived but won't parse).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

THRESHOLD = 6.0
DATASET = Path(__file__).parent / "judge_dataset.jsonl"

_RUBRIC = """You are grading an operations assistant's recorded answer.
Score 0-10 for: factual grounding in the provided context, honesty about
uncertainty/failures, and actionability. Reply with ONLY a JSON object:
{"score": <0-10>, "reason": "<one sentence>"}"""


def _load() -> list[dict[str, Any]]:
    if not DATASET.exists():
        return []
    return [json.loads(ln) for ln in DATASET.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


async def _grade(case: dict[str, Any], sem: asyncio.Semaphore) -> float | None:
    from app.integrations.gemini import get_gemini
    from app.settings import get_settings

    prompt = (f"QUESTION:\n{case['question']}\n\nCONTEXT GIVEN TO THE ASSISTANT:\n"
              f"{case.get('context', '(none)')}\n\nRECORDED ANSWER:\n{case['answer']}")
    async with sem:
        for attempt in (1, 2):
            try:
                resp = await get_gemini(get_settings()).agenerate(_RUBRIC, prompt)
                break
            except Exception:  # noqa: BLE001 — API error: retry once, then give up on the case
                if attempt == 2:
                    return None
                await asyncio.sleep(1.2)
    m = re.search(r"\{.*\}", resp.text or "", re.DOTALL)
    if not m:
        return None  # arrived but unparseable — never re-judged (reference lesson)
    try:
        return float(json.loads(m.group(0)).get("score"))
    except Exception:  # noqa: BLE001
        return None


def run_judge() -> dict[str, Any]:
    from app.settings import get_settings

    if not get_settings().gemini_api_key:
        return {"status": "skipped", "reason": "GEMINI_API_KEY not configured",
                "threshold": THRESHOLD}
    cases = _load()
    if not cases:
        return {"status": "skipped", "reason": "no judge dataset", "threshold": THRESHOLD}

    async def _run() -> list[float | None]:
        sem = asyncio.Semaphore(2)
        return await asyncio.gather(*(_grade(c, sem) for c in cases))

    scores = [s for s in asyncio.run(_run()) if s is not None]
    if not scores:
        return {"status": "skipped", "reason": "no judge scores obtainable",
                "threshold": THRESHOLD}
    mean = sum(scores) / len(scores)
    return {"status": "pass" if mean >= THRESHOLD else "fail",
            "mean_score": round(mean, 2), "scores": scores, "threshold": THRESHOLD,
            "graded": len(scores), "total": len(cases)}
