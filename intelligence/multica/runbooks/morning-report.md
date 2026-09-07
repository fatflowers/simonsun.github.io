# Morning Intelligence

目的：按 Asia/Shanghai 时区，在 08:30 为前一日晚报后至当日 08:15 的窗口生成中文早报并自动发布。

读者版规则：全文总量最多 100 条，其中最多 3 个重点，其余均可作为一句话快讯；快讯没有独立的 9 条或 12 条上限。每条紧邻原文链接，顶部提供 30 秒速览。不发布历史旧闻、列表/分页/个人主页、未知日期、仅有标题或固定话术分析；不得将抓取时间当成发布时间。没有合格事件时正常跳过，不凑数。

1. 出刊前完整执行 `analyze-pending.md` 的发现、正文补抓与分析流程，并检查所有启用目标的覆盖。日报补漏时把七天前的时间作为 `research run --since <ISO>` 和 `analyze pending --since <ISO>` 的共同 cutoff，不能只使用默认 72 小时队列。允许补读近七个日历日内未在日报发布的有效事件，但必须保留真实日期并标注“近期补读”，不能冒充今天的新消息。importance=2 的有来源事件可进入一句话快讯，但不得占用重点位置；importance=1、无正文、无日期或无证据的内容仍不发布。
2. 执行 `intelligence/scripts/intelctl-secure report generate --edition morning`。
3. 没有有效新内容时将 Run 标记为 `skipped`，原因写 `no_effective_new_items`，不创建空文章。
4. 有内容时检查关键事实的 evidence URL、推断标记、重复项和敏感信息。
5. generate 返回 `ready` 后，执行 `intelligence/scripts/intelctl-secure report publish --edition morning --execute --push --published-url <根据返回 path 生成的 fatflowers.github.io URL>`。成功路径为 `draft → validating → ready → published`。
6. 发布及重试必须完整执行 `intelligence/multica/runbooks/publication-check.md`。失败时停止、保留现场，创建或更新当前 Multica Issue；不得在 push 后未经部署和正文核验就宣布已上线。
7. 成功时在 Issue 中记录 `pipeline_run_id`、`report_id`、Git commit、published URL、来源数量和最高重要度。

早报必须设置 `hiddenInHomeList: true`。
