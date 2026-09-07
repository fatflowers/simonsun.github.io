# Weekly Strategic Review

目的：每周日 20:00 生成最近七天的中文战略情报周报并自动发布。

每个信息单元必须关联就近原文；只使用已核验正文与日期的具体事件。全文总量最多 100 条，其中最多 3 个重点，其余均可作为补充；补充内容没有独立的 9 条或 12 条上限。没有足够证据时跳过，不发布标题拼接或列表页摘要。

1. 先执行 `analyze-pending.md` 的逐目标发现、正文补抓及分析，再读取最近七天已分析条目、日报及相关历史事件。research 默认回看 72 小时；补抓周报漏项时显式传入本周起点的 `--since <ISO>`，保留真实日期。不能因默认窗口过短就声称一周无更新。
2. 以目标、稳定标签和动态主题聚合，优先识别持续趋势、方向变化与跨目标关联。
3. 执行 `intelligence/scripts/intelctl-secure report generate --edition weekly`。
4. 每项趋势判断至少引用两条独立事件证据；无法验证的判断明确标为推断。
5. 完成公共来源、敏感信息、Front Matter、Hugo build、Git diff 和路径范围检查。
6. generate 返回 `ready` 后，执行 `intelligence/scripts/intelctl-secure report publish --edition weekly --execute --push --published-url <根据返回 path 生成的 fatflowers.github.io URL>`；失败则保留 draft 并更新 Multica Issue。
7. 在 Issue 中记录 Run ID、Report ID、Git commit、URL、覆盖目标和事件数量。
8. 发布及重试完整执行 `intelligence/multica/runbooks/publication-check.md`，实际核对对应部署与线上正文后再宣称已上线。

周报设置 `hiddenInHomeList: false`。信号较少时保持简短，无合格证据则正常跳过，不得用无证据内容填充篇幅。当前自动生成器的实际能力以输出为准：没有跨事件综合时不能把简单事件清单宣称为战略趋势分析。
