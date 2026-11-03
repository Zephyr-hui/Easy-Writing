# 阶段五：伏笔生命周期与一致性检查

第五阶段提供本地检查服务与命令行入口。它遍历当前已发布知识，生成有双方证据、判断条件和修复建议的报告；作者可以忽略、标记有意安排、标记已处理或重新打开问题。检查本身不调用 DeepSeek，不读取整部小说，也不自动修改事实或伏笔状态。

## 1. 运行验收样稿

在 VS Code 的项目终端执行：

```powershell
.\.venv\Scripts\python.exe scripts/demo_stage5.py
```

脚本使用 `examples/stage5/book.json` 的六章样稿及人工标准标注，在独立数据库 `data/stage5_demo/novel.db` 中创建新演示项目。每次运行保留已有项目、作者状态及历史；人工补录的 123 条分页候选明确标为测试数据，不代表自动发现了这些伏笔。

验收包含：

- 正常转交与真正重叠持有的区分。
- 倒叙按故事时间比较，不按章节编号判断先后。
- 使用密码早于明确获知事件的疑点。
- 事实违反作者确认的世界规则。
- 伏笔部分回收建议，由作者确认后才修改状态。
- 开放解释不当作漏填；125 条伏笔全量分页盘点。
- 有意安排在相同依据的重跑中保留；知识变化后旧报告标为过期。

结果文件：`data/stage5_demo/stage5_acceptance.json`、`stage5_report.json`。提取响应来自人工 fixture，验收验证结构化检查逻辑；实际小说的自动提取质量仍需另行评测。

完整回归 213 项通过，第五阶段新增 25 项，覆盖超过 100 条问题的完整分页、关联方向、证据失效、作者重审、规则区间、跨项目隔离及损坏时间网络。`pip check`、wheel 构建与新增迁移资源检查通过。已有数据的 v3 旧库副本升级到 v4，原有表计数、13 条事实、7 个事件与外键保持正确，升级前备份可打开，原始库保持原状。

## 2. 查看与处理检查报告

演示运行后，取得项目编号：

```powershell
$reviewDb = "data/stage5_demo/novel.db"
$acceptance = Get-Content data/stage5_demo/stage5_acceptance.json -Raw -Encoding utf8 | ConvertFrom-Json
$bookProjectId = $acceptance.project_id
$report = .\.venv\Scripts\python.exe -m easy_writing check run --project $bookProjectId --db $reviewDb | ConvertFrom-Json
$report | ConvertTo-Json -Depth 30
```

自己的作品使用 `project list --db 实际数据库路径` 取得项目 ID，再替换上面变量。`check run --chapter 2` 仅检查正文边界内的当前知识；报告面向作者，章节参数限定来源范围，不是历史作者编辑状态的存档。

查看已有报告和分页问题：

```powershell
.\.venv\Scripts\python.exe -m easy_writing check list --project $bookProjectId --db $reviewDb
.\.venv\Scripts\python.exe -m easy_writing check show --project $bookProjectId --run $report.id --limit 50 --offset 0 --db $reviewDb
```

`finding_total` 是整份报告问题数，`filtered_total` 是处理状态筛选后的问题数。`findings` 默认返回前 100 条，按 `next_offset` 继续读取，直到为 `null`。检查阶段本身扫描全部当前记录，不使用向量检索的 Top-K。

作者处理问题：

```powershell
$findingId = $report.findings[0].id
.\.venv\Scripts\python.exe -m easy_writing check decide --project $bookProjectId --finding $findingId --status intentional --reason "此处是作者有意安排，后文揭示" --db $reviewDb
.\.venv\Scripts\python.exe -m easy_writing check history --project $bookProjectId --db $reviewDb
```

处理状态：`open` 待处理、`ignored` 暂不处理、`intentional` 有意安排、`resolved` 作者标记已处理。后三种不进入 `open_issues`；仍保留检测依据、原始严重度和处理原因。`resolved` 是作者决定，不等于系统已自动验证正文修复。

