# AmPull

**AmPull = Agent Multi-link Downloader（agent 多链接下载器）**

单文件、零依赖的命令行下载工具：把一个文件分成 16MB 的块，用 16 个连接同时下载。
中断后重跑同一条命令即可续传；模型仓库的分片（GGUF 等）可一条命令批量拉取。
为编码 agent（CLI 自动化）设计——JSON 收据、机器可读契约、状态单查，人也能直接用。

> 当前版本 v5.19（UA `ampull/5.19`）。

## 特性

- **多连接并发**：16 连接分块并发（`--conns` / `--chunk-mb` 可调），大文件提速明显
- **断点续传**：分块账本（`.part` / `.part.done` / `.part.lock`）自动记录进度，重跑原命令自动接上
- **批量能力**：多链接逐片下载 / `--manifest` 清单 / `--repo` HuggingFace 整仓（默认走 hf-mirror.com，国内全程直连）
- **镜像池**：`--mirror @auto` 自动学习历史镜像，主源挂了自动换源；批量模式每片独立建池，死站镜像整批跳过
- **代理智能分流**（v5.19 重做）：多源候选依次尝试——**扫描口**（本机端口**双跳探针**：
  CONNECT baidu 快筛 + CONNECT google 验证翻墙能力，"国内通墙外死"的口永不入选）
  > 环境变量 > 系统代理注册表 > 直连；扫描成功当日复用零重扫、失败当日退避、
  次日重扫（标记落 `ampull.state.json`）；agent 宿主注入的受限网关 env（国内可达
  墙外失败）只作原生首选的备用通道，不再让真梯子没出场机会；检测到 SOCKS 口/
  国内 HTTP 口自动提示开 HTTP/混合端口；局域网 IP 绑定的监听口也能发现。
  `--proxy` 可显式指定，`127.0.0.1` 自动绕过，`--no-proxy` 强制直连
- **内建重试**：网络抖动 / 5xx / 429 指数退避，agent 侧无需包一层重试
- **GitHub 404 分支回退**（v5.18）：归档链接 404 自动查默认分支并试 master/main/develop + codeload 直链，命中即改用新 URL（`gh-proxy` 等包装前缀保留）
- **双开保护**：同名文件已在别处下载中时报锁冲突并附处置建议
- **实时进度**：接收进度每 3 秒刷新（首块完成前也持续爬升）；真终端下另有 OSC 9;4 任务栏进度环；管道/重定向下自动切整行进度（5 秒一帧），`2> dl.log` 可实时 tail
- **零依赖**：仅 Python 标准库（3.8+），单文件即拷即用

## 快速开始

```bash
# 单文件（输出到当前目录）
python ampull.py "https://example.com/big.zip"

# 指定保存路径 + 32 连接 + SHA256 校验
python ampull.py "https://example.com/big.zip" "D:\下载\big.zip" -c 32 --sha256 abcd1234...

# 多个分片逐个下载（已完成自动跳过）
python ampull.py "https://host/model-00001.gguf" "https://host/model-00002.gguf" "D:\模型"

# 清单批量（每行：URL [sha256]）
python ampull.py --manifest 清单.txt "D:\模型"

# HuggingFace 整仓（国内默认走 hf-mirror.com 直连）
python ampull.py --repo unsloth/Qwen3.8-Flash-Next-GGUF --include "UD-IQ4_XS/*.gguf" "D:\模型"
```

## 下载中边下边用

```bash
python ampull.py --serve "D:\下载\big.zip" --port 0
# 起一个 127.0.0.1 回环 Range 服务（--port 0 随机端口），
# 播放器/解压器/别的下载器可直接按 Range 拉取尚未下完的文件
```

## 代理自动检测（v5.19）

**用户侧什么都不用做**：开着梯子（任意模式——系统代理/PAC/TUN/只开本地口）直接下载即可，AmPull 自动发现可用代理并当日复用。

