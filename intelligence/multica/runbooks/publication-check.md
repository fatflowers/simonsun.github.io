# 发布与重试验收（所有版次共用）

1. 同一次 generate/publish 固定相同的 edition、date、窗口及其他参数；跨午夜也不能换日期。读取 generate 返回状态：只有 ready 才继续，skipped 正常结束，failed 停止并记录具体门禁。CLI 已写入 Run 状态，不另行猜测状态修改命令。
2. 阅读实际 Markdown：30 秒速览能说明看点，重点说明发生什么、对谁有用、下一步怎么做；每条重点和快讯紧邻可点击原文。同一事件不能用不同来源重复占位。补读标明真实日期，不拿采集时间冒充发布时间。不要为数量阈值调整事实或评分。
3. 发布命令返回后，记录 pipeline_run_id、report_id、commit_sha、pushed 和完整公开 URL。只允许本报告路径及必要图片进入提交；不能清理、提交或覆盖工作区其他改动。
4. 用 `gh run list --workflow hugo.yml --commit <commit_sha> --limit 5 --json databaseId,status,conclusion,headSha,url` 查该提交的构建与部署；对同一个仍在执行的 run 查询 `gh run view <databaseId> --json status,conclusion,headSha,url`。采用有限轮询，总等待不超过 10 分钟，不启动重复发布。部署失败立即记录失败证据；观察超时记“部署尚未验证”，不可称已上线。
5. 部署成功后实际 GET 返回的公开 URL，要求 HTTP 200，并核对本次标题、每条信息及其就近原文链接。200 但内容仍旧也不算通过。普通 GET 用 curl 或浏览器即可，不需要额外凭据；禁止只做 HEAD 或只看首页。
6. 重试前通过 `intelligence/scripts/intelctl-secure run list --limit 20`、`run show <run_id>`、对应 Git 提交和以上部署/正文检查确认前次结果。如果同版同日文章已上线且与原提交一致，记录已发布并结束，不重复 generate/publish、不创建第二篇、不把零新条目当事故。如果已 push 但仍部署中，只跟踪原 run；如果数据库与 Git/线上不一致，保留证据并更新故障 Issue，不能直接 SQL 改状态或自动覆盖已发布文章。
7. 只有部署与线上正文均通过才向读者宣称“已发布”。若 push 成功而部署失败，明确报告这一区别，即使 D1 已为 published 也不能隐瞒。修订历史文章须走项目已实现的审计修订流程，定时任务不得自创 API、删记录或重置 Git。
8. 线上正文核验成功后读取 `intelligence/config/notifications.yaml` 的 `report_lark`。morning、midday、evening、weekly、ad-hoc 所有报告都必须通知：
   - 先运行 `intelctl audit list --entity-type report --entity-id <report_id>`；已存在 action=`lark.report_sent` 时跳过。
   - 使用 bot 身份向唯一配置群发送 Markdown：`lark-cli im +messages-send --as bot --chat-id oc_6a65bd218e5dd85031c2d04814de54e1 --markdown <内容> --idempotency-key <稳定且不超过50字符的report键>`。
   - 消息包含报告标题、版次、周期、简介、条目数量和完整 `published_url`。不得在公开 URL 尚未通过 GET 与 artifact 指纹核验前发送。
   - 飞书返回 `ok=true` 后，用 `intelctl audit create --actor intelligence-operator --action lark.report_sent --entity-type report --entity-id <report_id> --after <任务目录内JSON>` 记录 chat_id、message_id、published_url、sent_at。
   - 发送最多重试 2 次。通知失败不撤销已发布报告，但必须记录为发布后通知失败并保留下一轮补发；响应不确定且没有 message_id 时不得盲目重复。
