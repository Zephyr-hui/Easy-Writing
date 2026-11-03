# 阶段二：章节存储与有证据的信息提取

本阶段提供本地命令行入口。在项目目录打开 PowerShell，使用项目 `.venv` 即可运行；图形工作台在后续阶段实现。

## 已实现的流程

导入 UTF-8 TXT/Markdown → 保存不可变正文版本 → 分段和分块 → 提取结构化信息 → 校验引用及局部关联 → 保存分块进度 → 整章事务发布 → 查看、回查与作者修正。

默认数据库为 `data/easy_writing.db`；可在 `.env` 设置 `DATABASE_PATH`，也可为每个命令传入 `--db` 覆盖。不同小说使用独立项目 ID；一文件一章，项目内章节编号标识同一章，后续修改使用相同编号。

## 1. 五章离线演示

```powershell
.\.venv\Scripts\python.exe scripts/demo_stage2.py --provider fake --db data/stage2_offline.db
```

脚本导入 `examples/stage2/01.txt` 至 `05.txt`，使用对应的人工 JSON fixture，检查每条事实的引用。再次执行同一命令，应报告 `cached_chapters: 5`、`model_calls: 0`。

假模型不会提取任意小说。它只返回明确提供的 fixture，适合离线验证存储流程、错误恢复与界面开发。自定义 fixture 格式为：

```json
{"blocks": {"b0001": {"summary": "分块摘要", "summary_evidence": [{"paragraph_id": "p0001", "quote": "当前原文中的短句"}], "entities": [], "events": [], "facts": [], "links": [], "foreshadowing": []}}}
```

段落、分块规则及完整结构见 `ingestion/chunker.py`、`ingestion/models.py` 和样例 JSON。fixture 必须逐字对应所导入的正文。

## 2. 用 DeepSeek 导入自己的章节

先在本机 `.env` 配置密钥。以下从项目目录运行：

```powershell
$bookProjectId = (.\.venv\Scripts\python.exe -m easy_writing project create "我的小说" | ConvertFrom-Json).id
.\.venv\Scripts\python.exe -m easy_writing ingest "C:\小说\第一章.txt" --project $bookProjectId --chapter 1 --title "第一章" --provider deepseek
.\.venv\Scripts\python.exe -m easy_writing chapters --project $bookProjectId
```

将示例路径替换为实际章节文件。支持 `.txt`、`.md`、`.markdown`，包括 UTF-8 BOM；读取时保留 CRLF 等原始换行。数据库中的偏移基于去除 BOM 后保存的 Unicode 文本，不是 UTF-8 字节索引。

阶段二导入默认每块不超过 2400 个正文字符、输出上限 4096 token，可通过 `--max-chars`、`--max-output-tokens` 调整。这是字符分块预算，不等于精确 token 预算；提示还包含 JSON Schema 和少量人物背景。

长段落先切成固定的不超过 200 字符的定位片段，再组成模型分块，避免变更模型分块预算后改变同一版本的段落 ID。`***`、`---`、`* * *` 分隔符用于区分场景。按顺序处理分块时仅携带上一块的简短摘要与至多 30 条已有实体信息，背景内容不允许作为当前块的原文证据。

每个分块需要一次成功请求；限流、响应格式错误等可能触发有限重试。请求用量继续写入阶段一的 JSONL 日志。正文、提取结果和缓存保存在本地数据库，已被 `.gitignore` 排除。

## 3. 查看人物、事件、事实与摘要

```powershell
.\.venv\Scripts\python.exe -m easy_writing project list
.\.venv\Scripts\python.exe -m easy_writing knowledge --project $bookProjectId --kind entity
.\.venv\Scripts\python.exe -m easy_writing knowledge --project $bookProjectId --kind event
.\.venv\Scripts\python.exe -m easy_writing knowledge --project $bookProjectId --kind fact --limit 50 --offset 0
.\.venv\Scripts\python.exe -m easy_writing knowledge --project $bookProjectId --kind summary
.\.venv\Scripts\python.exe -m easy_writing knowledge --project $bookProjectId --kind thread
```

- `entity`：人物、地点、组织、物品与概念，包含别名和稳定 ID。
- `event`：事件内容、参与者、可为空的故事时间描述，以及提及性质。
- `fact`：主体、关系、文字或实体对象、认知主体、事件生效边界、明确或推测属性。
- `summary`：由各分块摘要汇集的章节摘要及证据，无需再次请求模型。
- `thread`：候选伏笔或悬念，不等于作者确认的伏笔。
- `link`：本分块内的候选事件连接；已实现的时间线查询、跨章事件对齐和连接确认见 [阶段三](STAGE3.md)。

自动提取结果默认 `unconfirmed`，不自动升级为作者设定。同名或别名只有唯一精确匹配时才复用已有实体；有歧义时保留独立候选。模型可用 `distinct` 指明同名不同身份。语义相近但措辞不同的名称可能需要作者合并。

## 4. 回查原文

```powershell
$bookFacts = .\.venv\Scripts\python.exe -m easy_writing knowledge --project $bookProjectId --kind fact | ConvertFrom-Json
$bookFactId = $bookFacts[0].id
.\.venv\Scripts\python.exe -m easy_writing source --kind fact --id $bookFactId
```

