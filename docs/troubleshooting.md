# Pitfalls and Recovery / 避坑与恢复

[Setup](setup.md) · [Processing](processing.md) · [Security](../SECURITY.md)

## English

### Read the correct evidence first

Check the exact failing command, its exit code, selected interpreter, active case binding, and sanitized error code. A process existing, a port listening, a green source test, and a finished customer capture are four different facts.

Keep raw case logs local: native tools can include filenames or text in them. For a public bug report, reproduce with a synthetic input and include only the sanitized diagnostic.

### Installation and startup

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| `node` / `npm` is not recognized | PATH not refreshed or wrong terminal | Reopen PowerShell; run `Get-Command node,npm`; confirm Node 24+. |
| PowerShell refuses `npm.ps1` | Script execution policy | Use `npm.cmd`, or a one-command `powershell.exe -ExecutionPolicy Bypass`. Do not change machine policy. |
| `py -3.12` missing | Launcher absent or no 3.12 installation | Use the actual trusted Python executable and create a new venv from it. Do not mix pip from a different interpreter. |
| `ModuleNotFoundError: pdfplumber` | Python requirements were installed into another environment | Use `& $Python -m pip install -r requirements-local.txt`; run `& $Python -m pip check`. |
| ExcelJS/Sharp or a workbook helper cannot import | Only app dependencies installed | Run `npm ci` at the root too. Keep `pipeline/lib` with its builders. |
| Electron executable is missing after npm | Browser binary download failed | Inspect the install exit/error and permitted network path; rerun app `npm ci` in this independent checkout. Do not copy a private browser profile as a fix. |
| Source install complains about a workflow index | Optional integration mistaken for a core dependency | The index tool is opt-in; its absence should not block app compilation. Do not install private neighboring tools. |
| Control port 3311 is occupied | Another one-shot instance or unrelated listener | Identify the listening PID; close only the instance you own. Do not terminate every Node/Electron process. |
| Local server healthy but desktop not ready / `ERR_ABORTED` | Capture request sent during initial browser navigation | Use `capture-one.ps1`, which waits for desktop readiness. Do not race a raw Start request against Electron startup. |
| Login page remains visible | Fresh isolated session or expired authentication | Complete login/MFA manually in the collector, confirm the bound record, then press Start once. Login is not automated by a bundled credential. |

### Capture integrity and concurrency

| Symptom | Meaning | Correct response |
| --- | --- | --- |
| Duplicate Start returns HTTP 409/busy | Another initialization/capture owns the startup lease | Wait for its terminal state. Busy rejection is protection, not a reason to restart repeatedly. |
| Recovery marker points to a different record than the current page | Identity mismatch after navigation/crash | The engine must re-open/reverify the marker's record before saving. Do not suppress the identity check. |
| A long job's reservation looks old | Age is not proof of a dead owner | Check liveness/ownership. Live owners do not expire simply after a fixed number of hours. |
| Lock helper startup/timeout fails | Exclusive ownership was not obtained | Keep the run stopped; restore its documented OS helper. Do not downgrade to a directory-only lock. |
| Selected root tabs visited but counts disagree | Missing pages, permissions, schema drift, or wrong count field | Inspect endpoint/page reconciliation; patch the adapter with a synthetic regression. Visiting a tab is not full recursive coverage. |
| Discovered attachment URL has no local receipt | URL discovery is not successful preservation | Retry only the authorized download path or record a source gap. Never count an unmaterialized URL as a saved file. |
| Trade pages absent from AUTO7 results | Trade is intentionally a separate manual channel | Use a reviewed single-thread/manual procedure. Combined full extraction requires its manifest even after AUTO7 passes; see [the manual contract](manual-trade.md). Do not replay restricted trade APIs. |
| Signed URL metadata changed | Audit credential redaction | Actual requests use the original allowed URL; audit metadata hides signatures/tokens. Check original bytes, not the displayed redacted URL, for object identity. |

### Local extraction

