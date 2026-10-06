# Security and privacy

## English

This repository publishes software, not captured business records. Browser sessions and generated evidence remain local and are not part of any release.

Report a vulnerability privately through the repository's GitHub **Security → Report a vulnerability** feature when available. If unavailable, open an issue containing only a general description and request a private channel. Do not publish credentials, production screenshots or customer-specific proof.

### Boundaries

- The single-record entry point requires an explicit record ID; it does not authorize other records.
- Trade pages are outside automated capture. Use the supported manual, read-only workflow.
- A hash proves byte identity, not factual accuracy, chronology or source completeness.
- Search snippets, shared IDs and record co-occurrence are not official identity or parent-child facts.
- Encrypted, deleted and inaccessible sources must remain explicit gaps.
- Runtime exports can contain private content even when their filenames mention privacy. Do not upload them.
- `.gitignore` and automated scanning are defense in depth, not a replacement for the exact selected-file review.

See [open-source boundary](docs/open-source-boundary.md) for the release process.

## 中文

本仓库公开软件，不公开采集到的业务记录。浏览器会话及生成的证据留在本机，不属于发布内容。

若仓库支持，使用 GitHub **Security → Report a vulnerability** 私下报告漏洞；若未启用，只提交不含敏感内容的概要，并请求私下沟通渠道。不要公开凭据、生产截图或客户特定证据。

### 边界

- 单记录入口必须显式指定记录 ID，不自动授权其他记录。
- 贸易页面不进入自动采集，应使用支持的手工只读流程。
- 哈希证明字节一致，不证明业务事实、时间顺序或来源完整。
- 搜索摘要、共用 ID 和记录共现不等于官方身份或父子事实。
- 加密、删除及不可访问来源必须保留明确缺口。
- 即使文件名提到隐私，运行导出仍可能含私密内容，不要上传。
- `.gitignore` 和扫描仅是多层防护，不能代替逐个发布文件的检查。

发布流程见[开源边界](docs/open-source-boundary.md)。
