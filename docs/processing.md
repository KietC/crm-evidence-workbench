# Processing, Lineage, and Delivery / 处理、血缘与交付

[Setup](setup.md) · [Architecture](architecture.md) · [Troubleshooting](troubleshooting.md)

## English

### 1. Bind inputs before running anything

Use the case root reported by your collector. The root must contain a matching `case_identity.json` and valid capture manifests. A path named `company_<id>` alone does not establish identity. Source hashes, session IDs, final status, and scope filters must agree.

All commands below run from the repository root with the Python environment configured in [setup](setup.md). Examples use fictional placeholders:

```powershell
$Python = Join-Path $PWD '.venv\Scripts\python.exe'
$CaseRoot = 'C:\EvidenceData\company_123456789'
$CompanyId = '123456789'
```

Replace these locally before executing. Do not paste real case paths, IDs, or output payloads into public issues.

### 2. Know which entry point you need

| Entry point | Purpose | Prerequisite / stopping condition |
| --- | --- | --- |
| `cli.py verify` | Case/scope verification | Existing compatible case; inspect failures rather than weakening checks. |
| `cli.py rebuild-corpus` | Normalize captured evidence | Existing source capture. |
| `cli.py index-raw` | Hash/FTS-index raw evidence | Existing `raw/`; output stays local. |
| `cli.py prepare-fields` | Build field-routed evidence shards | Existing normalized corpus; `--max-chars` bounds input size, not a completeness claim. |
| `cli.py normalize-fields` | Reconcile local summary audit counts | Existing local field-shard summaries; does not generate model answers. |
| `cli.py reduce-fields` | Deterministic exact-union of summary lists | All required source summaries already exist. |
| `cli.py assemble-profile` | Assemble reduced field artifacts | Required reduced fields and audit manifest already exist. |
| `cli.py prepare-segment` | Build bounded segment-selection input | Existing normalized evidence; input preparation is not a semantic decision. |
| `single_customer_full_extract.py prepare/run/verify` | Formal recursive file-processing scope | Passing automatic scope **and a required reviewed manual-trade manifest**; valid mail scope when declared. |
| `build_single_customer_final_delivery.py build/verify` | Initial combined delivery | Compatible processed case, **manual-trade manifest and matching receipt**, and other required source receipts. |
| `build_unredacted_local_package.py` | Full local database/FTS export | Compatible verified evidence and extraction data. This output is private. |
| `freeze_completion_v2.py` | Freeze a prior verified baseline for a successor run | **An existing initial delivery and its v1 authority files. Not a first-capture bootstrap.** |
| `build_relation_timeline_v2.py build/verify` | Explicit relations + dual timelines | Existing local source package, extraction v2 if supplied, reviewed current expected counts. |
| `build_completion_v2_delivery.py prepare/finalize/verify` | Stage and publish a successor delivery | Verified previous package, extraction, relations, UI-gap manifest, catalog/report QA. |
| `resume_completion_v2.ps1` / `status_completion_v2.ps1` | Existing completion run control/status | A valid completion pointer and frozen baseline bound to this case. |

These are different schemas/stages, not interchangeable success messages. Some initial helpers expect `derived/full_extract/` and a v1 delivery; the newer extractor's default output is `derived/full_extract_v2/`. Do not rename folders to make a failed precondition appear satisfied. Use `--output-dir` only as part of a consciously bound workflow, and verify the receiving helper's expected schema.

Check current flags directly:

```powershell
& $Python cli.py --help
& $Python pipeline\single_customer_full_extract.py prepare --help
& $Python pipeline\single_customer_full_extract.py run --help
& $Python pipeline\build_relation_timeline_v2.py build --help
& $Python pipeline\build_completion_v2_delivery.py prepare --help
```

The field-summary helpers do not include an automatic LLM inference runner. Supplying an arbitrary JSON file is not a valid substitute for the required summaries. Any optional semantic processing of private evidence needs your separately configured local model workflow and human/source validation; these helpers do not authorize an external model upload.

### 3. Prepare, run, then verify the extraction scope

`prepare` combines approved source scopes, classifies real file formats, registers content objects and occurrences, and builds pending tasks. It does not certify those tasks as extracted.

The current implementation unconditionally requires the manual-trade manifest in `prepare`, `run`, and `verify`. An automatic seven-tab capture alone will fail with `MANUAL_TRADE_MANIFEST_MISSING`. Follow [manual-trade prerequisites](manual-trade.md) before this section. The source release provides consumers/tests but not a general manual capture/receipt producer; no empty PASS workaround is valid. Initial combined delivery additionally requires the matching receipt. These are separate integration prerequisites, not evidence that your own trade pages have been collected.

```powershell
& $Python pipeline\single_customer_full_extract.py prepare `
    --case-root $CaseRoot --company-id $CompanyId `
    --archive-total-bytes 4294967296 --archive-max-members 10000
```

`run` performs the scheduled work. Configure native tools first; missing local executables are failures, not remote fallback opportunities.

