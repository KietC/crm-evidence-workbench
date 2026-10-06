# Evidence Trail — Local CRM Evidence Workbench

**Save one OKKI customer's communications and files locally, so you can review what happened and inspect the original sources.**

[English](#english) · [中文](#中文) · [Step-by-step setup](docs/setup.md) · [Troubleshooting](docs/troubleshooting.md) · [Architecture](docs/architecture.md)

## English

This workbench was developed for **OKKI / Xiaoman at `crm.xiaoman.cn`** on Windows. Use it when a customer's history is spread across messages, attachments and activity records, and you need a local archive before reviewing the case.

### When to use it

| Your question | What the workbench helps you do |
| --- | --- |
| **What happened with this customer?** | Save the selected OKKI record's available communications and files, then inspect their original sources and recorded gaps. |
| **Where is that quotation or specification?** | After configuring the required local processing, extract document text and build searchable records and attachment catalogs. |
| **How do these messages, files and events relate?** | After the processing prerequisites are met, build source-backed relationships and separate business-event and collection timelines. |

**Invented example:** two messages contain the same `example-quote.pdf`. The archive can recognize that the file bytes are identical while preserving both message occurrences. During review, you can trace the quotation to each message instead of losing one appearance during deduplication. Business-event time and the time this tool collected the record remain separate.

### Input and output

| You provide | You get |
| --- | --- |
| One authorized OKKI customer record and a login completed in the collector's browser | Local source objects/files, capture metadata, hashes, manifests and reconciliation/source-gap records |
| A valid captured case **plus the reviewed manual-trade prerequisites** | Local extraction results and, where configured, searchable SQLite/FTS, relationships, timelines, catalogs and reports |
| Optional local OCR, conversion or media tools | Additional derivatives linked back to preserved original files |

**Capture and full processing are different stages.** Automatic capture does not by itself unlock the current full extractor. `prepare`, `run` and `verify` require a reviewed manual-trade manifest; initial combined delivery also requires its matching receipt. This public release has the consumers and tests, **not a general-purpose manual-trade capture/receipt producer**. The [manual-trade guide](docs/manual-trade.md) explains that integration prerequisite. Keep a limited archive explicitly limited when it is not met.

### Real platforms and tools used

| Platform or tool | Its actual role |
| --- | --- |
| **OKKI / Xiaoman — `crm.xiaoman.cn`** | The CRM targeted by the [current adapter](app/src/adapter.ts) and [synthetic scope configuration](config/scope.json). A real case uses your own selected record and account. |
| **Windows + PowerShell** | The supported complete operating path, including launch/control scripts and optional Office COM conversion. |
| **Electron + Playwright Core** | Provides the local browser/control panel and collects the bound record through the implemented adapter. |
| **Python + SQLite/FTS + DuckDB** | Processes local evidence and produces searchable records and data exports after source prerequisites are satisfied. |
| **Tesseract + Poppler** | Optional local OCR and PDF page rendering; executables and language files are installed separately. |
| **Microsoft Office / LibreOffice + ExcelJS** | Optional document conversion/export, plus open-source spreadsheet generation. Office is optional and is not redistributed. |
| **FFmpeg / FFprobe + an external local ASR driver** | Optional media preparation and a defined transcription integration interface. The ASR driver and model weights must be supplied separately. |
| **Codex** | The development and assisted operating context. The standalone app and processing scripts can be used without a Codex conversation. |

These names explain where the tool runs and what it uses. They are not customer examples or a claim of affiliation with those platforms.

### Start with one route

1. **New to the project:** follow [Requirements](#requirements), then [synthetic checks](#quick-start-synthetic-checks-only). These do not access a customer.
2. **Ready to preserve a customer record:** after checks pass, use [single-record capture](#capture-your-own-authorized-record), complete login yourself and inspect the resulting archive.
3. **Need extraction, search, relationships or reports:** satisfy [manual-trade prerequisites](docs/manual-trade.md), then follow [local processing](#local-processing). Do not treat an absent prerequisite as a successful run.

Detailed installation and expected results remain in [Setup](docs/setup.md); failure paths are in [Troubleshooting](docs/troubleshooting.md).

### Scope and maturity

This is a source release, not a hosted service, a customer dataset, or a claim that every CRM installation can be captured completely.

The included adapter targets the logged-in OKKI / Xiaoman web application at `crm.xiaoman.cn`. Adapter routes, visible labels, permissions, pagination, and response schemas can change. Other CRM products require a real adapter implementation; changing the origin alone is not enough.

The default learning path is **single-record, fresh login, synthetic tests first**. Ordinary queue/multi-instance collection remains advanced functionality. Sensitive trade/customs pages are excluded from the automatic capture channel and require a separately reviewed manual workflow.

Important distinctions:

- A passing source test is not production validation.
- A hash confirms byte identity, not the accuracy of the source's business claims.
- A processed task is not necessarily a successful extraction; inspect terminal states and source gaps.
- Source-local sequence is not absolute business chronology. Unknown time zones stay unknown.
- Missing, deleted, inaccessible, encrypted, or opaque objects remain explicit gaps. They are not invented or silently counted as complete.

### Requirements

| Component | Required for | Notes |
| --- | --- | --- |
| Windows 10/11 or Windows Server with an interactive desktop | Supported end-to-end workflow | Office COM and the launch scripts are Windows-specific. |
| Node.js 24 or later | App, tests, spreadsheet builders | Use the committed npm lockfiles. |
| Python 3.12 | Local pipeline and tests | A dedicated virtual environment is recommended. |
| Patched SQLite with FTS5/WAL support | Local authority database | Run `scripts/sqlite_preflight.py`; Python's version alone is not sufficient. |
| uv (recommended) | Obtain a current managed Python | See [setup](docs/setup.md) for the existing-Python alternative. |
| Git | Clone/contribute | ZIP users can skip Git. |
| PowerShell | Windows launch and checks | Do not change machine-wide execution policy just for this repository. |
| Internet during installation | npm/PyPI packages and Electron download | Capturing a CRM also connects to that CRM and its allowed assets. Local processing is a separate phase. |

OCR, legacy Office, PDF rendering, and ASR have additional local prerequisites. See [the ordered installation guide](docs/setup.md#english) before enabling them.

### Quick start: synthetic checks only

These commands install source dependencies and run local synthetic checks. They do **not** log into a CRM or capture a customer.

Run in PowerShell from a new working directory:

```powershell
git clone https://github.com/KietC/crm-evidence-workbench.git
Set-Location crm-evidence-workbench

uv python install 3.12
uv venv --managed-python --python 3.12 .venv
$Python = Join-Path $PWD '.venv\Scripts\python.exe'
uv pip install --python $Python --require-hashes -r requirements-lock.txt
& $Python scripts/sqlite_preflight.py

# Root dependencies power the open-source spreadsheet builders.
npm ci

# App dependencies include Electron; no Playwright browser download is needed.
Push-Location app
try {
    npm ci
    npm run check
    npm run build
    npm test
    npm run smoke
} finally {
    Pop-Location
}

& $Python -m unittest discover -s pipeline -p 'test_*.py'
& $Python -m unittest discover -s verify -p 'test_*.py'
```

Check each native command's exit code before continuing. For a fail-fast, consolidated check, use:

```powershell
.\scripts\check.ps1 -Python $Python
```

Read [setup](docs/setup.md) for exact expected results, tool detection, configuration order, and the boundary between checks and live collection. Do not use downloaded customer examples as test fixtures.

### Capture your own authorized record

Only start this phase after the synthetic checks pass and you have selected a record you are allowed to read. No account, password, cookie, or production capture is bundled.

1. Start a one-shot collector bound to your record ID.
2. Complete login yourself inside its local browser if required.
3. Check the selected record and press Start once.
4. Keep the bound record open; do not navigate to another customer during capture.
5. Inspect the case reconciliation and source gaps before post-processing.

Example syntax; `123456789` is a fictional placeholder, **not** a usable record:

```powershell
# Replace the placeholder with your own authorized record ID before executing.
.\app\scripts\capture-one.ps1 -CompanyId '123456789' -NoAutoNext -NoProfileClone
```

The one-shot launcher disables auto-next and shared queue processing. Its control port is `127.0.0.1:3311` and its local browser debugging port is `127.0.0.1:9434`. Do not expose either port through a public bind, tunnel, or reverse proxy. A browser debugging port can control a logged-in browser.

Runtime profiles and evidence are private even if source code is public. [Open-source boundary](docs/open-source-boundary.md) explains what must never enter a commit or issue.

### Local processing

Do not build an empty folder and call it a capture. The processing scripts expect a valid case identity, source manifests, and evidence produced by the collector or a compatible importer.

**Required before the commands below:** the current full extractor requires a reviewed manual-trade manifest in addition to the automatic seven-tab scope. `prepare`, `run`, and `verify` all stop with `MANUAL_TRADE_MANIFEST_MISSING` when it is absent. The release includes its consumers and synthetic tests, not a general-purpose manual-trade capture/receipt producer. Complete the [manual-trade prerequisites and verification checklist](docs/manual-trade.md) first; do not create an empty PASS manifest to bypass the gate. Initial combined delivery also requires a matching manual-trade receipt. Automatic capture alone is not a turnkey combined extraction/delivery.

From the repository root, set the actual case path and its matching record ID locally:

```powershell
# Fictional example path and ID; replace both with your local case binding.
$CaseRoot = 'C:\EvidenceData\company_123456789'
$CompanyId = '123456789'

& $Python pipeline\single_customer_full_extract.py prepare `
    --case-root $CaseRoot --company-id $CompanyId

# Install/configure OCR and media tools first; see docs/setup.md.
& $Python pipeline\single_customer_full_extract.py run `
    --case-root $CaseRoot --company-id $CompanyId `
    --parse-workers 4 --ocr-workers 2

& $Python pipeline\single_customer_full_extract.py verify `
    --case-root $CaseRoot --company-id $CompanyId
```

Re-running `run` on the same prepared scope resumes unfinished tasks; it does not require a `--resume` flag. Do not run two writers against one case. Archive preparation has a cumulative expansion cap of 4 GiB and 10,000 inspected entries by default.

For relations, business/collection timelines, completion stages, optional reports, and exact CLI flags, see [processing workflow](docs/processing.md). Run a script's `--help` before using a copied command. Advanced completion commands require counts derived from **your** manifests; the release does not carry a former installation's counts.

### Repository map

```text
.
├── app/                    Electron collector, TypeScript source, UI, adapter, tests
├── capture/                Lower-level evidence capture utilities
├── config/                 Generic/synthetic scope configuration
├── pipeline/               Local extraction, relations, timelines, catalogs, reports
├── verify/                 Case verification and source checks
├── scripts/                Consolidated checks and privacy scanning
├── docs/                   Bilingual setup, workflows, pitfalls, and design notes
├── cli.py                  Deterministic local post-processing dispatcher
├── requirements-local.txt Python processing dependencies
├── package.json            Open-source spreadsheet dependencies
├── resume_completion_v2.ps1
└── status_completion_v2.ps1
```

Private runtime profiles, cases, outputs, downloaded models, and test working directories are not part of the source release. See [architecture](docs/architecture.md) for the evidence flow and locking model.

The spreadsheet builders' PNGs are open-source SVG/Sharp layout previews. They are not native Excel/Office rendering or recalculation certificates; final human-format QA remains a separate local step.

### How evidence is kept

- Keeps browser collection and local post-processing separate.
- Provides an Electron browser/control panel and a single-record capture entry point.
- Saves source objects, request/visit metadata, manifests, and hashes before building derivatives.
- Extracts native document text and optionally performs local OCR, archive expansion, and media processing.
- Retains both content identity and occurrence identity: one byte-identical attachment can belong to several messages.
- Builds explicit source-backed relations, a business-event timeline, and a separate collection timeline.
- Exports local SQLite/FTS, Parquet, GraphML, spreadsheet catalogs, and document reports when the corresponding prerequisites are available.
- Includes synthetic regression tests for identity guards, locks, startup races, evidence storage, archive limits, lineage, and delivery checks.

### Dependencies and optional integrations

| Integration | Included in source | What you install separately |
| --- | --- | --- |
| Electron + Playwright Core | App wrapper, adapter, npm lockfile | npm packages; Electron is downloaded by its package installer. |
| ExcelJS | Spreadsheet builders | Root npm dependencies. No proprietary spreadsheet SDK is required. |
| pypdf / pdfplumber / Pillow / python-docx / DuckDB | Python integration code | Install the hash-pinned `requirements-lock.txt`; `requirements-local.txt` declares direct dependencies. |
| Tesseract | Local OCR driver | Executable and chosen `*.traineddata` language files. |
| Poppler | PDF page-rendering driver | `pdftoppm` on PATH or explicit `--pdftoppm`. |
| 7-Zip | Bounded archive driver | `7z`/`7zz`; Windows standard installation path is also detected. |
| Ghostscript | EPS/PostScript rendering driver | `gswin64c`, `gswin32c`, or `gs`, or explicit `--ghostscript`. |
| Microsoft Office COM | Read-only legacy conversion and Word PDF export | Licensed local Office; this is optional and not itself open source. |
| LibreOffice | Local legacy-conversion fallback | Local LibreOffice installation. |
| FFmpeg / FFprobe | Media probing, decoding, frame preparation | Local executables or explicit flags. |
| Local ASR pipeline | An explicit integration interface | A compatible driver, its model weights, and its own environment. No Whisper/Qwen weights or private driver are bundled. |
| Private WARC export | Exporter and hash-pinned archive requirements | Optional `warcio`; generated WARC stays private. |

External tools, model weights, and service terms have their own licenses. The project's MIT license does not relicense them.

### Choose the right project

| Your task | Project |
| --- | --- |
| Preserve and review one OKKI customer's communications and files | **CRM Evidence Workbench — this repository** |
| Archive another adapted website or research public market leads | [Local Evidence Collector](https://github.com/KietC/local-evidence-collector) |
| Transcribe existing media and make reviewed learning materials | [Polyglot Media Workbench](https://github.com/KietC/polyglot-media-workbench) |
| Show compatible Xiaomi phone captions on a Windows screen | [Caption Relay](https://github.com/KietC/caption-relay) |
| Organize a trade-show exhibitor directory | [Exhibitor Research Archive](https://github.com/KietC/exhibitor-research-archive) |
| Investigate the manufacturer behind a product | [FactoryTrace](https://github.com/KietC/factorytrace) |

These links are selection guidance. A shared capture core or a media-workbench handoff still needs a supported interface and validation; this table does not claim those integrations are complete.

### Development and contributions

Use temporary synthetic inputs when changing source. Never attach real mail, customer names, attachments, tokens, login profiles, or raw production logs to an issue or pull request. Include the command, exit code, runtime versions, and sanitized error code instead.

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and [troubleshooting](docs/troubleshooting.md). A source scan is a release check, not proof that arbitrary future data is anonymous.

### License

Project source is released under the [MIT License](LICENSE). Third-party dependency notices and licenses remain applicable. This project is not affiliated with or endorsed by a CRM vendor.

---

## 中文

**把某一 OKKI 客户的沟通和文件保存到本地，方便复盘发生过什么，并查看原始来源。**

这套工作台实际为 **OKKI / 小满 `crm.xiaoman.cn`** 开发，完整运行路线以 Windows 为主。客户历史分散在邮件、附件和动态里，你需要先保存本地原件再复盘时，可以使用它。

### 适合什么场景

| 你想知道什么 | 工作台能帮你做什么 |
| --- | --- |
| **这个客户之前发生过什么？** | 保存选定 OKKI 记录中可取得的沟通和文件，查看原件及明确记录的来源缺口。 |
| **那份报价或规格文件在哪里？** | 配置必需的本地处理后，提取文档文字，建立可检索记录和附件目录。 |
| **邮件、附件和事件是什么关系？** | 满足后处理前提后，建立有来源的关系，并分开业务事件时间线与采集时间线。 |

**虚构示例：**两封邮件都附有同一份 `example-quote.pdf`。归档可以识别两份附件字节相同，同时保留它分别出现在两封邮件里的记录。复盘时能从报价追溯到每封邮件，不会因为去重丢掉一次出现。业务发生时间与本工具采集记录的时间也分开保存。

### 输入什么，最后得到什么

| 你提供什么 | 最后得到什么 |
| --- | --- |
| 一条有权读取的 OKKI 客户记录，并在采集器浏览器中自己完成登录 | 本地原始对象/文件、采集元数据、哈希、清单以及对账/来源缺口记录 |
| 有效的采集案例，**加上审查后的手动贸易前提材料** | 本地提取结果，以及配置后的 SQLite/FTS 检索、关系、时间线、目录和报告 |
| 可选的本地 OCR、转换或媒体工具 | 与原文件保持来源关联的更多派生结果 |

**采集与完整后处理是两个阶段。** 自动采集本身不能直接开启当前完整提取器。`prepare`、`run`、`verify` 要求审查后的手动贸易清单；初始联合交付还要求匹配回执。本公开版本包含消费端和测试，**没有通用手动贸易采集/回执生成器**。[手动贸易指南](docs/manual-trade.md)说明了这项集成前提。未满足时，有限归档仍应明确标为有限范围。

### 实际用到了哪些平台和工具

| 平台或工具 | 在本项目中的实际作用 |
| --- | --- |
| **OKKI / 小满 — `crm.xiaoman.cn`** | [当前适配器](app/src/adapter.ts)和[合成范围配置](config/scope.json)针对的 CRM。真实案例使用你自己选定的记录和账号。 |
| **Windows + PowerShell** | 完整支持的操作路线，包括启动/控制脚本及可选 Office COM 转换。 |
| **Electron + Playwright Core** | 提供本地浏览器/控制面板，通过已实现的适配器采集绑定记录。 |
| **Python + SQLite/FTS + DuckDB** | 满足来源前提后，处理本地证据并生成可检索记录与数据导出。 |
| **Tesseract + Poppler** | 可选本地 OCR 和 PDF 逐页渲染；程序及语言包需另行安装。 |
| **Microsoft Office / LibreOffice + ExcelJS** | 可选文档转换/导出，以及开源表格生成。Office 可选，仓库不分发它。 |
| **FFmpeg / FFprobe + 外部本地 ASR 驱动** | 可选媒体准备及明确的转写接口，ASR 驱动和模型权重需另行提供。 |
| **Codex** | 开发及辅助操作环境，独立应用和处理脚本不依赖 Codex 聊天才能使用。 |

这些名称说明工具实际运行在哪里、用了什么，不是客户样例，也不代表与平台存在合作关系。

### 先选一条入门路线

1. **第一次使用：**先看[运行要求](#运行要求)，再做[合成检查](#快速开始只运行合成检查)，不会访问真实客户。
2. **需要保存客户原件：**检查通过后，按[单记录采集](#采集自己的授权记录)自己登录，检查得到的归档。
3. **需要提取、检索、关系或报告：**先满足[手动贸易前提](docs/manual-trade.md)，再按[本地处理](#本地处理)操作；不能把缺少前提当成处理成功。

详细安装和预期结果仍在[配置指南](docs/setup.md)，失败处理见[避坑指南](docs/troubleshooting.md)。

### 范围与成熟度

这是源码发布，不是托管服务、客户数据集，也不是“所有 CRM 都能绝对完整采集”的承诺。

所带适配器针对 `crm.xiaoman.cn` 上登录后的 OKKI / 小满 Web 应用。路由、可见标签、权限、分页和响应结构都可能变化。适配其他 CRM 需要真正实现适配器，不能只改域名。

默认学习顺序是：**先合成测试，再全新登录，再单记录采集**。普通队列和多实例采集属于高级功能。敏感贸易/海关页面不进入自动采集通道，需要单独审核的手动流程。

必须分清：

- 源码测试通过，不等于生产验证通过。
- 哈希证明字节一致，不证明源系统业务内容正确。
- 任务处理过，不等于成功提取；要看终态和源缺口。
- 单一来源的序号，不等于绝对业务时间顺序；未知时区保持未知。
- 已删除、失效、无权限、加密或无法解释的对象都明确保留缺口，不能编造，也不能悄悄算成完整。

### 运行要求

| 组件 | 用途 | 说明 |
| --- | --- | --- |
| Windows 10/11 或有交互桌面的 Windows Server | 完整支持的工作流 | Office COM 和启动脚本为 Windows 专用。 |
| Node.js 24 或以上 | 应用、测试、Excel 构建 | 使用仓库内 npm 锁文件。 |
| Python 3.12 | 本地处理与测试 | 建议独立虚拟环境。 |
| 已修补 SQLite，支持 FTS5/WAL | 本地权威数据库 | 运行 `scripts/sqlite_preflight.py`，不能只看 Python 版本。 |
| uv（推荐） | 获取较新的托管 Python | 已有 Python 的替代路径见 [配置指南](docs/setup.md)。 |
| Git | 克隆和贡献 | 下载 ZIP 的用户可以不装。 |
| PowerShell | Windows 启动与检查 | 不必为此仓库修改整机执行策略。 |
| 安装阶段联网 | npm/PyPI 包及 Electron 下载 | CRM 采集也会连接 CRM 与允许的资源域；本地处理是独立阶段。 |

OCR、旧版 Office、PDF 渲染和 ASR 还有额外本地依赖。启用前按 [顺序配置指南](docs/setup.md#中文) 操作。

### 快速开始：只运行合成检查

以下命令安装源码依赖并运行本地合成检查，**不会**登录 CRM 或采集客户。

在 PowerShell 的新工作目录运行：

```powershell
git clone https://github.com/KietC/crm-evidence-workbench.git
Set-Location crm-evidence-workbench

uv python install 3.12
uv venv --managed-python --python 3.12 .venv
$Python = Join-Path $PWD '.venv\Scripts\python.exe'
uv pip install --python $Python --require-hashes -r requirements-lock.txt
& $Python scripts/sqlite_preflight.py

# 仓库根目录依赖负责开源 Excel 构建器。
npm ci

# 应用依赖含 Electron；无需另装 Playwright 浏览器。
Push-Location app
try {
    npm ci
    npm run check
    npm run build
    npm test
    npm run smoke
} finally {
    Pop-Location
}

& $Python -m unittest discover -s pipeline -p 'test_*.py'
& $Python -m unittest discover -s verify -p 'test_*.py'
```

每个外部命令结束后都应检查退出码。需要统一的失败即停检查，可运行：

```powershell
.\scripts\check.ps1 -Python $Python
```

[配置指南](docs/setup.md) 说明了预期结果、工具检查、配置顺序，以及测试和真实采集的边界。不要把下载来的客户资料当测试样本。

### 采集自己的授权记录

只有在合成检查通过、且已选定自己有权读取的记录后，才开始此阶段。仓库不带账号、密码、Cookie 或生产采集物。

1. 启动绑定该记录 ID 的 one-shot 采集器。
2. 如需登录，在其本地浏览器内自己完成。
3. 确认当前记录正确后，只按一次 Start。
4. 保持绑定记录打开，采集中不要跳到其他客户。
5. 后处理之前，先看案例对账与源缺口。

以下只是命令格式，`123456789` 是虚构占位值，**不是**可用客户：

```powershell
# 执行前替换为自己有权读取的记录 ID。
.\app\scripts\capture-one.ps1 -CompanyId '123456789' -NoAutoNext -NoProfileClone
```

one-shot 禁用自动下一客户和共享队列处理。控制端口为 `127.0.0.1:3311`，本地浏览器调试端口为 `127.0.0.1:9434`。不要通过公开监听、隧道或反向代理暴露它们；调试端口可以控制已登录浏览器。

源码公开不等于运行资料公开。浏览器配置和证据始终是私密材料。[开源边界](docs/open-source-boundary.md) 说明了不能提交或贴到 Issue 的内容。

### 本地处理

不能建一个空目录就当成采集结果。处理脚本需要有效的案例身份、来源清单，以及采集器或兼容导入器生成的证据。

**以下命令的必需前置条件：** 当前完整提取器除自动七标签范围外，还强制要求经过审查的手动贸易清单。缺少时，`prepare`、`run`、`verify` 都会报 `MANUAL_TRADE_MANIFEST_MISSING`。本发布包含清单消费端与合成测试，不包含通用手动贸易采集/回执生成器。先完成 [手动贸易前置条件与验收清单](docs/manual-trade.md)，不能创建空 PASS 清单绕过门槛。初始联合交付还要求匹配的贸易回执。仅自动采集不是开箱即用的联合提取/交付。

从仓库根目录开始，只在本机设置实际案例路径和匹配的记录 ID：

```powershell
# 虚构示例路径和 ID；请替换为自己的本地案例绑定。
$CaseRoot = 'C:\EvidenceData\company_123456789'
$CompanyId = '123456789'

& $Python pipeline\single_customer_full_extract.py prepare `
    --case-root $CaseRoot --company-id $CompanyId

# 先配置 OCR 与媒体工具，见 docs/setup.md。
& $Python pipeline\single_customer_full_extract.py run `
    --case-root $CaseRoot --company-id $CompanyId `
    --parse-workers 4 --ocr-workers 2

& $Python pipeline\single_customer_full_extract.py verify `
    --case-root $CaseRoot --company-id $CompanyId
```

对同一已准备范围重新执行 `run` 会续跑未完成任务，无需 `--resume` 参数。不要给一个案例启动两个写进程。压缩包准备默认累计限额为 4 GiB 展开字节、10,000 个检查条目。

关系、业务/采集时间线、补全阶段、可选报告和准确参数见 [处理工作流](docs/processing.md)。复制命令前先看脚本 `--help`。高级补全命令的数量必须来自**自己的**清单，不能沿用某次旧安装的统计数。

### 仓库结构

```text
.
├── app/                    Electron 采集器、TypeScript、界面、适配器与测试
├── capture/                底层证据采集工具
├── config/                 通用/合成范围配置
├── pipeline/               本地提取、关系、时间线、目录和报告
├── verify/                 案例校验与源码检查
├── scripts/                统一检查与隐私扫描
├── docs/                   双语配置、工作流、避坑与设计说明
├── cli.py                  确定性本地后处理入口
├── requirements-local.txt Python 处理依赖
├── package.json            开源 Excel 依赖
├── resume_completion_v2.ps1
└── status_completion_v2.ps1
```

运行配置、案例、输出、下载模型和测试临时目录不属于源码发布。证据流和锁模型见 [架构](docs/architecture.md)。

表格构建器的 PNG 是开源 SVG/Sharp 布局预览，不是 Excel/Office 原生渲染或重算证书；最终人读版 QA 仍需单独在本机完成。

### 原件和关系如何保留

- 将浏览器采集与本地后处理分开。
- 提供 Electron 浏览器/控制面板，以及绑定单一记录的采集入口。
- 先保存原始对象、请求/访问元数据、清单与哈希，再生成派生结果。
- 提取文档原生文字，可选启用本地 OCR、压缩包递归与媒体处理。
- 同时保留“内容对象”和“出现实例”：字节相同的一份附件可以属于多封邮件。
- 建立有明确来源的关系、业务事件时间线，以及独立的采集时间线。
- 在相关依赖齐备时导出本地 SQLite/FTS、Parquet、GraphML、Excel 目录和文档报告。
- 提供身份绑定、锁、启动竞争、证据存储、压缩预算、血缘和交付检查的合成回归测试。

### 依赖与可选集成

| 集成 | 源码中包含 | 需要自己安装 |
| --- | --- | --- |
| Electron + Playwright Core | 应用封装、适配器和 npm 锁文件 | npm 包；Electron 由其安装程序下载。 |
| ExcelJS | Excel 构建器 | 仓库根目录 npm 依赖；不需要专有表格 SDK。 |
| pypdf / pdfplumber / Pillow / python-docx / DuckDB | Python 集成代码 | 安装带哈希锁定 `requirements-lock.txt`；`requirements-local.txt` 声明直接依赖。 |
| Tesseract | 本地 OCR 调用器 | 可执行文件与所选 `*.traineddata` 语言包。 |
| Poppler | PDF 逐页渲染调用器 | PATH 中的 `pdftoppm` 或 `--pdftoppm`。 |
| 7-Zip | 有预算限制的压缩包调用器 | `7z`/`7zz`；也会识别 Windows 标准安装位置。 |
| Ghostscript | EPS/PostScript 渲染调用器 | `gswin64c`、`gswin32c`、`gs` 或 `--ghostscript`。 |
| Microsoft Office COM | 只读旧文档转换与 Word PDF 导出 | 本地正版 Office；可选且不是开源软件。 |
| LibreOffice | 旧文档转换本地兜底 | 本地 LibreOffice。 |
| FFmpeg / FFprobe | 媒体探测、解码和画面准备 | 本地程序或明确参数。 |
| 本地 ASR 流水线 | 明确的集成接口 | 兼容驱动、模型权重及其独立环境；不带 Whisper/Qwen 权重或私有驱动。 |
| 私密 WARC 导出 | 导出器与带哈希锁定的依赖 | 可选 `warcio`；生成的 WARC 不能公开。 |

外部工具、模型权重和服务条款有各自的许可证。项目 MIT 许可证不能替它们重新授权。

### 六个项目怎么选

| 你要做的事 | 选择的项目 |
| --- | --- |
| 保存和复盘某一 OKKI 客户的沟通与文件 | **CRM Evidence Workbench：本仓库** |
| 归档其他已适配网站，或研究公开市场线索 | [Local Evidence Collector](https://github.com/KietC/local-evidence-collector) |
| 转写已有音视频，制作审校学习材料 | [Polyglot Media Workbench](https://github.com/KietC/polyglot-media-workbench) |
| 把兼容小米手机字幕显示在 Windows 大屏上 | [Caption Relay](https://github.com/KietC/caption-relay) |
| 整理展会展商名单 | [Exhibitor Research Archive](https://github.com/KietC/exhibitor-research-archive) |
| 调查产品背后的制造方 | [FactoryTrace](https://github.com/KietC/factorytrace) |

这些链接用于选工具。共用采集核心或对接音视频工作台仍需要受支持的接口及验证，不能据此声称已完成集成。

### 开发与贡献

修改源码只用临时合成输入。Issue 和 PR 不得包含真实邮件、客户名字、附件、Token、登录配置或原始生产日志。只附命令、退出码、运行时版本和清理后的错误码。

见 [CONTRIBUTING.md](CONTRIBUTING.md)、[SECURITY.md](SECURITY.md) 和 [避坑指南](docs/troubleshooting.md)。源码扫描只是发布检查，不能证明以后任意输入都已经匿名。

### 许可证

项目源码使用 [MIT License](LICENSE)。第三方依赖的声明和许可证仍然有效。项目不隶属于任何 CRM 厂商，也不代表其背书。
