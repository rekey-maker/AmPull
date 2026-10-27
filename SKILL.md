---
name: ampull
description: 调用本机 AmPull 多连接下载器执行文件下载。Use when the user asks to 下载/下到/保存一个 URL 资源或文件，或提到下载器、镜像下载、HuggingFace/魔搭/TUNA 源、断点续传、安装包下载；也用于 m3u8/HLS 视频流下载（含 ffmpeg 依赖的处理指引）。
---

# AmPull 调用契约

本机已有 AmPull（单文件零依赖多连接下载器：分块并发 + 断点续传 + 镜像池 + 代理自动检测），agent 直接以 CLI 调用。用户表达下载意图时本技能生效，**不要再用 curl/浏览器下载器替代**。

**大小豁免（2026-10-17 用户裁定："超过 5-10M 的都走 ampull"，取保守下界）**：预计超过 5 MB 的下载一律走 AmPull；只有明确小于 ~5 MB 的小文件才允许 `curl -x <代理>` 直取。大小未知时按较大处理（先探测 Content-Length 或直接走 AmPull）。API 查询、校验用 JSON 这类"非下载产物"的请求不受此限，仍用 curl。

## 工具位置

- 主用：**与本文件（SKILL.md）同目录的 `ampull.py`**——用本文件被加载时的实际目录拼出脚本绝对路径。不要凭记忆硬编码历史路径：目录可能整体迁移过（此前就发生过从旧目录迁走、agent 按老路径调用报 can't open file）
- 备用（哈希一致的同步副本）：D 盘 `test\dl\ampull.py`。主用不存在时按此顺序试；都找不到就全盘文件搜索 `ampull.py`（文件名唯一，命中即用）
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

## AmPull MCP（v5.21，优先用它）客户端里能直接看到 13 个工具，**不用拼命令行**；MCP 与 CLI 同源（内部就是子进程跑
CLI 取 `--json` 收据），参数语义完全一致。

| 工具 | 干什么 | 关键提示 |
|---|---|---|
| `ampull_download` | 单/多 URL 下载 | `out_dir` **总按目录处理**（自动创建）；不给则落 `<程序目录>/downloads/`；文件名用 `as_name`；大文件 `wait=false`；`reset_proxy=true` 清代理当日标记 |
| `ampull_manifest` | 清单批量（每行 `URL [sha256]`） | 适合几十上百个文件 |
| `ampull_repo` | HuggingFace 整仓 | **不给 `include` 会整仓全下**（几百 GB），先限定 |
| `ampull_hls` | HLS/m3u8 下载 | `quality` 数字按高度、不达标按宽度兜底；长任务 `wait=false` |
| `ampull_hls_probe` | **只看不下载**：档位/段数/时长/加密/可下载性 | 下载前先探，选档不靠猜 |
| `ampull_job` | 查异步任务状态与收据 | 不给 id 列全部（含历史目录）；`prune_days=N` 清 N 天前已结束的任务目录 |
| `ampull_stop` | 停本进程发起的任务（含 serve） | 断点保留，重跑即续 |
| `ampull_tools` | ffmpeg 自举：list/path/install/remove | 先复用已有；`install` 需 `yes=true`（默认 dry-run） |
| `ampull_status` | 产物状态（只读） | 支持多路径 |
| `ampull_clean` | 残留清理 | **默认只列不删**；`yes=true` 才删，`all=true` 连断点 |
| `ampull_journal` | 台账最近 N 条（只读） | 认 `DL_JOURNAL`；排查"上次下到哪/报了什么错" |
| `ampull_serve` | 回环 Range 服务（后台） | 返回 url，播放器可边下边用；`ampull_stop` 收 |
| `ampull_schema` | 完整契约 | 与 CLI `--schema` 同一份（版本也从它取，不硬编码） |

**已知限制（用之前知道更好）**：
- MCP 服务**单线程串行**处理请求：同步等一个大下载时，同一连接上的后续调用会排队。
  所以长任务一律 `wait=false`，让 `ampull_job` 能在下载进行中回话。
