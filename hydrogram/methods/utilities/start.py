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

import contextlib
import logging

import hydrogram
from hydrogram import raw

log = logging.getLogger(__name__)


class Start:
    async def start(self: "hydrogram.Client"):
        """Start the client.

        This method connects the client to Telegram and, in case of new sessions, automatically manages the
        authorization process using an interactive prompt.

        Returns:
            :obj:`~hydrogram.Client`: The started client itself.

        Raises:
            ConnectionError: In case you try to start an already started client.

        Example:
            .. code-block:: python

                from hydrogram import Client

                app = Client("my_account")


                async def main():
                    await app.start()
                    ...  # Invoke API methods
                    await app.stop()


                app.run(main())
        """
        is_authorized = await self.connect()

        try:
            if not is_authorized:
                await self.authorize()

            if not await self.storage.is_bot() and self.takeout:
                self.takeout_id = (
                    await self.invoke(raw.functions.account.InitTakeoutSession())
                ).id
                log.info("Takeout session %s initiated", self.takeout_id)

            if self.update_manager is None:
                await self.invoke(raw.functions.updates.GetState())
        except BaseException:
            # ── ljlhubuserbot fork fix (humiuke-main) ──
            # 上游只接 ``(Exception, KeyboardInterrupt)``。调用方用 ``wait_for`` 给 start() 限时的话,
            # 超时是 CancelledError:会话已经连上、带着心跳,client 却被调用方丢掉,
            # 成了没人管的活会话(2026-11-24 事故同一类泄漏)。
            await self.disconnect()
            raise
        else:
            try:
                self.me = await self.get_me()
                await self.initialize()
            except BaseException:
                # 同上:get_me / initialize 失败或被取消也必须把连接收掉。
                # initialize 走到一半时 dispatcher 可能已启动,is_initialized 还没置位,
                # 所以不能走 terminate(),直接停更新管理器和会话。
                if self.update_manager is not None:
                    await self.update_manager.stop()
                if not self.is_initialized:
                    # 已断开时 disconnect() 会抛 ConnectionError,别让它盖住原始异常
                    with contextlib.suppress(ConnectionError):
                        await self.disconnect()
                raise

            return self