```powershell
& $Python pipeline\single_customer_full_extract.py run `
    --case-root $CaseRoot --company-id $CompanyId `
    --parse-workers 4 --ocr-workers 2 --ocr-languages 'eng'
```

`--max-tasks N` limits a pilot. `--skip-models` performs only non-model work and intentionally leaves OCR/ASR/frame-OCR pending. Both can return an incomplete state; neither certifies full coverage.

```powershell
& $Python pipeline\single_customer_full_extract.py verify `
    --case-root $CaseRoot --company-id $CompanyId
```

Read the task-state counts and gaps. Internally, completed and explicit source-gap tasks are terminal; pending/running/failed work is not. A `source_gap` can be an honest accepted terminal state without being successful text extraction.

### 4. Local ASR integration contract

The source release does **not** contain a private ASR implementation, model weights, or a ready-made GPU environment. Whisper/Qwen/diarization are a driver contract, not proof those engines were installed.

Provide the driver explicitly with `--asr-script` or the local `CRM_ASR_SCRIPT` environment variable; select its Python using `--asr-python`. The extractor invokes:

```text
python driver.py --input <local_audio_file> --output <local_output_directory>
```

For video, the extractor first uses FFmpeg to make mono 16 kHz PCM audio. The driver must genuinely run the required local recognition/diarization stages and write one of:

```text
<output>/state/pipeline_result.json
<output>/_state/pipeline_result.json
```

The normal success result must be a JSON object with `"status": "complete"`. This small status field is an integration signal, not an independent quality audit: retain the transcripts, time codes, disagreements, diarization output, source bindings, engine/model versions, and duration checks in the driver's output.

An optional legacy no-speech path imports the driver's Python module and expects:

- `Pipeline(input_path, output_root)`.
- `run_qwen()` returning an object with a `results` list whose items can contain `text`, `transcript`, or `prediction`.
- `run_diarization()` returning speaker segments.
- `logs/3dspeaker_diarization.log` when explaining an empty voice/embedding result.

A custom driver that implements only the CLI success path need not implement that legacy recovery path; any non-success result will fail rather than silently invent a transcript. Never wire these paths to an external AI upload for private evidence.

Example configuration, with fictional local tool paths:

```powershell
& $Python pipeline\single_customer_full_extract.py run `
    --case-root $CaseRoot --company-id $CompanyId `
    --parse-workers 4 --ocr-workers 2 `
    --asr-script 'C:\Tools\LocalASR\driver.py' `
    --asr-python 'C:\Tools\LocalASR\.venv\Scripts\python.exe' `
    --ffmpeg 'C:\Tools\FFmpeg\bin\ffmpeg.exe' `
    --ffprobe 'C:\Tools\FFmpeg\bin\ffprobe.exe'
```

### 5. Keep format-specific limits visible

- **Images:** preserve the original object, OCR text, boxes, confidence, and blank decision. Low confidence is not license to guess.
- **PDF:** native text and page OCR are separate products. A password-required PDF stays a source gap; no password brute force is performed.
- **OOXML:** retain document/table/header/footer/comment/embedded-object structure where supported. Rendered appearance and extracted text require separate QA.
- **Legacy Office:** use the local read-only conversion path; COM runs serially by application. Do not use a live Office editing session as a conversion worker. A LibreOffice fallback can change rendering and must remain labeled.
- **Archives:** preserve container → member occurrence relations. Reject unsafe member paths/symlinks and enforce cumulative member/byte budgets across recursion. A duplicate SHA does not erase a second parent occurrence.
- **EPS/unknown binaries:** keep native text/rendered OCR or an opaque status; do not claim every binary has meaningful extractable text.
- **Media:** record actual streams, duration, frame selection/coverage, OCR, ASR, and disagreements. A silent clip is not the same as an ASR crash.

### 6. Build explicit relations and two timelines

First obtain a valid source package for your case. `--source-package` refers to a **local evidence/data package**, not this GitHub repository. The public repository contains code only.

Review your inventory, then supply the three expected counts explicitly:

```powershell
# Set these from your reviewed inventory; the program does not guess them.
$AttachmentOccurrences = [int](Read-Host 'Expected attachment occurrence count')
$MailAttachmentRelations = [int](Read-Host 'Expected unique mail-to-attachment relations')
$MultiparentAttachments = [int](Read-Host 'Expected attachments with multiple parent mails')
$SourcePackage = 'C:\EvidenceData\company_123456789\deliveries\local_source_package'
$ExtractV2 = Join-Path $CaseRoot 'derived\full_extract_v2'
$RelationshipV2 = Join-Path $CaseRoot 'derived\relationship_v2'

& $Python pipeline\build_relation_timeline_v2.py build `
    --case-root $CaseRoot --company-id $CompanyId `
    --source-package $SourcePackage --full-extract-v2 $ExtractV2 `
    --output-dir $RelationshipV2 --workers 4 --duckdb-python $Python `
    --expected-attachment-occurrences $AttachmentOccurrences `
    --expected-mail-attachment-relations $MailAttachmentRelations `
    --expected-multiparent-attachments $MultiparentAttachments

