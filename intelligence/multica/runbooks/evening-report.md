# Evening Intelligence

目的：按 Asia/Shanghai 时区，在 19:00 为当日 08:15 至 18:45 的窗口生成中文晚报并自动发布。

执行仓库日报编辑规则：全文总量最多 100 条，其中最多 3 个重点，其余均可作为快讯；快讯没有独立的 9 条或 12 条上限。每条紧邻原文。无可核验日期或正文、目录页、旧闻和模板化分析不得出刊；没有合格新事件则跳过。

1. 完整执行 `analyze-pending.md` 的逐目标发现、正文补抓及分析，核对覆盖缺口，不能只检查旧分析队列。日报补漏时把七天前的时间作为 `research run --since <ISO>` 和 `analyze pending --since <ISO>` 的共同 cutoff，不能只使用默认 72 小时队列。纳入窗口内事件及近七个日历日尚未在日报发布的合格补读；补读保留真实日期和“近期补读”标记。importance=2 的有来源事件可进入一句话快讯，但不得占用重点位置；importance=1、无正文、无日期或无证据的内容仍不发布。
2. 执行 `intelligence/scripts/intelctl-secure report generate --edition evening`。
3. 没有有效新内容时将 Run 标记 `skipped`，原因写 `no_effective_new_items`，不创建空文章。
4. 有内容时去除已发布条目，完成全部自动发布门禁。
5. generate 返回 `ready` 后，执行 `intelligence/scripts/intelctl-secure report publish --edition evening --execute --push --published-url <根据返回 path 生成的 fatflowers.github.io URL>`；发布失败则保留 draft 并创建或更新 Issue。
6. 在 Issue 中记录 Run、Report、Commit、URL、来源数量、目标覆盖和结转条目数。
7. 发布及重试完整执行 `intelligence/multica/runbooks/publication-check.md`，实际核对对应部署与线上正文后再宣称已上线。

晚报必须设置 `hiddenInHomeList: true`。
