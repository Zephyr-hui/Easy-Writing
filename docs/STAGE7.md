# 阶段七：改稿、恢复、备份与整体验收

第七阶段补齐跨章依赖、保守的作者标注迁移、待复核隔离、派生资料刷新、入库恢复、数据库备份恢复、作品知识导出和调用统计。前七阶段的第一版开发范围已实现；自然语言判断质量仍以实际小说和作者复核为准。

## 1. 工作台入口

继续运行原来的本地工作台：

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

在“项目导入 → 改稿与数据维护”中查看依赖、待复核清单、失效摘要、恢复任务、备份 / 导出和用量。首页显示新稿尚未入库或有待复核内容的提示。保留原有四个主要入口。

旧数据库首次打开时升级至 v6，保留 `.pre-v6.bak`。迁移没有覆盖原稿，也不触发模型请求；升级前的工作流任务和作者历史仍保留。

## 2. 改稿后的处理规则

以相同章节号导入新正文。新原文先保存为不可变版本；提取未完成时，旧章节知识不作为新稿使用。改稿和提取发布都记录变更。

只有同时满足以下条件才自动沿用作者标注：

- 旧、新记录的语义字段一致。
- 原文证据所在的整段文字、引用文字与段内位置一致。
- 在同章旧、新结果中各有唯一匹配。
- 相关生效、结束、获知、身份及回收锚点仍能对应当前版本。

符合条件时沿用确认状态、事件时间、身份对应、剧情线角色、事实模式、获知标注、伏笔转换和可安全改绑的规则 / 使用关联。变更对照保存在 `record_successions`；旧记录和作者历史不删除。迁移连接沿用原来的方向与类型，不因为事件前后关系新增因果。

内容改变、引用段落改变、同名同文多条候选或锚点失效时不猜测。原有作者决定保留为待复核项目；新的提取结果继续是候选。原回收事件改变时，伏笔的旧状态不能自动当作新稿的回收结论。

这种匹配是局部文本与结构相同的保守迁移，不是全文语义等价证明。相同句子处于不同叙述语境、作者改变意图等情况，仍需人工复核。

## 3. 跨章依赖与隔离

事件生效 / 结束锚点、角色获知事件、信息使用及伏笔转换中已明确的事件依赖会在章节改稿时传播。其他语义依赖需要作者登记，例如第十三章沿用第十二章的密码：

```powershell
$stageDb = "data/stage7_demo/novel.db"
$acceptance = Get-Content data/stage7_demo/stage7_bge_acceptance.json -Raw -Encoding utf8 | ConvertFrom-Json
$bookProjectId = $acceptance.project_id
.\.venv\Scripts\python.exe -m easy_writing revision depend --project $bookProjectId --source-kind fact --source "上游事实ID" --target-kind fact --target "后文事实ID" --reason "后文使用此处的口令" --db $stageDb
```

上游变更沿依赖图传播，循环依赖采用已访问集合终止。受影响记录进入待复核清单；为避免同章摘要、事件描述或原文旁证间接带回待核查信息，所在章节暂时整体退出模型知识资料与事实查询。知识列表仍供作者查看，并标记 `requires_review`；资料包覆盖范围列出 `quarantined_chapters` 与待复核数量。

没有登记、也没有数据库锚点的语义关系不会被擅自推断。系统保守失效改稿章及后文章节的摘要，提醒刷新，不直接否决后文所有独立事实。作者仍应登记重要的跨章前提。

查看与处理：

```powershell
.\.venv\Scripts\python.exe -m easy_writing revision status --project $bookProjectId --limit 50 --offset 0 --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing revision resolve --project $bookProjectId --review "复核项ID" --reason "已对照新稿修正后文，或确认这里保留有意误导" --db $stageDb
```

正文确实改变时，应重新写入并提取受影响章节，再登记新的依赖；不能把“解除隔离”当作改写正文或自动修正事实。仅在已经核对当前记录时填写复核原因。已退休记录的复核项用于处理旧作者标注，不会把旧记录恢复成当前事实。伏笔显式 `--correction` 重新确认时，也会记录并解除该伏笔的相关待复核状态。

## 4. 刷新摘要、向量、路径与检查

```powershell
.\.venv\Scripts\python.exe -m easy_writing revision refresh --project $bookProjectId --embedding bge --db $stageDb
```

`--embedding none` 只刷新本地派生资料；`fake` 验证测试向量流程。刷新操作：

1. 从当前知识生成明确标为“结构化刷新摘要”的概览，标出确认状态和角色观点，不伪装为原文逐句摘要。保留来源引用与刷新历史；有待复核记录的章节继续延期。
2. 按内容哈希与模型身份增量更新向量。旧向量保留作历史，当前查询只使用当前记录和哈希。
3. 重建时间线与各剧情线概览 / 路径视图，保存快照。
4. 运行新的本地一致性检查，保存当前报告。

更改核心 / 普通角色使概览与报告过期，但正文检索文本未变时不重做向量。更改连接重新计算路径、约束与报告；更改事件 / 事实文字更新有关摘要和检索文本。普通节点、完整证据和其他剧情线角色继续保留。

旧报告和计划仍可审计，标记快照过期；不会静默删除作者历史。新查询排除旧代和隔离内容。刷新各步骤提交结果时核对快照，遇到并发改稿拒绝保存混合版本的派生视图。检查与上述摘要刷新在本机完成，不调用 LLM；BGE 嵌入也是本地计算。

## 5. 入库断点恢复

```powershell
.\.venv\Scripts\python.exe -m easy_writing recovery jobs --project $bookProjectId --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing recovery resume --project $bookProjectId --job "入库任务ID" --provider deepseek --db $stageDb
# 离线恢复：
.\.venv\Scripts\python.exe -m easy_writing recovery resume --project $bookProjectId --job "入库任务ID" --provider fake --fixture "恢复响应.json" --db $stageDb
```

