# Personal Intelligence

这是 `fatflowers.github.io` 仓库中的公开情报系统。它以 Target → Channel → Tag 组织来源，通过 AIsa MCP/RSS/HTTP 等固定采集器获取公共信息，使用 D1 保存运行数据，由 Multica Cloud + Codex 分析，并将中文报告自动发布到当前 Hugo 博客。

生产资源：

- Worker：`https://personal-intelligence-api.sunliumin.workers.dev`
- D1：`personal-intelligence`（APAC）
- Multica：TomSun / 个人信息站点 / Intelligence Operator
- 公开报告：`https://fatflowers.github.io/zh/categories/intelligence/`

## v1 边界

### 读者版编辑标准（2026-09-06）

- 30 秒速览、全文最多 100 条，其中最多 3 个重点，其余作为快讯且不设独立的 9 条或 12 条上限；每条紧邻原文与补充证据，不在文末堆来源。
- 不展示星级、模型置信度百分比或内部分类标签，公开标签最多 5 个。
- 只发表窗口内、有正文支持的具体新事件；目录、分页、GitHub 主页、discovery_only、baseline、未知日期和固定话术分析均被排除。
- 无合格事件时正常跳过。抓取成功不等于值得出刊。
- 当前网页 Diff 尚无可信的前后对照证据与变更时间；无原始发布时间的页面暂不进入新闻报告，避免误报。
- 已发布内容的人工授权更正通过 editorial-revision 接口保留旧正文、commit 和原因的审计记录。

- 只采集公共来源，只生成公开报告。
- Git 中的 YAML 是目录和策略的配置来源；D1 保存同步副本与运行数据。
- 原始网页、截图、浏览器 Profile、Token 和 API Key 不进入 Git。
- MCP 工具只在首次接入或契约变化时发现；日常采集调用固定 binding。
- 自动报告通过全部校验后直接提交并推送 `main`，不走人工审核。

## 目录

```text
intelligence/
├── config/
│   ├── catalog.yaml
│   ├── mcp-tools.yaml
│   ├── report-policy.yaml
│   └── schedules.yaml
├── multica/
│   ├── agent-instructions.md
│   ├── config.yaml
│   ├── deployment-status.yaml
│   ├── runbooks/
│   └── skill/SKILL.md
└── launchd/
```

应用代码、Schema、Worker 和测试由相邻目录承载；本 README 只描述配置和运维契约。

## 首批目录

- 目标：Composio、OpenAI、Anthropic、Simon Willison、MCP Ecosystem。
- 核心频道：22 个，配置为 enabled。
- 补充频道：6 个，配置为 disabled。
- 配置中的 enabled 表示业务期望；MCP 频道还必须满足 binding `status: verified` 才能进入定时采集。

快速检查：

```bash
intelligence/scripts/intelctl-secure catalog validate
intelligence/scripts/intelctl-secure target list
intelligence/scripts/intelctl-secure channel list --target composio
```

## 配置修改流程

```text
自然语言请求
→ Intelligence Operator 读取现状
→ intelctl dry-run / before-after
→ Schema 校验
→ 新频道最小只读测试与契约测试
→ 写入并同步 D1
→ audit event
→ Git commit
→ 返回 Run ID、影响范围和下一次运行时间
```

目标和频道的“删除”默认是 disable。不要手改生产 D1，也不要在定时采集时临时搜索替代 MCP 工具。

## 调度职责

- Multica Cloud：每 30 分钟采集、每小时分析，08:30 早报，13:00 高信号午报，19:00 晚报，周日 20:00 周报，每日 07:45 健康检查。
- 所有时间均使用 `Asia/Shanghai`。
- 调度声明来源是 `config/schedules.yaml`；修改时同步更新对应 Multica Autopilot trigger。

## 自动发布门禁

报告必须依次通过：

1. 来源均为公共 URL。
2. 关键事实有 evidence。
3. 敏感内容扫描。
4. Front Matter 检查。
5. `hugo --minify`。
6. `git diff --check`。
7. 变更路径只在 `content/posts/intelligence/` 与 `static/images/intelligence/`。

任何一项失败都停止发布并保留 draft。Git 冲突不得自动解决后强推。

## 状态追踪

实施任务只使用：`未完成 → 进行中 → 已完成`。Multica 相关配置状态记录在 `multica/deployment-status.yaml`，主项目状态仍以设计文档任务表为准。

运行状态使用：`pending → running → succeeded|failed` 或 `pending → skipped`。每次执行至少应关联 `pipeline_run_id` 与 `multica_run_id`；报告运行还应关联 `report_id`、Git commit 和 published URL。

## 故障原则

- 单次临时失败只记录并有限重试。
- 同一频道连续失败三次创建或更新 Multica Issue。
- MCP 未授权、工具不存在、Schema 变化、报告失败、发布失败或所有频道失败立即报告。
- 没有新内容或午报阈值未达到属于 skipped，不是失败。
- 恢复后更新原 Issue，避免重复告警。

## 密钥

本仓库不得出现真实密钥。开发环境使用 `.dev.vars` 或 Keychain；Cloudflare 使用 Worker Secret；GitHub Actions 使用 GitHub Secrets。不要在 Multica Issue、日志或报告中粘贴凭据。

## Multica Cloud 应用

声明文件位于 `multica/config.yaml`，目标项目为“个人信息站点”。实际创建 Agent、Skill、Autopilot 时，应把平台返回的非敏感 ID 和验收证据写入 `multica/deployment-status.yaml`，不得把平台凭据写回仓库。

Worker、D1、Intelligence Operator、Skill 与 Autopilot 的实际 ID 记录在 `multica/deployment-status.yaml`。