& $Python pipeline\build_relation_timeline_v2.py verify `
    --case-root $CaseRoot --package-dir $RelationshipV2 --workers 4
```

Zero is valid only when your reviewed manifest actually contains zero. It is not a universal way to disable validation.

The model distinguishes:

1. Content objects keyed by lowercase SHA-256.
2. Physical/record occurrences carrying source paths and pointers.
3. Semantic edges supported by explicit source fields.
4. Business time with original text, precision, parse status, and zone state.
5. Collection time with session, request sequence, and captured timestamp.

Mail → attachment, reply → original, document → folder, archive → member, and original → derivative edges need real field/pointer evidence. Co-occurrence stays co-occurrence. An unresolved trade candidate remains unbound, not a customer fact.

Unknown or local-unzoned business time must not be presented as UTC. Use stable source/collection tie-breakers for equal/missing times without pretending they resolve the real chronology. A parent relationship does not authorize moving a reply or attachment across business time.

### 7. Existing-baseline completion workflow

Use this section only when a previous verified local delivery and all required v1 authority files already exist. It is a successor/migration path, not a shortcut for a fresh capture.

1. Freeze the prior authority files and generate the current-run pointer:

   ```powershell
   & $Python pipeline\freeze_completion_v2.py `
       --case-root $CaseRoot --company-id $CompanyId --workers 4
   ```

2. Complete any UI-gap revisit using the same bound record. Trade remains separate and manual. Do not manufacture `ui_gap_revisit_latest.json` or mark inaccessible views PASS.
3. Prepare/run/verify extraction into that completion run's configured output.
4. Build/verify relations with your expected counts.
5. Prepare the delivery with six reviewed baseline counts:

   ```powershell
   $OldFiles = [int](Read-Host 'Expected prior verified file count')
   $HistoricalMailFiles = [int](Read-Host 'Expected historical mail file count')
   $MinEvidenceFiles = [int](Read-Host 'Minimum expected combined evidence file count')
   $OldPackage = 'C:\EvidenceData\company_123456789\deliveries\full_unredacted_local\final_example'
   $UiGapManifest = Join-Path $CaseRoot 'manifests\ui_gap_revisit_latest.json'

   & $Python pipeline\build_completion_v2_delivery.py prepare `
       --case-root $CaseRoot --company-id $CompanyId `
       --old-package $OldPackage --full-extract-v2 $ExtractV2 `
       --relationship-v2 $RelationshipV2 --ui-gap-manifest $UiGapManifest `
       --expected-old-files $OldFiles `
       --expected-historical-mail-files $HistoricalMailFiles `
       --expected-min-evidence-files $MinEvidenceFiles `
       --expected-attachment-occurrences $AttachmentOccurrences `
       --expected-mail-attachment-relations $MailAttachmentRelations `
       --expected-multiparent-attachments $MultiparentAttachments
   ```

6. Use the returned stage path and `delivery_payload.json` to build the spreadsheet/catalog and DOCX, then export/inspect PDF where required. Do not predict the stage name from a prior run.
7. Generate honest workbook/report receipts from the exact final bytes and actual visual inspection. Completion requires the independent native workbook receipt described below; the automatic structure/SVG receipt cannot grant `DELIVERY_V2_PASS`. Do not pass a `--visual-inspection-pass` flag unless somebody inspected those pages.
8. Finalize and verify the returned delivery:

   ```powershell
   & $Python pipeline\build_completion_v2_delivery.py finalize --stage $StagePath
   & $Python pipeline\build_completion_v2_delivery.py verify --delivery $DeliveryPath
   ```

`$StagePath` and `$DeliveryPath` above must be the actual paths from your stage/publish receipts. There is intentionally no fictitious final PASS file bundled in the repository.

For an already-created completion run, inspect before mutation:

```powershell
$ExpectedCountsPath = 'C:\EvidenceData\expected-counts.json'
.\status_completion_v2.ps1 -CaseRoot $CaseRoot -CompanyId $CompanyId -Python $Python -AsJson
.\resume_completion_v2.ps1 `
    -CaseRoot $CaseRoot -CompanyId $CompanyId `
    -ExpectedCountsPath $ExpectedCountsPath -Python $Python `
    -ParseWorkers 4 -OcrWorkers 2 -RelationshipWorkers 4 -DryRun -AsJson
