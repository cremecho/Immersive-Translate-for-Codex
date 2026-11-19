# 本项目的文档翻译入口

当用户上传文档并说「保留排版翻译」或「沉浸式翻译」时，[本地翻译技能](skill/immersive-translate-local/SKILL.md) 是完整执行入口。当前对话翻译文本包，程序完成图形保留、排版和验收。`finish` 返回 `delivery_status: ready` 时直接交付文件。通用 PDF/Word 校样流程用于用户另行要求人工审版的任务。
