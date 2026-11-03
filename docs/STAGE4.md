# 阶段四：混合检索、原文工具与 LangGraph 问答

本阶段已接入真实本地 BGE 中文向量模型、增量向量索引、混合检索、资料预算、限定版本的局部原文回查，以及 DeepSeek 原生工具调用。LangGraph 负责编排查询步骤，Tools 是经过参数校验的只读 Python 业务函数。

## 1. 安装与演示

基础环境包含检索流程、NumPy、LangGraph 和测试。真实 BGE 额外需要语义依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,semantic]"
```

本机已完成安装。`requirements.lock.txt` 固定基础与测试依赖；`requirements.semantic.lock.txt` 固定包含本地模型运行库的完整测试组合。复现完整环境可先安装语义锁文件，再用 `pip install --no-deps -e .` 安装项目。

默认演示完全离线，使用明确标记的测试向量：

```powershell
.\.venv\Scripts\python.exe scripts/demo_stage4.py --embedding fake
```

真实中文语义验收：

```powershell
.\.venv\Scripts\python.exe scripts/demo_stage4.py --embedding bge
```

演示创建独立数据库 `data/stage4_demo/novel.db`，先建立第三阶段的节选样稿，再验证语义召回、缓存、章节边界、局部回查、普通节点和长文本切分；不会调用 DeepSeek。报告为同目录的 `stage4_acceptance.json`。

`fake` 仅验证流程，字符哈希向量不具备真实语义能力。真实模型使用 CPU，首次需要从 Hugging Face 下载公开模型文件，后续使用 `.cache/models`。本地推理不向 Hugging Face 上传小说文本；启动时可能仍检查模型元数据。模型加载时间单列在指标中，不能只用查询阶段延迟代表冷启动总耗时。

## 2. 配置

在现有 `.env` 中按需增加，原有密钥无需修改：

```dotenv
EMBEDDING_BACKEND=bge
EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
EMBEDDING_CACHE=.cache/models
RETRIEVAL_LOG=data/retrieval_calls.jsonl
```

可用 `EMBEDDING_REVISION` 显式固定模型版本。本实现解析并记录实际仓库提交 SHA；本地模型目录按文件内容生成版本标识。模型标识、版本、维度、输入上限、归一化、查询前缀和切分策略共同决定索引命名空间。不同模型或配置的向量不混用。

真实验收模型为 `BAAI/bge-small-zh-v1.5`，512 维，输入上限 512 token，提交为 `7999e1d3359715c523056ef9478215996d62a620`。文档按实际 tokenizer 长度切分，每段均校验长度；长查询超过上限时直接提示缩短，不静默截断。分段保留字符位置和原文末尾。

## 3. 增量建立索引与混合搜索

先从 `project list` 取得实际项目 ID。以下命令在项目目录运行：

```powershell
$stageDb = "data/stage4_demo/novel.db"
$bookProjectId = (.\.venv\Scripts\python.exe -m easy_writing project list --db $stageDb | ConvertFrom-Json)[0].id
.\.venv\Scripts\python.exe -m easy_writing index --project $bookProjectId --embedding bge --db $stageDb
.\.venv\Scripts\python.exe -m easy_writing search --project $bookProjectId --question "地下房间的通行口令是什么？" --embedding bge --db $stageDb
```

`index` 对当前有效知识生成向量。已索引且内容未变的记录复用缓存；新章节、修订文字或新模型配置只生成所缺部分。原有缓存可保留作历史，但检索只使用当前允许记录及其当前文本哈希，旧向量不会把失效知识带回来。

检索先确定项目、当前发布版本、章节、角色和故事时点，再融合实体精确匹配、中文关键词及向量相似度。一个记录的多个向量片段合并为一个候选，采用排名融合；长文本优先返回匹配片段，而非总是截取开头。核心/普通角色不作为排除条件。

`--embedding none` 仅用精确与关键词检索，不加载 BGE；`--embedding fake` 只用于流程测试。`search` 不调用 LLM，但会按需要更新本地向量缓存，并通过 LangGraph 组织检索与补证据步骤。

可追加 `--chapter 10`、`--actor 林月`、`--at-event 事件ID`、`--subject 钥匙`、`--predicate 持有人` 或 `--confirmed-only`。含“知道/知晓/获知/知情”的问题要求显式 `--actor`，不会在角色不明时回退到全局摘要。

## 4. 接入 DeepSeek 生成回答

```powershell
.\.venv\Scripts\python.exe -m easy_writing research --project $bookProjectId --question "请核对原文，交接钥匙后由谁持有？" --embedding bge --provider deepseek --db $stageDb
```

此命令只将本次资料包、工具定义及有限的补充结果发给 DeepSeek，不发送整部小说。`--provider fake` 返回固定格式的演示回复，不是模型理解质量的验收。

流程：

```text
范围与版本检查 → 混合检索 → 按条件局部回查 → 预算资料包
                                             ↓
                                    DeepSeek 原生 tools
                                             ↓
                                  校验工具参数、返回限额结果
                                             ↓
                                  有限补查 → 校验引用 → 回答
