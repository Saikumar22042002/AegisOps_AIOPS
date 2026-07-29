"""GW-1 (unit tier) — the progressive-streaming ladder.

A chat platform has no SSE, so an answer is streamed by rewriting one preview message. That is
cheap for the reader and expensive for the API, so every property below is about spending edits
carefully and degrading honestly. The clock and sleep are injected, so throttling and backoff
are asserted deterministically with no real waiting.

Covered:

* **batching** — an edit costs both a minimum interval AND a minimum number of new characters,
  so a fast token stream produces a trickle, not an edit storm;
* **cursor** — `▌` while generating, gone from the final text;
* **progress folding** — step lines appear above the answer in the SAME message, deduped and
  bounded, with no second message;
* **429 backoff** — we wait exactly what the channel asked for and raise the floor interval for
  the rest of the turn; the final answer still lands;
* **edit-failure fallback** — a real failure degrades ONCE: no more edits, one plain final
  message, and the abandoned preview is cleaned up;
* **not-modified is benign** — it must never trip that fallback;
* **drafts** — used when the chat supports them, abandoned mid-turn the moment it says no,
  and never required;
* **truncation / withholding** — the final message goes through `render.outbound`, so it is
  redacted, cut with a deep link, or withheld entirely for High confidentiality.
"""

from __future__ import annotations

from app.gateways import render
from app.gateways.stream import INITIAL_TEXT, MAX_PROGRESS_LINES, PreviewStream
from app.gateways.transport import Button
from app.settings import Settings
from tests.gw_fakes import FakeTransport


def _settings(interval_ms: int = 1000, min_chars: int = 50, **over) -> Settings:
    base = {"aegisops_telegram": "on", "telegram_bot_token": "t",
            "web_public_url": "http://localhost:3000",
            "gateway_edit_min_interval_ms": interval_ms,
            "gateway_edit_min_chars": min_chars}
    base.update(over)
    return Settings(**base)


def _stream(t: FakeTransport, **kw) -> PreviewStream:
    """A PreviewStream on the fake's injected clock, with a sleep that just advances it."""
    async def _sleep(seconds: float) -> None:
        t.advance(seconds)

    return PreviewStream(t, "chat-1", _settings(**kw), clock=t.now, sleep=_sleep)


# ── the preview ──────────────────────────────────────────────────────────────────────────────


async def test_start_posts_one_preview():
    t = FakeTransport()
    s = _stream(t)
    await s.start()
    assert len(t.sent) == 1 and t.sent[0].text == INITIAL_TEXT
    assert s.message_id == "1"


async def test_tokens_are_batched_by_chars_and_interval():
    """Neither gate alone may trigger an edit."""
    t = FakeTransport()
    s = _stream(t, interval_ms=1000, min_chars=50)
    await s.start()

    # Enough characters, but the interval has not elapsed → no edit.
    await s.token("x" * 80)
    assert s.edits == 0

    # Interval elapsed and characters present → exactly one edit.
    t.advance(1.0)
    await s.token("y" * 10)
    assert s.edits == 1

    # More characters but no time → still one.
    await s.token("z" * 80)
    assert s.edits == 1

    # Time HAS elapsed and 80 unrendered characters are waiting → edit #2.
    t.advance(1.0)
    await s.token("!")
    assert s.edits == 2

    # Time elapses but only a couple of new characters arrive → the char gate holds it back.
    t.advance(1.0)
    await s.token("ab")
    assert s.edits == 2

    # A fast burst of 40 small tokens spends ONE edit, not forty: the interval has elapsed, so
    # the first push that clears the char gate goes out and the rest are batched behind it.
    for _ in range(40):
        await s.token("abcde")
    assert s.edits == 3
    for _ in range(40):
        await s.token("fghij")
    assert s.edits == 3          # no further time has elapsed → nothing more is spent
    t.advance(1.0)
    await s.token("k")
    assert s.edits == 4


async def test_cursor_shows_while_generating_and_is_gone_at_the_end():
    t = FakeTransport()
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.token("partial answer")
    assert t.edits[-1].text.endswith(render.STREAM_CURSOR)

    await s.finish("the full answer")
    assert t.edits[-1].text == "the full answer"
    assert render.STREAM_CURSOR not in t.edits[-1].text
    assert len(t.sent) == 1          # never a second message on the happy path


