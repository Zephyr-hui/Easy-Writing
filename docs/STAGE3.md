# 阶段三：时间线、人物关系与结构化问答

本阶段提供本地命令行和可复用的 Python 业务服务。查询、列表、事件图编辑及样稿演示不调用真实 LLM，也不需要 API Key。增强的 LangGraph、模型 Tool Calling 和向量检索已在 [第四阶段](STAGE4.md) 实现，本文的 query 命令继续保持离线行为。

## 1. 一次运行完整演示

在项目目录执行：

```powershell
.\.venv\Scripts\python.exe scripts/demo_stage3.py
```

五个节选章节位于 `examples/stage3/book.json`。脚本使用明确的人工 fixture 入库，再通过作者标注建立时间关系、状态区间与角色获知记录。默认数据库为 `data/stage3_demo.db`，报告为 `data/stage3_acceptance.json`；可用 `--db` 指定独立文件。

演示检查：

- 钥匙交接前由林舟持有，交接后由林月持有。
- 第二十章才揭示的密码，不出现在第十章的读者查询中。
- 林月获知密码之前没有对应认知依据，获知之后能查到记录。
- 修复发生在交接之前，但通过第二十章的回忆揭示。
- 第二十五章复述交接，作者对齐后为同一事件的另一处提及，原文证据保留。
- 主线中修复节点从核心降为普通后，支线中仍是核心；概览折叠路径仍保留它。

重复运行会复用五章提取缓存，保留样稿 ID，重新验证相同的作者标注和查询。演示中的真实模型请求数始终为零。

## 2. 浏览人物、关系与两条时间线

```powershell
$stageDb = "data/stage3_demo.db"
$bookProjectId = (.\.venv\Scripts\python.exe -m easy_writing project list --db $stageDb | ConvertFrom-Json)[0].id
.\.venv\Scripts\python.exe -m easy_writing characters --project $bookProjectId --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing relations --project $bookProjectId --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing timeline --project $bookProjectId --order narrative --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing timeline --project $bookProjectId --order story --db $stageDb
```

`characters` 返回人物及可见别名。`relations` 返回以人物为主体和实体对象的关系事实，保留谓词、确认状态与原文依据，不从共同出场自动编造人物关系。

`narrative` 按章节和原文位置列出每次提及，包含发生、回忆、复述等性质。同一事件可以出现多次。同位置的提及允许稳定展示，但不代表已经确定细粒度先后。

`story` 根据已确认的 before 连接和可比较的数值时间生成偏序。`unordered_groups` 表示当前约束未区分的事件组，不宣称组内事件同时发生。`time_unknown` 保留无明确数值起点的事件；原文“次日”“十年前”等描述仍保留，不自动猜日期。

`--chapter 10` 将结果限制为截至第十章已揭示的信息；留空为作者视角，读取当前已发布的全部章节。角色认知还需要额外指定 `--actor`，不能把读者已知信息自动当作角色已知。

## 3. 标注时间与事件连接

先用 `knowledge --kind event` 或 `timeline --order narrative` 找到实际事件 ID。以下占位 ID 须替换：

```powershell
.\.venv\Scripts\python.exe -m easy_writing event-time --event "事件A" --start 0 --end 1 --frame main --revealed-at 1 --reason "作者确认第一天发生" --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing event-link --source "事件A" --target "事件B" --relation before --revealed-at 5 --reason "第五章已有明确顺序" --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing event-neighbors --project $bookProjectId --event "事件B" --direction in --relation before --depth 2 --db $stageDb
```

时间采用作者定义的统一数值刻度，例如 0、1、2 代表相对日；只给 `--start` 时视为时点。不同 `--frame` 的数值不可直接比较。不传起止值的 `event-time` 可清除已知时间；不使用自然语言日期解析器。

连接类型：

| 类型 | 语义 |
|---|---|
| before | 严格先后；已知区间必须满足 A 的结束小于 B 的开始 |
| overlaps | 时间区间有交集，允许端点相接 |
| during | A 的区间被 B 包含，允许端点相同 |
| causes | 作者确认的因果，不由时间先后自动生成 |
| foreshadows | A 为 B 铺垫 |
| resolves | 回收事件指向埋设事件 |

`before` 不表示紧邻，一个事件允许多个前因和后续。前驱由反向查询得到，不额外保存 after。普通因果等关系可以形成环；严格 before 不允许形成环。

新增或确认连接时检查环、传递路径与时间范围冲突、区间关系及时间参照系。逻辑冲突的连接保留为 `needs_review`，返回 `validation_errors`，不参与有效排序；跨项目、自连接等无效输入直接拒绝。修改已知时间若与已确认约束冲突，则整体回滚。

第二阶段提取的候选连接也可以确认或否决：

```powershell
.\.venv\Scripts\python.exe -m easy_writing edit --kind link --id "连接ID" --status confirmed --reason "核对原文后确认" --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing edit --kind link --id "连接ID" --status rejected --reason "时间先后被误当成因果" --db $stageDb
```

手工标注的时间、连接、事件身份对齐及角色获知记录，`--revealed-at` 默认留空，表示仅作者视角可用。设为某章后才可进入对应读者范围。候选连接通过原文提取时，其可见性依据当前版本的引用章节。即使两端事件都已经出现，后文才解释的连接也不会提前公开。

## 4. 同一事件的多处提及

```powershell
.\.venv\Scripts\python.exe -m easy_writing event-merge --source "后文复述事件ID" --target "原事件ID" --revealed-at 25 --reason "作者确认复述同一次交接" --db $stageDb
```

