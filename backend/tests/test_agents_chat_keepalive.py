"""Exercise the actual SSE endpoint during quiet agent work and disconnects."""
import asyncio
import importlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents.runtime.executor import ExecutorEvent
from app.agents.schemas.conversation import SendMessageRequest

chat_module = importlib.import_module("app.agents.api.chat")
pytestmark = pytest.mark.asyncio


async def _response(monkeypatch, run):
    monkeypatch.setattr(chat_module, "HEARTBEAT_INTERVAL_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(chat_module.agent_service, "get_agent", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())))
    monkeypatch.setattr(chat_module.conversation_service, "create_conversation", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())))
    monkeypatch.setattr(chat_module, "AgentExecutor", lambda: SimpleNamespace(run=run))
    return await chat_module.chat(
        uuid.uuid4(), SendMessageRequest(content="Summarize spending"),
        ctx=SimpleNamespace(workspace=SimpleNamespace(id=uuid.uuid4()), user_id=uuid.uuid4()),
        session=object(),
    )


async def test_quiet_chat_sends_heartbeats_without_cancelling_agent(monkeypatch):
    release = asyncio.Event()
    completed = False

    async def run(**kwargs):
        nonlocal completed
        await release.wait()
        completed = True
        yield ExecutorEvent(type="text_delta", text="Your summary")
        yield ExecutorEvent(type="done", finish_reason="stop")

    response = await _response(monkeypatch, run)
    stream = response.body_iterator
    try:
        assert b"event: conversation" in await anext(stream)
        for _ in range(3):
            assert await asyncio.wait_for(anext(stream), 0.1) == b": keep-alive\n\n"
        assert not completed
        release.set()
        assert b"Your summary" in await asyncio.wait_for(anext(stream), 0.1)
        assert b"event: done" in await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert completed
    finally:
        await stream.aclose()


async def test_disconnect_cancels_pending_agent_work(monkeypatch):
    finalized = asyncio.Event()

    async def run(**kwargs):
        try:
            await asyncio.Event().wait()
            yield ExecutorEvent(type="done")
        finally:
            finalized.set()

    response = await _response(monkeypatch, run)
    stream = response.body_iterator
    await anext(stream)
    try:
        await asyncio.wait_for(anext(stream), 0.1)
    finally:
        await stream.aclose()
    assert finalized.is_set()


async def test_agent_exception_still_reaches_client(monkeypatch):
    async def run(**kwargs):
        raise RuntimeError("test failure")
        yield ExecutorEvent(type="done")

    response = await _response(monkeypatch, run)
    chunks = [chunk async for chunk in response.body_iterator]
    assert b"event: conversation" in chunks[0]
    assert b"event: error" in chunks[1]
    assert b"test failure" in chunks[1]
