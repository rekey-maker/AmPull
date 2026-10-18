---
name: ampull
description: 调用本机 AmPull 多连接下载器执行文件下载。Use when the user asks to 下载/下到/保存一个 URL 资源或文件，或提到下载器、镜像下载、HuggingFace/魔搭/TUNA 源、断点续传、安装包下载。Do not use for 网页浏览、页面抓取或内容分析。
---

# AmPull 调用契约

本机已有 AmPull（单文件零依赖多连接下载器：分块并发 + 断点续传 + 镜像池 + 代理自动检测），agent 直接以 CLI 调用。用户表达下载意图时本技能生效，**不要再用 curl/浏览器下载器替代**。

**大小豁免（2026-10-17 用户裁定："超过 5-10M 的都走 ampull"，取保守下界）**：预计超过 5 MB 的下载一律走 AmPull；只有明确小于 ~5 MB 的小文件才允许 `curl -x <代理>` 直取。大小未知时按较大处理（先探测 Content-Length 或直接走 AmPull）。API 查询、校验用 JSON 这类"非下载产物"的请求不受此限，仍用 curl。

## 工具位置

- 主用：**与本文件（SKILL.md）同目录的 `ampull.py`**——用本文件被加载时的实际目录拼出脚本绝对路径。不要凭记忆硬编码历史路径：目录可能整体迁移过（此前就发生过从旧目录迁走、agent 按老路径调用报 can't open file）
- 备用（哈希一致的同步副本）：`D:\test\dl\ampull.py`。主用不存在时按此顺序试；都找不到就全盘文件搜索 `ampull.py`（文件名唯一，命中即用）
- 运行：`python <脚本路径> ...`（零第三方依赖）
- **技能目录部署形态**：SKILL.md 与 `ampull.py` 成对放进任意 agent 技能目录（如 `~/.zcode/skills/ampull/`、`~/.trae-cn/skills/ampull/`、`~/.workbuddy/skills/ampull/`）即可被 `/ampull` 或下载意图自动触发——安装就是**把这两个文件拷进当前宿主的技能目录**（用户手动或让 agent 执行均可）；源目录 `Desktop/AmPull/` 是唯一事实源，更新后把新版两文件重新拷贝即可

## 动手前先读契约（红线）

**每次会话首次调用前先跑 `--schema`，不要凭记忆猜参数**：

```
python "<ampull.py 所在目录>/ampull.py" --schema
```

返回 ~2k token 的 JSON 契约（全部参数 + 错误词表，反射导出、与程序版本同步）。文档/记忆里的参数描述可能过期，一律以 schema 输出为准。

## 标准调用流程

1. `--schema` 拿契约
2. 构造命令：`python <脚本> <URL> <输出目录> [选项] --json`
3. 解析 **stdout 最后一行 JSON 收据**（人读信息走 stderr）
4. 核对收据：`ok`、`path`、`bytes`、`sha256`、`warning`；`verified` 三态——null=未提供校验值无从核对、true=与期望一致、false=不符；失败看 `error`（词表码）+ `error_msg`（人话一行）

**前台非阻塞默认（v5.18 约定）**：下载命令**一律前台非阻塞跑**（`--json`），**预计超 10 分钟也一样**——宿主命令卡片实时流式显示进度条（卡片默认折叠，点开即见百分比/速度/剩余时间；v5.13 起画「接收进度」：1MB 步进计数，首块完成前也持续爬升、不再 0.0% 定格；失败重试瞬间百分比可能小幅回跳属正常），用户全程可见；agent 启动后即返回做别的事，不守着。预计超 10 分钟的下载：前 10 分钟卡片流式显示，到时平台自动转后台（进程不死，完成推送照达）。

**stderr 进度自适应（v5.15）**：进度帧按 stderr 是否 tty 自动分流——真终端保持 `\r` 单行覆盖刷新；**agent 捕获/重定向（isatty=False）时自动切整行输出，5 秒一帧节流（首帧与 100% 必出）**，所以 agent 捕到的 stderr 是稀疏可解析的行序列而非 `\r` 糊线，想给用户展示中段进度可直接取最后几帧。注意两点：① 不要用 `| tail -N` 这类过滤器套在 stderr 上——过滤器要等 EOF 才吐「最后 N 行」，表现为全程静默、进程结束才出几行（v5.15 前的历史坑，当时被误判为缓冲问题）；想边跑边看就 `2> dl.log` 后另起 `tail -f dl.log`。② stdout 收据语义不受影响，`--json` 下照旧只读最后一行。

**输出编码（v5.17）**：管道/重定向（非 tty）下 stdout/stderr 字节恒 UTF-8，不随 `PYTHONUTF8`/`PYTHONIOENCODING` 漂移——agent 捕获层按 UTF-8 解码即可（Windows 默认 GBK 控制台直接管道捕获中文也不再乱码）；真终端跟随控制台代码页不变；JSON 收据恒纯 ASCII，任何解码下安全。

