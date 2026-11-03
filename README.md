# Easy-Writing

使用 Python 构建的长篇小说创作助手。支持章节知识入库、双时间线、历史状态与认知、本地 BGE 混合检索、局部原文回查、LangGraph 与 DeepSeek 只读工具、伏笔与一致性检查、下一章计划、改稿依赖、恢复和备份导出，以及带会话记忆的多轮创作问答。

启动本地工作台（本机已安装界面依赖）：

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

打开 `http://127.0.0.1:8501`，创建作品后可导入内置样章。四个入口为项目导入、知识查看、创作问答和检查报告；下一章路线保存为独立计划，由作者明确采纳。页面重运行读取已有结果，失败任务不自动重试。

在「创作问答」中可以新建或继续会话，基于已导入章节追问人物、事件和写作建议。需要沿事件链补查时，发送前勾选「深度对话」。操作说明和调用预算见 [阶段八](docs/STAGE8.md)。

工作台将对话、逐轮记忆和滚动摘要保存到独立会话库，接近模型输入预算时会压缩上下文。默认会话库位于小说知识库同目录，可通过 `CHAT_DATABASE_PATH` 配置。旧会话迁移与两库备份操作见 [阶段九](docs/STAGE9.md)。

「项目导入」支持单章 Word、PDF、TXT 与 Markdown，可先预览和校订再确认入库。扫描 PDF 可选本地 OCR，设置与命令见 [阶段十](docs/STAGE10.md)。

Windows 桌面版双击 [最新版 EasyWriting.exe](dist/stage14-settings/EasyWriting/EasyWriting.exe) 启动。发布目录同时保留 [上一版](dist/stage14-update/EasyWriting/EasyWriting.exe)。打包方法见 [阶段十一](docs/STAGE11.md)。

桌面版创作问答由模型先选工具。工具范围和调用预算见 [阶段十二](docs/STAGE12.md)。

新建作品自动建立小说知识库和会话库，旧版共享库可在「数据与设置」中迁移。操作说明见 [阶段十三](docs/STAGE13.md)。

桌面版支持按作品保存问答设定、多行弹窗和从选定路线生成标题与正文草稿。操作说明见 [阶段十四](docs/STAGE14.md)。

新环境可安装 `pip install -e ".[dev,ui]"`；需要 BGE 时使用 `.[dev,ui,semantic]`。对应固定组合见 `requirements.ui.lock.txt`、`requirements.full.lock.txt`。

章节入库见 [阶段二](docs/STAGE2.md)，时间线与结构化查询见 [阶段三](docs/STAGE3.md)，语义检索和问答见 [阶段四](docs/STAGE4.md)，伏笔与检查见 [阶段五](docs/STAGE5.md)，工作台和下一章计划见 [阶段六](docs/STAGE6.md)，改稿、恢复、备份和用量见 [阶段七](docs/STAGE7.md)。

第七阶段的操作入口位于“项目导入 → 改稿与数据维护”。明确依赖受影响时，相关章节暂时退出查询并提示复核；仅对内容与证据唯一匹配的记录自动沿用作者标注。

较长样稿整体验收（默认测试向量，不调用 DeepSeek）：

```powershell
.\.venv\Scripts\python.exe scripts/demo_stage7.py
# 使用已缓存的真实中文语义模型：
.\.venv\Scripts\python.exe scripts/demo_stage7.py --embedding bge --chapters 120
```

第六阶段离线演示（不调用 DeepSeek）：

```powershell
.\.venv\Scripts\python.exe scripts/demo_stage6.py
```

第五阶段完整离线演示（不调用 DeepSeek）：

```powershell
.\.venv\Scripts\python.exe scripts/demo_stage5.py
```

检查遍历当前有效结构化知识，报告双方证据、条件和建议。状态冲突检查需要单值状态与时间标注；人物使用知识和世界规则需要明确关联标注。自动建议由作者确认，缺少记录仅表示覆盖不足。

第四阶段离线流程演示及真实中文语义演示：

```powershell
.\.venv\Scripts\python.exe scripts/demo_stage4.py --embedding fake
# 真实 BGE 首次需要模型文件；本机已安装语义依赖。
.\.venv\Scripts\python.exe scripts/demo_stage4.py --embedding bge
```

第三阶段完整离线演示（无需密钥）：

```powershell
.\.venv\Scripts\python.exe scripts/demo_stage3.py
```

五章离线样稿可直接运行，再次执行会复用缓存：