```

A reviewed private counts file is mandatory. Copy the shape of [`config/expected_counts.example.json`](../config/expected_counts.example.json) to your **private data directory**, replace the synthetic `company_id` and every count, and verify the source manifests. The schema is `evidence_trail.expected_counts.v1`; fields are `old_files`, `historical_mail_files`, `min_evidence_files`, `attachment_occurrences`, `mail_attachment_relations`, and `multiparent_attachments`. Do not execute with the example's sample counts and do not commit your real file.

A dry run does not process tasks or complete delivery. After reviewing the next stage and configuring tools, invoke the same resume command without `-DryRun` only for the run you intend to advance.

#### Independent native workbook QA is mandatory

Before `finalize`, open the **actual final XLSX in Microsoft Excel read-only**, confirm there is no repair/corruption prompt, check every worksheet for clipped text, overlaps, images, navigation, and readability, and check that formula errors are zero. Inspect hidden preservation sheets as well without saving changes. Close without saving and confirm the workbook bytes have not changed. An SVG/Sharp preview and automatic OOXML checks are useful but are not native opening or human visual inspection.

Keep the generated `qa/workbook_v2_verification.json` unchanged. After the real inspection, record a **separate** local `qa/workbook_native_qa.json` in the returned stage directory using the following contract. This table is not a ready-made PASS JSON; there is deliberately no command that automatically asserts somebody inspected Excel.

| Field | Required evidence/value |
| --- | --- |
| `schema` | Exactly `evidence_trail.workbook_native_qa.v1`. |
| `company_id` | String matching the bound case/CLI ID. |
| `status` | `PASS` only after the actual checks pass. |
| `opened_read_only` | Boolean `true`, reflecting actual Microsoft Excel read-only opening. |
| `repair_prompt_seen` | Boolean `false`; a repair prompt fails the gate. |
| `all_sheets_visual_inspection_pass` | Boolean `true` only after every sheet was actually inspected. |
| `formula_errors` | Integer `0`, based on the real workbook check. |
| `workbook_sha256` | SHA-256 of the exact `客户<company_id>_全案例递归补全目录_v2.xlsx` bytes that were inspected. |
| `structural_receipt_sha256` | SHA-256 of the unchanged `qa/workbook_v2_verification.json` bytes. |

Compute the two hashes locally, **after** the inspection and closing Excel; this command computes hashes only and writes no success receipt:

```powershell
$WorkbookPath = Join-Path $StagePath "客户$($CompanyId)_全案例递归补全目录_v2.xlsx"
$StructuralReceiptPath = Join-Path $StagePath 'qa\workbook_v2_verification.json'
(Get-FileHash -LiteralPath $WorkbookPath -Algorithm SHA256).Hash.ToLowerInvariant()
(Get-FileHash -LiteralPath $StructuralReceiptPath -Algorithm SHA256).Hash.ToLowerInvariant()
```

If either file changes, the old native receipt no longer certifies those bytes; preserve it and redo the corresponding QA rather than edit its hash to unblock publication. Contract-validation tests only verify field/hash binding and rejection behavior; they do not run Office or prove a human inspected a sheet. The validator cannot determine whether a claimed human observation truly happened, so truthful local recording is essential. Report/DOCX/PDF QA remains a separate requirement.

### 8. Other local export helpers

These helpers retain their own contracts. Always run `--help` and supply a valid matching prior output; a table of flags is not permission to substitute any JSON file.

| Helper | Required inventory gates | Output meaning |
| --- | --- | --- |
| `collect_customer_pi_archive.py build` | `--expected-unique`, `--expected-occurrences`, `--expected-parent-mails`, `--expected-multiparent`, `--expected-bytes`, `--expected-confirmed`, `--expected-suspected` | PI candidates/originals and parent-mail occurrences; suspected remains suspected. |
| `extract_pi_tables_local.py` | `--expected-files`, `--expected-pages` | OCR/table extraction with terminal review states. |
| `prepare_pi_source_reproduction.py` | `--expected-files`, `--expected-pages` | Source-grid reproduction payload; unreliable grids remain page images. |
| `prepare_mail_timeline_workbook.py` | `--expected-mails`, `--expected-known-time-mails`, `--expected-pending-time-mails`, `--expected-mail-attachment-relations`, `--expected-attachment-occurrences`, `--expected-attachments`, `--expected-unresolved-documents` | Chronological mail/document/image payload; unresolved source time/content stays visible. |
| `unredacted_explorer_server.py` | `--db`; optional `--check` | Local read-only full-text explorer. Keep on loopback; the database is private. |

Spreadsheets are human navigation/catalogs, not the sole source of full-text or occurrence truth. Long text and large relation inventories belong in the local authoritative database/FTS/Parquet. The catalog can cap displayed rows while recording the uncapped source total.

### 9. Final completion means several gates, not one green cell

Confirm independently:

- Identity, scope, source bytes/hashes, and record counts.
- All accessible content has a real processing terminal state.
- Explicit relation edges have valid source paths and JSON pointers.
- Original time values remain present and dual timelines can be rebuilt stably.
- Spreadsheet text injection/coercion protections, filters/freeze panes, and all-sheet visual checks.
- DOCX/PDF page-level inspection and the claimed read-only Office check.
- Immutable published manifests/checksums and a consistent database backup.
- Explicit source-gap list; absolute completeness is not claimed while gaps exist.

`ACCESSIBLE_DATA_PASS` and `ABSOLUTE_COMPLETENESS_SOURCE_GAPS` describe different axes. Keep both when the accessible workflow is complete but source-side gaps remain. Source-only synthetic test results certify neither axis for your future capture.

---

## 中文

### 1. 运行前先绑定输入

使用采集器返回的案例路径。里面必须有匹配的 `case_identity.json` 和有效采集清单，仅叫 `company_<id>` 的目录不代表身份成立。来源哈希、会话 ID、最终状态和范围过滤必须一致。

以下命令都在仓库根目录使用 [配置指南](setup.md) 的 Python 环境，示例均为虚构占位：

```powershell
$Python = Join-Path $PWD '.venv\Scripts\python.exe'
$CaseRoot = 'C:\EvidenceData\company_123456789'
$CompanyId = '123456789'
```

执行前只在本机替换，不要把真实案例路径、ID 或输出内容贴到公开 Issue。

### 2. 明确自己需要的入口

| 入口 | 用途 | 前提/停止条件 |
| --- | --- | --- |
| `cli.py verify` | 案例/范围校验 | 已有兼容案例；看失败原因，不削弱门槛。 |
| `cli.py rebuild-corpus` | 采集证据标准化 | 已有采集来源。 |
| `cli.py index-raw` | 原始证据哈希/FTS 索引 | 已有 `raw/`；输出留本机。 |
| `cli.py prepare-fields` | 按字段划分证据分片 | 已有标准化语料；`--max-chars` 控制输入体积，不证明完整。 |
| `cli.py normalize-fields` | 对齐本地摘要审计数量 | 已有本地字段分片摘要，不生成模型答案。 |
| `cli.py reduce-fields` | 摘要列表确定性精确并集 | 所需来源摘要已齐备。 |
| `cli.py assemble-profile` | 组装归并字段 | 所需字段及审计清单已经存在。 |
| `cli.py prepare-segment` | 准备有界分群选择输入 | 已有标准化证据；准备输入不等于语义判断。 |
| `single_customer_full_extract.py prepare/run/verify` | 正式递归文件范围 | 自动范围通过，**强制要求审查后的贸易清单**；声明邮件范围时也须有效。 |
| `build_single_customer_final_delivery.py build/verify` | 初始联合交付 | 兼容的已处理案例、**手动贸易清单与匹配回执**，以及其他必需来源回执。 |
| `build_unredacted_local_package.py` | 本地完整数据库/FTS 导出 | 兼容的已验收证据与提取数据；输出私密。 |
| `freeze_completion_v2.py` | 为后续运行冻结旧基线 | **已有初始交付与 v1 权威文件，不是首次采集引导。** |
| `build_relation_timeline_v2.py build/verify` | 显式关系 + 双时间线 | 已有本地来源包、如提供则有效提取 v2，以及审查后的期望数量。 |
| `build_completion_v2_delivery.py prepare/finalize/verify` | 暂存/发布后续交付 | 旧包、提取、关系、UI 补采清单、目录/报告 QA 均已验收。 |
| `resume_completion_v2.ps1` / `status_completion_v2.ps1` | 已有补全运行控制/状态 | 与本案例绑定的补全指针和冻结基线。 |

这些是不同结构与阶段，成功消息不能互换。部分初始辅助工具要求 `derived/full_extract/` 和 v1 交付；新提取器默认是 `derived/full_extract_v2/`。不能改目录名伪造前提。只有明确绑定工作流后才用 `--output-dir`，并核对接收工具要求的结构。

查看准确参数：

```powershell
& $Python cli.py --help
& $Python pipeline\single_customer_full_extract.py prepare --help
& $Python pipeline\single_customer_full_extract.py run --help
& $Python pipeline\build_relation_timeline_v2.py build --help
& $Python pipeline\build_completion_v2_delivery.py prepare --help
```

字段摘要辅助工具不带自动 LLM 推理 runner，任意 JSON 不能替代要求的摘要。私密证据如需语义处理，要另配本地模型并做人/来源验证，不能据此上传外部模型。

### 3. 先 prepare，再 run，最后 verify

`prepare` 合并认可来源范围、识别真实格式、登记内容对象与出现实例并建立待处理任务，不代表已经提取。

当前实现的 `prepare`、`run`、`verify` 都无条件要求手动贸易清单。只有自动七标签采集时，会报 `MANUAL_TRADE_MANIFEST_MISSING`。先完成 [手动贸易前置条件](manual-trade.md) 再运行本节。本发布提供消费端/测试，不提供通用手动采集/回执生成器，不能用空 PASS 绕过。初始联合交付还需要匹配回执。这些是独立集成前提，不意味着自己的贸易页面已经采集。

```powershell
& $Python pipeline\single_customer_full_extract.py prepare `
    --case-root $CaseRoot --company-id $CompanyId `
    --archive-total-bytes 4294967296 --archive-max-members 10000
