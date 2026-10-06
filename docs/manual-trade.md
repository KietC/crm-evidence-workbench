# Manual trade evidence: required integration contract / 手动贸易证据：必需集成契约

[README](../README.md) · [Setup](setup.md) · [Processing](processing.md) · [Troubleshooting](troubleshooting.md)

## English

### What is required, and what is not shipped

Automatic capture intentionally excludes the sensitive trade/customs channel. However, the current `single_customer_full_extract.py` implementation **unconditionally loads a reviewed manual-trade manifest during `prepare`, `run`, and `verify`**. An automatic seven-tab pass alone does not meet that precondition. Missing manifests produce `MANUAL_TRADE_MANIFEST_MISSING`.

This release ships the manifest consumers and synthetic validation fixtures, **not a general-purpose manual-trade capture or receipt producer**. You need an independently reviewed local manual-capture/verification workflow that emits the contract below. This is an integration prerequisite, not an installation step that `npm ci` or the automatic collector supplies. Do not copy a synthetic test manifest into a real case, invent an empty PASS manifest, or weaken the validator to make a seven-tab capture appear complete.

There are two distinct gates:

| Stage | Required manual evidence |
| --- | --- |
| Full extraction `prepare/run/verify` | Reviewed `trade_manual_capture_manifest.json`, its listed files, and matching hashes. |
| Initial combined delivery `build_single_customer_final_delivery.py build` | The same reviewed manifest **plus** a matching `trade_manual_capture_receipt.json` binding its path, byte size, SHA-256, session, status, and counts. |

If trade is inaccessible, unsupported, or not yet reviewed, preserve the automatic capture and explicitly retain an incomplete/scoped result. Do not claim the combined full-extraction or final-delivery gate passed. Document an actually observed source gap; “not yet collected” is not proof of a source-side absence.

### Manual collection and coverage review

Keep the workflow bound to one authorized record and its local case. Use only actual visible controls/states; do not guess other record IDs, enter related customers, construct trade endpoints, replay restricted APIs, or submit business mutations. Use a single browser operator, never a parallel trade crawler. A conservative local procedure waits at least 12 seconds between ordinary clicks and 20 seconds for pagination/detail clicks, and waits for the selected state/content to stabilize before proceeding. These are operating defaults, not a claim that every source has the same limits.

Before approving a manifest:

1. Record the root view, visible buyer/seller modes, candidate/detail tabs, filters, pagination, and permitted downloads actually encountered.
2. Reconcile displayed totals, unique source identifiers, page states, and the terminal page/empty state. Preserve source counts separately from file counts.
3. Save each stable state locally using the reviewed workflow: available page text/HTML, screenshots, natural downloads, timestamp, session, and click sequence. Preserve originals rather than retyped summaries.
4. Keep candidate identity separate from the CRM customer identity. A name/country match alone is not a verified binding. Unresolved candidates remain unbound.
5. Record inaccessible/deleted/blocked material and the evidence supporting each gap. Do not describe unseen content as captured.
6. Review coverage and identity before the producer assigns `PASS` or `PASS_WITH_SOURCE_GAPS`. Hash validity proves byte identity, not that all visible states were visited or correctly interpreted.

All of this stays local. Do not attach raw manifests, screenshots, downloads, or authenticated URLs to a public issue.

### Required directory and selection rules

The layout below is structural, not an example complete capture:

```text
<case-root>/
└── evidence/
    └── manual_trade/
        └── <manual-session>/
            ├── trade_manual_capture_manifest.json
            ├── trade_manual_capture_receipt.json  # Required for initial combined delivery
            └── <original evidence files/subdirectories listed by the manifest>
```

Full extraction discovers `evidence/manual_trade/*/trade_manual_capture_manifest.json` and selects the file with the **newest filesystem modification time**, with a path tie-breaker. It does not select by business event time or provide a manual-manifest override flag. Check which file will be selected; copying/touching an older manifest can change selection. Do not silently overwrite an earlier session. Keep the reviewed selection and prepared scope binding stable.

Initial combined delivery discovers the newest `*/trade_manual_capture_receipt.json` by modification time unless `--trade-receipt` is supplied. `--trade-manifest` can explicitly select its matching manifest. Both must remain inside the bound case; an explicit path does not bypass identity/hash/status checks.

### Manifest field checklist

The following is a field contract, **not a ready-made PASS template**. A reviewed producer must derive values from the actual preserved scope.

