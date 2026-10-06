# Evidence Trail — Desktop Collector

Local-first, read-only capture for a deliberately selected CRM case. This directory contains the OKKI vendor adapter and the Electron desktop shell, not customer data or a logged-in browser profile. See the [root README](../README.md) for processing and release instructions.

## English

### 1. Prerequisites

- Windows 10/11, PowerShell, Node.js 24 or newer, and npm on `PATH`.
- At least 8 GiB RAM; keep a short, writable local path and sufficient free disk space for your own evidence.
- An authorized CRM account. Authentication, MFA and CAPTCHA are completed manually in the collector. No login bypass is provided.
- Python 3 is needed for optional archive export or offline profile cloning; OCR and document processing have additional requirements described at the repository root.

### 2. Install before starting

From the repository root:

```powershell
node --version
npm --version
Set-Location app
.\scripts\install.ps1
npm run check
npm test
```

`install.ps1` uses `npm ci`, then builds TypeScript and runs a synthetic adapter smoke test. Normal installation allows Electron's pinned install script to download its binary. Do **not** set `ELECTRON_SKIP_BINARY_DOWNLOAD=1` for a desktop installation; that switch is only for source-only CI that will not launch Electron. A failed download must be resolved before starting the desktop. Do not replace `npm ci` with an unreviewed dependency update.

### 3. Open a fresh desktop and sign in

```powershell
.\scripts\start.ps1 -NoMonitor
```

Sign in manually in the embedded CRM pane. Confirm your selected customer before pressing **Start**. The normal queue launcher can advance to other customers; use the one-shot command below whenever the scope is one customer only. Closing the launcher terminal does not necessarily stop the desktop: close the application to stop that instance.

### 4. Run a strictly bound one-shot case

```powershell
$CompanyId = 'YOUR_NUMERIC_ID' # Replace with your own authorized numeric ID.
.\scripts\capture-one.ps1 -CompanyId $CompanyId -NoAutoNext -NoProfileClone
```

The value must contain digits only. The launcher opens a new local profile by default, waits for both the HTTP health endpoint and a **fresh desktop-ready marker**, and only then requests capture. If you are not signed in yet, finish login in the opened collector and press Start once. An `accepted: true` response means the asynchronous job was accepted, not that capture is complete.

```powershell
# Reuse completed byte-identical objects; preserve previous sessions.
.\scripts\capture-one.ps1 -CompanyId $CompanyId -NoAutoNext -ResumeExisting -NoProfileClone

# Capture visible UI gaps into a new independent session.
.\scripts\capture-one.ps1 -CompanyId $CompanyId -NoAutoNext -RevisitUiGaps -NoProfileClone
```

Do not combine `-RevisitUiGaps` with `-ResumeExisting`. Revisit requires an explicit one-shot binding and never enables queue traversal. Trade/customs routes are excluded from the automatic collector and must remain a separate visible, slow manual workflow.

### 5. Paths, ports and optional features

| Setting | Default / behavior |
| --- | --- |
| `OKKI_WORKSPACE_ROOT` | Repository root; evidence is written to its `cases/` directory |
| `OKKI_RUNTIME_ROOT` | `app/runtime/`; the PowerShell launcher assigns per-instance paths |
| Desktop control / CDP | Normal first instance: `127.0.0.1:3211` / `9334` |
| One-shot control / CDP | `127.0.0.1:3311` / `9434` |
| Backend-only `npm start` | Control `3210`, CDP `9333`; no embedded desktop is launched |
| `OKKI_INSTANCE_COUNT` | `1`; adjust planned instance count before performance scaling |
| `OKKI_BROWSER_EXECUTABLE` | Optional absolute Chrome/Chromium/Edge path for external-browser tools |
| `-CloneLocalProfile` | Explicitly opt in to local-only profile/cookie copying; never publish the output |
| `-NoMonitor` | Do not start the optional standalone model-review monitor |

For storage outside the checkout, set `OKKI_WORKSPACE_ROOT` **before** starting. Runtime data, case names, cookies, raw logs and generated evidence are not safe source-release artifacts. Automatic model review is disabled unless a local monitor configuration is explicitly enabled; it must only receive counts, hashes and error classes.

Optional, hash-pinned WARC tooling:

```powershell
.\scripts\install-archive-tools.ps1 -Python python
```

### Common pitfalls

- **Port already occupied:** close the owning project instance; do not kill unrelated processes or delete live locks.
- **`ERR_ABORTED` immediately after launch:** health is not desktop readiness. Use the one-shot launcher, which waits for the desktop marker.
- **`accepted` but incomplete:** inspect job errors, reconciliation and source gaps. A progress bar or hash is not proof of completeness.
- **Path contains spaces:** launchers quote native paths; keep the checkout short to avoid Windows path limits.
- **Missing Chrome:** the desktop uses Electron; external-browser smoke tests need `OKKI_BROWSER_EXECUTABLE`.
- **Signed URL expired / source deleted:** preserve explicit source-gap states; never fabricate missing bodies.
- **More CPU does not mean safe network concurrency:** all instances share a memory budget; binary downloads have tighter caps than JSON pagination.
- **Linux/macOS:** selected TypeScript/Python helpers are portable, but these `.ps1` desktop launchers, Windows tests and Office COM are Windows-specific. Other desktop platforms are not claimed verified.