相同依据的再次检查保留作者决定。对象、证据或相关时间依据变化时不沿用旧决定；时间网络变化采用保守策略，可能要求重新审阅。已过期报告可供审计，不能再提交问题决定，需要重新检查。决定历史单独保存，不会仅因处理状态变化就让知识快照过期。

报告结论分为：

| certainty | 含义 |
|---|---|
| `confirmed_conflict` | 已确认、明确的结构化依据满足冲突条件；仍以原文与标注正确、完整为前提 |
| `needs_review` | 时间、身份、获知渠道或提取结果尚需核查 |
| `suggestion` | 建议，例如可能的部分回收；不自动写入知识 |

## 3. 伏笔状态与作者确认

```powershell
.\.venv\Scripts\python.exe -m easy_writing thread list --project $bookProjectId --limit 50 --offset 0 --db $reviewDb
.\.venv\Scripts\python.exe -m easy_writing thread list --project $bookProjectId --state planted --db $reviewDb
```

`total`、`next_offset` 用于完整分页，`state_counts` 提供当前范围全部伏笔的状态计数。候选、已埋设、开放解释等都会进入盘点。

| 状态 | 含义 |
|---|---|
| `candidate` | 提取到的候选细节，尚未确认是伏笔 |
| `planted` | 作者确认埋设 |
| `reinforced` | 后续强化，可重复记录 |
| `partially_resolved` | 部分回收，可继续强化或回收 |
| `resolved` | 作者确认全部回收 |
| `cancelled` | 作者取消 |
| `open` | 作者保留开放解释 |

确认转换时始终填写原因，可关联当前事件或当前原文证据：

```powershell
.\.venv\Scripts\python.exe -m easy_writing thread transition --project $bookProjectId --id "伏笔ID" --state planted --event "埋设事件ID" --reason "确认此细节为后文铺垫" --db $reviewDb
.\.venv\Scripts\python.exe -m easy_writing thread transition --project $bookProjectId --id "伏笔ID" --state partially_resolved --event "回收事件ID" --reason "已解释含义，但仍有缺口" --db $reviewDb
```

事件和证据参数都留空时，原因本身记录作者决定，该转换仅进入完整作者视图。给出 `--chapter` 时，只采用该范围中可见的状态依据。普通生命周期从候选开始，经埋设、强化或部分回收推进；重开终止状态、跳过确认步骤或修正判断时，显式添加 `--correction`，保留旧历史。

作者补录漏提取的伏笔：

```powershell
.\.venv\Scripts\python.exe -m easy_writing thread create --project $bookProjectId --title "门前划痕" --description "可能关联藏宝图的线索" --event "当前事件ID" --reason "作者补录" --db $reviewDb
```

补录时复制当前事件的有效出处，初始仍为候选。`resolves` 沿用第三阶段的“回收事件 → 埋设事件”方向，`foreshadows` 为“铺垫事件 → 后续事件”。检查发现已确认的关联时，只对绑定埋设 / 铺垫事件的伏笔提出部分回收 / 强化建议。未回收本身不算错误；`open`、`cancelled`、`resolved` 不被当作漏填。当前版本变化使状态依据失效时显示 `state_needs_review`，需要作者核查并用 `--correction` 重新确认。

## 4. 检查依据怎样建立

### 单值状态与明确时间约束

使用第三阶段的 `fact-context` 把命题标为单值 `state`，并标注生效事件、可选结束事件；用 `event-time` / `event-link` 表达故事时间。原始提取命题默认 `assertion`，不擅自视为单值。