async def test_progress_folds_into_the_same_preview_above_the_answer():
    t = FakeTransport()
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.progress("Routed → cloudops (92%)")
    await s.token("Planning your bucket")
    text = t.edits[-1].text
    assert "· Routed → cloudops (92%)" in text
    assert text.index("Routed") < text.index("Planning")   # progress ABOVE the answer
    assert len(t.sent) == 1                                 # no second message


async def test_progress_is_deduped_and_bounded():
    t = FakeTransport()
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.progress("same")
    await s.progress("same")          # duplicate → no new line, no edit spend
    edits_after_dupe = s.edits
    await s.progress("same")
    assert s.edits == edits_after_dupe

    for i in range(6):
        await s.progress(f"step {i}")
    shown = t.edits[-1].text
    assert shown.count("· ") <= MAX_PROGRESS_LINES
    assert "step 5" in shown and "step 0" not in shown      # newest kept


async def test_empty_progress_is_ignored():
    t = FakeTransport()
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.progress("   ")
    assert s.edits == 0


# ── 429 backoff ──────────────────────────────────────────────────────────────────────────────


async def test_rate_limit_backs_off_by_the_channels_own_delay():
    t = FakeTransport(rate_limit_edit_at=1, rate_limit_retry_after=5.0)
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()

    await s.token("first chunk")           # 429 → recorded, not raised
    assert s.edits == 0

    # Inside the window the channel asked for: no further attempts.
    t.advance(4.0)
    await s.token("more text here")
    assert s.edits == 0

    # Past it: editing resumes.
    t.advance(2.0)
    await s.token("even more text")
    assert s.edits == 1


async def test_rate_limit_raises_the_floor_interval_for_the_rest_of_the_turn():
    t = FakeTransport(rate_limit_edit_at=1, rate_limit_retry_after=4.0)
    s = _stream(t, min_chars=1, interval_ms=100)   # 0.1s floor to begin with
    await s.start()
    t.advance(0.2)                                 # clear the initial 0.1s floor
    await s.token("aaaa")                          # 429 with retry_after=4
    assert s.edits == 0
    t.advance(4.1)
    await s.token("bbbb")
    assert s.edits == 1
    # The floor is now 4s, not 0.1s: a push 0.5s later is deferred.
    t.advance(0.5)
    await s.token("cccc")
    assert s.edits == 1
    t.advance(4.0)
    await s.token("dddd")
    assert s.edits == 2


async def test_finish_waits_out_a_backoff_so_the_answer_still_lands():
    t = FakeTransport(rate_limit_edit_at=1, rate_limit_retry_after=3.0)
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.token("hello")            # 429
    await s.finish("final answer")    # sleeps out the backoff, then edits
    assert t.edits and t.edits[-1].text == "final answer"
    assert len(t.sent) == 1           # no fallback message needed


async def test_finish_retries_once_on_a_429_then_falls_back():
    """If the final edit is ALSO 429'd and the retry fails, one plain message still delivers."""
    t = FakeTransport(rate_limit_edit_at=1, rate_limit_retry_after=1.0, fail_edit_from=1)
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.finish("must arrive")
    assert any("must arrive" in m.text for m in t.sent)


# ── edit-failure fallback ────────────────────────────────────────────────────────────────────


async def test_edit_failure_degrades_once_and_cleans_up_the_preview():
    t = FakeTransport(fail_edit_from=1)
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.token("some text")        # the edit fails → degraded
    await s.token("more text")        # no further edit attempts
    await s.finish("the answer")

    assert s.edits == 0
    assert len(t.sent) == 2 and "the answer" in t.sent[-1].text   # ONE plain final message
    assert t.deleted == [("chat-1", "1")]                          # preview cleaned up


async def test_not_modified_is_benign_and_never_degrades():
    """Telegram rejects a byte-identical edit. Treating that as a failure would tear down a
    perfectly healthy preview."""
    t = FakeTransport(not_modified_at=1)
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.token("abc")              # not-modified → benign
    await s.token("def")              # still editing
    assert s.edits == 1
    await s.finish("done")
    assert t.edits[-1].text == "done"
    assert len(t.sent) == 1 and not t.deleted


async def test_fallback_survives_an_undeletable_preview():
    t = FakeTransport(fail_edit_from=1, fail_delete=True)
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.finish("delivered anyway")
    assert any("delivered anyway" in m.text for m in t.sent)


