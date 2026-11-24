# humiuke/hydrogram —— ljlhubuserbot 自维护 fork 备忘

这里记录 **`humiuke-main` 分支** 相对 `01101sam/hydrogram` 的全部改动，方便未来
跟 layer / cherry-pick / 排障时一目了然。

## 仓库布局

```
origin    https://github.com/humiuke/hydrogram          ← 我们自己 push 这里
upstream  https://github.com/01101sam/hydrogram         ← 跟 layer 用,定期 rebase
mainline  https://github.com/hydrogram/hydrogram        ← 偶尔 cherry-pick 修复
```

工作分支:`humiuke-main`(基于 `01101sam/hydrogram@ccb47069` **Layer 224**;2026-11-24 带入 mainline 的会话恢复重写,layer 没动)。

> **为什么不跟 `01101sam` 的新 layer(2026-11-24 核对)**:它只自动更新 schema,不改高层代码。
> Layer 227(2026-06)起 `ImportChatInvite` / `JoinChannel` 返回 `messages.ChatInviteJoinResult`,`join_chat` 直接报错;
> Layer 229(2026-08)起键盘按钮重构(`KeyboardButtonCallback` 等没了),收到带内联按钮的消息就解析失败。
> 能安全跟到的最后一版是 Layer 225,和 224 没有实质差别。mainline 在升 232 时改过高层代码(`d79c1199`),
> 但要求 Python ≥3.11。**要升 layer 就走 mainline + 升 Python,不要 rebase 到 `upstream/dev`。**
> 升之前用「对照 schema 检查高层代码里构造的 raw 对象」的办法核一遍(见 ljlhubuserbot 2026-11-24 的记录)。

仓库级 git config:`user.name = humiuke`、`user.email = 79509903+humiuke@users.noreply.github.com`
(GitHub noreply 别名)。所有 commit 用这个身份,真实邮箱不在历史里出现。**新克隆要先配这两项再提交。**

## 我们的 commit(`ccb47069` 之上,按顺序)

| commit | 类型 | 说明 |
|---|---|---|
| `a83784cf` | cherry-pick mainline `edb10c60` | `fix(session): prevent race condition during connection cleanup` —— `Session.stop()` 先取消 `recv_task` 再关连接,防 `_read_ready()` `TypeError` |
| `170f1da4` | cherry-pick mainline `1e95e33c` | `feat(updates): add opt-in catch-up and gap recovery` —— 可选的更新补漏(`Client(catch_up=True)` 才启用,**默认关,hub 没开**)。带进来只因为下一条依赖它改过的同一批文件;`MySQLStorage` 没实现 `get_update_states` / `set_update_state`,要开得先补 |
| `cadb9cd0` | cherry-pick mainline `8da10a7c` | `fix(session): recover MTProto connections safely` —— **会话恢复重写**(2026-11-24 事故的根治):`stop()` 取消恢复任务、恢复有上限(5 次)+ 退避、旧连接迟到的失败不影响新连接、握手途中被关不算启动成功、并发请求共用一次恢复。行为变化:途中的请求不再自动重发,抛 `ConnectionLostError` / `RequestTimeoutError`(送达与否不确定);新增 `Client.connection_health`。取代了我们原来的 `18558d8f`(限制重试次数),那个补丁不再需要 |
| `4901f42f` | 自家修复 | `feat(types): gracefully handle unknown MessageEntity types` —— 未知 entity class fallback 到 `UNKNOWN`,原 raw 对象挂 `_raw_entity` |
| `41bdb5e1` | 自家修复 | `perf(client): parallelize per-channel GetChannelDifference` —— `handle_updates` 按 channel 去重 + `asyncio.gather` 并行(只作用于默认的非 catch_up 路径) |
| `73492e30` | 自家修复 | `fix(connection): abort TCP transport when graceful close hangs` —— `TCP.close` 在 `wait_closed()` 失败/超时时 `transport.abort()`,上限 3s |
| `cf67f38e` | 自家测试 | `test: regression tests for the 2026-11-24 zombie session incident` —— `tests/test_fork_incident_20261124.py`:已停止的会话网络恢复后不许再建连接;握手途中被关不算启动成功 |
| `335c0894` | backport mainline `1f290021` | `fix(session): preserve RPC cancellation on Python 3.10` —— 上游用 `asyncio.timeout`(3.11+)取代 `wait_for`,修「取消被吞、留下待应答记录」;我们线上是 3.10,用 `async_timeout` 兜住(新增依赖 `async-timeout; python_version < '3.11'`) |

