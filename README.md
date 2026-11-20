# Team Memory 团队记忆服务

Team Memory 让团队成员和各自的 AI 助手共用一份项目记忆。团队可以把评审决定、协作分工和变更依据保存在自托管的服务里。新成员接手时，问自己的助手“上次评审定了什么？谁负责验收？”，就能从他有权查看的项目记录中找回答案。

## 一次项目交接

11 月 16 日，Atlas 的负责人在评审记录中确认“下周一上线”。几天后，小林接手发布工作，在自己的 Codex 会话里问“Atlas 哪天上线，依据是什么？”他能查到 **2026 年 11 月 23 日**，也能沿着引用读到评审原话。负责人个人笔记里还有一句“我猜小林可能负责验收”。这句猜测留在本人的待复验列表，小林读到的项目分工里没有这项安排。

| 团队里的信息 | 保存在哪里 | 谁能使用 |
| --- | --- | --- |
| 已确认的决定、分工、风险和进展 | 项目空间 | 获授权的成员及其 Agent 共享当前结论，按权限查看证据、提出修订或复验。 |
| 个人偏好、工作笔记、对协作事项的观察 | 个人空间 | 本人及其授权 Agent 使用。即使笔记提到了某个项目或同事，也留在个人空间。 |
| 群内讨论和原始消息 | 群组空间 | 群与项目绑定后，有权限的人可将提炼出的结论发布到项目。群原文按群组权限读取。首版支持飞书群历史归档。 |

后台每天按空间整理新增材料。证据不足、仅属转述或与现有决定冲突的内容进入人工复验。团队既可以取用已经确认的项目上下文，也可以单独查看还有哪些问题需要人来定。

## 用两位成员跑通示例

需要 Python 3.11 或以上版本、支持 FTS5 的 SQLite 和 uv。在仓库根目录执行以下命令。示例中，项目负责人 `demo` 有复验权，小林 `lin` 可以查阅项目记忆、提交修订。两人连接同一个服务，使用各自的凭证。

```sh
uv sync --locked
uv run team-memory init --data-dir ~/.local/share/team-memory-atlas
uv run team-memory grant demo project:atlas review
uv run team-memory grant demo personal:demo review
uv run team-memory grant lin project:atlas write
uv run team-memory import --manifest examples/codex/atlas.toml --dry-run
uv run team-memory import --manifest examples/codex/atlas.toml
uv run team-memory digest --space project:atlas --date 2026-11-16
uv run team-memory digest --space personal:demo --date 2026-11-16
uv run team-memory serve
```

默认监听 `127.0.0.1:8765`。服务启动时也启动定时 worker，处理已入库的材料。可以用 `TEAM_MEMORY_DIR` 覆盖数据目录。独立部署 worker 时使用 `serve --no-worker` 和 `team-memory worker`。交互式 HTTP 文档位于本机服务的 `/docs`，健康检查在 `/health`。

### 小林用 Codex 接手项目