async def test_a_failed_preview_start_still_delivers_the_answer():
    from app.gateways.transport import TransportError

    t = FakeTransport()

    async def _boom(chat_id, text, *, buttons=None):
        raise TransportError("nope")

    t.send = _boom  # type: ignore[method-assign]
    s = _stream(t)
    await s.start()
    assert s.message_id is None
    # finish() must not raise even when there is nowhere to put the answer.
    await s.finish("answer")


# ── drafts ───────────────────────────────────────────────────────────────────────────────────


async def test_drafts_are_used_when_supported_and_cost_no_edits():
    t = FakeTransport(draft_support=True)
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.token("streaming via draft")
    assert s.draft_pushes == 1 and s.edits == 0
    assert t.drafts and "streaming via draft" in t.drafts[-1][1]

    # The FINAL answer is always a real message, never a draft.
    await s.finish("final")
    assert t.edits and t.edits[-1].text == "final"


async def test_drafts_are_abandoned_mid_turn_when_the_channel_says_no():
    t = FakeTransport(draft_support=True)
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.token("first")
    assert s.draft_pushes == 1

    t.draft_support = False           # the channel stops accepting drafts
    await s.token("second")
    assert s.edits == 1               # seamlessly fell back to the edit ladder
    await s.token("third")
    assert s.edits == 2               # and stays there — no repeated draft attempts
    assert s.draft_pushes == 1


async def test_drafts_are_never_required():
    t = FakeTransport(draft_support=False)
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.token("plain edit path")
    assert s.draft_pushes == 0 and s.edits == 1


# ── the final message goes through render.outbound ───────────────────────────────────────────


async def test_final_answer_is_redacted():
    t = FakeTransport()
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.finish("key AKIAIOSFODNN7EXAMPLE created")
    assert "AKIAIOSFODNN7EXAMPLE" not in t.edits[-1].text
    assert "REDACTED" in t.edits[-1].text


async def test_final_answer_is_truncated_with_a_deep_link():
    t = FakeTransport(max_text_len=300)
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.finish("word " * 500, deep_link="http://localhost:3000/?run=r1")
    text = t.edits[-1].text
    assert len(text) <= 300
    assert "Truncated for chat" in text and "run=r1" in text


async def test_high_confidentiality_is_withheld_from_the_channel():
    t = FakeTransport()
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.finish("-----BEGIN RSA PRIVATE KEY-----abc-----END RSA PRIVATE KEY-----",
                   level="High", deep_link="http://localhost:3000/?run=r1")
    text = t.edits[-1].text
    assert "PRIVATE KEY" not in text
    assert "run=r1" in text


async def test_in_progress_preview_is_bounded_to_the_channel_limit():
    t = FakeTransport(max_text_len=200)
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.token("z" * 5000)
    assert len(t.edits[-1].text) <= 200
    assert t.edits[-1].text.startswith("…")      # the TAIL is what matters while streaming


async def test_finish_carries_buttons_through():
    t = FakeTransport()
    s = _stream(t, min_chars=1, interval_ms=0)
    await s.start()
    await s.finish("approve me?", buttons=[Button(label="✅", token="apv:r1:approved")])
    assert t.edits[-1].buttons and t.edits[-1].buttons[0].token == "apv:r1:approved"


# ── the driver's final composition ───────────────────────────────────────────────────────────


def test_final_body_prefers_the_answer():
    from app.gateways.driver import RunOutcome, final_body

    body, buttons = final_body(RunOutcome(run_id="r", answer="the answer"), _settings())
    assert body == "the answer" and buttons is None


def test_final_body_reports_an_error_when_there_is_no_answer():
    from app.gateways.driver import RunOutcome, final_body

    body, _ = final_body(RunOutcome(run_id="r", error="terraform plan failed"), _settings())
    assert "terraform plan failed" in body


def test_final_body_falls_back_to_progress_before_saying_nothing():
    from app.gateways.driver import RunOutcome, final_body

    body, _ = final_body(RunOutcome(run_id="r", steps=["Applying 3 resources…", "Verified"]),
                         _settings())
    assert "Verified" in body


def test_final_body_attaches_approval_buttons_for_a_parked_run():
    from app.gateways.driver import RunOutcome, final_body

    body, buttons = final_body(
        RunOutcome(run_id="r7", answer="Planned.", interrupt={"workflow": "aws.s3",
                                                             "mode": "apply", "plan": {}}),
        _settings())
    assert "Approval required" in body
    assert buttons and [b.token for b in buttons] == ["apv:r7:approved", "apv:r7:rejected"]