恢复读取数据库中的不可变原文及提取规格，校验模型地址 / 名称 / 思考配置、原输出预算、分块参数与提示签名。仅请求未完成分块；已完成块继续验证出处并复用。整章最终原子发布，重复恢复已发布的当前任务返回缓存，不重复写知识。

活动租约尚未到期时拒绝抢占。服务中断后需确认旧进程已退出，并在租约过期后明确恢复；不会因页面重运行自动恢复。原文已更新、已有更晚提取代次或提取器签名变化时拒绝继续，避免恢复操作回滚新稿。

v6 之前没有保存恢复规格的旧任务，仍可使用原文件和原提取配置显式 `ingest --retry`；程序不会根据任务哈希猜测旧模型配置。新增调用登记尝试归属，即使后续证据验证失败，也可在原日志中查到对应用量。

## 6. 备份、恢复和导出

完整数据库备份使用 SQLite backup API，包含数据库中所有作品、原文、向量、作者历史和任务结果；不包含 `.env`、外部日志和模型文件。

```powershell
.\.venv\Scripts\python.exe -m easy_writing archive backup --output data/backups/novel-001.db --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing archive restore --backup data/backups/novel-001.db --output data/restored/novel-001.db --db $stageDb
```

备份旁保存 SHA-256 校验清单，并检查数据库完整性与外键。恢复前验证数据库及存在的校验清单，只写入未占用的新文件；不能覆盖当前库、源备份或已有输出。完成后从工作台左侧手动切换到恢复库，原工作库保留。

备份保存的是一致时间点，可能含运行中状态和租约。恢复副本不会自动调用模型；恢复入库前应确认旧工作库中的同一任务不再执行。

按作品导出当前知识：

```powershell
.\.venv\Scripts\python.exe -m easy_writing archive export --project $bookProjectId --output data/exports/novel-001.json --db $stageDb
```

导出包括当前作品、当前章节、有效知识、引用、可用依赖、覆盖缺口和作者历史元数据；隔离记录不冒充当前知识。默认附当前正文，添加 `--no-sources` 可省略正文。历史元数据仍保留作者原因，未导出旧知识全文或其他作品内容。该 JSON 用于知识交换，完整可恢复副本请用 SQLite 备份。

## 7. 调用统计

```powershell
.\.venv\Scripts\python.exe -m easy_writing usage-stats --project $bookProjectId --log data/llm_calls.jsonl --db $stageDb
# 不加 --project：统计所提供日志的全局尝试；可重复 --log 合并日志。
```

按提供日志中的 `(call_id, attempt, provider)` 去重，分别显示请求数、重试次数对应的尝试数、接口失败尝试、已知输入 / 输出 / 合计用量和未知用量次数。不把失败、超时或缺失日志当成零费用，不写死价格。

作品归属来自入库尝试、缓存分块、工作流与计划中的调用编号。没有可靠归属的请求另列，不冒充作品总费用。调用元数据日志继续不保存提示与回复；数据库里的作者任务结果和知识导出可能含小说资料。

## 8. 更长样稿的实测

```powershell
.\.venv\Scripts\python.exe scripts/demo_stage7.py
# 真实本地语义模型：
.\.venv\Scripts\python.exe scripts/demo_stage7.py --embedding bge --chapters 120
```

默认使用测试向量，只验收流程。真实 BGE 首次需要模型文件，本机已有缓存；后续可在终端设置 `HF_HUB_OFFLINE=1` 使用缓存。脚本每次创建独立演示作品，保留已有作品和历史。

本轮真实 BGE 实测：

| 项目 | 结果 |
|---|---|
| 样稿规模 | 120 章，179040 字符，初始有效知识 480 条 |
| 模型 | BAAI/bge-small-zh-v1.5，512 维，固定提交 7999e1d3359715c523056ef9478215996d62a620 |
| 四条带章节实体的换措辞查询 | 标准事实均排在第 1 位 |
| 已建语料集后的热查询核心耗时 | 中位数 478.14ms，最大 480.43ms |
| 首次查询核心耗时，含增量索引 | 1837.33ms |
| 单独记录的模型冷加载 | 19260.22ms |
| 单次资料包预算估计 | 3638～3974；全文估计 533280，估计单位为 UTF-8 字节 |
| 付费模型调用 | 0 |

查询核心耗时不包含语料集构建和模型加载，不是端到端问答耗时。资料包估计不是 DeepSeek 计费 token，不能据此推算固定节省比例。正文含重复的规模测试填充，事实由人工 fixture 标注；四条检索样本不代表任意题材的提取、推理或创作准确率。

演示验证第十二章改稿后旧值不进入当前资料、后文依赖隔离直至改写 / 复核、普通节点与证据保留、旧报告过期、重复索引新增 0、两块任务只续传缺失的 1 块，以及备份恢复一致。报告见 `data/stage7_demo/stage7_bge_acceptance.json`，测试向量报告为 `stage7_acceptance.json`。

基于本机该规模实测，第一版继续使用 SQLite + NumPy。更大的作品、多作者并发或更严格延迟目标，应先按同样边界扩大评测，再决定是否需要其他存储或索引方案。

完整回归 256 项通过，新增 17 项，包括维护面板的显式刷新 / 备份与重运行检查；现有页面交互共 7 项。`pip check`、wheel 与新增模块 / v6 SQL 资源检查通过。已有 v5 库副本升级保持全部旧表计数、2 份计划和 4 项工作流任务，外键和升级前备份均通过验证，原始库保持原状。本轮未进行新的浏览器目视检查，界面由 AppTest 验收。