对齐保留原事件行、提及、事实引用和证据；`source --kind event` 可以查看合并身份的全部历史出处。只有作者明确确认才对齐，不凭相似标题自动合并。

不能对齐不同项目、相同身份、时间标注冲突或对齐后形成 before 自环的事件。若双方在同一剧情线的角色冲突，保守保留核心角色；之后可显式调整。读者范围内仅在身份对齐已揭示时合并；复制时间标注也不会早于身份对齐的揭示边界。

## 5. 主线、支线与核心节点

```powershell
.\.venv\Scripts\python.exe -m easy_writing plot list --project $bookProjectId --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing plot create --project $bookProjectId --name "调查主线" --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing plot role --plot "剧情线ID" --event "事件ID" --role core --reason "设为转折点" --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing plot role --plot "剧情线ID" --event "事件ID" --role ordinary --reason "降级为推进细节" --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing plot view --plot "剧情线ID" --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing plot view --plot "剧情线ID" --expand --db $stageDb
```

角色按“剧情线 + 事件”保存，普通节点也需加入相应剧情线。概览仅展示核心节点，路径可经过该线的普通节点；展开视图展示全部已归属事件与确认连接。

路径摘要返回 `via`、`link_ids`、`relation_types` 和快照 ID，可以回查每一步。折叠不会写入直接因果或虚构连接。路径不存在时不会强行连接核心节点。`--max-paths`、`--max-depth` 限制展开，达到预算标记 `truncated`。

降级只改变所选剧情线的展示角色，不删除事件、关系或证据，不影响其他剧情线，也不妨碍普通节点被查询。

## 6. 人物历史状态与认知

原始提取的事实保留为命题。对于持有人等单值状态，由作者设置模式和生效区间：

```powershell
.\.venv\Scripts\python.exe -m easy_writing fact-context --id "事实ID" --from-event "生效事件ID" --until-event "结束事件ID" --mode state --reason "确认持有区间" --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing state --project $bookProjectId --subject "钥匙" --predicate "持有人" --at-event "查询事件ID" --db $stageDb
```

生效起点包含，结束事件不包含。`fact-context` 是替换该事实的整套生效边界，省略起点或终点表示未设定，不是保留旧值。模式 `assertion` 为普通命题，`state` 为作者声明的单值状态。只有确定更晚且不依赖未知时间的状态才覆盖早期状态；未确认候选不会静默替换作者确认的状态。仍有多个候选时一并返回，由作者处理。

`--at-event` 指定故事时点；`--chapter` 指定读者信息边界。两者是独立条件。只给章节不会把该章最后提及的事件强当作唯一故事时点；不指定时点可查看历史命题。时间无法确定时标记 `time_uncertain`，不伪造结论。可用 `--confirmed-only` 限制为作者确认的事实。

角色获知是独立记录：

```powershell
.\.venv\Scripts\python.exe -m easy_writing learn --fact "事实ID" --actor "林月" --event "获知事件ID" --revealed-at 20 --reason "她此时听到密码" --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing state --project $bookProjectId --actor "林月" --subject "地窖" --at-event "查询事件ID" --db $stageDb
```

认知查询仅使用明确的 `knowledge_holder` 或作者记录的获知关系，不能从全局事实推断人人知道。结果为空代表缺少可证明的认知依据，不证明角色实际不知道。查询时点还会筛选该时点有效的命题；过往状态的记忆需结合历史记录查看。

## 7. 第一条知识问答流程

```powershell
.\.venv\Scripts\python.exe -m easy_writing query --project $bookProjectId --question "第十章地窖的密码是什么？" --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing query --project $bookProjectId --question "林月知道密码吗？" --actor "林月" --chapter 10 --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing query --project $bookProjectId --question "钥匙由谁持有？" --subject "钥匙" --predicate "持有人" --at-event "交接事件ID" --db $stageDb
```

流程为：确定章节和实体范围 → 校验知识覆盖 → 查询状态/认知 → 关键词排序 → 输出命题与引用。支持常用中文/阿拉伯章节号；“第十章之前”限定到第九章。无法识别或相互冲突的章节表达会提示修正，跨章节比较需分别查询。多人物认知问题可用 `--actor` 明确角色；`--subject`、`--predicate` 提供精确控制。

`query` 是本地知识问答，返回 `model_calls: 0`。旧 `ask` 命令仍是独立的模型文本接口，不会自动检索小说库。新增的 search/research 及其 Tools、语义检索见 [第四阶段](STAGE4.md)。

所有结果携带快照 ID；章节查询显示已入库、过期和未导入的章节区间。未知、未入库和未检索到都不能等价为“不存在”。自动提取的未确认命题、语义理解及证据是否足以支持结论，仍需作者核对。

## 8. 数据库兼容与验收

数据库升级到版本 2。原版本 1 在升级前通过 SQLite backup API 保存 `原文件名.pre-v2.bak`，迁移在事务内执行，不覆盖已有备份。新建数据库直接建立两个版本的结构。备份、数据库、正文及日志均排除于版本控制之外。

实际阶段二验收数据库已升级，原 5 章、7 个事件、13 条事实及全部证据数量保持不变，外键检查通过。完整自动测试包括旧功能回归、迁移、倒叙、时间冲突、角色认知边界、事件对齐、节点降级、路径预算及 CLI。第三阶段验证不发起真实模型调用。

2026-11-02 验收：155 项自动测试通过，依赖检查与 wheel 构建通过，v2 迁移 SQL 和业务服务打包检查通过。
