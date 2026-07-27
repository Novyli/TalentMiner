# TalentMiner — 学术人物联系方式挖掘

输入论文/文本 → 提取作者 → 跨源搜索联系方式 → 生成关系网络 → 导出 CSV。

## 安装
```bash
bash setup.sh
```

## 运行
```bash
bash start.sh
```
浏览器打开 **http://127.0.0.1:8899**

## 三种输入方式
1. **粘贴文本** — 用 `**姓名**` 标注作者，例如：
   `来自CUHK的**Ming Zhong**、来自Nebius的**Jinglei Cheng**合作完成量子编译器研究`
2. **DOI 检索** — 输入 DOI，自动从 OpenAlex 拉取作者
3. **上传 PDF** — 拖拽上传，自动解析

## 搜索来源
- OpenAlex API (2.5 亿论文，免费，无需 API Key)
- ORCID 公开 API (邮箱)
- Google Scholar profile 页
- ResearchGate profile 页
- DuckDuckGo 搜索大学 contact 页
- arXiv 论文元数据

## 输出
- **交互式关系网络图** (D3.js 力导向图)
- **CSV 导出**：姓名、邮箱、ORCID、Scholar/RG/Twitter/LinkedIn 链接、被引数、论据来源
- **SQLite 数据库**永久保存爬取结果 (talentminer.db)

## 技术要求
- Python 3.9+
- macOS / Linux / Windows WSL
- 网络连接

## 文件结构
```
TalentMiner/
├── app.py              # FastAPI 后端
├── crawler.py          # 爬虫引擎
├── pdf_parser.py       # PDF 解析 + 作者提取
├── graph_builder.py    # 关系图构建 (NetworkX)
├── storage.py          # SQLite 持久化
├── static/             # 前端 (HTML/CSS/JS/D3.js)
├── requirements.txt    # Python 依赖
├── setup.sh            # 一键安装
└── start.sh            # 一键启动
```
