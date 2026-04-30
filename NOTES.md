# humiuke/hydrogram —— ljlhubuserbot 自维护 fork 备忘

这里记录 **`humiuke-main` 分支** 相对 `01101sam/hydrogram` 的全部改动，方便未来
跟 layer / cherry-pick / 排障时一目了然。

## 仓库布局

```
origin    https://github.com/humiuke/hydrogram          ← 我们自己 push 这里
upstream  https://github.com/01101sam/hydrogram         ← 跟 layer 用,定期 rebase
mainline  https://github.com/hydrogram/hydrogram        ← 偶尔 cherry-pick 修复
```

工作分支:`humiuke-main`(基于 `01101sam/hydrogram@ccb47069` Layer 224)。

仓库级 git config:`user.email = 79509903+humiuke@users.noreply.github.com`(GitHub
noreply 别名)。所有 commit 用这个邮箱,真实邮箱 `kleen@qq.com` 不在历史里出现。

## 我们的 commit(从 base `2ee6b91d` 开始)

| commit | 类型 | 说明 |
|---|---|---|
| `2ee6b91d` | cherry-pick mainline | `fix(session): prevent race condition during connection cleanup` —— 把 `recv_task.cancel()` 移到 `connection.close()` 之前,防 `_SelectorSocketTransport._read_ready()` 在 socket 已关闭时仍被回调而 `TypeError` |
| `18558d8f` | 自家修复 | `fix(session): bound retries for connection-closed and Session.start` —— 修两个无限重试 bug:1) `Session.invoke` 的 `protocol.closed` 分支不消耗 retries;2) `Session.start` 里 `while True: connection.connect()` 不限次数。改完两者都会在 N 次失败后 raise `ConnectionError`,业务侧能接住 |
| `e5176179` | 自家修复 | `feat(types): gracefully handle unknown MessageEntity types` —— `MessageEntityType._missing_` classmethod 让未知 entity class fallback 到 `UNKNOWN`,`MessageEntity._parse` 把原 raw 对象挂到 `_raw_entity`。Telegram schema 升级 lag(例如 `MessageEntityFormattedDate`)不再炸整批消息 |
| `7155a327` | 自家修复 | `perf(client): parallelize per-channel GetChannelDifference` —— `Client.handle_updates` 的串行 `await GetChannelDifference` 改成"按 channel 去重 + `asyncio.gather` 并行",N 次串行 RPC 压到 O(1) 墙上时间 |

Tag:
- `v0.2.1.dev0+ljl.1` = HEAD at `2ee6b91d`(cherry-pick 完成时)
- `v0.2.1.dev0+ljl.2` = HEAD at `7155a327`(B1-B5 全部合完)

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