```

`run` 执行任务。先配置本地工具，缺少程序是失败，不是上传云端的机会。

```powershell
& $Python pipeline\single_customer_full_extract.py run `
    --case-root $CaseRoot --company-id $CompanyId `
    --parse-workers 4 --ocr-workers 2 --ocr-languages 'eng'
```

`--max-tasks N` 限制试点规模。`--skip-models` 只做非模型任务，明确留下 OCR/ASR/视频画面 OCR。二者都可能返回未完成，不能证明全覆盖。

```powershell
& $Python pipeline\single_customer_full_extract.py verify `
    --case-root $CaseRoot --company-id $CompanyId
```

必须检查任务状态和缺口。内部完成与明确源缺口是终态；pending/running/failed 不是。`source_gap` 可以是诚实接受的终态，但不代表成功提取文字。

### 4. 本地 ASR 集成契约

发布源码**不带**私有 ASR 实现、模型权重或现成 GPU 环境。Whisper/Qwen/说话人分离是驱动契约，不代表这些引擎已经安装。

通过 `--asr-script` 或本机 `CRM_ASR_SCRIPT` 环境变量明确提供驱动，通过 `--asr-python` 选择其 Python。提取器调用：

```text
python driver.py --input <本地音频文件> --output <本地输出目录>
```

