"""ljlhubuserbot 2026-11-24 事故的回归测试（fork 自有，不属于上游）。

事故：旧 fork（f8327699）的 ``Session.stop()`` 取消不了正在跑的重连循环。hub 的看门狗丢掉旧 Client 后，
旧会话在后台继续重连，网络恢复时自己复活成「僵尸会话」：带着独立的 TCP 连接和心跳永久存活，
把更新交给已经停掉的 Client（队列只进不出，5 天涨到 6 GB），并和别的僵尸一起对 Telegram 形成重连风暴
（实测每秒约 22 条新连接）。握手途中被对端关闭的连接还会留下「自认为已启动、实际已死、socket 不关」的会话。

同步到上游 8da10a7c（会话恢复重写）后这两条都不应再出现；这里按线上场景原样钉住。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from hydrogram import Client
from tests.test_connection_resilience import Harness


@pytest.mark.asyncio
async def test_stopped_session_never_reconnects_after_network_recovers(monkeypatch):
    """看门狗在会话重连途中 stop() 它；之后网络恢复，这个会话不许再建任何连接。"""
    harness = Harness(monkeypatch)
    session = harness.session
    await session.start()

    network_up = False
    attempted = asyncio.Event()

    async def connect():
        attempted.set()
        await asyncio.sleep(0)
        if not network_up:
            raise ConnectionResetError("offline")

    harness.configure = lambda transport: setattr(transport, "connect", connect)
    # 连接被对端断开 -> 会话进入后台重连
    harness.transport.queue.put_nowait(None)
    await asyncio.wait_for(attempted.wait(), 1)

    # hub: _restart_account -> client.stop() -> session.stop()，随后这个 Client 被丢弃
    await asyncio.wait_for(session.stop(), 1)
    opened_before = len(harness.transports)

    # 网络恢复。旧 fork 在这里复活成僵尸
    network_up = True
    for _ in range(50):
        await asyncio.sleep(0)

    assert len(harness.transports) == opened_before, "已停止的会话又去建了新连接（僵尸会话）"
    assert not session.is_started.is_set()
    assert session.health["state"] == "stopped"
    assert session.recovery_task.done()
    assert session.ping_task is None and session.recv_task is None
    assert all(t.closed for t in harness.transports), "有连接没被关闭（CLOSE-WAIT 泄漏）"
    harness.client.handle_updates.assert_not_called()


@pytest.mark.asyncio
async def test_peer_close_during_handshake_is_not_a_started_session(monkeypatch):
    """握手途中对端关连接（线上：握手后 1 秒内被 FIN）：不许报告启动成功，连接必须全部关掉。"""
    harness = Harness(monkeypatch)

    def configure(transport):
        transport.drop_pongs = True
        transport.queue.put_nowait(None)  # EOF

    harness.configure = configure
    try:
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(harness.session.start(), 2)
        assert not harness.session.is_started.is_set()
        assert all(t.closed for t in harness.transports)
        assert harness.session.ping_task is None
    finally:
        await harness.session.stop()


def _connected_client(name: str) -> Client:
    """connect() 已成功的 Client:会话是假的,只看 start() 失败时有没有把它停掉。"""
    client = Client(name, in_memory=True)
    session = AsyncMock()

    async def connect():
        client.session = session
        client.is_connected = True
        return True  # 已授权

    client.connect = connect
    client.storage.close = AsyncMock()
    client.storage.is_bot = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_start_cancelled_after_connect_stops_the_session():
    """hub 用 wait_for 给 client.start() 限时;超时(取消)时已连上的会话必须被停掉。"""
    client = _connected_client("start-cancelled")
    entered = asyncio.Event()

    async def hang(*_args, **_kwargs):
        entered.set()
        await asyncio.sleep(3600)

    client.invoke = hang  # start() 里的 GetState 卡住
    try:
        task = asyncio.create_task(client.start())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        client.session.stop.assert_awaited_once()
        assert not client.is_connected
    finally:
        client.executor.shutdown()


@pytest.mark.asyncio
async def test_start_failing_in_get_me_stops_the_session():
    """连接和 GetState 都成功、get_me 失败:同样不能留下活会话。"""
    client = _connected_client("start-get-me-fails")
    client.invoke = AsyncMock(return_value=None)
    client.get_me = AsyncMock(side_effect=RuntimeError("boom"))
    try:
        with pytest.raises(RuntimeError, match="boom"):
            await client.start()
        client.session.stop.assert_awaited_once()
        assert not client.is_connected
    finally:
        client.executor.shutdown()