| Error / symptom | Cause or distinction | Recovery |
| --- | --- | --- |
| `CASE_IDENTITY_MISSING` / `CASE_IDENTITY_MISMATCH` | Empty/wrong case or mismatched ID | Use the collector's actual case path and exact matching ID. Do not fabricate identity metadata. |
| `MANUAL_TRADE_MANIFEST_MISSING` | Full extractor requires manual-trade evidence; AUTO7 alone does not supply it | Complete the [reviewed manual channel and manifest](manual-trade.md) or retain an automatic-only/incomplete stage. Do not create empty PASS fixtures or remove validation. |
| `MANUAL_TRADE_MANIFEST_NOT_PASS` / `MANUAL_TRADE_STATUS_GAP_CONTRADICTION` | Wrong schema/status or inconsistent gap declaration | Review real coverage and gaps, then generate a matching successor manifest; do not edit status merely to unblock. |
| `MANUAL_TRADE_RECEIPT_MISSING` / manifest SHA mismatch | Initial combined delivery lacks a matching reviewed receipt or manifest changed | Reconcile the manual scope and produce a new hash-bound receipt; retain the prior evidence. Preparation and combined delivery have different prerequisites. |
| `CASE_EXTRACTION_LOCK_BUSY` | Another preparation/run holds the case writer lock | Wait or stop only your known owner gracefully; then rerun. Deleting a lock file does not safely transfer ownership. |
| `PREPARED_SCOPE_BINDING_MISMATCH` / scope hash mismatch | Source or current session changed after preparation | Preserve the old result, review the new scope, and prepare a successor. Do not edit DB bindings by hand. |
| `ARCHIVE_TOTAL_BYTE_LIMIT` / `ARCHIVE_TOTAL_MEMBER_LIMIT` | Cumulative recursion budget reached | Review scope/disk budget; explicitly raise `prepare` limits only if appropriate. Defaults are 4 GiB/10,000 entries across containers, not per archive. |
| Password-required PDF/archive | Encrypted source, no authorized password available | Retain source bytes and an explicit password gap. No brute force. |
| `OCR_LANGUAGE_PACK_MISSING` | Chosen language data missing from the selected Tesseract pack | Check the same executable's `--list-langs` with the same `--tessdata-dir`; install needed traineddata or use an appropriate language list. |
| OCR tool works from one terminal but not pipeline | Different PATH/executable/data path | Pass explicit `--tesseract`, `--tessdata-dir`, `--pdftoppm` etc. Prefer ASCII native-tool paths. |
| Native PDF text is empty | Scanned/image-only PDF | Native parsing alone is insufficient; configure page rendering + OCR. Empty text is not missing bytes. |
| Legacy Office conversion hangs or prompts | Damaged/password file or COM modal dialog | Run in a clean local session, preserve source, inspect the timeout receipt, then use the labeled local fallback if available. Never kill an unrelated open Office document. |
| `LOCAL_ASR_PIPELINE_NOT_FOUND` | No compatible local driver configured | Supply `--asr-script` / `CRM_ASR_SCRIPT` and `--asr-python`; model weights and the driver are not bundled. |
| Audio stream exists but no speech | Can be a valid silent/no-voice clip | Require independent no-speech evidence or a driver failure state; do not write a fabricated sentence to make it look complete. |
| `--skip-models` run finished but verify incomplete | OCR/ASR tasks intentionally remained pending | Configure tools and rerun without the flag. Do not change pending to completed. |
| Unknown format marked opaque | Not meaningfully parsed by available tools | Preserve original and opaque status; add a new parser with synthetic fixtures if needed. |

### Relations, time, and delivery