视频会先用 FFmpeg 生成单声道 16 kHz PCM 音频。驱动必须真的执行所需本地识别/分离步骤，并写入以下之一：

```text
<output>/state/pipeline_result.json
<output>/_state/pipeline_result.json
```

普通成功结果为含 `"status": "complete"` 的 JSON 对象。这个状态仅是接口信号，不是独立质量审查。驱动输出应保留转录、时间码、分歧、说话人结果、来源绑定、引擎/模型版本与时长检查。

可选旧版无语音处理路径会导入驱动模块，要求：

- `Pipeline(input_path, output_root)`。
- `run_qwen()` 返回含 `results` 列表的对象，行内可以有 `text`、`transcript`、`prediction`。
- `run_diarization()` 返回说话人片段。
- 空语音/embedding 解释日志 `logs/3dspeaker_diarization.log`。

只实现 CLI 成功路径的自定义驱动不必实现旧恢复分支；非成功结果会失败，不能默默编转录。私密证据不能接到外部 AI 上传接口。

虚构本地工具路径示例：

```powershell
& $Python pipeline\single_customer_full_extract.py run `
    --case-root $CaseRoot --company-id $CompanyId `
    --parse-workers 4 --ocr-workers 2 `
    --asr-script 'C:\Tools\LocalASR\driver.py' `
    --asr-python 'C:\Tools\LocalASR\.venv\Scripts\python.exe' `
    --ffmpeg 'C:\Tools\FFmpeg\bin\ffmpeg.exe' `
    --ffprobe 'C:\Tools\FFmpeg\bin\ffprobe.exe'
```

### 5. 每种格式都要保留限制

- **图片**：保留原件、OCR 文字、坐标、置信度和空白判断；低置信度不能猜写。
- **PDF**：原生文字和逐页 OCR 是不同结果；需要密码的 PDF 保留缺口，不暴力破解。
- **OOXML**：在支持范围内保留正文/表格/页眉页脚/批注/嵌入对象；外观和文字要分别验收。
- **旧 Office**：本地只读转换，每种应用串行。不能把正在编辑的 Office 会话当转换 worker。LibreOffice 可能改变版式，必须标记。
- **压缩包**：保留容器→内部文件出现关系，拒绝危险路径/符号链接，累计限制递归条目/字节。同 SHA 不代表能删掉另一父级出现。
- **EPS/未知二进制**：保留原生文字、渲染 OCR 或 opaque 状态，不能声称任意二进制都有可读文字。
- **媒体**：记录流、时长、选帧/覆盖、OCR、ASR 和分歧；静音视频不等于 ASR 崩溃。

### 6. 建立显式关系和双时间线

先取得本案例有效来源包。`--source-package` 指的是**本地证据/数据包**，不是 GitHub 源码仓库；公开仓库只有代码。

审查自己台账后，明确提供三种期望数量：

```powershell
# 来自自己的审查台账，程序不会猜。
$AttachmentOccurrences = [int](Read-Host '期望附件出现数量')
$MailAttachmentRelations = [int](Read-Host '期望唯一邮件附件关系数量')
$MultiparentAttachments = [int](Read-Host '期望多父邮件附件数量')
$SourcePackage = 'C:\EvidenceData\company_123456789\deliveries\local_source_package'
$ExtractV2 = Join-Path $CaseRoot 'derived\full_extract_v2'
$RelationshipV2 = Join-Path $CaseRoot 'derived\relationship_v2'

& $Python pipeline\build_relation_timeline_v2.py build `
    --case-root $CaseRoot --company-id $CompanyId `
    --source-package $SourcePackage --full-extract-v2 $ExtractV2 `
    --output-dir $RelationshipV2 --workers 4 --duckdb-python $Python `
    --expected-attachment-occurrences $AttachmentOccurrences `
    --expected-mail-attachment-relations $MailAttachmentRelations `
    --expected-multiparent-attachments $MultiparentAttachments

& $Python pipeline\build_relation_timeline_v2.py verify `
    --case-root $CaseRoot --package-dir $RelationshipV2 --workers 4