| Field | Required value / check |
| --- | --- |
| `schema` | Exactly `okki.trade.manual_capture_manifest.v1`. |
| `company_id` | String matching the bound case/CLI ID. |
| `status` | `PASS` only for reviewed coverage without declared source gaps; `PASS_WITH_SOURCE_GAPS` only with actual, nonempty source gaps. |
| `source_gaps` | List. Must be empty for `PASS` and nonempty for `PASS_WITH_SOURCE_GAPS`. Each gap should retain its observed reason and source reference. |
| `artifacts` | List of original evidence-file references. Every row is an object with `relative_path`, `bytes`, and `sha256`. |
| `artifacts[].relative_path` | Nonempty unique relative file path, relative to the manifest directory. No absolute path, escape outside that directory, or duplicate path. Normalize separators to `/`. |
| `artifacts[].bytes` | Nonnegative integer equal to the actual file byte size. |
| `artifacts[].sha256` | 64 hexadecimal characters, computed over the actual saved file bytes. Prefer lowercase consistently. |
| `counts.artifact_files` | Number of listed artifact files; must equal the `artifacts` length. |
| `counts.artifact_bytes` | Sum of actual artifact byte sizes. |
| `session` | Required for combined delivery; same value as the receipt. Use a 1–128 character identifier matching `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`. |
| Other `counts` keys | Coverage-specific nonnegative integers derived from the reviewed UI inventory. Combined delivery validates all supplied counts as such and requires exact manifest/receipt equality. Do not use a former case's values. |

The extractor accepts the two artifact count checks when the corresponding keys are present; the initial combined-delivery consumer requires both. Use the stricter contract above when producing a scope intended for both stages. The extractor also rehashes every listed manual file during preparation, running, and verification. A file must exist and match both its declared bytes and SHA.

For combined delivery, each source-gap row must be an object with a nonnegative `candidate_index`. Include a meaningful `code` and source-backed explanation; optional `observed_numeric_tokens` should be a list when supplied. These metadata checks cannot prove the gap is justified; coverage review remains mandatory.

### Receipt field checklist

Generate the receipt only after freezing the reviewed manifest and its evidence. Rewriting JSON formatting changes the manifest hash even if the human-readable values appear identical.

| Field | Required value / check |
| --- | --- |
| `schema` | Exactly `okki.trade.manual_capture_receipt.v1`. |
| `company_id` | Same bound case/CLI ID as the manifest. |
| `session` | Same valid session identifier as the manifest. |
| `status` | Same reviewed `PASS` or `PASS_WITH_SOURCE_GAPS` status as the manifest. |
| `manifest_path` | Existing manifest path inside the case. Prefer a case-relative path for portability. Used unless `--trade-manifest` overrides selection. |
| `manifest_bytes` | Actual manifest file byte size. |
| `manifest_sha256` | SHA-256 of the exact final manifest bytes. |
| `counts` | Object exactly equal to the manifest's `counts`, including keys and values. |

Combined delivery always verifies the receipt-bound manifest size/hash, artifact paths/sizes, and totals. **Its default mode does not rehash each manual artifact.** Include `--verify-manual-artifact-hashes` when building the combined delivery to perform that additional full-file recheck. No status/receipt can substitute for missing original files.

Check the consumer's real flags before integrating:

```powershell
& $Python pipeline\single_customer_full_extract.py prepare --help
& $Python pipeline\build_single_customer_final_delivery.py build --help
```

Do not use `--output-dir` to rename a v2 extraction into a compatible v1 result without validating the receiving schema; see [stage boundaries](processing.md).

### Final preflight before full extraction

- The automatic capture identity, terminal status, processing scope, and reconciliation are valid.
- The intended manual manifest is the one discovered by the extractor.
- Its record ID matches the case; status and source gaps are consistent and honestly reviewed.
- Every evidence path is unique/contained, every file exists, and bytes/hashes/counts reconcile.
- Source totals and visited-state coverage have been reviewed independently of hash checks.
- For initial combined delivery, a matching hash-bound receipt also exists; session and all counts agree.
- Input files are no longer being written; the prepared scope will not be changed in place.