| Symptom | What went wrong | Required fix |
| --- | --- | --- |
| Relation points to a pointer in another artifact | Provenance mismatch | Align artifact/record/source path/JSON pointer to the actual containing record; verify the pointed value. |
| Same attachment loses all but one parent | Content dedup was confused with occurrence dedup | Keep one SHA object and all distinct mail → attachment occurrences/edges. |
| Timeline sorted by filename | Source-path dictionary order substituted for time | Rebuild business and capture axes from original time fields and source sequence, with zone/precision/unknown states. |
| Reply moved across business timestamps | Parent nesting used as chronology | Keep the parent edge but restore time order. Uncertain time remains explicit. |
| Required `--expected-*` arguments appear | Release removed installation-specific baseline constants | Read your own current inventory and supply all relevant counts. Do not copy someone else's numbers or use zero to disable gates. |
| `FINAL_DELIVERY_NOT_FOUND` / authority files missing | Existing-baseline migration invoked on a fresh case | The completion-v2 freeze requires a compatible verified v1 delivery. Follow the correct stage; do not manufacture a `final_*` folder. |
| SQLite integrity/features/version gate fails | Runtime mismatch or incomplete database support | Check `sqlite3.sqlite_version`, FTS5, foreign keys, and supported WAL behavior in the selected interpreter. Use the documented patched runtime; do not remove the gate. |
| DuckDB export runs out of memory | Export parallelism exceeds the local budget | Use `CRM_DUCKDB_THREADS` and `CRM_DUCKDB_MEMORY_LIMIT`; start smaller and measure. |
| Workbook preview looks good but Excel differs | Preview and native Office are different renderers | The open-source PNG is an SVG/Sharp layout preview. Completion-v2 requires actual Microsoft Excel read-only opening and every-sheet human inspection, not just the preview. |
| `WORKBOOK_NATIVE_QA_MISSING` / `WORKBOOK_NATIVE_QA_NOT_PASS` | Independent native/human receipt is absent or not passed | Perform real Excel read-only opening and all-sheet/formula inspection, then record `qa/workbook_native_qa.json` using [the native QA contract](processing.md#independent-native-workbook-qa-is-mandatory). Keep the structural receipt unchanged. No auto-PASS template is valid. |
| `WORKBOOK_NATIVE_OPEN_NOT_PASS` / `WORKBOOK_HUMAN_VISUAL_NOT_PASS` / `WORKBOOK_NATIVE_FORMULA_ERRORS` | Read-only/no-repair, visual, or formula check did not pass | Repair the artifact through a reviewed successor, then repeat native QA on its final bytes. Do not flip booleans to make finalization pass. |
| `WORKBOOK_NATIVE_HASH_MISMATCH` / `WORKBOOK_NATIVE_RECEIPT_HASH_MISMATCH` | Workbook or structural receipt changed after native QA | Preserve old QA; check the current files and redo the inspection/binding. Do not rewrite the structural receipt or copy a stale hash. |
| Workbook catalog omits rows at a cap | Display limit, not necessarily source loss | Compare authoritative total versus displayed count. Full data stays in local SQLite/Parquet/FTS. |
| Word PDF receipt missing | Office export/visual QA stage not completed | Install/configure local Word if that gate is required, export exact final DOCX, render/inspect every page, then generate the real receipt. |
| Copy of SQLite main file misses recent data | WAL was not consistently backed up | Use a SQLite-consistent backup/checkpoint workflow. Never delete `-wal` to shrink a live database. |

SQLite documents a WAL-reset corruption fix in 3.51.3 and newer, with specific older-branch backports; Python's own version does not tell you which SQLite library it embeds. Verify the selected runtime against the current gate and [upstream WAL notes](https://sqlite.org/wal.html#walresetbug).

The independent workbook receipt must have `schema=evidence_trail.workbook_native_qa.v1`, matching `company_id`, reviewed `status=PASS`, `opened_read_only=true`, `repair_prompt_seen=false`, `all_sheets_visual_inspection_pass=true`, and `formula_errors=0`. Its `workbook_sha256` must bind the inspected XLSX; `structural_receipt_sha256` must bind the unmodified `qa/workbook_v2_verification.json`. Only record these values after actual checks; synthetic contract tests do not run Excel. See [the complete QA instructions and hash-only commands](processing.md#independent-native-workbook-qa-is-mandatory).

### Before reporting “done”

Read the final receipt from the current bytes/current run, not a stale log. Report source checks, capture reconciliation, extraction coverage, source gaps, relation provenance, chronology, and human-format QA separately. A failed gate stays failed even when the output file exists.

---

## 中文

### 先看正确证据

核对失败命令、退出码、选定解释器、当前案例绑定和清理后的错误码。进程存在、端口监听、源码测试通过、客户采集完成，是四件不同的事。

案例原始日志留本机，原生工具可能把文件名/文字写进去。公开问题应使用合成输入复现，只提供清理后的诊断。

### 安装与启动

| 现象 | 可能原因 | 处理方法 |
| --- | --- | --- |
| 找不到 `node` / `npm` | PATH 未刷新或终端错误 | 重开 PowerShell，用 `Get-Command node,npm`，确认 Node 24+。 |
| 阻止 `npm.ps1` | 脚本执行策略 | 用 `npm.cmd` 或一次进程 `-ExecutionPolicy Bypass`，不改整机策略。 |
| 没有 `py -3.12` | 启动器或 3.12 未安装 | 用准确可信 Python 路径创建 venv，不混另一环境的 pip。 |
| `pdfplumber` 未找到 | 依赖装进另一个环境 | 用 `& $Python -m pip install -r requirements-local.txt`，再 `pip check`。 |
| ExcelJS/Sharp/表格辅助库导入失败 | 只装了 app 依赖 | 根目录也执行 `npm ci`，构建器要与 `pipeline/lib` 一起保留。 |
| npm 后没有 Electron 程序 | 二进制下载失败 | 检查安装退出码和允许网络路径，在独立副本重装 app；不要复制私密浏览器配置来解决。 |
| 安装抱怨工作流索引 | 把可选集成当核心依赖 | 索引是 opt-in，缺失不能阻止编译，不装隔壁私有工具。 |
| 3311 控制端口占用 | 另一个 one-shot 或无关服务 | 识别 PID，只关闭自己的实例，不批量杀 Node/Electron。 |
| 服务正常但桌面未就绪 / `ERR_ABORTED` | 浏览器初始导航时抢先采集 | 用等待 ready 的 `capture-one.ps1`，不要原始请求抢 Start。 |
| 停留登录页 | 全新隔离会话或认证过期 | 本地采集器内手动登录/MFA，确认记录后按一次 Start；没有内置凭据代登。 |

### 采集完整性与并发

| 现象 | 含义 | 正确处理 |
| --- | --- | --- |
| 重复 Start 返回 HTTP 409/busy | 初始化/采集已持有启动租约 | 等终态；busy 是保护，不能反复重启。 |
| 恢复标记记录与当前页面不一致 | 崩溃/导航后身份不符 | 写入前重新打开并核对标记记录，不能删身份检查。 |
| 长任务 reservation 时间老 | 时间老不证明 owner 死了 | 看活性/归属，活进程不能只因超过几小时而过期。 |
| 锁 helper 启动/等待超时 | 没有拿到独占归属 | 停止，恢复其 OS helper，不能降级成目录锁。 |
| 根标签访问完但数量不对 | 缺页、权限、结构变化或计数字段错 | 看分页/接口对账，改适配器并加合成回归；访问标签不是完整递归。 |
| 有附件 URL 但没有本地回执 | 发现网址不等于保全成功 | 只重试授权下载或记缺口，不把网址算原件。 |
| AUTO7 没有贸易页 | 贸易故意是独立手动通道 | 使用审查后的单线程手动流程；AUTO7 通过后，联合完整提取仍要求手动清单，见 [手动契约](manual-trade.md)。不重放受限贸易接口。 |
| 签名 URL 元数据变了 | 审计凭据脱密 | 真正请求仍用允许原 URL，审计隐藏签名/Token；对象身份看字节，不看脱密显示 URL。 |

### 本地提取

| 错误/现象 | 原因或区别 | 恢复方法 |
| --- | --- | --- |
| `CASE_IDENTITY_MISSING` / `CASE_IDENTITY_MISMATCH` | 空案例/错案例/ID 不符 | 用采集器实际路径及匹配 ID，不编身份元数据。 |
| `MANUAL_TRADE_MANIFEST_MISSING` | 完整提取器强制要求手动贸易证据，AUTO7 不会生成 | 完成 [审查后的手动通道与清单](manual-trade.md)，否则保留自动采集/未完成阶段；不造空 PASS，不删校验。 |
| `MANUAL_TRADE_MANIFEST_NOT_PASS` / `MANUAL_TRADE_STATUS_GAP_CONTRADICTION` | 结构/状态不符或缺口声明矛盾 | 审查真实覆盖和缺口，再生成一致的后继清单；不能只改状态解锁。 |
| `MANUAL_TRADE_RECEIPT_MISSING` / 清单 SHA 不符 | 初始联合交付缺匹配回执，或清单后来变化 | 对账手动范围并生成新哈希绑定回执，保留旧证据；准备与联合交付的前提不同。 |
| `CASE_EXTRACTION_LOCK_BUSY` | 另一 prepare/run 持有案例写锁 | 等待或仅优雅停止自己明确的 owner，再重跑；删锁不会安全转移归属。 |
| `PREPARED_SCOPE_BINDING_MISMATCH` / 范围哈希不符 | prepare 后来源/会话改变 | 保留旧结果、审查新范围、准备新后继，不手改库绑定。 |
| `ARCHIVE_TOTAL_BYTE_LIMIT` / `ARCHIVE_TOTAL_MEMBER_LIMIT` | 累计递归预算已到 | 核对范围/磁盘后才明确扩大 prepare 预算；默认 4 GiB/10,000 是跨容器，不是每个压缩包。 |
| PDF/压缩包需要密码 | 加密且无授权密码 | 保留原件和密码缺口，不暴力破解。 |
| `OCR_LANGUAGE_PACK_MISSING` | 选定语言包不在实际目录 | 同程序、同 `--tessdata-dir` 检查 `--list-langs`，补语言包或选合适语言。 |
| 终端 OCR 正常但流水线失败 | PATH/程序/语言路径不同 | 明确传工具路径，优先 ASCII 原生工具目录。 |
| PDF 原生文字为空 | 扫描/图片 PDF | 配 PDF 渲染与 OCR；无文字不代表丢了字节。 |
| 旧 Office 卡住/弹框 | 损坏/密码/COM 模态框 | 干净本地会话运行，保留源，看超时回执，可用时走标记后的本地兜底；不杀无关正在编辑文档。 |
| `LOCAL_ASR_PIPELINE_NOT_FOUND` | 未配置兼容本地驱动 | 提供 `--asr-script` / `CRM_ASR_SCRIPT`、`--asr-python`；模型/驱动不在仓库。 |
| 有音轨但无语音 | 可能真是静音/无语音片段 | 要独立无语音证据或驱动失败状态，不能编一句总结让它看似完整。 |
| `--skip-models` 后 verify 不完整 | OCR/ASR 故意仍 pending | 配工具后去掉参数重跑，不能改 pending 为 completed。 |
| 未知格式 opaque | 现有工具不能合理解释 | 保留原件和 opaque，必要时增加解析器和合成测试。 |

### 关系、时间与交付

| 现象 | 原因 | 必须修正 |
| --- | --- | --- |
| 关系引用另一个 artifact 的指针 | 来源血缘不符 | 使 artifact/record/路径/指针都对应真正包含记录，并核对值。 |
| 同附件只剩一个父级 | 内容去重误当出现去重 | 一个 SHA 原件，全量独立邮件→附件出现/边。 |
| 时间按文件名排序 | 路径字典序替代时间 | 按源时间和采集序重建双轴，保留时区/精度/未知。 |
| 回复跨业务时间移位 | 把父子层级当时间 | 保留父子边但恢复时间顺序，不确定仍明示。 |
| 要求 `--expected-*` | 发布去掉了安装特有基线常数 | 读自己的当前台账并填齐，不复制旧数量、不用零关门槛。 |
| `FINAL_DELIVERY_NOT_FOUND` / 权威文件缺失 | 新案例错用旧基线迁移 | v2 freeze 要兼容已验收 v1；按阶段来，不能编 `final_*`。 |
| SQLite 完整性/能力/版本失败 | 运行时不符或能力缺失 | 查选定解释器 SQLite 版本、FTS5、外键、WAL；使用规定补丁运行时，不删门槛。 |
| DuckDB 内存不足 | 并行导出超过预算 | 调 `CRM_DUCKDB_THREADS`、`CRM_DUCKDB_MEMORY_LIMIT`，先小后实测。 |
| 预览好看但 Excel 不同 | 预览与原生 Office 不同 | 开源 PNG 是 SVG/Sharp 布局预览；completion-v2 必须真实 Microsoft Excel 只读打开、逐表人工检查，不能只看预览。 |
| `WORKBOOK_NATIVE_QA_MISSING` / `WORKBOOK_NATIVE_QA_NOT_PASS` | 独立原生/人工回执缺失或未通过 | 真实只读打开 Excel、逐表/公式检查后，按 [原生 QA 契约](processing.md#强制要求独立原生工作簿-qa) 记录 `qa/workbook_native_qa.json`，结构回执不改；不能用自动 PASS 模板。 |
| `WORKBOOK_NATIVE_OPEN_NOT_PASS` / `WORKBOOK_HUMAN_VISUAL_NOT_PASS` / `WORKBOOK_NATIVE_FORMULA_ERRORS` | 只读/无修复、视觉或公式检查未通过 | 通过审查后的后继修复文件，再对最终字节重新验收，不能改布尔值解锁。 |
| `WORKBOOK_NATIVE_HASH_MISMATCH` / `WORKBOOK_NATIVE_RECEIPT_HASH_MISMATCH` | 原生 QA 后工作簿或结构回执改变 | 保留旧 QA，检查当前文件并重做检查/绑定；不能改结构回执或复制旧哈希。 |
| 目录到上限少了一部分行 | 展示上限不一定是源丢失 | 看权威总数/展示数，完整数据仍在 SQLite/Parquet/FTS。 |
| Word PDF 回执缺失 | 导出/视觉 QA 未完成 | 门槛要求时装本地 Word，导出准确最终 DOCX，逐页检查后生成真实回执。 |
| 只复制 SQLite 主文件丢最近数据 | WAL 未一致性备份 | 使用 SQLite 一致性备份/检查点，不能删活库 `-wal` 缩体积。 |

SQLite 官方说明 WAL-reset 问题在 3.51.3 及以上修复，并有特定旧分支回补。Python 版本不能直接证明其内嵌 SQLite 版本，应按当前门槛和 [上游 WAL 说明](https://sqlite.org/wal.html#walresetbug) 检查。

独立工作簿回执要求 `schema=evidence_trail.workbook_native_qa.v1`、匹配 `company_id`、审查后的 `status=PASS`、`opened_read_only=true`、`repair_prompt_seen=false`、`all_sheets_visual_inspection_pass=true`、`formula_errors=0`。`workbook_sha256` 绑定所检查 XLSX，`structural_receipt_sha256` 绑定未改的 `qa/workbook_v2_verification.json`。只有真实检查后才记录，合成契约测试不会运行 Excel。见 [完整 QA 操作与仅计算哈希的命令](processing.md#强制要求独立原生工作簿-qa)。

### 声称完成之前

读当前字节/当前运行的最终回执，不读旧日志。源码检查、采集对账、提取覆盖、源缺口、关系血缘、时间与人读 QA 分开报告。文件存在也不能让失败门槛变通过。