服务启动后，在另一个终端为小林签发项目凭证，并在该终端启动 Codex。桌面客户端也可以接入，需要能读取同名环境变量。[Codex MCP 配置](https://developers.openai.com/codex/mcp/)

```sh
export TEAM_MEMORY_TOKEN="$(uv run team-memory token lin --client codex --space project:atlas --max-role write)"
codex mcp add team-memory --url http://127.0.0.1:8765/mcp --bearer-token-env-var TEAM_MEMORY_TOKEN
```

在新会话中问“请查 `project:atlas` 的上线日期，并给出评审原话”。助手可用 `get_context` 取得当前决定，再用 `get_memory` 读取证据。小林能查到日期和引文。查询负责人的 `personal:demo` 会被拒绝。同一项目中的其他 Agent 也可以通过 HTTP 查询这份决定。管理员可用 `team-memory revoke-token lin --client codex` 收回示例凭证。

| MCP 工具 | 用途 |
| --- | --- |
| `get_context` | 获取指定空间的当前结论和来源 ID，预算按序列化后的字符数计。显式设置 `include_pending` 可附带待确认项。 |
| `search_memory` | 按词、中文短词或项目代号检索有效结论。 |
| `get_memory` | 获取正文、来源片段、历史状态。可指定版本，或用 `as_of` 和 `known_at` 查询历史。 |
| `record_memory` | 提交新线索或修订。传入稳定的 `request_id`，可附来源 ID 和原文引述。 |
| `list_reviews`、`open_review` | 查看有权复验的事项及其本机页面地址。 |

### 两个人一起更新发布日期

1. **小林提出改期。** 在刚才的 Codex 会话中说：“回归验收需要多一天。请用 `record_memory` 向 `project:atlas` 提交修订，沿用主题 `release`，候选结论是『Atlas 改为 11 月 24 日上线，以便完成回归验收』。” 服务返回复验项编号。此时再查项目的当前决定，仍是 11 月 23 日。
2. **负责人核对并确认。** 负责人在自己的终端执行 `uv run team-memory token demo --client reviewer --human --space personal:demo --space project:atlas`，在本机服务的 `/review` 页面输入凭证，选择 `project:atlas`。页面并列展示原决定与改期提议。负责人可以确认、修改后确认、驳回或暂缓。切换到 `personal:demo`，才能看到自己对小林分工的那条猜测。
3. **其他成员读到新决定。** 负责人确认改期后，小林用原来的凭证开启新会话，再问“哪天上线，为什么改期？”就能读到 11 月 24 日和回归验收的原因。用 `get_memory` 指定原版本，还可以查到 11 月 23 日的决定和评审原话。

### 其他 Agent 使用 HTTP

每个 Agent 可用为自己签发的凭证访问同一项目。以下命令复用上一步的小林凭证，读取他在 Codex 中看到的项目决定。

```sh
curl -sS http://127.0.0.1:8765/v1/context \
  -H "Authorization: Bearer $TEAM_MEMORY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"space":"project:atlas","budget":3000}'
```

用 `POST /v1/sources` 逐项写入来源版本，`POST /v1/memories` 提交引用它的结论。批量来源接口会报告每一项的结果。外部 Agent 提交的来源独立记账并进入候选流程。项目名、作者名和客户端自报的判断都不能代替授权。确需撤回时，复验人可调用 `DELETE /v1/sources/{source_id}` 或带版本号的 `DELETE /v1/memories/{id}?version=1`。

## 导入与定时总结

**Codex 和项目材料。** 在 TOML 清单中逐项指定来源类型、稳定标识、文件、发生时间、目标空间。个人笔记、选中的 Codex 摘要和项目评审记录都支持增量重导。建议用显式 `occurred_at` 保留业务时间。对上游编辑使用新 `revision`。完全恢复为曾用过的旧文本时也应分配新版本。Codex 原生记忆保持只读。清单写法见 [Atlas 示例](examples/codex/atlas.toml)。

**抽取模式。** 默认离线模式将普通段落作为待复验线索。清单中经操作者核验的原文可以使用 `[decision|发布窗口]` 等标记指定类型和主题。可信作者须在对应空间有复验权。Codex 二次摘要始终保留派生来源身份。在模型模式下设置 `TEAM_MEMORY_MODEL_URL` 为完整的 OpenAI 兼容 chat completions 地址，`TEAM_MEMORY_MODEL` 为模型名，按需设置 `TEAM_MEMORY_MODEL_KEY`。模型会收到所处理来源的正文，输出必须带能在原文中定位的引文。仅在对应数据可交由该模型处理时启用。证据不足、推测、责任分配不明或变更既有决定时均进入复验。

**作业与日报。** 默认每天当地时间 02:00 处理前一日及积压的来源。用 `TEAM_MEMORY_TIMEZONE` 和 `TEAM_MEMORY_SCHEDULE` 设置时区和时间。手动 `digest` 可指定空间和日期补跑。失败批次可重试。查询 `GET /v1/digest?space=project:atlas&day=2026-11-16` 获取按当前权限和撤回状态生成的 Markdown，可按需保存。

**个人助手工作区。** 如果已有按 `personal_workspace/`、`group_workspace/` 或 `project/` 组织的本地归档，可以先配置目标空间，再运行 `team-memory import-workspace /path/to/workspace --space project:atlas --dry-run` 预览映射。去掉 `--dry-run` 后导入。成功归并和来源缺失的旧条目均在私有迁移台账中保留旧 ID 和原始字段。

**飞书群历史归档。** 群归档使用飞书群空间和每条消息的 `msg_id`、`sender_id`、`timestamp`、`text`。可在清单中指定 `adapter = "feishu_group"` 和 `space = "group:群标识"`。同一归档完整重导时会处理缺失消息的撤回。解析失败的快照保留先前消息。绑定群与项目的操作为 `team-memory bind group:群标识 project:项目标识`。跨范围发布要求双方空间的人工复验权限和单独撰写的项目结论，解绑用 `unbind`。

## 数据与测试

SQLite 保存授权、证据关系、记忆版本、复验历史和任务进度。不可变原文保存在私有数据目录的 `events/`。备份包含原文和凭证摘要，应按源目录的权限保存。以下命令分别生成一致性快照、恢复到新目录，并运行自动化验收。

```sh
uv run team-memory backup ~/team-memory-backup
uv run team-memory restore ~/team-memory-backup ~/team-memory-restored
uv run pytest -q
```

任务范围、Codex 对照实验方法和飞书独立验收条件见 [开发与验收计划](docs/implementation-plan.md)。