**完成靠推送不靠轮询**：任务退出通知就是完成信号（前台强转后台后也一样，正常结束和进程暴毙都会退出），agent 被宿主自动唤醒，**不做任何 sleep 轮询/阻塞等待**。被唤醒后先读 stdout 收据核对 `ok/error/warning` 再向用户回报；`resumable`＝进程死了没下完，重跑原命令自动续传。注意强转后台后命令卡片不流式（只显示 job id 与日志路径）：用户问进度时**单查一次** `--status <产物路径>`（<1 秒，可一次给多个路径）报数（收据含 progress_pct 与实测速度 speed_bytes_s，active 另带 eta_s——起步正增益采样段<2 时 eta=null 属正常，speed 照报）；agent 有其他任务间隙时可顺手播报一次，无间隙就等推送；明显超过预期时长仍无推送时对账。--status 单查永远安全，轮询循环永远禁止。

## 常用选项速查（细节以 --schema 为准）

| 选项 | 用途 |
|---|---|
| `--repo 仓库ID` | **HF 整仓批量。默认源就是 hf-mirror.com，国内全程直连、无需开代理**（不要默认它在直连 huggingface.co 而做错误的网络预判） |
| `--status 路径 [路径...]` | 查询下载状态/进度（active/resumable/done/foreign/missing + speed_bytes_s、active 另带 eta_s），后台下载时用它替代盯日志；起步期即有 size/0%（账本先行，不再闭门羹） |
| `--conns N` / `--chunk-mb N` | 并发连接数 / 分块大小（大文件提速） |
| `--mirror @auto` 或 `--mirror @文件` | 镜像池：自动学习历史镜像或显式列表（仅 http(s)），主源挂自动换源；批量模式每片独立建池，死站镜像整批跳过 |
| `--as 名称` | 指定落盘文件名（默认从 URL 取） |
| `--sha256 <hex>` | 内容校验，不符即判废 |
| `--hf-token <token>` | HuggingFace 私有仓库 |
| `--proxy URL` | 显式代理（默认自动检测环境） |
| `--manifest 文件` | 批量下载清单 |
| `--clean 目录` | 清理断点/残留（收尾用） |

## 代理自动检测（v5.19 重做——agent 无需手动配代理）

- **多源候选依次尝试**：扫描口（本机端口**双跳探针**：CONNECT baidu 快筛 + CONNECT google 验证境外连通性——"仅境内可达"的口永不入选）> 环境变量 > 系统代理注册表 > 直连
- **当日标记**：扫描成功记 `scan_ok_date`（`ampull.state.json`，与脚本同目录），当日全部调用**零重扫**直接复用；失败当日退避；次日自动重扫（监听端口可能变）
- **对 agent 的含义**：github/huggingface/raw.githubusercontent 等境外下载**直接下发即可**，代理由 AmPull 自动发现并当日复用，不要手动预判"没代理下不了"；发现失败时 stderr 有诚实归因（如"今日扫描未发现境外代理口"）
- 检测到 SOCKS 口/国内 HTTP 口会打 stderr 提示（SOCKS 不转发——请让用户在代理客户端开 HTTP/混合口）；`--proxy` 显式指定恒优先；`--no-proxy` 强制直连

## 关键行为（不要重复造轮子）

- **断点续传自动**：中断后重跑原命令自动接上，无需参数；不要自己删 .part/.part.done
- **代理自动检测（v5.19 分类扫描）**：环境变量/系统代理/扫描口多源候选自动生效（详见上方"代理自动检测"节）。国内直连 huggingface.co、raw.githubusercontent 等境内直连不可达源不要先判"下不了"，直接调它试
- **双开保护**：同名文件已在别处下载中会报锁冲突并附处置建议，不要强删锁文件（锁机制依赖 Windows msvcrt；POSIX 下仅标记文件保护，超 10 分钟的长下载可能被 --clean 误判为残片）
- **loopback 直连**：127.0.0.1/localhost 自动绕过代理，本地桩测试可直接用
- **重试内建**：网络抖动/5xx/429 退避重试已内建；agent 侧失败不要立刻原样重跑，先读 error_msg
- **GitHub 404 分支回退**（v5.18）：`github.com/.../archive/...` 归档链接撞 404（默认分支不叫 main 之类的分支名问题）自动查仓库默认分支并试 master/main/develop + codeload 直链，命中即改用新 URL 继续下载（stderr 打印改用后的链接，收据 `warning=gh_branch_fallback`）——agent 无需为分支名再猜一轮；gh-proxy 等包装前缀保留。仅 404 触发，403/401 是鉴权问题不回退

## 失败处置

- `error_msg` 是人话结论，先读它；词表码语义见 --schema（`http_403`、`incomplete`、`size_mismatch`、`etag_mismatch` 等；`conflict_rename` 不是错误码——目标名被占时静默改名另存，ok=true）
- 探测期失败（最常见形态）收据 `path` 为 null 属正常
- 重试前先分类：网络类可换参重试；校验类（size/etag 不符、响应矛盾）重试无意义，换源或换 URL
- 更完整的运行历史、挂账与踩坑记录见设计文档《AmPull-Agent化改造方案.md》（`AMPull-backup/` 历史归档子目录，与 SKILL.md 同目录树下）

## 用户偏好

- 测试性下载放临时目录（如 E:\dltest），完成后清理，只留用户要的产物
- 真实下载优先于模拟桩；大文件先问用户，不要自作主张下几个 GB