- 客户端"取消调用"**不会**杀掉已起的子进程（JSON-RPC 没有取消语义）；要停用 `ampull_stop`。
- 没有 MCP progress 通知（`notifications/progress` 未实现）：进度看 `ampull_status` 或 job 的 `out.log`。
- `jobs/` 会随任务累积：定期 `ampull_job(prune_days=7)`。

**长任务纪律**：几分钟以上的下载一律 `wait=false` → 拿 `job` → 用 `ampull_job` 查；
不要用同步模式硬等（客户端工具调用有超时）。收据靠 `jobs/<id>/out.json` 落盘，
MCP 重启后仍能查。

**下载前先探**：HLS 用 `ampull_hls_probe(url, quality)`；它会回档位表、选中档、段数、
时长、加密方式、`downloadable`。用户问"有没有 720"就靠它答（记得宽度/高度口径）。

## HLS / m3u8 视频流（v5.20 起；以 `--schema` 是否含 `hls` 为准）

没有 MCP 时用 CLI：

**先用 `--schema` 确认本机版本有 `hls` 子命令**。没有就说明当前版本不支持 HLS，按下面「本机版本无 hls 时」降级，**不要自己拼 m3u8 分片清单去骗 `--manifest`**（AES-128 的段下回来是密文，白干）。

```bash
python <脚本> hls "<m3u8-url>" <输出目录> --quality best --workers 8 --json
```

- URL 可以是主列表（多档码率）或子列表；`--quality` 支持 `best|worst|1080|720|480`
  - 数字按**高度**匹配（≥ 目标里最接近的档）；高度都不达标时**按宽度**再匹配一次
    （站方常以宽度标"720"，例如 `RESOLUTION=720x406` 其实是 720 宽 / 406 高），
    此时收据 `warning=quality_matched_by_width`；宽高都达不到才退最高档（`quality_fallback`）
  - 所以用户问"有没有 720"时：先看收据 `hls.quality`（`720p`/`406p`）与 `variant_url`，
    再如实说明"这档是 720 宽、406 高"，别直接说没有
- 收据多一个 `hls` 子对象：`segments_total/done/failed`、`encrypted`、`key_count`、`muxed`、`duration_playlist`、`duration_media`
- **进度靠推送**，与普通下载同规矩；进度帧形如 `seg 320/924 · 88.4 MB · 1.9 MB/s`

### 收据 warning 怎么处理

| warning / error | agent 动作 |
|---|---|
| `ffmpeg_missing` | 走下面「ffmpeg 依赖」流程（**必须先问用户**），装完重跑原命令；用户拒绝就接受 `.ts` 产物 |
| `mux_skipped` | 成品是 `.ts`，数据没丢（半截 mp4 会自动清掉）；告知用户可直接用播放器打开，或修好 ffmpeg 后重跑封装 |
| `playlist_refreshed` | 分片签名过期已自动重拉一次，无需动作 |
| `duration_mismatch` | 收据里两个时长都给了，如实报给用户，别当成失败 |
| `hls_drm` | **真 DRM（SAMPLE-AES/Widevine），不支持**；直接告知，不要重试、不要换工具绕 |
| `hls_live_unsupported` | 直播流，一期不支持；如实说明 |

### ffmpeg 依赖：先复用、没有才装，装前必须用户同意

Windows **不自带** ffmpeg，它是第三方工具。缺了会走 `.ts` 降级，产物照样能播。
**新机器/首次遇到 `ffmpeg_missing` 时的标准流程**：

1. **先查**：`python <脚本> tools path ffmpeg --json`（七条通道，见下）。命中就直接重跑，
   **不装**——多数机器上已经有了（winget/choco/scoop、共享位、别的程序自带）。