mainline(`hydrogram/hydrogram`)2026-10-28 之后的其余提交(要求 Python ≥3.11、Layer 232 + 高层适配、MTProxy、
媒体并发上限、随机 ID 等)**没有带**:线上是 Python 3.10;以后升级 Python 再评估整体换到 mainline。

### 2026-11-24 事故备忘

旧版(`f8327699`)`Session.stop()` 取消不了正在跑的 `restart()→start()` 重试循环。hub 看门狗丢掉旧 Client 后,
旧会话网络恢复时自己复活(独立连接 + 心跳,永久存活),把更新塞进已停掉的 Client 的队列(只进不出,5 天 6 GB),
约 130 个这样的会话对 Telegram 形成重连风暴(实测每秒约 22 条新连接,握手后 1 秒内被对端关闭)。
排查方法:`ss -tanpi` 采样新建连接速率;journal 里 `GetChannelDifference batch member failed: Client has not been started yet` 持续刷屏。

Tag:
- `v0.2.1.dev0+ljl.1` ~ `ljl.3`:旧基线(Layer 224)上的历史 tag,见 git
- `v0.2.1.dev0+ljl.4`(待打)= 本次同步后的 HEAD

## 常见运维操作

### 跟 layer:从 upstream 同步 schema 升级

`01101sam/hydrogram` 通过 GitHub Actions 自动跟 Telegram 最新 layer。我们
定期 rebase 把它的进度合过来:

```bash
cd ~/IdeaProjects/hydrogram
git fetch upstream
git checkout humiuke-main
git rebase upstream/dev
# 解决冲突(主要是 hydrogram/raw/ 自动生成的代码)
git push origin humiuke-main --force-with-lease
git tag -a "v0.2.1.dev0+ljl.<n>" -m "..."
git push origin "v0.2.1.dev0+ljl.<n>"
```

### cherry-pick 主仓修复

```bash
git fetch mainline dev
git cherry-pick <mainline-commit-sha>
git push origin humiuke-main
```

### bump ljlhubuserbot pin

```bash
NEW_SHA=$(git rev-parse humiuke-main)
echo "新 sha: $NEW_SHA"
# 改 ljlhubuserbot/requirements.txt 里的 @<sha>
# 然后:
cd ~/IdeaProjects/ljlhubuserbot
source venv/bin/activate
pip install --upgrade --force-reinstall --no-deps \
  "git+https://github.com/humiuke/hydrogram.git@$NEW_SHA"
pytest tests/  # 必跑
```

## 改动的边界(什么进 fork,什么留 patches.py)

| 类型 | 归宿 |
|---|---|
| 通用 bug 修复(任何 hydrogram 用户都受益) | **进这个 fork**,改源码 |
| 项目特定策略(MySQL pool 集成 / 日志去重 / 业务监控) | 留 `tgbot/patches.py` |

> 反例:`ljlhubuserbot/tgbot/patches.py` 现在保留的 `_patch_handle_updates`
> 把 `ChannelInvalid` / `RuntimeError("get_connection timeout")` 都吞了 ——
> 前者属可推广的 fork 修复,后者纯属业务侧 MySQL 池超时,**不要**进 fork。

## 已知风险点(下次 rebase 注意)

- **`hydrogram/client.py:586` `handle_updates`**:我们重写过整段,upstream 后续
  可能新增 update 处理(e.g. 新版的 `UpdateChannelMessages` 处理),rebase 时
  要把新逻辑合并进我们的并行版本。
- **`hydrogram/session/session.py` `Session.invoke`**:同理,upstream 改了同
  函数其他分支(FloodWait / RPCError 处理)时要 careful merge,我们已经改了
  closed 分支和 OSError 分支的 log format。
- **`hydrogram/session/session.py` `Session.start`**:加了 `MAX_START_ATTEMPTS`
  类常量和指数退避循环,rebase 同名函数有改动时要 careful merge。
- **`hydrogram/enums/message_entity_type.py`**:加了 `_missing_` classmethod。
  upstream 给 MessageEntityType 加新 enum 值是常态(layer 升级),不冲突;但
  如果它自己也加了 `_missing_` 就要 merge。
- **`hydrogram/types/messages_and_media/message_entity.py` `_parse`**:在
  原 return 之上挂了 `_raw_entity`,upstream 改 `_parse` 实现时容易冲突。
- **`hydrogram/connection/transport/tcp/tcp.py` `TCP.close`**:加了
  `CLOSE_WAIT_TIMEOUT=3` 常量,失败分支补 `transport.abort()`,finally 清
  `self.writer/reader`。upstream 若同函数有改动(尤其引入新的关闭路径)
  rebase 时要 careful merge,务必保留 abort 兜底,否则线上会重新爆 CLOSE_WAIT
  泄漏雪崩。