返回章节、正文版本、段落、完整定位片段、逐字引用、段内及全文偏移，起点包含、终点不包含。`is_latest_source` 表示引用是否属于当前正文版本。人物合并后仍可查看原身份及目标身份的证据。

程序会拒绝不存在的段落、不匹配的原文、无法定位的重复引用和缺乏引用依据的别名；JSON 还需通过字段、局部 ID 和引用关系校验。这些检查能确保引用可定位，不能代替作者判断推论是否成立。

## 5. 作者确认、修正及人物合并

修改前可先查看影响范围：

```powershell
.\.venv\Scripts\python.exe -m easy_writing edit --kind fact --id $bookFactId --field object_value --value "作者修正后的对象" --reason "修正提取错误" --preview
.\.venv\Scripts\python.exe -m easy_writing edit --kind fact --id $bookFactId --field object_value --value "作者修正后的对象" --reason "修正提取错误"
.\.venv\Scripts\python.exe -m easy_writing edit --kind fact --id $bookFactId --status confirmed --reason "已核对原文"
.\.venv\Scripts\python.exe -m easy_writing history --project $bookProjectId --id $bookFactId
```

状态支持 `confirmed`、`rejected`、`unconfirmed`。否决的记录不进入默认查询与相关事实检索文字。仅确认原有内容不会无故使摘要过期；事实内容被修改或否决时，相关章节摘要标记过期，不继续作为有效摘要使用。可使用 `--include-stale` 查看过期记录，再用 `edit --kind summary --field text` 提供作者修订的摘要。

可修改字段：实体的 `name`；事件的 `title`、`summary`、`story_time`；事实的 `predicate`、`object_value`；伏笔候选的 `title`、`description`；摘要的 `text`。内部标识和实体指针不能通过任意字段修改。

修正事实的文字对象会清除原实体对象指向，防止旧指向覆盖新内容。原文证据保持原貌；作者改动通过 `history` 单独保存修改前后值、原因与时间，不能把作者修订冒充原文原话。

添加别名与合并身份（将 ID 替换为实际实体 ID）：

```powershell
.\.venv\Scripts\python.exe -m easy_writing alias --id "实体ID" --name "阿舟" --reason "作者确认别名"
.\.venv\Scripts\python.exe -m easy_writing merge --source "被合并ID" --target "保留ID" --reason "确认同一人物" --preview
.\.venv\Scripts\python.exe -m easy_writing merge --source "被合并ID" --target "保留ID" --reason "确认同一人物"
```

合并通过身份映射保留原实体和证据，不能跨项目、跨类型或自合并。合并、改名等实体修订先保守地使本项目已发布摘要过期，并同步重建本地事实检索文字；不会自动发起付费摘要生成。

## 6. 更新章节、失败恢复与缓存

修改正文后，用相同项目 ID、章节编号重新导入。新正文形成新版本；新的整章知识全部通过校验后才替换默认发布版本。旧版正文与证据保留，可以回查。

同一正文版本、提取模型配置、提示与结构版本、分块预算对应同一任务。重复导入已完成任务直接返回 `cached: true`，不重新调用 LLM；改变这些提取条件会形成新任务。

失败后查看章节进度，再显式重试：

```powershell
.\.venv\Scripts\python.exe -m easy_writing chapters --project $bookProjectId
.\.venv\Scripts\python.exe -m easy_writing ingest "C:\小说\第一章.txt" --project $bookProjectId --chapter 1 --provider deepseek --retry
```

重试复用已通过校验并保存的分块。JSON 通过但证据不匹配的块不缓存、不自动发布，作者可检查来源和模型后重试。若进程在收到模型响应后、保存缓存前被终止，该块仍可能再次请求；本地缓存不能保证服务端恰好计费一次。

同一任务的活跃租约会阻止重复执行。强制终止后，需等待租约过期再使用 `--retry`；默认租约至少 15 分钟，并在分块前续期。已经捕获的异常会立即释放租约并将任务标记失败。

新稿入库失败时，上一次完整发布的知识仍在数据库中，但 `knowledge_is_current` 为 `false`。默认知识查询排除这类旧知识；`--include-stale` 可以查看并明确辨认旧版本。任务提取期间出现更新正文时，旧任务不能发布覆盖新稿。

同一版本的重复导入会保留作者修订。跨版本作者标注的保守迁移与跨章依赖复核现已在[第七阶段](STAGE7.md)实现：只沿用内容和证据唯一匹配的记录，变化或歧义由作者重审，不把旧版事实直接套到新版正文。

## 7. 验收与后续边界

真实 DeepSeek 五章样稿验收数据库为 `data/stage2_live.db`，元数据报告为 `data/stage2_acceptance.json`。首次五次请求全部成功，提取 13 条事实；覆盖所有知识类型的 62 次引用检查通过。再次导入五章全部命中缓存，新增模型调用为零。

本阶段交付存储、局部候选连接、事件提及和知识检索文字。时间线重建、作者确认的跨章事件对齐、核心节点角色编辑及状态查询已在 [阶段三](STAGE3.md) 实现；向量检索、一致性判断、伏笔自动回收判断及图形工作台仍按后续阶段实施。

2026-11-02 完整自动测试 112 项通过，依赖检查及 wheel 构建通过，安装包 SQL 资源检查通过。测试默认离线并隔离实际 .env；真实接口验收由显式 DeepSeek 演示单独执行。
