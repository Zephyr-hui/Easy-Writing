# 阶段十 · Word 与 PDF 单章导入

## 工作台

在 **项目导入** 中选择 **正文文件（每份文件一章）**，上传 `.docx`、`.pdf`、UTF-8 `.txt` 或 Markdown 文件。检查章节号、标题与正文预览，可以先校订文本，再点击 **确认导入并提取本章**。本地解析和预览可以离线完成。只有明确提交后才会保存章节并运行知识提取。

- Word 会按文档顺序提取标题与正文段落，采用修订后的文字。旧版 `.doc` 请在 Word 中另存为 `.docx`。表格、图片、脚注及批注等内容通过提示交给作者核对补充。
- PDF 保留实际页码，并在 **PDF 页码与文字提取结果** 中标出文字层、OCR 或待补充的页。请检查跨页断句、页眉页脚和多栏阅读顺序。加密 PDF 需先解锁。
- 每份文件最多 32 MiB，单章正文最多 200 万字符，PDF 最多 300 页。保持原文时，重复导入继续复用已有版本与提取缓存。

## 本地 OCR

上传扫描 PDF 后，可以勾选 **对没有可用文字层的 PDF 页启用本地 OCR**。混合文件只识别缺少可用文字的页，一次最多 30 个 OCR 页。OCR 在本机运行。可切换页码逐页查看提取结果，尤其需要校对人名、专有词和版面复杂的页面。

在新环境安装时，可在项目虚拟环境中运行以下命令。

```powershell
python -m pip install -e '.[ui,ocr]'
```

默认语言为 `chi_sim+eng`。把 `chi_sim.traineddata` 和 `eng.traineddata` 放在项目的 `.cache/tessdata`，或在本机 `.env` 设置 `OCR_TESSDATA_PATH` 和 `OCR_LANGUAGE`。可从 [Tesseract 官方语言模型](https://github.com/tesseract-ocr/tessdata_fast) 获取语言文件。

## 命令行

先预览，再用同一文件确认导入。仅预览时无需作品 ID、章节号或模型密钥。

```powershell
easy-writing ingest '第一章.docx' --preview
easy-writing ingest '第一章.pdf' --preview --ocr
easy-writing ingest '第一章.docx' --project '<作品ID>' --chapter 1 --confirm --provider deepseek
```

可用 `--reviewed-text '校订稿.txt'` 提交校订过的 UTF-8 文本。识别扫描页后正式导入时也应保留 `--ocr`，以便复现来源映射。离线验收可用 `--provider fake --fixture` 提供章节对应的提取响应。

## 版本与来源

小说知识库升至 v9，新增按正文版本保存的文件来源记录。**知识查看** 的证据区和命令行 `source` 可查看文件名及 Word 段落号或 PDF 页码。证据引用以确认入库的文本及其偏移为准。改写后无法唯一对应的段落标记为作者校订。来源记录保留文件哈希和定位信息，原文件继续由作者保管。