2. 确实没有 → **问用户**：需要下载 ffmpeg（官方构建，约 **180~200MB**），是否允许？
3. 用户同意 → `python <脚本> tools install ffmpeg --yes`
   - 不带 `--yes` 只打印计划：下载源 / 体积 / sha256 / 落盘路径
   - **自动安装到 `<程序目录>/tools/ffmpeg/<版本>/bin/ffmpeg.exe`**（相对程序、便携；
     不写用户目录、不改 PATH、不写 System32；`tools/` 已进 .gitignore）
   - 自动按平台选构建（Windows: BtbN win64-gpl zip / Linux: linux64-gpl tar.xz），
     sha256 取自 GitHub Release 的 digest，装完跑 `-version` 烟测，失败自动回滚不留垃圾
4. 装完重跑原 `hls` 命令（断点自动跳过已下分片——`ffmpeg_missing`/`mux_skipped` 时
   断点目录**刻意保留**，所以重跑只做封装、不重下）
5. 用户拒绝 → 接受 `.ts` 产物，别反复劝

**macOS**：`tools install` 没有官方构建，会提示 `brew install ffmpeg`（装完在
`/opt/homebrew/bin`，自动发现）；也可以用 `AMPULL_FFMPEG=/opt/homebrew/bin/ffmpeg` 显式指定。

**禁止**：自己 `curl` 一个 ffmpeg.exe、手动改系统 PATH、往 System32 写文件、未经同意静默下载几十 MB。

**先查再用**：`python <脚本> tools path ffmpeg --json` 会按七条通道找——
`--ffmpeg` → `AMPULL_FFMPEG` → `AMPULL_FFMPEG_DIRS` → 程序目录 `tools/` →
**共享安装位**（`C:\ffmpeg\bin`、`<盘>:\tools\ffmpeg\bin` 等）→ PATH →
**别处自带的（有限扫描 + `-version` 烟测）**。
也就是说：用户机器上如果已经装了（winget/choco/scoop/系统 PATH）、放在共享位、或者别的
程序里带了一份，一般都能直接命中，**不必急着装**。收据 `source` 告诉你是哪条通道：
`explicit|env|env_dir|local|shared|path|discovered`。

`python <脚本> tools list --scan` 会列出扫到的候选（未探活），判断"能不能借"用这个。

**多工具共用一份 ffmpeg 时不要复制**：`tools install` 现在**先复用后安装**——
只要发现任何可用 ffmpeg 就直接返回 `source=reused`、`bytes=0`，不下载第二份；
确实要装进程序目录才加 `--force`（会写 `tools/ffmpeg/<版本>/bin`，不碰用户目录、不改 PATH）。

想手动指别处那份（比如 OBS/剪辑软件目录里的）：加环境变量
`AMPULL_FFMPEG_DIRS=D:\SomeApp\bin`（多个用 `;` 分隔）或直接用 `--ffmpeg <路径>`。

### 本机版本无 hls 时怎么办

- 有 ffmpeg：让 ffmpeg 直拉 `ffmpeg -user_agent "Mozilla/5.0" -i "<m3u8>" -c copy -bsf:a aac_adtstoasc out.mp4`（单连接顺序拉，慢但能用）
- 无 ffmpeg：如实告诉用户"当前 AmPull 版本不支持 HLS，本机也没有 ffmpeg"，给两条路（升级 AmPull / 装 ffmpeg），**不要假装能下**
- 无论哪条，都别把 924 行逐片清单丢进 `--manifest` 当"支持 HLS"——那条路只对**明文 TS** 成立

## 失败处置

- `error_msg` 是人话结论，先读它；词表码语义见 --schema（`http_403`、`incomplete`、`size_mismatch`、`etag_mismatch` 等；`conflict_rename` 不是错误码——目标名被占时静默改名另存，ok=true）
- 探测期失败（最常见形态）收据 `path` 为 null 属正常
- 重试前先分类：网络类可换参重试；校验类（size/etag 不符、响应矛盾）重试无意义，换源或换 URL
- 更完整的运行历史、挂账与踩坑记录见设计文档《AmPull-Agent化改造方案.md》（`AMPull-backup/` 历史归档子目录，与 SKILL.md 同目录树下）

## 用户偏好

- 测试性下载放临时目录（如 d:\dltest），完成后清理，只留用户要的产物
- 真实下载优先于模拟桩；大文件先问用户，不要自作主张下几个 GB
