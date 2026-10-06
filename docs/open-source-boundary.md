# Public Source, Private Evidence / 公开源码，私密证据

[README](../README.md) · [SECURITY.md](../SECURITY.md)

## English

### What this repository contains

Generalized source code, dependency manifests/lockfiles, synthetic tests, adapter definitions, generic configuration examples, and documentation. Example record IDs, paths, names, and messages are fictional.

Public vendor API routes and UI labels describe an adapter. They are not a previous user's company profile or customer data. This is an unofficial integration; it is not vendor endorsement.

The GitHub source is public and MIT-licensed. `private: true` in the npm manifests only prevents accidental publication to the npm package registry; it does not restrict cloning, modification, or redistribution under MIT.

### What must remain local

- Real customers, contacts, mail bodies, business records, names, addresses, and attachment filenames.
- Case directories, source captures, screenshots, page HTML/MHTML/WARC, JSON responses, documents, images, audio, and video.
- OCR/ASR outputs, searchable databases, relation graphs, exports, and reports derived from real evidence.
- Browser profiles, cookies, cookie bootstrap files, sessions, access tokens, signed URLs, passwords, and private keys.
- Private logs, local checkpoints, source-to-production mappings, production counts, host inventories, private model drivers, and local-only machine paths.
- `.env` files, model weights, caches, build/test working directories, and dependency folders.

An encrypted or “private” GitHub repository is still an external upload. An ignore rule is useful but does not make files already tracked in Git disappear. A byte hash does not anonymize the file it references.

### Before publishing a source copy

1. Build an independent source-only directory. Do not publish a live working checkout containing evidence.
2. Select files explicitly and inspect every selected file. Reject symlinks/reparse points into private directories.
3. Scan text and metadata for credentials, private paths, real IDs, company/customer features, and accidentally copied output.
4. Review the committed diff and staged file list, including hidden files and lockfile registry URLs.
5. Run synthetic tests on source only. Do not trigger a live capture as a release check.
6. Record approved relative paths, byte counts, and SHA-256 bindings outside the public bundle when a local source-location map would be sensitive.
7. Publish from a fresh Git history containing only the generalized source. Do not push the old production history.
8. Verify remote visibility, actual committed files, and release/archive contents after upload.

The included privacy scanner is a heuristic preflight, not proof that arbitrary information is non-sensitive. False positives need inspection; unknown secret formats can be missed. Human review remains necessary.

### Reporting a bug safely

Provide source file/line, source commit, tool versions, command shape with fictional IDs/paths, exit code, and a sanitized error code. Reproduce with a minimal synthetic file if possible.

Do not paste raw production exceptions if they include file names, full URLs, cookies, body text, or headers. Do not attach “just one” actual customer file for convenience. Share a security concern through the route in [SECURITY.md](../SECURITY.md), not a public issue containing the secret.

### Local runtime precautions

Keep collector/CDP/explorer listeners on `127.0.0.1`. Do not tunnel a logged-in browser or private full-text database to the public internet. Give the private data directory appropriate local access controls and backups. The MIT license covers source code, not permission to redistribute someone else's evidence.

---

## 中文

### 仓库包含什么

通用化源码、依赖清单/锁文件、合成测试、适配器定义、通用配置示例和文档。示例记录 ID、路径、名字和消息均为虚构。

公开厂商接口路由/UI 标签描述的是适配器，不是原用户的公司特征或客户资料。这是非官方集成，不代表厂商背书。

GitHub 源码公开并采用 MIT。npm 清单中的 `private: true` 只是防止误发布到 npm 包仓库，不限制按 MIT 克隆、修改或再分发源码。

### 哪些必须留本机

- 真实客户/联系人/邮件正文/业务记录、名字、地址、附件名。
- 案例目录、采集原件、截图、页面 HTML/MHTML/WARC、JSON 响应、文档/图片/音视频。
- 真实证据派生的 OCR/ASR、检索数据库、关系图、导出物和报告。
- 浏览器配置、Cookie、Cookie 引导文件、会话、Token、签名 URL、密码和私钥。
- 私密日志、检查点、源码→生产位置映射、生产数量、机器台账、私有模型驱动和机器特有路径。
- `.env`、模型权重、缓存、构建/测试工作目录和依赖目录。

加密或“私密”GitHub 仓库依然是外部上传。ignore 有用，但不能让已跟踪文件从历史自动消失。字节哈希不等于文件匿名。

### 发布源码副本之前

1. 建立独立源码目录，不发布带证据的运行副本。
2. 显式选文件并逐件检查，拒绝指向私密目录的符号链接/reparse point。
3. 扫描文字和元数据里的凭据、私有路径、真实 ID、公司/客户特征、误复制产物。
4. 检查提交 diff 和暂存文件，包括隐藏文件和锁文件中的 registry 地址。
5. 只对源码跑合成测试，不能把真实采集当发布检查。
6. 记录认可相对路径/字节/SHA；敏感本地位置映射不能放公开包。
7. 从只含通用化源码的新 Git 历史发布，不 push 旧生产历史。
8. 上传后核对可见性、实际提交文件和 release/归档内容。

所带隐私扫描是启发式预检，不是“任意信息都不敏感”的证明。误报要检查，未知秘密格式可能漏掉，人工审查仍必要。

### 安全报告问题

提供源码位置、commit、工具版本、虚构 ID/路径的命令格式、退出码和清理后的错误码。尽量用最小合成文件复现。

异常若含文件名、完整 URL、Cookie、正文或请求头，不能原样贴。不能图方便上传“仅一份”真实客户文件。秘密泄漏按 [SECURITY.md](../SECURITY.md) 处理，不要在公开 Issue 中贴秘密。

### 本地运行注意

采集器/CDP/全文浏览器只监听 `127.0.0.1`。不能把已登录浏览器或私密全文库开公开隧道。私密目录应有本地访问控制和备份。MIT 只授权源码，不授权传播别人的证据。
