"""P0 defect regressions (D1, D2, D5, D7, F-10) — one test pins each closed defect."""

from __future__ import annotations

import re
from pathlib import Path

from app.agents.provider_errors import classify_provider_error, suggest_retry

APP_DIR = Path(__file__).parents[1] / "app"


# ── D1: classify → suggest_retry now CONNECT (the kinds agree) ────────────────────────────

def test_d1_classified_bad_location_reaches_the_one_click_retry():
    """The cross-boundary case both old suites missed: the classifier's real output must
    drive a retry suggestion. Before P0, classify emitted `bad_location` while the retry
    matcher wanted `bad_region` — each side's tests passed, the seam was dead."""
    f = classify_provider_error(
        "Error: Invalid value for field 'zone': 'us-central9-z'. Unknown zone.")
    assert f is not None and f.kind == "bad_location"
    r = suggest_retry(f, "create a vm named web-1, region=us-central9", cloud="gcp",
                      current_region="us-central9")
    assert r is not None, "classified failure must produce the one-click retry"
    assert r["kind"] == "bad_location" and r["to"] != "us-central9"


def _code_lines(path: Path):
    """Source lines with comments stripped — literal scans must not trip on the
    explanatory comments that document the removals themselves."""
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        yield i, line.split("#", 1)[0]


def test_d1_no_bad_region_literal_survives_in_app():
    hits = [f"{p.name}:{i}" for p in APP_DIR.rglob("*.py")
            for i, code in _code_lines(p) if "bad_region" in code]
    assert not hits, f"stale 'bad_region' kind literal in code at: {hits}"


# ── D5: the phantom `applying` status is gone from every read ─────────────────────────────

def test_d5_no_applying_status_literal_in_app():
    """`applying` was read in 6 predicates and written by nothing. After removal, no
    status literal may reference it anywhere in app code (equivalence protocol:
    P0 review §10 — zero writers verified before removal)."""
    pattern = re.compile(r"[\"']applying[\"']")
    hits = [f"{p.name}:{i}" for p in APP_DIR.rglob("*.py")
            for i, code in _code_lines(p) if pattern.search(code)]
    assert not hits, f"phantom 'applying' status still read at: {hits}"


# ── F-10: the approval-wait metric can actually record ────────────────────────────────────

def test_f10_chat_module_has_module_level_select():
    """`resolve_approval_core` uses `select` for the approval-wait metric; the import was
    function-local only, so the metric block always died with a swallowed NameError."""
    import app.api.chat as chat_mod
    assert hasattr(chat_mod, "select"), "module-level sqlalchemy select import missing"


# ── D2: the dead lazy model-resolution path is gone ───────────────────────────────────────

def test_d2_dead_model_resolution_removed():
    from app.integrations.gemini import GeminiLLM
    assert not hasattr(GeminiLLM, "_ensure_model")
    assert not hasattr(GeminiLLM, "_resolve")
    assert not hasattr(GeminiLLM, "astream_text")  # D7: zero callers


# ── D7: dead code deletions hold ──────────────────────────────────────────────────────────

def test_d7_agents_llm_generate_deleted():
    from app.agents import llm
    assert not hasattr(llm, "generate")


def test_d7_provider_seam_is_catalog_only():
    """The validate-only seam keeps its honest catalog surface; the never-called dispatch
    passthroughs are gone (they return with the real P1 provider layer)."""
    from app.integrations.llm.gemini_provider import GeminiProvider
    for dead in ("astream", "agenerate", "aembed"):
        assert not hasattr(GeminiProvider, dead)
    for alive in ("serves", "models", "default_model", "enabled"):
        assert hasattr(GeminiProvider, alive)


def test_d7_run_ended_at_is_written_at_every_terminal_transition():
    """Source-level pin (behavioral variant needs live PG → container tier): every
    terminal status writer also stamps ended_at."""
    src = (APP_DIR / "api" / "chat.py").read_text(encoding="utf-8")
    assert src.count("run.ended_at = datetime.now(") >= 3, (
        "_persist_result / _force_terminal / _mark_cancelled must all stamp ended_at")
