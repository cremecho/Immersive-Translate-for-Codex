---
name: immersive-translate-local
description: 在 Codex 当前对话中把上传的 Word、TXT、Markdown 或 PDF 翻译为保留排版的双语文件。当用户对文档说「保留排版翻译」或「沉浸式翻译」时使用。
---

这是「保留排版翻译」「沉浸式翻译」的完整工作流。模型翻译文本包，程序负责版面处理和成品检查。默认目标语言为简体中文 `zh-CN`。用户另外要求逐页人工审版或翻译图内文字时，再按该要求处理。

用 `load_workspace_dependencies` 找到 Python 路径 `$py`。入口 `$entry = 'C:/Projects/Doc_Translate/translate.py'`。Windows PowerShell 可设 `[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)`。

1. **取任务包。** 每个附件运行 `& $py -X utf8 $entry start $inputFile --target zh-CN --compact`。用返回的 `job` 拼接各组 `prompt` 和 `answer` 的相对路径。相同原件及目标语言可续译。包列表为空时跳到第 3 步。
2. **翻译。** 按 `lane` 分工，主对话可翻译一组。调用子代理时用 `fork_turns="none"`，只给分配的包路径、必要术语和本步规则。每包由一人一次读取 `prompt`，如 `Get-Content -Raw -Encoding utf8 -LiteralPath $prompt`。按 `items` 顺序翻译。字符串是原文，二元组是原文和 `contexts` 中的提示编号。以 UTF-8 一次写入 `answer`，例如 `["@原包key","第一项译文","第二项译文"]`。首项复制包内 `key`，之后每项译文占一个位置。协作者完成后只汇报文件名和条数。

   译文保留数字、单位、专名、网址、样式标记及完整算式和上下标。原行上下文用于理解公式旁的片段，每项只翻译该项原文。PDF 正文、独立表格、图题和图注照常翻译。插图、流程图、填字格及截图保留原貌，混入包内的图内文字、乱码或叠印标签在对应位置填 JSON `null`。每位译者直接从所分配的文本包取文。
3. **生成并验收。** 运行 `& $py -X utf8 $entry finish $job --auto-output` 自动选取或复用成品。用户指定输出路径时改用 `finish $job -o $output`。若返回 `shorten`，参考所列原文和当前译文，保留含义、数字和公式，精简指定段落。将 `{"段号":"精简译文"}` 写入任务目录下的 UTF-8 JSON，再运行 `finish $job --auto-output --corrections $path`。其他答案问题按错误中的包和位置修正。
4. **交付。** 返回 `delivery_status: ready` 时立即向用户提供 `output` 链接并结束。`delivery_status: review` 时只核对 `review_findings` 所列位置，修正确认的缺陷后重新生成。字号、数学、链接等提示可简述，完整信息在 `report`。