- **发现方式**：本机端口**双跳探针**（CONNECT baidu 快筛 + CONNECT google 验证翻墙能力——"国内通墙外死"的口永不入选）> 环境变量 > 系统代理注册表 > 直连，依次尝试
- **当日复用**：扫描成功记 `scan_ok_date`（`ampull.state.json`，与 `ampull.py` 同目录），当天所有调用零重扫；失败当日退避；**次日自动重扫**（监听端口可能变）
- **提示解读**（stderr）：`检测到 SOCKS 口` → 请在梯子客户端开 HTTP/混合端口（SOCKS 不转发）；`检测到国内 HTTP 口` → 该口无翻墙能力，仅提示；`今日扫描未发现翻墙代理口` → 已当日退避直连
- **换梯子/换端口后**：`python ampull.py <链接> --reset-proxy` 强制重新扫描（平时不需要）
- agent 宿主注入的受限网关环境变量（国内可达、墙外失败）只作原生首选的备用，不再挡住真梯子

## 查询下载状态

```bash
python ampull.py --status "D:\下载\big.zip"          # 可同时给多个路径
```

返回 `active / resumable / done / foreign / missing` 状态与 `progress_pct`、实测速度（`active` 另带 `eta_s`）。
只读操作，可与下载进程并存。起步期即有 size/0%（账本先行）；无足够速度采样时 `eta=null`（诚实显示，不虚报）。

## agent 集成

```bash
python ampull.py --schema      # ~2k token 机器可读契约：全部参数 + JSON 字段 + error 词表
```

- 参数/字段以 `--schema` 输出为准（反射导出，与程序版本同步，不要凭记忆猜）
- `--json` 模式下 **stdout 最后一行是 JSON 收据**：`ok / path / bytes / seconds / sha256 / verified / error / warning`；
  人读信息恒走 stderr
- **进度输出自适应**（v5.15）：真终端 `\r` 单行刷新不变；stderr 接管道/重定向时自动切整行输出
  （5 秒一帧节流，首帧与 100% 必出）——想留档就 `2> dl.log` 后 `tail -f dl.log`，
  接 `| tail -6` 这类过滤器则要等进程结束才见输出（过滤器语义如此，非工具卡住）
- `verified` 三态：`null`＝未提供校验值无从核对、`true`＝与期望一致、`false`＝不符
- 失败先读 `error_msg`（人话结论）；`error` 是词表码（`http_403`、`incomplete`、`size_mismatch` 等，权威清单以 `--schema` 为准）
- 探测期失败时收据 `path` 为 null 属正常形态

运行历史（含镜像学习库）与程序同目录：`ampull.py` 旁的 `downloads.jsonl` / `mirrors.jsonl`（明文 JSON Lines，可删可迁——整文件夹拷走历史不丢；测试可用 `DL_JOURNAL` / `DL_MIRROR_HISTORY` 环境变量另指路径）。

## 常用参数速查

| 选项 | 用途 |
|---|---|
| `-c, --conns N` | 并发连接数（默认 16） |
| `--chunk-mb N` | 分块大小 MB（默认 16，1~512） |
| `--repo 仓库ID` | HuggingFace 整仓批量（默认 hf-mirror.com） |
| `--include` | 整仓通配过滤（需配 `--repo`） |
| `--hf-token TOKEN` | HF 私有仓库 |
| `--mirror @auto / @文件` | 镜像池：自动学习或显式列表 |
| `--as 名称` | 指定落盘文件名 |
| `--sha256 HEX` | 内容校验，不符即判废 |
| `--proxy URL` | 显式代理（默认自动检测） |
| `--manifest 文件` | 批量清单 |
| `--serve 目录` | 回环 Range 服务（下载中即可边下边用） |
| `--status 路径...` | 查询下载状态（只读） |
| `--clean 目录` | 清理断点/残留 |
| `--json` | stdout 输出 JSON 收据（agent 用） |
| `--schema` | 导出机器可读契约 |

完整参数以 `python ampull.py --help` / `--schema` 为准。

## 断点续传说明

- 中断（Ctrl+C / 断网 / 进程被杀）后**重跑原命令**即可自动续传，无需任何参数
- 下载中同目录会看到 `.part`（分块数据）、`.part.done`（进度账本）等文件——**不要手工删除**，它们就是断点本身
- 下载完成后自动归一为最终产物；不想要半成品时用 `--clean 目录` 收尾

## 目录内文件

| 文件 | 说明 |
|---|---|
| `ampull.py` | 主程序（单文件，v5.18） |
| `SKILL.md` | agent 调用契约（与本文件互补：这是给人看的，那是给 agent 看的） |
| `downloads.jsonl` / `mirrors.jsonl` | 运行历史 / 镜像学习库（程序自动维护） |

