#  Hydrogram - Telegram MTProto API Client Library for Python
#  Copyright (C) 2017-2023 Dan <https://github.com/delivrance>
#  Copyright (C) 2023-present Hydrogram <https://hydrogram.org>
#
#  This file is part of Hydrogram.
#
#  Hydrogram is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Lesser General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Hydrogram is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with Hydrogram.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations

import asyncio
import bisect
import contextlib
import logging
import os
from datetime import datetime, timedelta
from hashlib import sha1
from io import BytesIO
from typing import TYPE_CHECKING, ClassVar

import hydrogram
from hydrogram import raw
from hydrogram.crypto import mtproto
from hydrogram.errors import (
    AuthKeyDuplicated,
    BadMsgNotification,
    FloodWait,
    InternalServerError,
    RPCError,
    SecurityCheckMismatch,
    ServiceUnavailable,
)
from hydrogram.raw.all import layer
from hydrogram.raw.core import FutureSalts, Int, MsgContainer, TLObject

from .internals import MsgFactory, MsgId

if TYPE_CHECKING:
    from hydrogram.connection import Connection

log = logging.getLogger(__name__)


class Result:
    def __init__(self):
        self.value = None
        self.event = asyncio.Event()


class Session:
    START_TIMEOUT = 2
    WAIT_TIMEOUT = 15
    SLEEP_THRESHOLD = 10
    MAX_RETRIES = 10
    ACKS_THRESHOLD = 10
    PING_INTERVAL = 5
    STORED_MSG_IDS_MAX_SIZE = 1000 * 2
    RECONNECT_THRESHOLD = timedelta(seconds=10)

    TRANSPORT_ERRORS: ClassVar = {
        404: "auth key not found",
        429: "transport flood",
        444: "invalid DC",
    }

    def __init__(
        self,
        client: hydrogram.Client,
        dc_id: int,
        auth_key: bytes,
        test_mode: bool,
        is_media: bool = False,
        is_cdn: bool = False,
    ):
        self.client = client
        self.dc_id = dc_id
        self.auth_key = auth_key
        self.test_mode = test_mode
        self.is_media = is_media
        self.is_cdn = is_cdn

        self.connection: Connection | None = None

        self.auth_key_id = sha1(auth_key).digest()[-8:]

        self.session_id = os.urandom(8)
        self.msg_factory = MsgFactory()

        self.salt = 0

        self.pending_acks = set()

        self.results = {}

        self.stored_msg_ids = []

        self.ping_task = None
        self.ping_task_event = asyncio.Event()

        self.recv_task = None

        self.is_started = asyncio.Event()
        self.restart_lock = asyncio.Lock()

        self.last_reconnect_attempt = None

    # ── ljlhubuserbot fork constant (humiuke-main) ──
    # ``Session.start`` 网络持续故障时的重试上限。
    # 上游 hydrogram ``while True:`` 无限重试,如果调用方期望 ``client.start()`` 在
    # 合理时间返回(例如 watchdog 触发的 ``_restart_account``),网络持续不通会让
    # 调用方永久卡住、僵尸 client 永不死亡。
    # 30 次 + 指数退避(2~30s) ≈ 最多 ~10 分钟尝试,放弃后 raise ConnectionError
    # 给上层(Account._restart_account 接住后丢弃 client,等 watchdog 下轮再触发)。
    MAX_START_ATTEMPTS = 30

    async def start(self):
        attempts = 0
        while True:
            attempts += 1
            self.connection = self.client.connection_factory(
                dc_id=self.dc_id,
                test_mode=self.test_mode,
                ipv6=self.client.ipv6,
                proxy=self.client.proxy,
                media=self.is_media,
                protocol_factory=self.client.protocol_factory,
            )

            try:
                await self.connection.connect()

                self.recv_task = self.client.loop.create_task(self.recv_worker())

                await self.send(raw.functions.Ping(ping_id=0), timeout=self.START_TIMEOUT)

                if not self.is_cdn:
                    await self.send(
                        raw.functions.InvokeWithLayer(
                            layer=layer,
                            query=raw.functions.InitConnection(
                                api_id=await self.client.storage.api_id(),
                                app_version=self.client.app_version,
                                device_model=self.client.device_model,
                                system_version=self.client.system_version,
                                system_lang_code=self.client.lang_code,
                                lang_code=self.client.lang_code,
                                lang_pack="",
                                query=raw.functions.help.GetConfig(),
                            ),
                        ),
                        timeout=self.START_TIMEOUT,
                    )

                self.ping_task = self.client.loop.create_task(self.ping_worker())

                log.info("Session initialized: Layer %s", layer)
                log.info("Device: %s - %s", self.client.device_model, self.client.app_version)
                log.info("System: %s (%s)", self.client.system_version, self.client.lang_code)
            except AuthKeyDuplicated as e:
                await self.stop()
                raise e
            except (OSError, RPCError) as e:
                await self.stop()
                # ── ljlhubuserbot fork fix (humiuke-main) ──
                # 上游这里 ``await self.stop()`` 后什么都不做继续 while,网络持续
                # 故障时会无限循环。加重试上限 + 指数退避 + 显式 raise,让上层
                # 能区分"暂时性失败"(继续 retry)和"持久性失败"(放弃)。
                if attempts >= Session.MAX_START_ATTEMPTS:
                    log.error(
                        "[%s] Session.start failed after %d attempts, giving up: %s",
                        self.client.name,
                        attempts,
                        e,
                    )
                    raise ConnectionError(
                        f"[{self.client.name}] Session start failed after "
                        f"{Session.MAX_START_ATTEMPTS} attempts"
                    ) from e
                # 指数退避封顶 30s,前几次快速重试,长期故障时放慢
                backoff = min(2 ** min(attempts, 6), 30)
                log.warning(
                    "[%s] Session.start attempt %d/%d failed (%s), retry in %ds",
                    self.client.name,
                    attempts,
                    Session.MAX_START_ATTEMPTS,
                    type(e).__name__,
                    backoff,
                )
                await asyncio.sleep(backoff)
            except Exception as e:
                await self.stop()
                raise e
            else:
                break

        self.is_started.set()

        log.info("Session started")

    async def stop(self):
        self.is_started.clear()

        self.stored_msg_ids.clear()

        self.ping_task_event.set()

        if self.ping_task is not None:
            await self.ping_task

        self.ping_task_event.clear()

        # Cancel recv_task first to avoid race condition with connection close
        # If connection closes before recv_task finishes, asyncio transport callbacks
        # can become None while still being invoked, causing TypeError
        if self.recv_task and not self.recv_task.done():
            self.recv_task.cancel()

            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, RuntimeError):
                await asyncio.wait_for(self.recv_task, timeout=1.0)

            self.recv_task = None

        if self.connection:
            await self.connection.close()

        if not self.is_media and callable(self.client.disconnect_handler):
            try:
                await self.client.disconnect_handler(self.client)
            except Exception as e:
                log.exception(e)

        log.info("Session stopped")

    async def restart(self):
        async with self.restart_lock:
            now = datetime.now()
            if (
                self.last_reconnect_attempt
                and now - self.last_reconnect_attempt < self.RECONNECT_THRESHOLD
            ):
                log.info("Reconnecting too frequently, sleeping for a while")
                await asyncio.sleep(5)

            self.last_reconnect_attempt = now
            await self.stop()
            await self.start()

    async def handle_packet(self, packet):
        data = await self.client.loop.run_in_executor(
            hydrogram.crypto_executor,
            mtproto.unpack,
            BytesIO(packet),
            self.session_id,
            self.auth_key,
            self.auth_key_id,
        )

        messages = data.body.messages if isinstance(data.body, MsgContainer) else [data]

        log.debug("Received: %s", data)

        for msg in messages:
            if msg.seq_no % 2 != 0:
                if msg.msg_id in self.pending_acks:
                    continue
                self.pending_acks.add(msg.msg_id)

            try:
                if len(self.stored_msg_ids) > Session.STORED_MSG_IDS_MAX_SIZE:
                    del self.stored_msg_ids[: Session.STORED_MSG_IDS_MAX_SIZE // 2]

                if self.stored_msg_ids:
                    if msg.msg_id < self.stored_msg_ids[0]:
                        raise SecurityCheckMismatch(
                            "The msg_id is lower than all the stored values"
                        )

                    if msg.msg_id in self.stored_msg_ids:
                        raise SecurityCheckMismatch(
                            "The msg_id is equal to any of the stored values"
                        )

                    time_diff = (msg.msg_id - MsgId()) / 2**32

                    if time_diff > 30:
                        raise SecurityCheckMismatch(
                            "The msg_id belongs to over 30 seconds in the future. "
                            "This usually means your system clock is ahead of the actual time. "
                            "Please synchronize your system time with an NTP server to avoid "
                            "this error in Hydrogram."
                        )

                    if time_diff < -300:
                        raise SecurityCheckMismatch(
                            "The msg_id belongs to over 300 seconds in the past. "
                            "This usually means your system clock is behind the actual time. "
                            "Please synchronize your system time with an NTP server to avoid "
                            "this error in Hydrogram."
                        )
            except SecurityCheckMismatch as e:
                log.info("Discarding packet: %s", e)
                await self.connection.close()
                return
            else:
                bisect.insort(self.stored_msg_ids, msg.msg_id)

            if isinstance(msg.body, (raw.types.MsgDetailedInfo, raw.types.MsgNewDetailedInfo)):
                self.pending_acks.add(msg.body.answer_msg_id)
                continue

            if isinstance(msg.body, raw.types.NewSessionCreated):
                continue

            msg_id = None

            if isinstance(msg.body, (raw.types.BadMsgNotification, raw.types.BadServerSalt)):
                msg_id = msg.body.bad_msg_id
            elif isinstance(msg.body, (FutureSalts, raw.types.RpcResult)):
                msg_id = msg.body.req_msg_id
            elif isinstance(msg.body, raw.types.Pong):
                msg_id = msg.body.msg_id
            elif self.client is not None:
                self.client.loop.create_task(self.client.handle_updates(msg.body))

            if msg_id in self.results:
                self.results[msg_id].value = getattr(msg.body, "result", msg.body)
                self.results[msg_id].event.set()

        if len(self.pending_acks) >= self.ACKS_THRESHOLD:
            log.debug("Sending %s acks", len(self.pending_acks))

            try:
                await self.send(raw.types.MsgsAck(msg_ids=list(self.pending_acks)), False)
            except OSError:
                pass
            else:
                self.pending_acks.clear()

    async def ping_worker(self):
        log.info("PingTask started")

        while True:
            try:
                await asyncio.wait_for(self.ping_task_event.wait(), self.PING_INTERVAL)
            except asyncio.TimeoutError:
                pass
            else:
                break

            with contextlib.suppress(OSError, RPCError):
                await self.send(
                    raw.functions.PingDelayDisconnect(
                        ping_id=0, disconnect_delay=self.WAIT_TIMEOUT + 10
                    ),
                    False,
                )

        log.info("PingTask stopped")

    async def recv_worker(self):
        log.info("NetworkTask started")

        while True:
            packet = await self.connection.recv()

            if packet is None or len(packet) == 4:
                if packet:
                    error_code = -Int.read(BytesIO(packet))

                    log.warning(
                        "Server sent transport error: %s (%s)",
                        error_code,
                        Session.TRANSPORT_ERRORS.get(error_code, "unknown error"),
                    )

                if self.is_started.is_set():
                    self.client.loop.create_task(self.restart())

                break

            self.client.loop.create_task(self.handle_packet(packet))

        log.info("NetworkTask stopped")

    async def send(
        self, data: TLObject, wait_response: bool = True, timeout: float = WAIT_TIMEOUT
    ):
        message = self.msg_factory(data)
        msg_id = message.msg_id

        if wait_response:
            self.results[msg_id] = Result()

        log.debug("Sent: %s", message)

        payload = await self.client.loop.run_in_executor(
            hydrogram.crypto_executor,
            mtproto.pack,
            message,
            self.salt,
            self.session_id,
            self.auth_key,
            self.auth_key_id,
        )

        try:
            await self.connection.send(payload)
        except OSError as e:
            self.results.pop(msg_id, None)
            raise e

        if wait_response:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.results[msg_id].event.wait(), timeout)

            result = self.results.pop(msg_id).value

            if result is None:
                raise TimeoutError("Request timed out")

            if isinstance(result, raw.types.RpcError):
                if isinstance(
                    data,
                    (
                        raw.functions.InvokeWithoutUpdates,
                        raw.functions.InvokeWithTakeout,
                    ),
                ):
                    data = data.query

                RPCError.raise_it(result, type(data))

            if isinstance(result, raw.types.BadMsgNotification):
                log.warning(
                    "%s: %s",
                    BadMsgNotification.__name__,
                    BadMsgNotification(result.error_code),
                )

            if isinstance(result, raw.types.BadServerSalt):
                self.salt = result.new_server_salt
                return await self.send(data, wait_response, timeout)

            return result
        return None

    async def invoke(
        self,
        query: TLObject,
        retries: int = MAX_RETRIES,
        timeout: float = WAIT_TIMEOUT,
        sleep_threshold: float = SLEEP_THRESHOLD,
    ):
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.is_started.wait(), self.WAIT_TIMEOUT)

        if isinstance(
            query, (raw.functions.InvokeWithoutUpdates, raw.functions.InvokeWithTakeout)
        ):
            inner_query = query.query
        else:
            inner_query = query

        query_name = ".".join(inner_query.QUALNAME.split(".")[1:])

        while retries > 0:
            try:
                if (
                    self.connection is None
                    or self.connection.protocol is None
                    or getattr(self.connection.protocol, "closed", True)
                ):
                    # ── ljlhubuserbot fork fix (humiuke-main) ──
                    # 上游 hydrogram 此分支只 ``restart() + sleep(1) + continue``,**不减
                    # retries 也不抛异常**。如果 ``restart()`` 起不来(账号被 LogOut /
                    # DC 拒绝 / 网络持续丢包),整个 invoke 会陷入无限重试不抛异常
                    # 死循环,业务层永远收不到 OSError 触发不了 _restart_account / 重连。
                    # 修复:closed 分支也消耗 retries,N 次后 raise ConnectionError
                    # (OSError 子类),让上层接住异常做处置。
                    retries -= 1
                    if retries == 0:
                        raise ConnectionError(
                            f"[{self.client.name}] Session connection closed "
                            f"and reconnect failed after {Session.MAX_RETRIES} attempts"
                        )
                    log.warning(
                        "[%s] Connection is closed or not established. Attempting to reconnect...",
                        self.client.name,
                    )
                    await self.restart()
                    await asyncio.sleep(1)
                    continue

                return await self.send(query, timeout=timeout)
            except FloodWait as e:
                amount = e.value

                if amount > sleep_threshold >= 0:
                    raise

                log.warning(
                    '[%s] Waiting for %s seconds before continuing (required by "%s")',
                    self.client.name,
                    amount,
                    query_name,
                )

                await asyncio.sleep(amount)
            except (OSError, InternalServerError, ServiceUnavailable) as e:
                retries -= 1
                if retries == 0:
                    raise e

                # ── ljlhubuserbot fork fix (humiuke-main) ──
                # 上游用 ``[%s]`` 打的是 retry 计数(1~9),跟 closed 分支的
                # ``[client.name]`` 风格不一致,排障时定位不到是哪个 bot。
                # 改成 ``[uid attempt=N/MAX]`` 同时保留两个信息。
                (log.warning if retries < 2 else log.info)(
                    '[%s attempt=%d/%d] Retrying "%s" due to: %s',
                    self.client.name,
                    Session.MAX_RETRIES - retries,
                    Session.MAX_RETRIES,
                    query_name,
                    str(e) or repr(e),
                )

                if isinstance(e, OSError) and retries > 1:
                    try:
                        await self.restart()
                    except Exception as restart_error:
                        log.warning(
                            "[%s] Failed to restart session: %s",
                            self.client.name,
                            str(restart_error) or repr(restart_error),
                        )

                await asyncio.sleep(0.5)

        raise TimeoutError("Exceeded maximum number of retries")