```

只有清单确实为零才填零，不能作为通用关闭验收的办法。

模型分开保存：

1. 以小写 SHA-256 唯一的内容对象。
2. 带来源路径/指针的物理或记录出现实例。
3. 有明确源字段支持的语义边。
4. 保留原文、精度、解析状态、时区状态的业务时间。
5. 带会话、请求序号和采集时间的证据时间。

邮件→附件、回复→原邮件、文档→文件夹、压缩包→成员、原件→派生物，都必须有真实字段/指针证据。共现仍是共现；未识别贸易候选保持未绑定，不能当成客户事实。

未知或本地无时区时间不能写成 UTC。同时间/缺失时间用稳定来源/采集键排序，不能假装解决了真实时间顺序。父子关系不能让回复或附件跨业务时间重排。

### 7. 已有基线的补全流程

只有已有经过验证的本地旧交付和全部所需 v1 权威文件时，才使用本节。这是后继/迁移路径，不是首次采集捷径。

1. 冻结旧权威文件并建立本轮指针：

   ```powershell
   & $Python pipeline\freeze_completion_v2.py `
       --case-root $CaseRoot --company-id $CompanyId --workers 4
   ```

2. 使用相同绑定记录补采 UI 缺口；贸易仍独立手动。不能编造 `ui_gap_revisit_latest.json`，不能把无权限视图写成 PASS。
3. 对本轮配置输出 prepare/run/verify 提取。
4. 按期望数量 build/verify 关系。
5. 用六种经过核对的基线数量准备交付：

   ```powershell
   $OldFiles = [int](Read-Host '期望旧版验证文件数')
   $HistoricalMailFiles = [int](Read-Host '期望历史邮件文件数')
   $MinEvidenceFiles = [int](Read-Host '合并证据最小期望文件数')
   $OldPackage = 'C:\EvidenceData\company_123456789\deliveries\full_unredacted_local\final_example'
   $UiGapManifest = Join-Path $CaseRoot 'manifests\ui_gap_revisit_latest.json'

   & $Python pipeline\build_completion_v2_delivery.py prepare `
       --case-root $CaseRoot --company-id $CompanyId `
       --old-package $OldPackage --full-extract-v2 $ExtractV2 `
       --relationship-v2 $RelationshipV2 --ui-gap-manifest $UiGapManifest `
       --expected-old-files $OldFiles `
       --expected-historical-mail-files $HistoricalMailFiles `
       --expected-min-evidence-files $MinEvidenceFiles `
       --expected-attachment-occurrences $AttachmentOccurrences `
       --expected-mail-attachment-relations $MailAttachmentRelations `
       --expected-multiparent-attachments $MultiparentAttachments
   ```

6. 使用返回的 stage 路径和 `delivery_payload.json` 构建表格/目录与 DOCX，再按要求导出并检查 PDF，不照旧运行猜 stage 名称。
7. 对准确最终字节和真实视觉检查生成工作簿/报告回执。补全强制要求下述独立原生工作簿回执，自动结构/SVG 回执不能授予 `DELIVERY_V2_PASS`。没有检查页面的人，不能填 `--visual-inspection-pass`。
8. 最终发布并验证：

   ```powershell
   & $Python pipeline\build_completion_v2_delivery.py finalize --stage $StagePath
   & $Python pipeline\build_completion_v2_delivery.py verify --delivery $DeliveryPath
   ```

`$StagePath`、`$DeliveryPath` 必须来自自己的暂存/发布回执。仓库故意不带虚假的最终 PASS 文件。

对于已经建立的补全运行，变更前先检查：

```powershell
$ExpectedCountsPath = 'C:\EvidenceData\expected-counts.json'
.\status_completion_v2.ps1 -CaseRoot $CaseRoot -CompanyId $CompanyId -Python $Python -AsJson
.\resume_completion_v2.ps1 `
    -CaseRoot $CaseRoot -CompanyId $CompanyId `
    -ExpectedCountsPath $ExpectedCountsPath -Python $Python `
    -ParseWorkers 4 -OcrWorkers 2 -RelationshipWorkers 4 -DryRun -AsJson
```

必须提供经过核对的私密数量文件。参照 [`config/expected_counts.example.json`](../config/expected_counts.example.json) 的结构，复制到**私密数据目录**，替换合成 `company_id` 和所有数量，再核对来源清单。结构名为 `evidence_trail.expected_counts.v1`，字段为 `old_files`、`historical_mail_files`、`min_evidence_files`、`attachment_occurrences`、`mail_attachment_relations`、`multiparent_attachments`。不能直接拿示例数量执行，也不能提交自己的真实配置。

dry-run 不处理任务，也不完成交付。看明白下一阶段并配置工具后，只对确实要推进的那个运行去掉 `-DryRun`。

#### 强制要求独立原生工作簿 QA

