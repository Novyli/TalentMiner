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

## 桌面安装包

桌面版会把 Python、FastAPI 和运行依赖全部打进应用。用户双击图标即可运行，
无需安装 Python 或打开终端。数据库和上传文件保存在每位用户自己的应用数据目录：

- macOS：`~/Library/Application Support/TalentMiner/`
- Windows：`%LOCALAPPDATA%\TalentMiner\`

开发机首次安装打包依赖：

```bash
./venv/bin/python -m pip install -r requirements-desktop.txt
```

macOS 构建：

```bash
bash packaging/build_macos.sh
```

产物位于 `release/TalentMiner-macOS-<架构>.dmg`。

Windows 构建需要 Windows 环境以及 Inno Setup：

```powershell
powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1
```

产物位于 `release/TalentMiner-Windows-x64-Setup.exe`。仓库中的
`.github/workflows/build-desktop.yml` 可以在推送 `v*` 标签或手动触发时，
分别在 macOS 与 Windows 环境生成两个安装包。

macOS 自动构建同时覆盖 Intel 与 Apple Silicon。设置 `MACOS_SIGN_IDENTITY`
后构建脚本会签名应用和 DMG；预先创建 notarytool 钥匙串配置并设置
`NOTARYTOOL_PROFILE` 后，还会自动完成 Apple 公证与装订。Windows 构建可通过
`WINDOWS_CERT_PATH` 和 `WINDOWS_CERT_PASSWORD` 对主程序与安装程序签名。

未签名的 macOS 应用首次打开会触发 Gatekeeper 提示；公开分发前建议配置
Apple Developer ID 签名与公证。Windows 公开分发前同样建议配置代码签名证书。

## 四种输入方式
1. **粘贴文本** — 用 `**姓名**` 标注作者，例如：
   `来自CUHK的**Ming Zhong**、来自Nebius的**Jinglei Cheng**合作完成量子编译器研究`
2. **DOI 检索** — 输入 DOI，自动从 OpenAlex 拉取作者
3. **上传 PDF** — 单篇保留人工勾选作者流程；多选或拖入多篇 PDF 后自动创建可恢复的批量任务（最多 20 篇）
4. **历史回溯** — 按关键词和日期范围从 OpenAlex 检索历史论文，预览并勾选后自动进入联系方式搜集；开放 PDF 不可用时使用作者元数据继续处理

历史回溯可选择全部作者、第一作者或通讯作者（索引缺失通讯作者标记时使用末位作者）。单次最多预览和处理 500 篇，后台任务支持服务重启恢复。OpenAlex 当前允许无 Key 访问；如服务策略变化或需要独立配额，可设置环境变量 `OPENALEX_API_KEY`。

## 搜索来源
- OpenAlex API (2.5 亿论文，免费，无需 API Key)
- ORCID 公开 API（邮箱、中文/拼音姓名变体、本人公开主页）
- Google Scholar profile 页
- ResearchGate profile 页
- OpenAlex 机构 ID → 官方机构主页与 sitemap
- 量子/物理院系、实验室、课题组成员、研究生/导师、答辩与学位公告路径（官方域名限定）
- 官方页面公开邮箱、办公电话；中国机构页面可识别公开手机号
- DuckDuckGo 站内限定搜索（不可用时自动回退官方 sitemap）
- arXiv 论文元数据

## 联系方式证据
- 中文高校会同时使用论文姓名、OpenAlex/ORCID 中文名和拼音变体进行官网检索
- 邮箱和电话按“本人、导师/办公室、课题组/公共账号、归属未确认”分类
- 只有归属证据达到阈值的联系方式才进入主联系人字段；其余保留为待复核或拒绝候选
- 人物详情记录每条检索式、访问页面、结果数量以及采用、跳过或拒绝原因
- ORCID 直接链接的个人主页会单独核验姓名后再提取公开联系方式
- ORCID 关联课题组主页会继续进入一层成员目录，支持“研究生标题＋姓名列表”页面
- OpenAlex 姓名变体必须与论文姓名兼容；疑似合并人物的污染别名会被丢弃

## 毕业生候选分析
- 从官方个人页识别博士生、硕士生、预计毕业年份、答辩和求职信号
- 结合 OpenAlex 近三年第一作者论文形成可解释的毕业候选评分
- 沿导师/实验室官网发现其他学生，单独标记为“官网候选”，不与已验证作者混合
- 记录中国教育、中国机构和公开中文履历等“中国关联证据”
- **不根据姓名推断国籍**；只有官方页面明确公开国籍时才填写国籍字段
- 关系图保留合作、同机构和同领域关系，并支持临近毕业/中国关联筛选

## 合作者网络扩展
- 点击紫色合作者节点可单独选择“深度搜集此合作者”
- 可按合作次数、近年合作、共同研究主题和多目标作者关联度自动扩展前 5 名
- 旧数据只有姓名时，会回到目标作者的共同署名论文解析唯一 OpenAlex ID
- 同名对应多个 OpenAlex ID 时自动拒绝，不按姓名强行匹配
- 基础作者和已扩展作者按 OpenAlex ID 去重，连字符样式不同不会重复深挖
- 扩展后升级为完整人物节点，并执行联系方式、毕业候选和中国关联分析
- 默认只扩展一层，扩展任务写入 SQLite，服务重启后可以恢复
- 页面会显示扩展任务经历过几次服务中断恢复

## 输出
- **交互式关系网络图** (D3.js 力导向图)
- **CSV 导出**：姓名、经验证邮箱、公开电话、毕业候选评分、预计毕业年份、中国关联证据、机构官网路径、ORCID、Scholar/RG/Twitter/LinkedIn 链接、被引数、搜索轨迹与论据来源
- **合并联系方式**：从历史记录中勾选需要合并的论文并命名任务；合并结果作为新的历史记录保存，按已验证 OpenAlex ID/ORCID 安全去重，保留置信度最高的联系方式和论文来源，可直接导出 CSV
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
