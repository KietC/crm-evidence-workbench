# Evidence Trail — Capture Architecture

## English

### Components and execution order

1. `scripts/install.ps1` installs the committed npm lock, compiles TypeScript and checks the vendor contract with synthetic input.
2. `scripts/start.ps1` computes a shared resource budget, assigns per-instance runtime/ports, and launches `electron-main.ts`. Login-profile cloning is explicit opt-in.
3. `electron-main.ts` attaches the control UI and isolated CRM browser pane, starts `server.ts`, and publishes a fresh desktop-ready marker. HTTP health alone is not browser readiness.
4. `server.ts` binds loopback only, reserves capture synchronously through `capture-start-gate.ts`, verifies the intended identity, and passes that identity into `CaptureEngine` before any evidence write.
5. `capture-engine.ts` installs scope/privacy guards before automatic navigation. It captures seven adapter-defined root tabs, follows supported observed read-only contracts, reconciles counts and persists a terminal receipt. Automatic trade/customs capture is excluded independently of the adapter.
6. `evidence-store.ts` stores original objects and source occurrences, immutable case identity, request/response receipts, SHA-256 and session scopes. New sessions append rather than replace raw evidence.
7. Separate local Python processing reads the resulting scope; its extraction, relationship, chronology and delivery status must be verified independently of capture.

### Invariants that must survive changes

| Boundary | Required behavior |
| --- | --- |
| One-shot identity | An explicit numeric ID is rechecked after browser awaits and before writes; no next-customer navigation or shared repair queue |
| UI revisit | Explicit one-shot identity, a new session, no `resume_existing`, no manual trade lane |
| Single writer | Synchronous start gate plus OS mutex/flock, owner-sensitive reservations and lease-loss fail-closed behavior |
| Read-only replay | Preserve observed customer/object/folder filters; mutate only supported pagination fields; never submit business writes |
| Privacy | Raw bodies, profiles, cookies and original filenames remain local; no external AI uploads |
| Signed URLs | Redact known authentication query/header values in receipts; preserve non-secret representation parameters and object identity |
| Evidence identity | SHA-256 proves bytes only; one content object can have several occurrence/parent references |
| Completion | Accepted/running/hash/reconciliation/test PASS are distinct; source gaps remain explicit |
| Chronology | Request sequence is capture order, not automatically business event or reply-thread order |

### Recovery

A crash may leave a local active marker or an unfinished session. Recovery re-opens and re-verifies the marker's intended company before starting. A live PID is not treated stale merely because a reservation is old. Kernel lock ownership ends when the helper pipe closes, including abrupt Node death; losing an acquired OS lease terminates work rather than falling back to an unlocked writer. Do not manually remove live locks or edit a completion state to force reuse.

`-ResumeExisting` reuses verified successful byte objects while continuing a new capture session. `-RevisitUiGaps` creates a separate session and scope so old accepted scopes are not silently rewritten. Session-local snapshots remain the authoritative objects; `latest` files are convenience pointers, not immutable evidence.

### Adapter and platform limits

The bundled OKKI adapter contains public integration routes, selectors, labels and resource hosts. It has no tenant-specific user/stage filters. Vendor layouts and undocumented contracts may change: capture errors and failed reconciliation require review, not a larger crawler scope. The desktop launch path is Windows-first; portable helpers do not imply Linux/macOS GUI verification.

Synthetic tests cover identity/startup, URL redaction, reconciliation, object reuse and cross-process lock recovery. No public release includes a CRM login, live production fixture or a promise of absolute source completeness. The optional model-review monitor is opt-in and limited to content-free status packets.

---

## 中文

### 组件与执行顺序

1. 安装脚本使用已提交的 npm 锁文件，构建 TypeScript，再用合成输入检查厂商契约。
2. 启动脚本计算共享资源预算，分配实例目录与端口，启动 Electron；复制登录配置必须显式选择。
3. 桌面挂载控制界面和隔离的 CRM 面板，启动后端并发布新的就绪标记。HTTP 健康不等于浏览器已就绪。
4. 本机后端通过同步启动门先占位，核对目标身份，再把该身份传给采集引擎；核实之前不能写证据。
5. 引擎在导航前安装范围与隐私保护，采集七个适配器根标签，处理已支持的观察到的只读契约，数量对账后落盘终态。自动贸易/海关禁区独立于配置，不会被旧适配器扩大。
6. 证据库保存原件与出现引用、不可变案例身份、请求/响应回执、SHA-256 和会话范围。新会话追加证据而不替换原件。
7. 本地 Python 后处理单独验证提取、关系、时间线和交付，不能拿采集通过代替后处理通过。

### 不可破坏的不变量

- 单客户：明确数字 ID，在浏览器异步等待后、写入前重新核对；不能进入下一客户或共享修复队列。
- UI 补采：明确单客户绑定，新会话，不能复用旧会话模式或进入人工贸易通道。
- 单写者：同步启动门、内核锁、所有者匹配的占位；租约丢失必须失败关闭。
- 只读重放：保留已观察的客户、对象、文件夹范围，只修改已支持的分页字段，不提交业务写操作。
- 隐私：正文、配置、Cookie 和原始文件名留在本机，禁止上传外部 AI。
- 签名网址：回执脱敏已知认证参数及请求头，保留不涉及凭证的表示转换参数与对象身份。
- 内容与关系：哈希只证明字节一致；同一内容对象可以有多个出现和父级引用。
- 完成状态：已接受、运行、哈希、对账、测试通过各自独立；源缺口必须明确保留。
- 时间顺序：请求序列代表采集顺序，不自动等于业务时间或邮件线程顺序。

### 中断恢复

崩溃可留下活跃标记或未完成会话，恢复时必须先打开并重新确认标记指定的客户。活跃 PID 不能仅因占位时间长就被视为过期。内核锁辅助进程的管道断开（包括 Node 被强杀）会结束所有权；已获取的租约意外丢失时终止工作，不能退回无锁写者。不要手动删除活跃锁或改终态来强行复用。

续采复用核验成功的字节对象，同时保留新会话。UI 补采建立独立会话和范围，不能静默重写旧合格范围。会话原件是权威证据，`latest` 只是方便读取的指针。

### 适配与平台边界

OKKI 适配器仅含公开集成网址、选择器、标签与资源域名，不含租户人员/阶段筛选。厂商布局和未公开契约可能变化；错误与对账失败需要核查，不能通过扩大采集范围掩盖。桌面入口以 Windows 为主，底层代码可移植不等于其他平台 GUI 已验收。

合成测试覆盖身份、启动、脱敏、对账、复用与跨进程锁恢复；公开版本不附带 CRM 登录或真实生产样本，也不承诺源系统绝对完整。可选模型监控必须显式启用，仅接收不含正文的状态数据。