在 `finalize` 前，必须使用 **Microsoft Excel 只读打开准确最终 XLSX**，确认没有修复/损坏提示，逐张检查截字、遮挡、图片、跳转及可读性，并核验公式错误为零。隐藏保全表也要检查，不保存修改。关闭不保存后，确认工作簿字节未变。SVG/Sharp 预览和自动 OOXML 检查有价值，但不是原生打开或人工视觉验收。

原自动 `qa/workbook_v2_verification.json` 必须原样保留。真实检查后，在返回的 stage 目录另行记录 **独立** 本地 `qa/workbook_native_qa.json`，符合下表。它不是现成 PASS JSON；故意不提供自动声称有人看过 Excel 的命令。

| 字段 | 必需证据/值 |
| --- | --- |
| `schema` | 固定 `evidence_trail.workbook_native_qa.v1`。 |
| `company_id` | 字符串，与绑定案例/命令行 ID 一致。 |
| `status` | 真实检查通过后才能为 `PASS`。 |
| `opened_read_only` | 布尔 `true`，表示真实 Microsoft Excel 只读打开。 |
| `repair_prompt_seen` | 布尔 `false`，出现修复提示就不通过。 |
| `all_sheets_visual_inspection_pass` | 每张表真的检查后才能为布尔 `true`。 |
| `formula_errors` | 根据真实工作簿检查得到整数 `0`。 |
| `workbook_sha256` | 所检查 `客户<company_id>_全案例递归补全目录_v2.xlsx` 准确字节的 SHA-256。 |
| `structural_receipt_sha256` | 原样 `qa/workbook_v2_verification.json` 字节的 SHA-256。 |

检查并关闭 Excel **之后**，在本地计算两个哈希；以下只算哈希，不写成功回执：

```powershell
$WorkbookPath = Join-Path $StagePath "客户$($CompanyId)_全案例递归补全目录_v2.xlsx"
$StructuralReceiptPath = Join-Path $StagePath 'qa\workbook_v2_verification.json'
(Get-FileHash -LiteralPath $WorkbookPath -Algorithm SHA256).Hash.ToLowerInvariant()
(Get-FileHash -LiteralPath $StructuralReceiptPath -Algorithm SHA256).Hash.ToLowerInvariant()
```

任一文件改变后，旧原生回执不再证明当前字节。应保留旧回执、重做对应 QA，不能改哈希解锁发布。契约测试只验证字段/哈希绑定及拒绝行为，不运行 Office，也不证明人工看过表。验证器无法判断声称的人工观察是否真实发生，必须如实本地记录。报告/DOCX/PDF QA 仍是另一道前提。

### 8. 其他本地导出辅助工具

各工具保留自己的接口。先运行 `--help`，再提供匹配的有效旧输出；参数表不代表能随便替换输入 JSON。

| 工具 | 必须提供的台账门槛 | 输出含义 |
| --- | --- | --- |
| `collect_customer_pi_archive.py build` | `--expected-unique`、`--expected-occurrences`、`--expected-parent-mails`、`--expected-multiparent`、`--expected-bytes`、`--expected-confirmed`、`--expected-suspected` | PI 候选/原件与父邮件出现；疑似保持疑似。 |
| `extract_pi_tables_local.py` | `--expected-files`、`--expected-pages` | OCR/表格提取和终态复核。 |
| `prepare_pi_source_reproduction.py` | `--expected-files`、`--expected-pages` | 原表格重建输入；不可靠网格保留页面图。 |
| `prepare_mail_timeline_workbook.py` | `--expected-mails`、`--expected-known-time-mails`、`--expected-pending-time-mails`、`--expected-mail-attachment-relations`、`--expected-attachment-occurrences`、`--expected-attachments`、`--expected-unresolved-documents` | 按时间的邮件/文档/图片输入；未知时间和内容仍显示。 |
| `unredacted_explorer_server.py` | `--db`；可选 `--check` | 本地只读全文浏览器，只允许 loopback，数据库不能公开。 |

Excel 是人读导航/目录，不是全文或出现关系的唯一真相。长文本、大规模关系应留在本机权威数据库/FTS/Parquet，工作簿可以限制展示行数，但必须保留未裁剪来源总数。

### 9. 最终完成要多重门槛，不是一格绿灯

分别确认：

- 身份、范围、来源字节/哈希、记录数量。
- 所有可取得内容有真实处理终态。
- 显式边有有效来源路径和 JSON 指针。
- 原始时间值仍保留，双时间线可稳定重建。
- 表格注入/自动类型转换防护、筛选/冻结、全部工作表视觉检查。
- DOCX/PDF 逐页检查和实际声称的 Office 只读打开。
- 不可覆盖发布清单/哈希，以及一致性数据库备份。
- 独立源缺口清单；有缺口不能声称绝对完整。

`ACCESSIBLE_DATA_PASS` 和 `ABSOLUTE_COMPLETENESS_SOURCE_GAPS` 是两个维度。当前可取得内容完成但源缺口仍在时，两者应同时保留。源码合成测试不证明未来案例的任一维度。