Only then continue with [local extraction](setup.md#10-configure-and-run-local-extraction). Missing manual evidence is a missing prerequisite, not proof that the automatic collector malfunctioned and not a reason to fake a success record.

### Source references

- [Full extractor](../pipeline/single_customer_full_extract.py): `load_manual_trade_scope`, called by preparation, running, and verification.
- [Initial combined-delivery builder](../pipeline/build_single_customer_final_delivery.py): `discover_manual_receipt` and `load_manual_trade`.
- [Open-source boundary](open-source-boundary.md): evidence and actual runtime manifests must stay private.

## 中文

### 必须有什么，发布没有什么

自动采集有意排除敏感贸易/海关通道。但当前 `single_customer_full_extract.py` 实现会在 **`prepare`、`run`、`verify` 中无条件加载审查后的手动贸易清单**。仅自动七标签通过不满足该前提，缺清单会报 `MANUAL_TRADE_MANIFEST_MISSING`。

本发布包含清单消费端和合成验收样例，**没有通用手动贸易采集器或回执生成器**。需要自行配置并审查本地手动采集/验收流程，输出下述契约。这是集成前提，不是 `npm ci` 或自动采集器会完成的安装步骤。不能复制合成测试清单进真实案例，不能编空 PASS 清单，也不能削弱验证把七标签采集说成完整。

两道门槛不同：

| 阶段 | 必需手动证据 |
| --- | --- |
| 完整提取 `prepare/run/verify` | 审查后的 `trade_manual_capture_manifest.json`、所列原文件及匹配哈希。 |
| 初始联合交付 `build_single_customer_final_delivery.py build` | 同一清单 **加上** 匹配的 `trade_manual_capture_receipt.json`，绑定路径、字节数、SHA-256、会话、状态和数量。 |

贸易无权限、不支持或尚未审查时，应保留自动成果，明确保留未完成/有限范围状态，不能声称联合完整提取或最终交付门槛通过。源缺口必须有真实观察依据，“还没采集”不证明源端不存在。

### 手动采集与覆盖审查

流程只绑定一个授权记录和本地案例，只操作真正可见的控件/状态。不猜其他记录 ID，不进入关联客户，不构造贸易接口，不重放受限 API，不提交业务修改。保持单个浏览器操作者，不使用并行贸易爬虫。保守流程普通点击至少间隔 12 秒，分页/详情点击至少 20 秒，并等待选中状态和内容稳定后再继续；这是操作默认值，不表示所有来源限额一致。

批准清单之前：

1. 登记实际遇到的根页面、买家/卖家模式、候选/详情标签、筛选、分页和允许下载。
2. 对账显示总数、唯一来源 ID、页面状态、末页/空状态；来源条数和文件数分开记录。
3. 通过审查后的流程在本地保存每个稳定状态：可取得的文字/HTML、截图、自然下载、时间戳、会话和点击序列。保留原件，不用手打摘要替代。
4. 贸易候选身份与 CRM 客户身份分离，名字/国家相同不等于已绑定；未闭合候选继续未绑定。
5. 登记无权限、删除、阻断材料及支持缺口的证据；没看见的内容不能写已采集。
6. 覆盖和身份审查后，才由生产者赋予 `PASS` 或 `PASS_WITH_SOURCE_GAPS`。哈希只证明字节身份，不证明已遍历全部可见状态或解释正确。

以上材料全部本地保存。不能将原清单、截图、下载、登录后 URL 附在公开 Issue。

### 必需目录与选择规则

下图只表示结构，不是完整采集样例：

```text
<case-root>/
└── evidence/
    └── manual_trade/
        └── <manual-session>/
            ├── trade_manual_capture_manifest.json
            ├── trade_manual_capture_receipt.json  # 初始联合交付必需
            └── <清单所列原证据文件/子目录>
```

完整提取从 `evidence/manual_trade/*/trade_manual_capture_manifest.json` 发现清单，按 **文件系统修改时间最新** 选择，路径作同时间的决胜排序；不是按业务发生时间选择，也没有手动清单覆盖参数。应确认实际被选文件，复制/触碰旧清单可能改变选择。不能默默覆盖旧会话，应稳定保留已审查选择和准备后的范围绑定。

初始联合交付默认按修改时间发现最新 `*/trade_manual_capture_receipt.json`，可用 `--trade-receipt` 明确指定。`--trade-manifest` 可指定其匹配清单。两者必须在绑定案例内部，显式路径不能绕过身份/哈希/状态检查。

### 清单字段检查

下表是字段契约，**不是可直接使用的 PASS 模板**。值必须由审查后的生产者从真实保全范围计算。

| 字段 | 必需值/检查 |
| --- | --- |
| `schema` | 固定 `okki.trade.manual_capture_manifest.v1`。 |
| `company_id` | 字符串，与绑定案例/命令行 ID 一致。 |
| `status` | 覆盖审查通过且没有声明源缺口才能为 `PASS`；确有非空源缺口才为 `PASS_WITH_SOURCE_GAPS`。 |
| `source_gaps` | 列表。`PASS` 必须为空，`PASS_WITH_SOURCE_GAPS` 必须非空；每项保留真实原因和来源指针。 |
| `artifacts` | 原证据文件引用列表，每项对象含 `relative_path`、`bytes`、`sha256`。 |
| `artifacts[].relative_path` | 相对清单目录的非空唯一文件路径，不得绝对路径、越出该目录或重复；统一 `/` 分隔。 |
| `artifacts[].bytes` | 与真实字节数相同的非负整数。 |
| `artifacts[].sha256` | 真实已保存文件字节的 64 位十六进制 SHA-256，建议统一小写。 |
| `counts.artifact_files` | 所列文件数，等于 `artifacts` 长度。 |
| `counts.artifact_bytes` | 真实文件字节数之和。 |
| `session` | 联合交付必需，与回执一致；1–128 字符，匹配 `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`。 |
| 其他 `counts` 字段 | 来自审查后 UI 台账的非负整数；联合交付校验全部数量类型且要求清单/回执完全相等，不用旧案例值。 |

提取器在两个文件数量字段存在时核验，初始联合交付则强制要求两者。为两个阶段生产范围时，应采用上述更严格契约。提取准备、运行和验证都会重算每个手动文件的哈希，文件必须存在且字节数/SHA 均一致。

联合交付还要求每个源缺口对象包含非负 `candidate_index`，应提供有意义的 `code` 及来源支持的说明；可选 `observed_numeric_tokens` 如提供应是列表。元数据校验不证明缺口合理，仍要审查覆盖。

### 回执字段检查

先冻结审查后的清单和证据，再生成回执。即使可读值没变，重新排版 JSON 也会改变清单哈希。

| 字段 | 必需值/检查 |
| --- | --- |
| `schema` | 固定 `okki.trade.manual_capture_receipt.v1`。 |
| `company_id` | 与清单一致的绑定案例/命令行 ID。 |
| `session` | 与清单一致的合法会话标识。 |
| `status` | 与清单相同、经过审查的 `PASS` 或 `PASS_WITH_SOURCE_GAPS`。 |
| `manifest_path` | 案例内现存清单路径，建议案例相对路径便于搬迁；未用 `--trade-manifest` 覆盖时按此选择。 |
| `manifest_bytes` | 真实清单文件字节数。 |
| `manifest_sha256` | 最终清单原字节的 SHA-256。 |
| `counts` | 与清单 `counts` 完全相同的对象，键和值均一致。 |

联合交付一定校验回执绑定的清单字节数/哈希、原件路径/大小和总数。**默认不会逐个重算手动原件哈希。** 构建联合交付时加入 `--verify-manual-artifact-hashes` 才追加全文件重算。状态/回执不能替代缺少的原文件。

集成前确认消费端真实参数：

```powershell
& $Python pipeline\single_customer_full_extract.py prepare --help
& $Python pipeline\build_single_customer_final_delivery.py build --help
```

不能用 `--output-dir` 把 v2 提取改名成 v1 来伪造兼容，必须校验接收结构，见 [阶段边界](processing.md)。

### 完整提取前最终检查

- 自动身份、终态、处理范围和对账均有效。
- 提取器发现的手动清单正是预期审查版本。
- 记录 ID 与案例一致，状态/源缺口一致且经过真实审查。
- 每个路径唯一且在界内，每个文件存在，字节数/哈希/数量一致。
- 来源总数及遍历状态覆盖已经独立审查，不由哈希代替。
- 初始联合交付还存在匹配的哈希绑定回执，会话/全部数量相同。
- 文件已经停止写入，准备后的范围不再原地修改。

通过后才继续 [本地提取](setup.md#10-配置并执行本地提取)。缺手动证据是缺少前提，不证明自动采集器坏了，也不能因此伪造成功记录。

### 源码指针

- [完整提取器](../pipeline/single_customer_full_extract.py)：`load_manual_trade_scope`，准备、运行、验证都会调用。
- [初始联合交付生成器](../pipeline/build_single_customer_final_delivery.py)：`discover_manual_receipt` 和 `load_manual_trade`。
- [开源边界](open-source-boundary.md)：证据和真实运行清单必须私密保存。
