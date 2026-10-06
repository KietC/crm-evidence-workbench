# Installation and Configuration, in Order / 按顺序安装与配置

[README](../README.md) · [Troubleshooting](troubleshooting.md) · [Processing](processing.md)

## English

### 0. Choose your stopping point

There are three different milestones. Do not skip one because a later script exists:

1. **Source checks:** install dependencies, compile, and run temporary synthetic tests. No CRM login is needed.
2. **Your own capture:** manually log in, bind one authorized record, collect evidence, and reconcile counts.
3. **Local extraction/delivery:** configure the optional tools that match your files, process, verify, then author and inspect exports.

The repository is not a turnkey installation of every OCR/ASR engine. It includes the drivers and validation flow; external executables, model weights, Office, and a compatible ASR driver must be supplied separately. Start with milestone 1.

### 1. Install and confirm the base tools

Use a normal interactive Windows account. Administrator privileges are not required by the source checks. Obtain installers from the corresponding upstream sources:

- [Node.js downloads](https://nodejs.org/en/download): use the Node 24 line as the documented baseline; the app requires Node 24 or newer.
- [uv installation](https://docs.astral.sh/uv/getting-started/installation/): recommended for a current managed Python 3.12 build. For Windows with WinGet, `winget install --id astral-sh.uv -e` is an upstream-documented option.
- [Python Windows downloads](https://www.python.org/downloads/windows/): an existing trusted Python 3.12+ is an alternative **only if its SQLite preflight passes**. Some installers include an older SQLite library.
- [Git for Windows](https://gitforwindows.org/): needed only for cloning and contribution.

Close and reopen PowerShell after installation so PATH changes are visible. Confirm all four commands before cloning:

```powershell
node --version
npm --version
uv --version
git --version
```

Expected: Node's major version is at least 24; uv, npm, and Git return versions rather than “not recognized.” Existing-Python users can skip uv and follow the alternative below. Reopening the terminal matters after installing any of them.

No `npx playwright install` step is needed: the app uses Playwright **Core**, while Electron supplies its embedded browser. Chrome/Edge are needed only for the alternative external-browser path.

### 2. Create an independent source checkout

Choose a writable, non-synced local folder. Do not clone over a live collector or an evidence case. An ASCII path reduces compatibility problems in native OCR/Office tools.

```powershell
$ProjectParent = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'EvidenceTrail\projects'
New-Item -ItemType Directory -Path $ProjectParent -Force | Out-Null
Set-Location $ProjectParent
git clone https://github.com/KietC/crm-evidence-workbench.git
Set-Location '.\crm-evidence-workbench'
$RepoRoot = $PWD.Path
```

The example uses a per-user writable directory rather than requiring write access to the drive root. If native tools later require ASCII-only paths, choose a separate ASCII path that your account already owns.

ZIP users: extract into a new folder, enter that folder, and set `$RepoRoot = $PWD.Path`. Check that `app\package.json`, `requirements-local.txt`, and `cli.py` exist there.

Do not run commands from `app` when they expect the repository root. The instructions explicitly use `Push-Location app` only for app commands.

### 3. Create a private Python environment

Use the [managed Python workflow](https://docs.astral.sh/uv/guides/install-python/), which keeps the project interpreter independent of an older system installation:

```powershell
uv python install 3.12
if ($LASTEXITCODE -ne 0) { throw 'Managed Python download failed' }
uv venv --managed-python --python 3.12 .venv
if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed' }
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
uv pip install --python $Python --require-hashes -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed' }
uv pip check --python $Python
if ($LASTEXITCODE -ne 0) { throw 'Python dependencies are inconsistent' }
& $Python scripts\sqlite_preflight.py
if ($LASTEXITCODE -ne 0) { throw 'Selected Python SQLite is not suitable; do not continue' }
```

Activation is optional because every command uses `$Python` explicitly. This also avoids changing a system Python and avoids an `Activate.ps1` execution-policy problem.

`requirements-local.txt` declares the direct dependencies; `requirements-lock.txt` pins them and their transitive dependencies with hashes. Neither is an ASR/model environment. Keep a local dependency/version record for a production run; do not publish a freeze containing internal package URLs or credentials.

Existing trusted Python alternative:

```powershell
py -3.12 -m venv .venv
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
& $Python -m pip install --require-hashes -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed' }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Python dependencies are inconsistent' }
& $Python scripts\sqlite_preflight.py
if ($LASTEXITCODE -ne 0) { throw 'SQLite preflight failed; use a patched Python distribution' }
```

The preflight checks real temporary-database capabilities and the [SQLite WAL-reset fix](https://sqlite.org/wal.html#walresetbug): SQLite 3.51.3+ or the supported 3.44.6+/3.50.7+ backport branches. A newer Python version or `pip install` alone does not upgrade the interpreter's built-in SQLite. Do not replace arbitrary DLLs in a live Python installation to force the test green; create a suitable new environment instead.

### 4. Install both npm dependency sets

The root dependency set is for spreadsheet/catalog builders; the app dependency set is for Electron and TypeScript. Installing one does not install the other.

```powershell
Set-Location $RepoRoot
npm ci
if ($LASTEXITCODE -ne 0) { throw 'Root npm installation failed' }

Push-Location app
try {
    npm ci
    if ($LASTEXITCODE -ne 0) { throw 'App npm installation failed' }
} finally {
    Pop-Location
}
```

`npm ci` uses the committed lockfile and recreates that checkout's `node_modules`. Do not point `node_modules` at another project's directory and then install into it. Electron's install can download a browser binary, so an npm-package success alone does not prove the Electron binary is present.

If PowerShell blocks `npm.ps1`, use `npm.cmd` for the same commands. Do not disable execution policy machine-wide. A one-command process-scoped script invocation is enough when necessary:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\check.ps1 -Python $Python
```

### 5. Pass the synthetic checks before logging in

```powershell
Set-Location $RepoRoot
.\scripts\check.ps1 -Python $Python
```

For diagnosing an individual stage:

```powershell
Push-Location app
try {
    npm run check
    if ($LASTEXITCODE -ne 0) { throw 'TypeScript check failed' }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw 'TypeScript build failed' }
    npm test
    if ($LASTEXITCODE -ne 0) { throw 'App synthetic tests failed' }
    npm run smoke
    if ($LASTEXITCODE -ne 0) { throw 'Adapter smoke failed' }
} finally {
    Pop-Location
}

& $Python -m unittest discover -s pipeline -p 'test_*.py'
if ($LASTEXITCODE -ne 0) { throw 'Pipeline synthetic tests failed' }
& $Python -m unittest discover -s verify -p 'test_*.py'
if ($LASTEXITCODE -ne 0) { throw 'Verifier synthetic tests failed' }
```

Expected: successful exits, no failed tests, and no CRM browser login. These checks use synthetic temporary evidence. The smoke check validates adapter contracts, not the current live site's permission/pagination behavior. Do not describe a passing smoke check as a completed customer capture.

### 6. Review the adapter before using a real account

The included adapter is [`app/adapters/okki/v1/adapter.json`](../app/adapters/okki/v1/adapter.json). Review:

1. `origin`, `customer_path`, and read endpoint contracts.
2. Root tabs, visible labels, and next-page labels.
3. The allowed asset host list and external AI blocked-host list.
4. Pagination safety caps and count paths.
5. The current application's permissions and whether the record is actually accessible.

[`config/scope.json`](../config/scope.json) is a generic example for the lower-level capture utilities. Its sample record ID is fictional. Editing it does not change the one-shot record ID passed to the app.

Do not “fix” a schema failure by removing count/hash/identity checks. Make a new adapter version, adjust synthetic fixtures, and verify the new live behavior on your own authorized scope. Other CRM products require code and contracts, not just a URL replacement.

### 7. Start a single-record collector with a fresh login

One-shot avoids shared queue/deferred processing and automatic next-customer navigation. Do not start an ordinary queue instance while learning the single-record workflow.

Before starting, check the two local ports:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 3311,9434 -ErrorAction SilentlyContinue |
    Select-Object LocalAddress,LocalPort,OwningProcess
```

If occupied, identify the owner. Do not kill every Electron/Node/Chrome process. Close only the collector instance you intentionally started, or wait until it exits.

The following is example syntax. Replace the fictional ID before execution:

```powershell
Set-Location $RepoRoot
.\app\scripts\capture-one.ps1 `
    -CompanyId '123456789' `
    -NoAutoNext `
    -NoProfileClone
```

The local collector window opens. If not authenticated:

1. Log in normally inside that window. Complete any required MFA yourself.
2. Navigate to or confirm the bound record.
3. Check the displayed identity against the intended record ID.
4. Press Start **once**. If Start is already running, wait; repeated clicks return a busy response.

A healthy `/healthz` response only proves the local server is alive; the launcher also waits for the desktop-ready marker. A login-required result is not a completed capture.

Record the case path displayed by the collector. Runtime lives below `app/runtime/`; generated cases/evidence are private. Use the actual returned path, not a directory guessed from a display name. Never push the checkout after it has accumulated private data without a fresh source-only export.

The control server and CDP endpoints are loopback-only. A CDP connection can read/control an authenticated browser. Do not expose them with a LAN listener, tunnel, or “temporary” public proxy.

### 8. Confirm capture evidence before processing

Inspect the local case's metadata and reconciliation, not just the green UI indicator:

- The case identity must match the selected record.
- The completed session's processing scope must bind to existing object hashes.
- Pagination and record totals must reconcile independently for the captured modules.
- Downloads must have local object receipts, not only a discovered URL.
- Deleted, expired, forbidden, and blocked objects must remain explicit source gaps.
- Manual trade evidence is a separate channel; an automatic seven-tab pass does not prove trade coverage. The current full extractor nevertheless requires its reviewed manifest before preparation. Complete [the manual-trade prerequisite](manual-trade.md); the automatic collector does not generate it.

Keep these local files private. If a module is blocked or schema-drifted, repair the adapter or retain a gap; do not manually edit the verification status to PASS.

### 9. Install optional tools to match the input formats

Only install what your input needs. Put executables and language/model files in a local tool directory such as `C:\Tools`. Tool/model downloads are separate from the source release.

| Input or output | Tool | Detection/configuration | Sanity check |
| --- | --- | --- | --- |
| Image OCR | [Tesseract](https://github.com/tesseract-ocr/tesseract) | `tesseract` on PATH or `--tesseract`; language directory via `--tessdata-dir` | `tesseract --version`; `tesseract --list-langs` |
| PDF page OCR | [Poppler](https://poppler.freedesktop.org/) plus Tesseract | `pdftoppm` on PATH or `--pdftoppm` | `pdftoppm -v` |
| EPS/PostScript OCR | [Ghostscript](https://www.ghostscript.com/) plus Tesseract | `gswin64c`/`gswin32c`/`gs` or `--ghostscript` | `gswin64c --version` |
| ZIP/RAR recursion | [7-Zip](https://www.7-zip.org/) for non-ZIP formats | `7z`/`7zz` or standard Windows install path | `7z i` |
| Media probing/frames | [FFmpeg](https://ffmpeg.org/download.html) | `ffmpeg`, `ffprobe` on PATH or flags | `ffmpeg -version`; `ffprobe -version` |
| Legacy DOC/XLS/PPT | Local Office COM, optional LibreOffice fallback | Read-only conversion scripts; no macros/links intentionally enabled | Try a synthetic file in a private temporary output folder. |
| DOCX → PDF | Local Microsoft Word | Word COM export scripts | Open the output read-only and inspect rendered pages. |
| Audio transcription | Your compatible local ASR driver | `--asr-script`, `--asr-python` | First run its own synthetic audio test. |

The upstream Tesseract/FFmpeg pages describe source and distribution options; do not assume an arbitrary third-party executable is an official binary. Keep tool licenses and downloaded-file checksums locally.

For Chinese + English OCR, install both `chi_sim.traineddata` and `eng.traineddata`. For English-only sources, `--ocr-languages eng` avoids requiring Chinese data. Check the **same executable and language directory** used by the pipeline:

```powershell
$Tesseract = 'C:\Tools\Tesseract\tesseract.exe'
$Tessdata = 'C:\Tools\Tesseract\tessdata'
& $Tesseract --version
& $Tesseract --tessdata-dir $Tessdata --list-langs
```

Native Windows tools can have non-ASCII path limitations. Prefer ASCII tool/data paths. The extractor stages configured language files locally, but this does not make every native executable Unicode-safe.

Configuration is deliberately explicit. These names configure different layers; they are not interchangeable:

| Layer | Setting | Meaning |
| --- | --- | --- |
| Direct extractor | `--ffmpeg`, `--ffprobe`, `--pdftoppm`, `--ghostscript`, `--tesseract` | Exact local executable path; otherwise supported PATH detection is used. |
| Direct extractor | `--tessdata-dir` / `TESSDATA_PREFIX`; `--ocr-languages` | Local language data and language list; default `chi_sim+eng`. |
| Direct extractor | `--asr-script` / `CRM_ASR_SCRIPT`; `--asr-python` | Compatible local driver and its interpreter. |
| Legacy conversion | `CRM_LIBREOFFICE`, `CRM_ANTIWORD` | Optional local conversion/native-text tools. |
| DuckDB export | `CRM_DUCKDB_THREADS`, `CRM_DUCKDB_MEMORY_LIMIT` | Default bounded CPU threads and `4GB`; memory is an integer `MB`/`GB` value. |
| DuckDB interpreter | `--duckdb-python` / `OKKI_DUCKDB_PYTHON` | Optional explicit local interpreter; defaults to the current environment. The `OKKI_` name is retained compatibility. |
| Completion wrapper | `-Ffmpeg`, `-Ffprobe`, `-Pdftoppm`, `-Ghostscript`, `-Tesseract` | Wrapper equivalents; defaults can use `EVIDENCE_FFMPEG`, `EVIDENCE_FFPROBE`, `EVIDENCE_PDFTOPPM`, `EVIDENCE_GHOSTSCRIPT`, `EVIDENCE_TESSERACT`. |
| Completion wrapper | `-Tessdata`, `-AsrScript`, `-AsrPython` | Defaults from `TESSDATA_PREFIX`, `EVIDENCE_ASR_SCRIPT`, `EVIDENCE_ASR_PYTHON`. |
| App (advanced) | `OKKI_INSTANCE_COUNT` | Planned shared instance budget, 1–8. Start with one; more instances do not license more site requests. |

Set only process-scoped environment variables in your own terminal when needed. Do not edit a machine proxy, DNS, route, or system-wide credential setting as an installation step. Prefer explicit CLI paths for repeatable runs.

### 10. Configure and run local extraction

The following path/ID are fictional. Replace them with the case path returned by your own capture and its exact ID. Do not execute against an empty folder.

Before executing, confirm the selected `evidence/manual_trade/<session>/trade_manual_capture_manifest.json` follows [the required schema, file/hash/count and coverage checklist](manual-trade.md). A valid automatic scope alone is insufficient: all three extraction commands load this manual manifest. If you have not implemented/reviewed the manual channel, stop at automatic capture rather than fabricate a PASS file. A matching receipt is additionally required for initial combined delivery, not for extraction preparation itself.

```powershell
$CaseRoot = 'C:\EvidenceData\company_123456789'
$CompanyId = '123456789'

& $Python pipeline\single_customer_full_extract.py prepare `
    --case-root $CaseRoot --company-id $CompanyId `
    --archive-total-bytes 4294967296 --archive-max-members 10000
if ($LASTEXITCODE -ne 0) { throw 'Scope preparation failed' }

& $Python pipeline\single_customer_full_extract.py run `
    --case-root $CaseRoot --company-id $CompanyId `
    --parse-workers 4 --ocr-workers 2 `
    --tesseract 'C:\Tools\Tesseract\tesseract.exe' `
    --tessdata-dir 'C:\Tools\Tesseract\tessdata' `
    --ocr-languages 'chi_sim+eng' `
    --pdftoppm 'C:\Tools\Poppler\bin\pdftoppm.exe' `
    --ghostscript 'C:\Tools\Ghostscript\bin\gswin64c.exe' `
    --ffmpeg 'C:\Tools\FFmpeg\bin\ffmpeg.exe' `
    --ffprobe 'C:\Tools\FFmpeg\bin\ffprobe.exe'
```

Pass only installed paths. If your scope contains audio/video transcription tasks, also pass a compatible `--asr-script` and its environment's `--asr-python`; see [the ASR contract](processing.md#local-asr-integration-contract). Missing ASR is an explicit integration error, not a silent cloud fallback.

For a native-parsing-only first pass:

```powershell
& $Python pipeline\single_customer_full_extract.py run `
    --case-root $CaseRoot --company-id $CompanyId `
    --parse-workers 4 --ocr-workers 2 --skip-models
```

`--skip-models` leaves OCR/ASR/frame-OCR work unfinished. It is **not** a way to claim full extraction. Re-run without it once the missing tools are configured. Native media probing may still require FFprobe.

After the actual full run:

```powershell
& $Python pipeline\single_customer_full_extract.py verify `
    --case-root $CaseRoot --company-id $CompanyId
```

Review counts and states, then continue with [relations, timelines, and delivery](processing.md). Do not proceed to “final” merely because the process returned no uncaught exception.

### 11. Resume safely and tune only after measuring

- A case has one extraction writer. If you see `CASE_EXTRACTION_LOCK_BUSY`, inspect the owning process; do not delete the lock to force a second writer.
- Re-run `run` against the same prepared scope to recover unfinished work. OS lock ownership is released on process death.
- A changed source manifest/hash is a new scope. `PREPARED_SCOPE_BINDING_MISMATCH` must not be bypassed.
- Start with 4 parse workers and 2 OCR workers, then measure completed tasks, memory, I/O, and source error rates. Increase CPU work without increasing website load blindly.
- Office COM and GPU ASR remain serial integration tails; more CPU workers do not remove them.
- The default archive cap is cumulative across recursive containers, including duplicate bytes inspected. Raise it only with a measured disk/time budget.
- Make consistent SQLite backups while data is live; copying only a WAL database's main file is not necessarily a usable backup.

The advanced completion wrappers have explicit `-CaseRoot` / `-CompanyId` bindings. Start with their dry-run/status mode, not a historical folder from someone else's machine. See [processing](processing.md) and [pitfalls](troubleshooting.md).

---

## 中文

### 0. 先选择自己的停止点

共有三个不同阶段，不能因为后面有脚本就跳过前面：

1. **源码检查**：安装依赖、编译、运行临时合成测试，不需要 CRM 登录。
2. **自己的采集**：手动登录，绑定一条授权记录，采集证据并完成数量对账。
3. **本地提取/交付**：按文件类型配置可选工具，处理、验证，再制作并检查导出物。

仓库不是所有 OCR/ASR 引擎的一键全集。包含的是调用与校验流程；程序、模型、Office 和兼容 ASR 驱动需要自己安装。先完成第一阶段。

### 1. 安装并确认基础工具

使用普通 Windows 交互账号，源码检查本身不要求管理员。安装来源：

- [Node.js 下载](https://nodejs.org/en/download)：文档基线为 Node 24，应用要求 24 或以上。
- [uv 安装](https://docs.astral.sh/uv/getting-started/installation/)：推荐用较新的托管 Python 3.12；Windows 有 WinGet 时，上游提供 `winget install --id astral-sh.uv -e`。
- [Python Windows 下载](https://www.python.org/downloads/windows/)：也可使用已有可信 Python 3.12+，但**必须通过 SQLite 预检**，某些安装版自带旧 SQLite。
- [Git for Windows](https://gitforwindows.org/)：仅克隆与贡献需要。

安装后重新打开 PowerShell，让 PATH 更新生效。克隆前先确认：

```powershell
node --version
npm --version
uv --version
git --version
```

预期：Node 主版本至少 24，uv/npm/Git 能返回版本，而非“找不到命令”。已有合格 Python 可不装 uv，使用下面替代路径。安装后都要重新开终端。

无需 `npx playwright install`：应用使用 Playwright **Core**，Electron 提供内嵌浏览器。只有外部浏览器路径才需要 Chrome/Edge。

### 2. 建立独立源码副本

选择可写、未同步的本地目录，不要克隆覆盖正在运行的采集器或证据案例。ASCII 路径能减少 OCR/Office 原生工具的问题。

```powershell
$ProjectParent = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'EvidenceTrail\projects'
New-Item -ItemType Directory -Path $ProjectParent -Force | Out-Null
Set-Location $ProjectParent
git clone https://github.com/KietC/crm-evidence-workbench.git
Set-Location '.\crm-evidence-workbench'
$RepoRoot = $PWD.Path
```

示例使用自己的可写用户目录，不要求驱动器根目录写权限。后续原生工具若必须用 ASCII 路径，应另选自己已拥有权限的 ASCII 目录。

ZIP 用户：解压到新目录，进入该目录，再设 `$RepoRoot = $PWD.Path`。确认目录中有 `app\package.json`、`requirements-local.txt`、`cli.py`。

要求仓库根目录的命令，不要在 `app` 中执行。文档只有应用命令才明确使用 `Push-Location app`。

### 3. 建立独立 Python 环境

采用 [托管 Python 流程](https://docs.astral.sh/uv/guides/install-python/)，让项目解释器独立于旧系统安装：

```powershell
uv python install 3.12
if ($LASTEXITCODE -ne 0) { throw '托管 Python 下载失败' }
uv venv --managed-python --python 3.12 .venv
if ($LASTEXITCODE -ne 0) { throw 'Python 环境创建失败' }
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
uv pip install --python $Python --require-hashes -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败' }
uv pip check --python $Python
if ($LASTEXITCODE -ne 0) { throw 'Python 依赖冲突' }
& $Python scripts\sqlite_preflight.py
if ($LASTEXITCODE -ne 0) { throw '选定 Python 的 SQLite 不合格，不能继续' }
```

无需激活，因为命令显式使用 `$Python`，这样不改系统 Python，也不会遇到 `Activate.ps1` 执行策略问题。

`requirements-local.txt` 声明直接依赖，`requirements-lock.txt` 带哈希锁定直接/传递依赖。二者都不是 ASR/模型环境。正式运行应在本机记录依赖版本；不能公开含内网包地址或凭据的 freeze。

已有可信 Python 的替代路径：

```powershell
py -3.12 -m venv .venv
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
& $Python -m pip install --require-hashes -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw '锁定依赖安装失败' }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Python 依赖冲突' }
& $Python scripts\sqlite_preflight.py
if ($LASTEXITCODE -ne 0) { throw 'SQLite 预检失败，请换已修补 Python 发行版' }
```

预检会真的检查临时数据库能力，以及 [SQLite WAL-reset 修复](https://sqlite.org/wal.html#walresetbug)：3.51.3+ 或支持的 3.44.6+/3.50.7+ 回补分支。Python 版本新或运行 pip，不代表自带 SQLite 自动升级。不能随便换活 Python 的 DLL 强行变绿，应新建合格环境。

### 4. 安装两套 npm 依赖

根目录依赖负责 Excel/目录构建，应用目录依赖负责 Electron/TypeScript，装一套不会自动装另一套。

```powershell
Set-Location $RepoRoot
npm ci
if ($LASTEXITCODE -ne 0) { throw '根目录 npm 安装失败' }

Push-Location app
try {
    npm ci
    if ($LASTEXITCODE -ne 0) { throw '应用 npm 安装失败' }
} finally {
    Pop-Location
}
```

`npm ci` 使用提交的锁文件，并重建当前副本的 `node_modules`。不要把它做成指向别的项目的链接后再安装。Electron 安装会下载浏览器程序，npm 包成功不等于 Electron 程序也已经下载成功。

PowerShell 阻止 `npm.ps1` 时，用 `npm.cmd` 执行同一命令，不要修改整机执行策略。必要时只对一次进程调用设置：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\check.ps1 -Python $Python
```

### 5. 登录前先通过合成检查

```powershell
Set-Location $RepoRoot
.\scripts\check.ps1 -Python $Python
```

定位具体阶段时：

```powershell
Push-Location app
try {
    npm run check
    if ($LASTEXITCODE -ne 0) { throw 'TypeScript 检查失败' }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw 'TypeScript 构建失败' }
    npm test
    if ($LASTEXITCODE -ne 0) { throw '应用合成测试失败' }
    npm run smoke
    if ($LASTEXITCODE -ne 0) { throw '适配器 smoke 失败' }
} finally {
    Pop-Location
}

& $Python -m unittest discover -s pipeline -p 'test_*.py'
if ($LASTEXITCODE -ne 0) { throw '流水线合成测试失败' }
& $Python -m unittest discover -s verify -p 'test_*.py'
if ($LASTEXITCODE -ne 0) { throw '验证器合成测试失败' }
```

预期：成功退出、测试无失败、不需要 CRM 登录。检查只使用临时合成证据。smoke 验证的是适配器契约，不是当前网站权限与分页；通过不等于客户采集完成。

### 6. 使用真实账号前核对适配器

所带适配器为 [`app/adapters/okki/v1/adapter.json`](../app/adapters/okki/v1/adapter.json)，依次核对：

1. `origin`、`customer_path` 与读取接口契约。
2. 根标签、可见文字和下一页文字。
3. 允许的资源域及外部 AI 阻止域。
4. 分页安全上限与数量字段路径。
5. 当前账号权限及记录是否真正可读。

[`config/scope.json`](../config/scope.json) 是底层工具的通用示例，里面的 ID 是虚构的。修改它，不会改变 app one-shot 参数绑定的 ID。

不能为“修复”结构错误而删掉数量、哈希、身份门槛。应建立新适配器版本，修改合成样本，并在自己的授权范围验证实际行为。换 CRM 要改代码和契约，不是只换 URL。

### 7. 用全新登录启动单记录采集器

one-shot 不参与共享队列/延后任务，也不会自动跳下一客户。学习单记录流程时不要同时启动普通队列实例。

先检查两个端口：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 3311,9434 -ErrorAction SilentlyContinue |
    Select-Object LocalAddress,LocalPort,OwningProcess
```

占用时先识别所属进程，不要批量杀 Electron/Node/Chrome。只关闭自己明确启动的那个采集器，或等待它退出。

以下只是格式示例，执行前替换虚构 ID：

```powershell
Set-Location $RepoRoot
.\app\scripts\capture-one.ps1 `
    -CompanyId '123456789' `
    -NoAutoNext `
    -NoProfileClone
```

本地采集窗口打开后，若尚未认证：

1. 在该窗口正常登录，需要 MFA 时自己完成。
2. 导航到或确认绑定记录。
3. 核对显示身份与目标 ID。
4. 只按 **一次** Start。正在运行就等，重复启动会返回 busy。

`/healthz` 正常仅证明本地服务活着；启动器还会等待桌面 ready 标记。要求登录不等于采集完成。

记录采集器显示的案例路径。运行配置在 `app/runtime/` 下，案例/证据始终私密。用实际返回路径，不要按显示公司名猜目录。副本产生真实数据后，未重新做源码白名单导出就不要 push。

控制服务与 CDP 只应监听 loopback。CDP 能读取/操作已登录浏览器，不能公开监听、开隧道或加“临时”公开代理。

### 8. 后处理前确认采集证据

应核对本地案例元数据与对账，不能只看界面绿灯：

- 案例身份与所选记录一致。
- 已完成会话的处理范围绑定现存对象哈希。
- 每个模块分别完成分页与记录总数对账。
- 下载有本地对象回执，不只是发现一个 URL。
- 删除、失效、无权限和阻断对象明确保留源缺口。
- 手动贸易证据是独立通道，自动七标签通过不能证明贸易完整。但当前完整提取器在准备前仍强制要求其审查后清单。先完成 [手动贸易前置条件](manual-trade.md)，自动采集器不会生成该清单。

这些本地文件不能公开。模块阻断或结构变化应修适配器或保留缺口，不能手改验证状态成 PASS。

### 9. 按输入类型配置可选工具

只装输入需要的工具。程序、语言包和模型可放在 `C:\Tools` 等本地工具目录；它们不是仓库源码的一部分。

| 输入或输出 | 工具 | 检测/配置 | 检查命令 |
| --- | --- | --- | --- |
| 图片 OCR | [Tesseract](https://github.com/tesseract-ocr/tesseract) | PATH 中 `tesseract` 或 `--tesseract`；语言目录 `--tessdata-dir` | `tesseract --version`；`tesseract --list-langs` |
| PDF 逐页 OCR | [Poppler](https://poppler.freedesktop.org/) + Tesseract | PATH 中 `pdftoppm` 或 `--pdftoppm` | `pdftoppm -v` |
| EPS/PostScript OCR | [Ghostscript](https://www.ghostscript.com/) + Tesseract | `gswin64c`/`gswin32c`/`gs` 或 `--ghostscript` | `gswin64c --version` |
| ZIP/RAR 递归 | 非 ZIP 格式使用 [7-Zip](https://www.7-zip.org/) | `7z`/`7zz` 或 Windows 标准安装路径 | `7z i` |
| 媒体探测/画面 | [FFmpeg](https://ffmpeg.org/download.html) | PATH 中 `ffmpeg`、`ffprobe` 或明确参数 | `ffmpeg -version`；`ffprobe -version` |
| 旧 DOC/XLS/PPT | 本地 Office COM，可选 LibreOffice 兜底 | 只读转换；不主动启用宏/链接 | 先用合成文件和私密临时输出目录测试。 |
| DOCX → PDF | 本地 Microsoft Word | Word COM 导出脚本 | 只读打开输出并检查渲染页。 |
| 音频转录 | 自己的兼容本地 ASR 驱动 | `--asr-script`、`--asr-python` | 先跑驱动自己的合成音频测试。 |

Tesseract/FFmpeg 上游页说明源码与分发来源；不能把随便找到的第三方程序当官方二进制。在本机保留工具许可证和下载哈希。

中英 OCR 需要 `chi_sim.traineddata` 和 `eng.traineddata`。纯英文输入用 `--ocr-languages eng`，不强求中文包。检查必须使用与流水线相同的程序和语言目录：

```powershell
$Tesseract = 'C:\Tools\Tesseract\tesseract.exe'
$Tessdata = 'C:\Tools\Tesseract\tessdata'
& $Tesseract --version
& $Tesseract --tessdata-dir $Tessdata --list-langs
```

某些 Windows 原生工具有非 ASCII 路径限制。优先使用 ASCII 工具/语言目录。提取器会本地暂存已配置语言包，但不能让所有原生程序自动兼容 Unicode。

配置刻意要求明确，下面不同层的名称不能混用：

| 层 | 设置 | 含义 |
| --- | --- | --- |
| 直接提取器 | `--ffmpeg`、`--ffprobe`、`--pdftoppm`、`--ghostscript`、`--tesseract` | 准确本地程序路径，否则按支持方式检查 PATH。 |
| 直接提取器 | `--tessdata-dir` / `TESSDATA_PREFIX`；`--ocr-languages` | 本地语言包及语言列表，默认 `chi_sim+eng`。 |
| 直接提取器 | `--asr-script` / `CRM_ASR_SCRIPT`；`--asr-python` | 兼容本地驱动及其解释器。 |
| 旧文档转换 | `CRM_LIBREOFFICE`、`CRM_ANTIWORD` | 可选本地转换/原生文字工具。 |
| DuckDB 导出 | `CRM_DUCKDB_THREADS`、`CRM_DUCKDB_MEMORY_LIMIT` | 默认有界 CPU 线程与 `4GB`；内存为整数 `MB`/`GB`。 |
| DuckDB 解释器 | `--duckdb-python` / `OKKI_DUCKDB_PYTHON` | 可明确本地解释器，默认当前环境；`OKKI_` 为兼容保留。 |
| 补全 wrapper | `-Ffmpeg`、`-Ffprobe`、`-Pdftoppm`、`-Ghostscript`、`-Tesseract` | wrapper 对应参数；默认可用 `EVIDENCE_FFMPEG`、`EVIDENCE_FFPROBE`、`EVIDENCE_PDFTOPPM`、`EVIDENCE_GHOSTSCRIPT`、`EVIDENCE_TESSERACT`。 |
| 补全 wrapper | `-Tessdata`、`-AsrScript`、`-AsrPython` | 默认来自 `TESSDATA_PREFIX`、`EVIDENCE_ASR_SCRIPT`、`EVIDENCE_ASR_PYTHON`。 |
| 应用（高级） | `OKKI_INSTANCE_COUNT` | 计划共用实例预算，1–8，先用 1；更多实例不代表能加网站请求。 |

需要时只在自己的终端设进程环境变量。安装不需要改整机代理、DNS、路由或系统凭据。可复现运行优先显式 CLI 路径。

### 10. 配置并执行本地提取

以下路径和 ID 是虚构的。替换为自己采集器返回的案例路径及准确 ID，不能对空目录运行。

执行前确认所选 `evidence/manual_trade/<session>/trade_manual_capture_manifest.json` 符合 [结构、文件/哈希/数量及覆盖检查](manual-trade.md)。仅自动范围有效并不足够，三个提取命令都会加载手动清单。未实现并审查手动通道时，应停在自动采集阶段，不能伪造 PASS 文件。初始联合交付另外需要匹配回执；提取准备本身不要求回执。

```powershell
$CaseRoot = 'C:\EvidenceData\company_123456789'
$CompanyId = '123456789'

& $Python pipeline\single_customer_full_extract.py prepare `
    --case-root $CaseRoot --company-id $CompanyId `
    --archive-total-bytes 4294967296 --archive-max-members 10000
if ($LASTEXITCODE -ne 0) { throw '范围准备失败' }

& $Python pipeline\single_customer_full_extract.py run `
    --case-root $CaseRoot --company-id $CompanyId `
    --parse-workers 4 --ocr-workers 2 `
    --tesseract 'C:\Tools\Tesseract\tesseract.exe' `
    --tessdata-dir 'C:\Tools\Tesseract\tessdata' `
    --ocr-languages 'chi_sim+eng' `
    --pdftoppm 'C:\Tools\Poppler\bin\pdftoppm.exe' `
    --ghostscript 'C:\Tools\Ghostscript\bin\gswin64c.exe' `
    --ffmpeg 'C:\Tools\FFmpeg\bin\ffmpeg.exe' `
    --ffprobe 'C:\Tools\FFmpeg\bin\ffprobe.exe'
```

只传确实安装的路径。有音视频转录任务时，还要提供兼容 `--asr-script` 及其环境的 `--asr-python`，见 [ASR 接口契约](processing.md#本地-asr-集成契约)。缺少驱动会明确报错，不会偷偷转云端。

先仅做原生解析：

```powershell
& $Python pipeline\single_customer_full_extract.py run `
    --case-root $CaseRoot --company-id $CompanyId `
    --parse-workers 4 --ocr-workers 2 --skip-models
```

`--skip-models` 会留下 OCR/ASR/视频画面 OCR 未完成，**不能**拿来声称完整提取。工具齐备后去掉参数重新跑。原生媒体探测仍可能需要 FFprobe。

实际完整运行后：

```powershell
& $Python pipeline\single_customer_full_extract.py verify `
    --case-root $CaseRoot --company-id $CompanyId
```

看完数量与终态后再继续 [关系、时间线和交付](processing.md)，不能因为进程没有未捕获异常就称最终完成。

### 11. 安全续跑，实测后才扩并发

- 一个案例只能有一个提取写进程。出现 `CASE_EXTRACTION_LOCK_BUSY` 时检查持有进程，不能删锁强开第二个。
- 同一准备范围重新执行 `run` 即续跑；进程死亡后 OS 锁自动释放。
- 来源清单/哈希改变就是新范围；不能绕过 `PREPARED_SCOPE_BINDING_MISMATCH`。
- 从解析 4、OCR 2 开始，看完成任务、内存、I/O 和源错误率后增加；不要盲目把 CPU 并发变成网站压力。
- Office COM、GPU ASR 仍是串行尾段，更多 CPU 不能消除它们。
- 压缩限额为全部递归容器累计预算，重复读取字节也计入；扩大前先测磁盘/时间预算。
- 运行中的 SQLite 应使用一致性备份；只复制 WAL 数据库主文件可能不可恢复。

高级补全脚本显式绑定 `-CaseRoot` / `-CompanyId`。先用 dry-run/status，不要照搬别人机器里的历史目录。见 [处理流程](processing.md) 和 [避坑](troubleshooting.md)。
