# Contributing to Evidence Trail

## English

### Development order

1. Read [setup](docs/setup.md) and [architecture](docs/architecture.md).
2. Install the versions required by both `package.json` files, using both lockfiles.
3. Create a separate branch. Do not copy a real case into this repository.
4. Make a small change and add a synthetic regression test.
5. Run `python scripts/check.py`, `npm run check`, then `npm --prefix app run check`, `npm --prefix app run build`, `npm --prefix app test`, and `npm --prefix app run smoke`.
6. Run `python scripts/privacy_scan.py --root . --tracked` after staging your changes. Inspect `git diff --cached --stat` and the actual diff.
7. Open a pull request with the problem, change, test commands and known limitations. A synthetic PASS is not a production capture PASS.

### Comment and document conventions

- Explain why a boundary, invariant or recovery step exists; do not narrate obvious syntax.
- Put the English explanation first and the equivalent Chinese explanation second.
- Keep CLI examples copyable and specify their working directory.
- Use `.test`, `.invalid` or `example.com` for fixture addresses. Never paste real emails, names, attachments, cookies, signed links, runtime paths or screenshots.
- Preserve existing `okki.*` schema identifiers and `OKKI_*` adapter environment names unless a versioned migration is included.
- Do not edit package lockfiles by hand. Generate them with npm.
- Keep dependency license notices and document newly introduced external tools.

### Minimal issue template

```text
Expected result:
Observed error code:
Tool versions / operating system:
Synthetic reproduction steps:
Tests run:
Known limitation:
```

Do not attach production logs. A content-free error code and a synthetic reproduction are preferable.

## 中文

### 开发顺序

1. 先读[安装配置](docs/setup.md)和[架构](docs/architecture.md)。
2. 按两个 `package.json` 的版本要求安装依赖，保留两个锁文件。
3. 新建独立分支，不要将真实案例复制到仓库。
4. 做范围明确的修改，并增加合成回归测试。
5. 运行 `python scripts/check.py`、`npm run check`，再依次运行 `npm --prefix app run check`、`npm --prefix app run build`、`npm --prefix app test` 和 `npm --prefix app run smoke`。
6. 暂存改动后运行 `python scripts/privacy_scan.py --root . --tracked`，检查 `git diff --cached --stat` 及实际差异。
7. 提交 PR 时说明问题、修改、测试命令及已知限制。合成测试通过不等于生产采集通过。

### 注释与文档规范

- 解释边界、不变量或恢复步骤存在的原因，不复述显而易见的语法。
- 英文说明在前，含义对应的中文说明在后。
- 命令可直接复制，并写清工作目录。
- 测试地址使用 `.test`、`.invalid` 或 `example.com`，不要粘贴真实邮件、姓名、附件、Cookie、签名链接、运行路径或截图。
- 未提供版本迁移时，保留 `okki.*` 协议标识和 `OKKI_*` 适配器环境变量。
- 不手改依赖锁文件，交给 npm 生成。
- 保留依赖许可证说明，并记录新增外部工具。

### 最小问题描述

```text
预期结果：
实际错误码：
工具版本 / 操作系统：
合成复现步骤：
已运行测试：
已知限制：
```

不要附生产日志，优先使用不含正文的错误码和合成复现。