- 两个不同值在同一生效事件，或显式有效区间重叠，才构成可证明的状态冲突。
- 更晚的新状态正常替换没有显式结束的旧状态，与第三阶段查询语义一致；左闭右开区间在交接边界不重叠。
- 时间未知时保留疑点；有角色观点标记的事实不作为全局状态冲突。
- 已确认时间网损坏时先报告时间问题，跳过依赖该时间网的状态、认知和规则判断。与明确约束冲突的候选时间连接保持待核查，不当作已确认事实。

### 人物使用了尚未获知的信息

第三阶段的 `learn` 记录获知事件。本阶段新增明确的“使用信息”标注：

```powershell
.\.venv\Scripts\python.exe -m easy_writing knowledge-use add --project $bookProjectId --fact "密码事实ID" --actor 林月 --event "输入密码事件ID" --reason "原文明确使用该密码打开门" --db $reviewDb
```

可用 `--evidence` 指定该事件的具体证据 ID；留空采用该事件的第一条当前有效证据。不能关联其他事件、其他项目或失效版本的证据。

检查把使用事件与作者记录的获知渠道比较。明确早于所有已记录渠道时报告提前知情；缺少渠道或时间未知时只产生 `awareness_gap` 待核查，不推断角色一定不知道。普通名字或秘密的提及不会自动当成使用。更早渠道、角色原本持有的知识和作者有意误导都需要据实补记。

### 已确认世界规则

先用 `edit --kind fact --status confirmed` 确认明确的全局规则事实，再显式定义可计算范围：

```powershell
.\.venv\Scripts\python.exe -m easy_writing check-rule add --project $bookProjectId --fact "规则事实ID" --subject 铁门 --predicate 穿墙 --operator forbid --value 成功 --reason "作者确认铁门绝不可穿墙" --db $reviewDb
```

`forbid` 禁止特定值，`require` 要求特定值；按已解析的主体与确切谓词匹配，值按当前显示文字比较。可用 `--from-event`、`--until-event` 设置规则生效区间；留空表示作者定义为全时间约束。普通命题检查生效点，持续状态检查区间交集，无法确定匹配时保留疑点。

当前范围限于单主体、单谓词的明确值约束。条件规则、多重例外、自然语言里的反讽和不可靠叙述需要作者先整理为适用范围或停用约束；该版本不会自动把一句自然语言转换成完整逻辑规则。规则来源被否决、变成未确认或失效后，相关约束不会继续当作确定世界设定。

两类标注都可分页查看、停用和重新启用，例如：

```powershell
.\.venv\Scripts\python.exe -m easy_writing check-rule list --project $bookProjectId --db $reviewDb
.\.venv\Scripts\python.exe -m easy_writing check-rule disable --project $bookProjectId --id "约束ID" --reason "规则存在例外，重新整理" --db $reviewDb
.\.venv\Scripts\python.exe -m easy_writing knowledge-use list --project $bookProjectId --db $reviewDb
```

`knowledge-use` 同样支持 `disable` / `enable`。标注的修改原因进入作者历史；伏笔转换也可通过原有 `history --id 伏笔ID` 查看。

## 5. 存储与当前边界

数据库升级到 v4，保存伏笔转换、规则约束、知识使用、完整检查报告和作者决定。旧 v3 数据库首次打开时升级并保留 `.pre-v4.bak`，保留原知识与出处；项目现有 `.env` 与密钥无需修改。

新服务位于 `services/foreshadowing.py`、`services/checking.py`，[第六阶段工作台](STAGE6.md)已复用这些服务。`research` 继续使用第四阶段的检索与工具问答，检查报告可由 `check` 命令或工作台查看。语义检索资料会显示当前有效伏笔状态；失效的回收依据不会悄悄沿用。

报告中的覆盖范围列出未更新章节和缺章，`all_current_records_scanned` 仅表示扫描了当前有效结构化知识，不代表阅读全文或证明整书没有问题。`analysis_ms` 记录本地扫描与分析耗时，`real_model_calls` 为 0。保守跨版本作者标注迁移与依赖复核见已实现的[第七阶段](STAGE7.md)。
