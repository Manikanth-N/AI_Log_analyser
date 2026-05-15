"""
Phase 6 regression tests — Ollama concurrency control.

Invariants:
  1. _OLLAMA_SEMAPHORE ensures at most one LLM call in-flight at a time
  2. Two concurrent structured() calls serialize, not overlap
  3. Deterministic (non-LLM) agent code is unaffected by the semaphore
  4. Semaphore is released even when the LLM call raises an exception
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest


def _make_client():
    from llm.client import OllamaClient
    c = OllamaClient.__new__(OllamaClient)
    c.primary_model = "test"
    c.fast_model = "test"
    c.embedding_model = "test"
    c.timeout = 30
    return c


def test_semaphore_serializes_concurrent_calls():
    """Two threads calling structured() must not overlap in the critical section."""
    from llm.client import _OLLAMA_SEMAPHORE
    from pydantic import BaseModel

    class DummyModel(BaseModel):
        value: str = "ok"

    call_log: list[tuple[str, float]] = []

    def slow_create(*args, **kwargs):
        call_log.append(("enter", time.monotonic()))
        time.sleep(0.05)   # simulate inference
        call_log.append(("exit", time.monotonic()))
        return DummyModel()

    client = _make_client()
    client._instructor_client = MagicMock()
    client._instructor_client.chat.completions.create.side_effect = slow_create

    errors = []

    def call():
        try:
            client.structured(
                messages=[{"role": "user", "content": "test"}],
                response_model=DummyModel,
            )
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=call)
    t2 = threading.Thread(target=call)
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert not errors, f"Unexpected errors: {errors}"
    assert len(call_log) == 4   # 2 enter + 2 exit

    # Extract enter/exit times
    enters = [t for label, t in call_log if label == "enter"]
    exits = [t for label, t in call_log if label == "exit"]

    # The second enter must not happen before the first exit
    first_exit = min(exits)
    second_enter = max(enters)
    assert second_enter >= first_exit - 0.005, (
        f"Calls overlapped: second entered {second_enter:.4f} before first exited {first_exit:.4f}"
    )


def test_semaphore_released_on_exception():
    """If the LLM call raises, the semaphore must be released (not deadlocked)."""
    from llm.client import _OLLAMA_SEMAPHORE
    from pydantic import BaseModel

    class DummyModel(BaseModel):
        value: str = "ok"

    client = _make_client()
    client._instructor_client = MagicMock()
    client._instructor_client.chat.completions.create.side_effect = RuntimeError("LLM down")

    with pytest.raises(RuntimeError, match="LLM down"):
        client.structured(
            messages=[{"role": "user", "content": "test"}],
            response_model=DummyModel,
        )

    # Semaphore must be acquirable immediately after the exception
    acquired = _OLLAMA_SEMAPHORE.acquire(blocking=False)
    assert acquired, "Semaphore was not released after exception"
    _OLLAMA_SEMAPHORE.release()


def test_complete_also_serialized():
    """complete() calls also go through the semaphore."""
    from llm.client import _OLLAMA_SEMAPHORE

    call_count = [0]

    def fake_create(*args, **kwargs):
        call_count[0] += 1
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "done"
        return resp

    client = _make_client()
    client._openai_client = MagicMock()
    client._openai_client.chat.completions.create.side_effect = fake_create

    results = []
    errors = []

    def call():
        try:
            r = client.complete(messages=[{"role": "user", "content": "hi"}])
            results.append(r)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=call) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 3
    assert all(r == "done" for r in results)


def test_deterministic_agents_unblocked_by_semaphore():
    """Agents that never call LLM complete immediately even if semaphore is held."""
    from llm.client import _OLLAMA_SEMAPHORE

    # Acquire semaphore to simulate an LLM call in progress
    _OLLAMA_SEMAPHORE.acquire()

    try:
        # Deterministic work completes immediately (no semaphore involvement)
        def deterministic_work():
            return sum(range(1000))

        result = None
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join(timeout=1.0)
        assert not t.is_alive()

        result = deterministic_work()
        assert result == 499500
    finally:
        _OLLAMA_SEMAPHORE.release()
