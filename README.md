# Team Memory

Team Memory 是一个可自托管的团队记忆服务。Codex 和其他 Agent 通过 MCP 或 HTTP 查询个人、项目和群组空间，返回的结论带版本、状态和可见证据。后台按日提取记忆，有疑点的结论进入人工复验队列。第一版支持飞书群历史归档导入。

## 本地运行

需要 Python 3.11 或以上版本、支持 FTS5 的 SQLite 和 uv。在仓库根目录执行以下命令。示例使用虚构的 Atlas 项目，真实记忆建议使用另一份私有数据目录。

```sh
uv sync --locked
uv run team-memory init --data-dir ~/.local/share/team-memory-atlas
uv run team-memory grant demo project:atlas review
uv run team-memory grant demo personal:demo review
uv run team-memory import --manifest examples/codex/atlas.toml --dry-run
uv run team-memory import --manifest examples/codex/atlas.toml
uv run team-memory digest --space project:atlas --date 2026-11-16
uv run team-memory digest --space personal:demo --date 2026-11-16
uv run team-memory serve
```

默认监听 `127.0.0.1:8765`。服务启动时也启动定时 worker，处理已入库的材料。可以用 `TEAM_MEMORY_DIR` 覆盖数据目录。独立部署 worker 时使用 `serve --no-worker` 和 `team-memory worker`。交互式 HTTP 文档位于本机服务的 `/docs`，健康检查在 `/health`。

## 连接 Codex

在启动 Codex 的终端设置凭证。只用项目记忆的客户端可省略个人空间授权。每次请求同时校验主体的空间权限和令牌范围，`--max-role read` 可以签发只读客户端凭证。

```sh
export TEAM_MEMORY_TOKEN="$(uv run team-memory token demo --client codex --space project:atlas --space personal:demo)"
codex mcp add team-memory --url http://127.0.0.1:8765/mcp --bearer-token-env-var TEAM_MEMORY_TOKEN
```

打开新会话，问“用 team-memory 查 Atlas 决定哪天上线，给出来源，再列出我的待复验记忆”。样例的项目决定应返回 **2026-11-23**，对小林分工的猜测应出现在个人待复验列表。桌面客户端需要能读取同名环境变量。管理员可用 `team-memory revoke-token demo --client codex` 收回该客户端已有的令牌。服务注册使用 [Codex MCP 配置](https://developers.openai.com/codex/mcp/)。

| MCP 工具 | 用途 |
| --- | --- |
| `get_context` | 获取指定空间的当前结论和来源 ID，预算按序列化后的字符数计。显式设置 `include_pending` 可附带待确认项。 |
| `search_memory` | 按词、中文短词或项目代号检索有效结论。 |
| `get_memory` | 获取正文、来源片段、历史状态。可指定版本，或用 `as_of` 和 `known_at` 查询历史。 |
| `record_memory` | 提交新线索或修订。传入稳定的 `request_id`，可附来源 ID 和原文引述。 |
| `list_reviews`、`open_review` | 查看有权复验的事项及其本机页面地址。 |

### 人工复验

用户单独执行 `uv run team-memory token demo --client reviewer --human --space personal:demo --space project:atlas`，在本机 `/review` 页面输入该凭证。Agent 凭证可以协助列出待办，提交决定要求人工凭证。页面支持确认、修改后确认、驳回及暂缓。服务记录处理人、修改前后的结论和处理时间。两人同时处理同一项时，第二次提交会要求刷新。

### 其他 Agent 使用 HTTP

将相同凭证放入 `Authorization: Bearer` 请求头即可。以下命令取用项目当前结论。

```sh
curl -sS http://127.0.0.1:8765/v1/context \
  -H "Authorization: Bearer $TEAM_MEMORY_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"space":"project:atlas","budget":3000}'
```

用 `POST /v1/sources` 逐项写入来源版本，`POST /v1/memories` 提交引用它的结论。批量来源接口会报告每一项的结果。外部 Agent 提交的来源独立记账并进入候选流程；项目名、作者名和客户端自报的判断都不能代替授权。确需撤回时，复验人可调用 `DELETE /v1/sources/{source_id}` 或带版本号的 `DELETE /v1/memories/{id}?version=1`。

## 导入与定时总结

**Codex 和项目材料。** 在 TOML 清单中逐项指定来源类型、稳定标识、文件、发生时间、目标空间。个人笔记、选中的 Codex 摘要和项目评审记录都支持增量重导。建议用显式 `occurred_at` 保留业务时间。对上游编辑使用新 `revision`；完全恢复为曾用过的旧文本时也应分配新版本。Codex 原生记忆保持只读。清单写法见 [Atlas 示例](examples/codex/atlas.toml)。

**抽取模式。** 默认离线模式将普通段落作为待复验线索；清单中经操作者核验的原文可以使用 `[decision|发布窗口]` 等标记指定类型和主题。可信作者须在对应空间有复验权。Codex 二次摘要始终保留派生来源身份。在模型模式下设置 `TEAM_MEMORY_MODEL_URL` 为完整的 OpenAI 兼容 chat completions 地址，`TEAM_MEMORY_MODEL` 为模型名，按需设置 `TEAM_MEMORY_MODEL_KEY`。模型会收到所处理来源的正文，输出必须带能在原文中定位的引文。仅在对应数据可交由该模型处理时启用。证据不足、推测、责任分配不明或变更既有决定时均进入复验。

**作业与日报。** 默认每天当地时间 02:00 处理前一日及积压的来源。用 `TEAM_MEMORY_TIMEZONE` 和 `TEAM_MEMORY_SCHEDULE` 设置时区和时间。手动 `digest` 可指定空间和日期补跑。失败批次可重试。查询 `GET /v1/digest?space=project:atlas&day=2026-11-16` 获取按当前权限和撤回状态生成的 Markdown，可按需保存。

**CoPaw 和飞书。** 管理员先配置目标空间，运行 `team-memory import-copaw ~/.copaw --space project:示例项目 --dry-run` 检查映射。去掉 `--dry-run` 才会导入。程序仅读取选定工作区及相关历史索引。成功归并和来源缺失的旧条目在私有迁移台账保存旧 ID 和原始字段。群归档使用飞书群空间和每条消息的 `msg_id`、`sender_id`、`timestamp`、`text`；可在清单中指定 `adapter = "feishu_group"` 和 `space = "group:群标识"`。同一归档完整重导时会处理缺失消息的撤回；解析失败的快照保留先前消息。绑定群与项目的操作为 `team-memory bind group:群标识 project:项目标识`。跨范围发布要求双方空间的人工复验权限和单独撰写的项目结论，解绑用 `unbind`。

## 数据与测试

SQLite 保存授权、证据关系、记忆版本、复验历史和任务进度。不可变原文保存在私有数据目录的 `events/`。备份包含原文和凭证摘要，应按源目录的权限保存。以下命令分别生成一致性快照、恢复到新目录，并运行自动化验收。

```sh
uv run team-memory backup ~/team-memory-backup
uv run team-memory restore ~/team-memory-backup ~/team-memory-restored
uv run pytest -q
```

任务范围、Codex 对照实验方法和飞书独立验收条件见 [开发与验收计划](docs/implementation-plan.md)。