```

工具白名单：

| 工具 | 作用 |
|---|---|
| search_memory | 在同一固定项目、章节、角色与时点范围补充检索 |
| read_source | 读取已允许的知识编号所对应的局部原文，可指定正文版本 |
| get_event_chain | 获取已确认的前置路径及边界，不把先后改写成因果 |
| get_ingestion_status | 查看入库覆盖和过期章节 |

模型不能通过工具更换项目或角色、执行 SQL、修改事实、读取任意工作区文件。未知工具和额外参数会被拒绝。研究问答的原生工具轮次当前固定为非思考模式，保留已生成工具调用与 tool 消息的正确对应关系；其他 ask/提取配置不受影响。

最终回答为 `answer`、`citations`、`confidence` 三部分。支持性回答须引用本次实际返回的知识、原文或路径编号；未见过的引用会被拒绝。引用编号校验不等于证明整句推论正确，未确认设定及语义判断仍需作者核对。

## 5. 原文触发条件与范围

默认 `--source-mode auto`，明确要求核实原文、时间不明、指定时点下有冲突状态，或相关新稿尚未发布知识时，才补充原文。历史状态随剧情变化本身不直接判作冲突。

- `--source-mode always`：预先核查选中的知识证据。
- `--source-mode never`：自动步骤和模型工具都禁止读取原文。
- `--max-source-reads 2`：整次任务的原文回查次数上限，包含工具补查。

读取单位为有关的引用或相邻定位片段，限定在对应版本与场景附近，不按整章投喂模型。`--at-event` 的原文回查只返回所选引用；角色认知范围禁用额外原文及全局摘要，并从资料包移除可能夹带旁白的原文 quote，只保留已允许的命题与引用元数据。

对于已经保存但尚未完成入库的新版本，只在允许章节内以关键词查找少量原文片段，标记 `unindexed`，不把它自动发布为事实。发现的 pending 编号可以在同一工具会话中继续局部核查，不是永久知识编号。角色或精确故事时点任务不读取这些未定位的新稿。

手动回查已入库知识：

```powershell
.\.venv\Scripts\python.exe -m easy_writing read-original --project $bookProjectId --key "fact:实际事实ID" --version-id "该证据的版本ID" --chapter 10 --db $stageDb
```

旧版本不属于当前任务证据时会被拒绝。原有 `source` 命令继续供作者审计历史出处；`read-original` 和模型工具使用当前任务范围。查询中如发生改稿或知识修改，快照校验会停止本次结果，避免混用版本。

## 6. 上下文、调用次数与计量

默认资料包预算 6000，完整模型输入预算 12000，输出上限 512 token。用以下参数调整：

```text
--context-budget 6000 --input-budget 12000 --max-output-tokens 512
--max-llm-calls 3 --max-tool-calls 3 --max-source-reads 2 --limit 8
```

资料包保留范围、快照、覆盖缺口、知识引用和路径边界。达到预算后优先省略引用正文、缩短预览或丢弃低优先候选，并显式标记截断。完整模型输入还计算系统提示、工具定义和历史工具消息的开销，超限时不再发请求。

文本预算采用 UTF-8 字节数的保守估计并预留协议开销，不是 DeepSeek 的精确 tokenizer，不能作为供应商计费硬上限。输出使用 `max_tokens` 限制；原生工具请求不进行适配器内部隐藏重试，所有模型轮次受同一预算限制。

`metrics` 记录查询延迟、模型加载延迟、新建向量数、本地嵌入 token、原文读取次数、真实 LLM 请求次数、实际返回的输入/输出 token、调用 ID 与停止原因。没有使用数据时保留 `null`；失败请求的未知用量不按零费用计算。

本地嵌入 token 与 DeepSeek 计费 token 分开记录。`data/retrieval_calls.jsonl` 只存元数据，不存问题、小说片段或回复；原始 API 尝试日志继续保存在 `llm_calls.jsonl`，可用 `llm_call_ids` 关联。CLI 输出和验收结果文件可能包含本次可见片段，它们与元数据日志用途不同，均应保留在被忽略的 data 目录。

LangGraph 执行显式关闭 LangSmith tracing，不把稿件发送至远端调试服务。一个工作流实例同时只处理一个任务；并行任务需创建独立实例，共享只读嵌入模型时仍分别绑定项目与范围。

Top-K 检索结果不代表全书盘点，覆盖缺口也不会因语义相似而消失。需要完整伏笔清单与检查报告时，使用[第五阶段](STAGE5.md)的 `thread list` 与 `check`，按返回的 `next_offset` 遍历结构化结果。

## 7. 实际验收与边界

完整离线测试 188 项通过，`pip check` 无依赖冲突；wheel 构建和资源检查通过，包含迁移 SQL 与检索模块，不包含本机密钥、数据库或模型权重。测试覆盖工具调用边界、角色与章节隔离、版本变化、预算、缓存、局部原文及日志脱敏。

真实 BGE 的三组换措辞样例，标准事实分别进入前 5 条的第 3、1、2 位；三组常规查询原文回查均为 0。显式原文核查返回 1 个有关片段。长文本切成 3 段，逐字覆盖完整内容，分段长度均在 tokenizer 上限内，实际编码通过。重复索引新增向量为 0，普通节点及路径可召回，第十章未泄露第二十章密码。

DeepSeek 原生工具闭环实际验收调用了 read_source 和 search_memory，再给出带有效引用的回答；共 2 次模型请求，接口报告输入 3598、输出 200、合计 3798 token。查询阶段约 1995ms，本次模型冷加载约 9792ms；这些是当前机器上的单次测量，不是速度承诺。未据此声称固定的 token 节省比例。

数据库升级到 v3，增加模型注册、带内容哈希的向量片段与检索索引；旧库在升级前保留备份。向量不随角色等级降级而删除，失效文本的向量不会参与当前候选排序。

基础与离线回归使用测试模型，不需要联网下载 BGE，也不调用 DeepSeek。真实语义与工具闭环分别手动验收。小型样稿不代表所有题材与复杂剧情的效果，后续应使用实际小说继续标注召回、误报和总调用消耗。

参考：[BGE 模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5)、[Sentence Transformers](https://www.sbert.net/docs/package_reference/sentence_transformer/model.html)、[LangGraph](https://docs.langchain.com/oss/python/langgraph/graph-api)、[DeepSeek 工具接口](https://api-docs.deepseek.com/api/create-chat-completion/)。
