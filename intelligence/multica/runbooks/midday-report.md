# Midday High Signals

目的：按 Asia/Shanghai 时区，在 13:00 仅当早报后至 12:45 出现 importance ≥ 4 的新信号时生成并自动发布午报。

执行仓库日报编辑规则：全文总量最多 100 条，其中最多 3 个重点，其余均可作为快讯；快讯没有独立的 9 条或 12 条上限。每条紧邻原文。无可核验日期或正文、目录页、旧闻和模板化分析不得出刊；没有合格新事件则跳过。

1. 完整执行 `analyze-pending.md` 的逐目标发现、正文补抓及分析，核对覆盖缺口。日报补漏时把七天前的时间作为 `research run --since <ISO>` 和 `analyze pending --since <ISO>` 的共同 cutoff。近七个日历日内未在日报发布的合格补读仍需达到午报阈值，并保留真实日期和“近期补读”标记。午报继续保持 importance>=4，不应用早晚报的放宽规则。
2. 执行 `intelligence/scripts/intelctl-secure report generate --edition midday`。
3. 若无 importance ≥ 4 的未发布条目，将 Run 标记 `skipped`，原因写 `importance_threshold_not_met`，保持静默且不创建空文章。
4. 若达到阈值，完成 evidence、敏感信息、Front Matter、Hugo build、Git diff 和变更路径检查。
5. generate 返回 `ready` 后，执行 `intelligence/scripts/intelctl-secure report publish --edition midday --execute --push --published-url <根据返回 path 生成的 fatflowers.github.io URL>`；失败则保留 draft 并创建或更新 Issue。
6. 只在实际发布、失败或需要用户操作时创建/更新 Issue；普通 skipped 不通知。
7. 发布及重试完整执行 `intelligence/multica/runbooks/publication-check.md`，实际核对对应部署与线上正文后再宣称已上线。

午报必须设置 `hiddenInHomeList: true`，已在早报使用的条目不得重复发布。