```powershell
.\.venv\Scripts\python.exe scripts/demo_stage2.py --provider fake --db data/stage2_offline.db
```

## 1. 在 VS Code 中安装

在项目目录的 PowerShell 终端运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

若项目已有 `.venv`，直接运行第二行。VS Code 使用 `Python: Select Interpreter` 选择 `.venv\Scripts\python.exe`。项目适用 Python 3.12 及以上；本阶段使用 Python 3.14 验证。

依赖范围在 `pyproject.toml`，基础与测试固定依赖在 `requirements.lock.txt`，含真实 BGE 运行库的完整组合在 `requirements.semantic.lock.txt`。复现基础组合时先安装锁定依赖，再安装项目：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

需要本地 BGE 时安装 `pip install -e ".[semantic]"`，或使用完整语义锁文件复现本机组合。普通离线测试无需下载模型。

## 2. 无密钥离线试用

```powershell
.\.venv\Scripts\python.exe -m easy_writing doctor
.\.venv\Scripts\python.exe -m easy_writing demo --provider fake
.\.venv\Scripts\python.exe -m easy_writing ask "如何设计人物冲突？" --provider fake
```

`doctor` 只检查配置，不请求网络。默认 provider 是 `fake`；假模型返回固定演示数据，不能分析任意小说，也不产生模型费用。`demo` 分别验收文本和 JSON 接口；JSON 示例同时检查证据是否逐字存在于演示原文。

默认在当前工作目录读取 `.env`。无需配置文件即可离线运行。需要配置时，在本机复制模板：

```powershell
Copy-Item .env.example .env
```

请不要覆盖已经填写好的 `.env`。环境变量优先于文件；可通过 `--env-file 路径` 指定其他配置文件，不会自动向上查找其他项目的密钥。

## 3. 手动验收 DeepSeek

编辑本机 `.env`，填写 `DEEPSEEK_API_KEY`，不要将密钥发到聊天或提交到仓库。保持 `LLM_PROVIDER=fake` 也可以通过命令显式选择 DeepSeek。

```powershell
.\.venv\Scripts\python.exe -m easy_writing doctor --provider deepseek
.\.venv\Scripts\python.exe -m easy_writing demo --provider deepseek
```

第二条会向配置的接口发送两项任务，正常情况下是两次请求；失败重试可能增加请求和费用。可使用 `--mode text` 或 `--mode json` 单独验收。缺少密钥时立即退出并提示，不发送请求。

自定义文本问题：

```powershell
.\.venv\Scripts\python.exe -m easy_writing ask "给我三个小说开篇的构思方向。" --provider deepseek
```