### Validation limits

`npm run check`, `npm run build`, `npm test`, and `npm run smoke` validate synthetic fixtures and local recovery behavior. They do **not** authenticate to CRM, assert live vendor compatibility, or certify production coverage. Read [ARCHITECTURE.md](ARCHITECTURE.md) before changing identity, replay, lock or evidence rules.

---

## 中文

### 1. 运行前条件

- Windows 10/11、PowerShell、Node.js 24 或更新版本，`node` 和 `npm` 必须能从命令行调用。
- 至少 8 GiB 内存；使用短且可写的本地目录，为自己的证据预留足够空间。
- 使用获授权的 CRM 账号，在采集器中人工登录、处理 MFA 或验证码；项目不提供登录绕过。
- 可选 WARC 归档与离线配置复制需要 Python 3；OCR、文档处理的其他依赖见仓库根目录说明。

### 2. 先安装，再启动

从仓库根目录执行：

```powershell
node --version
npm --version
Set-Location app
.\scripts\install.ps1
npm run check
npm test
```

安装脚本按 `npm ci → TypeScript 构建 → 合成 smoke` 顺序执行。正常桌面安装会运行固定版本 Electron 的安装脚本并下载浏览器二进制；**不要**设置 `ELECTRON_SKIP_BINARY_DOWNLOAD=1`，该开关仅适用于不启动桌面的源码 CI。下载失败必须先解决，不能拿随意更新依赖替代锁定安装。

### 3. 打开独立桌面并人工登录

```powershell
.\scripts\start.ps1 -NoMonitor
```

在内嵌 CRM 面板人工登录，核实客户后再点开始。普通队列模式可能自动进入其他客户；只处理一家时必须使用下面的单客户入口。关闭启动终端不一定停止桌面，应关闭应用本身。

### 4. 单客户硬绑定

```powershell
$CompanyId = 'YOUR_NUMERIC_ID' # 替换为你自己的获授权数字客户 ID。
.\scripts\capture-one.ps1 -CompanyId $CompanyId -NoAutoNext -NoProfileClone
```

ID 必须全是数字。默认创建独立本地配置，不复制登录 Cookie。启动器先等待健康端点，再等待**新的桌面就绪标记**，然后提交采集。如尚未登录，在已打开的采集器中完成登录再点一次开始即可。`accepted: true` 仅表示异步任务被接受，并不代表已经完成。

```powershell
# 中断后复用字节一致的已完成对象，不覆盖旧会话。
.\scripts\capture-one.ps1 -CompanyId $CompanyId -NoAutoNext -ResumeExisting -NoProfileClone

# UI 缺口补采始终创建独立新会话。
.\scripts\capture-one.ps1 -CompanyId $CompanyId -NoAutoNext -RevisitUiGaps -NoProfileClone
```

`-RevisitUiGaps` 不能和 `-ResumeExisting` 同用，补采必须有明确单客户绑定，不能开启下一客户队列。自动采集器排除贸易与海关接口；贸易数据必须另走可见、慢速、人工通道。

### 5. 配置顺序和避坑

存储到仓库外时，启动前设置 `OKKI_WORKSPACE_ROOT`，证据放在其 `cases/` 下。桌面首实例控制端口/CDP 为 `3211/9334`，单客户为 `3311/9434`，只绑定 `127.0.0.1`。单独 `npm start` 是无内嵌桌面的后端，默认 `3210/9333`，不要混用地址。

默认规划一个实例；需要扩大时先设置 `OKKI_INSTANCE_COUNT`，再评估吞吐与内存。只有显式 `-CloneLocalProfile` 才复制本地配置与 Cookie；这些产物绝不能上传。可选模型复验还需要本地配置显式启用，且仅允许计数、哈希与错误分类，不能传证据正文。

可选 WARC 归档依赖：

```powershell
.\scripts\install-archive-tools.ps1 -Python python
```

- 端口占用：关闭本项目对应实例，不要杀其他程序或删除活跃锁。
- 启动后 `ERR_ABORTED`：健康不等于桌面就绪，使用会等待新标记的单客户入口。
- 已接受但未完成：核对任务错误、对账和源缺口，进度条与哈希都不能证明完整。
- 路径有空格：入口已处理原生引号；仍应缩短目录以避免 Windows 长路径限制。
- 找不到 Chrome：桌面使用 Electron；外部浏览器合成测试需设置 `OKKI_BROWSER_EXECUTABLE`。
- 签名过期或源文件删除：保留缺口状态，不能补写虚构正文。
- 不要按 CPU 能力无限加并发：全部实例共享内存预算，二进制下载比 JSON 分页限制更严。
- Linux/macOS：部分底层代码可移植，但 PowerShell 桌面入口、Windows 测试和 Office COM 不属于跨平台已验证能力。

### 验证边界

检查、构建、测试与 smoke 只验证合成数据和本地恢复，不会登录真实 CRM，也不能证明线上适配或生产覆盖。修改身份、重放、锁和证据规则前请先阅读 [ARCHITECTURE.md](ARCHITECTURE.md)。