官方接口：`https://api.deepseek.com/chat/completions`。当前模板使用 `deepseek-flash`；模型名可通过 `EXTRACTION_MODEL`、`ANALYSIS_MODEL` 分别配置。接入参数依据 [DeepSeek 官方文档](https://api-docs.deepseek.com/)；实际可用模型以账户和官方文档为准。

模型请求显式设置思考模式，阶段一默认关闭，便于控制接口验收的输出与等待时间。需要时将 `ANALYSIS_THINKING` 或 `EXTRACTION_THINKING` 改为 `enabled`。第四阶段的 `research` 命令另提供有限轮次的原生工具调用，并固定关闭思考模式；使用方法见 [阶段四](docs/STAGE4.md)。

## 4. 配置与错误处理

| 配置 | 默认值 | 含义 |
|---|---|---|
| `LLM_PROVIDER` | `fake` | 默认模型后端；命令参数优先 |
| `DEEPSEEK_API_KEY` | 空 | 仅使用 DeepSeek 时需要 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | HTTPS 基础地址，可带 `/v1`，不带 `/chat/completions` |
| `EXTRACTION_MODEL` / `ANALYSIS_MODEL` | `deepseek-flash` | 提取与文本生成模型 |
| `EXTRACTION_THINKING` / `ANALYSIS_THINKING` | `disabled` | 显式开启或关闭思考 |
| `LLM_TIMEOUT_SECONDS` | `60` | HTTP 各阶段超时，连接超时最多 10 秒；不是整个重试任务的总时限 |
| `LLM_MAX_RETRIES` | `2` | 最多重试两次，一个任务最多三次尝试 |
| `LLM_RETRY_BASE_SECONDS` | `0.5` | 指数退避基数，含少量随机抖动 |
| `LLM_MAX_OUTPUT_TOKENS` | `1024` | 单次输出上限；截断时提示调整，不重复相同请求 |
| `LLM_USAGE_LOG` | `data/llm_calls.jsonl` | 每次调用尝试的元数据日志 |

处理策略：

- 认证失败、余额不足、格式或参数错误：立即报错，不重试。
- 限流、服务器错误、超时、连接失败：在同一个有限预算中退避重试。
- JSON 空白、格式错误、数据结构错误：分别记录并有限重试，不接收坏数据。
- 输出截断、未正常完成：提示修改输入或参数，不将半截内容当作成功结果。
- JSON 模式保证不了内容真实性。当前使用 Pydantic 校验，演示额外核对原文引用；正式入库的实体解析与证据管理在阶段二实现。

JSON 模式与空白响应处理依据 [DeepSeek JSON 输出说明](https://api-docs.deepseek.com/guides/json_mode/)。

## 5. 用量与隐私

每次 HTTP 尝试单独记录一行，包含同一任务的 `call_id`、尝试序号、结果状态、耗时和接口实际返回的 token 用量。收到响应但解析失败也会记录用量，不能只统计最后成功的一次。

用量缺失记为 `null`；假模型也不伪造为零费用。超时和网络失败无法得知服务端是否计费。查看同一个任务的所有日志，而非只看 CLI 最后成功响应的用量。价格未写死，当前不估算金额。

日志不保存提示、小说正文、回复、推理内容、认证头或原始错误正文。`.env`、`data/`、虚拟环境均已忽略。选择 DeepSeek 后，本次提示会发送至配置的远端接口；本地存储不代表离线处理。

日志写入失败会提示，不为补记日志而重发模型请求。

## 6. 离线测试

```powershell
.\.venv\Scripts\python.exe -m pytest
```

测试使用假模型和 HTTP MockTransport，不调用真实接口、不需要密钥。覆盖配置优先级、文本和 JSON 请求、重试边界、错误分类、用量缺失、失败请求用量、日志敏感信息和命令入口。

2026-11-02 阶段一初次验收：Python 3.14.7，72 项测试通过；`pip check` 通过；模块入口与安装后的命令入口均完成离线演示，Windows 管道中文输出正常。

同日真实 DeepSeek 验收通过：文本生成、JSON 提取均返回 HTTP 200，且均为首次成功；JSON 结构及原文证据校验通过。两次请求合计输入 342、输出 90、总计 432 token，日志用量已核对。详细记录见 PLAN.md 阶段一。

真实接口验收通过手动 `demo --provider deepseek` 完成，不随测试自动执行。

阶段二完成后，完整测试共 112 项通过；wheel 构建及 SQL 资源打包检查通过。真实 DeepSeek 五章样稿入库、原文回查和重复导入零新增调用验收通过，详见 [阶段二使用说明](docs/STAGE2.md)。

阶段三完成后，完整测试共 155 项通过，依赖、wheel 和迁移资源检查通过。旧库升级保持数据完整；离线演示验证双时间线、状态、认知、节点角色和事件对齐，详见 [阶段三使用说明](docs/STAGE3.md)。

阶段四完成后，完整测试共 188 项通过，`pip check`、wheel 构建与资源检查通过。真实 BGE 三组换措辞查询均召回标准事实；DeepSeek 原生工具闭环共 2 次请求、3798 token，详见 [阶段四使用说明](docs/STAGE4.md)。

阶段五完成后，完整测试共 213 项通过，`pip check`、wheel 与 v4 迁移资源检查通过。六章离线样稿和 125 条伏笔盘点验收通过；旧库副本升级保留各表数据与备份，本阶段未发起真实模型调用，详见 [阶段五使用说明](docs/STAGE5.md)。

阶段六完成后，完整测试共 239 项通过（含 6 项界面交互测试），依赖、wheel 与 v5 迁移资源检查通过。页面重运行不会重复模型请求，下一章路线独立保存、显式采纳；真实 DeepSeek 验收共 2 次请求、10650 token，补记样稿认知后复用既有草稿完成本地复核。浏览器目视验收因电脑操作授权超时未完成；详细记录见 [阶段六](docs/STAGE6.md)。

阶段七完成后，完整测试共 256 项通过（含 7 项界面交互测试），依赖、wheel 与 v6 迁移资源检查通过。120 章、179040 字符的真实本地 BGE 检索与改稿 / 恢复 / 备份整体验收通过；本阶段付费模型调用 0 次，详见 [阶段七](docs/STAGE7.md)。
