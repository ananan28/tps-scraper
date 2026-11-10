# 美国房产 / 人口线索抓取器

三个独立抓取器 + 一套打包脚本，都在这一个仓库里。每个抓取器都是"引擎 + PySide6 界面 + build 脚本"三件套，打成免安装 EXE 后拷到别的电脑直接双击运行。

## 一、三个抓取器分别拿来干嘛

| | **TPS 抓取器** | **Realtor 经纪人抓取器** | **Homes 经纪人抓取器** |
|---|---|---|---|
| 目标站 | truepeoplesearch.com | realtor.com | homes.com |
| 抓什么 | 住址 → 姓名/年龄/电话 | 房地产经纪人 + 电话 | 房地产经纪人 + 电话 |
| 索引键 | 街道地址（美国约 19 万条） | **ZIP 码**（美国约 4 万个） | **地理 slug**：市 / 县 / 州（不认 ZIP） |
| 反爬 | Cloudflare / Turnstile | **Kasada** | **Akamai Bot Manager** |
| 过站方式 | Playwright + stealth | nodriver | nodriver |
| 验证码 | **必须**（2Captcha，关了就 0 号码） | 不需要 | 不需要 |
| 代理 | 需要住宅代理才能上量 | 不需要，本机直连即可 | 不需要，本机直连即可 |
| 要不要进详情页 | 要 | **要**（列表页没有号码） | **不要**（列表页自带号码） |
| 人均成本 | 每地址 1-2MB + 可能一次解码 | 列表 12 人/页 + 每人 1 个约 0.47MB 详情页 | **1 个请求 = 48 人**，最便宜 |
| 翻页 | 无（一个地址一屏） | `/realestateagents/zip/{ZIP}/pg-{N}` | `/real-estate-agents/{slug}/p{N}/` |
| 并发 | 15-20（32GB 内存机） | 直连 ≤6 页，超了会被临时硬封 | 实测 4 页很稳，更多待测 |
| 性别筛选 | 界面可调（默认留男+中性+未知） | 只按姓名剔除女性，不可关 | 勾选框「剔除女性姓名」，默认开 |
| 年龄 | 50-85 可调 | 不估龄 | 无年龄字段 |
| 源码 | `engine_tps_v182.py` + `app_ui.py` | `realtor_engine.py` + `app_realtor.py` | `homes_engine.py` + `app_homes.py` |
| 打包 | `build_ui.py` / `build.py` | `build_realtor.py` | `build_homes.py` |
| 库文件 | `tps_data.db` | `realtor_data.db` | `homes_data.db` |
| 实测速度 | 取决于代理与解码 | 受 Kasada 限制，慢但稳 | 本机直连 4 页：**328–368 人/分** |

共用的东西：`engine_core.py`（姓名性别判定 + 姓名库 + TPS 引擎内核）、`user_names.json`（45,443 条姓名的性别库，本地生成，**不入库**）。

## 二、交付出去的东西是什么

打包产物都落在各自 `dist_*` 目录，再拷到桌面顶层变成可双击的成品文件夹。每个文件夹都是**自包含**的：EXE + `_internal` + `user_names.json` + 自己的 `.db`，整文件夹拷 U 盘就能在别的机器跑。

| 桌面文件夹 | 是什么 |
|---|---|
| `Homes经纪人抓取器\` | homes.com 抓取器成品（最新，带兜底页校验） |
| `Realtor经纪人抓取器\` | realtor.com 抓取器成品 |
| `TPS_v1.8.2_UI\` | **TPS 主用版**：1.8.2 引擎 + 可调阈值界面，内置 Chromium（`ms-playwright`） |
| `TPS_v1.8.2\` `TPS_v2.1.6\` `TPS_v2.1.7\` | TPS 历史版本，留着对比行为差异用 |
| `TPS抓取器-macOS\` | mac 端打包用的 zip（`.app` 必须在 mac 上跑 `打包Mac.command` 生成，Windows 交叉打不出来） |
| `phonedb\` | 号码库数据目录（不是软件） |
| `tps-scraper\` | **就是这个仓库**：全部源码 + 本机正在用的三个库 + 备份 |

## 三、绝不入库的东西

`.gitignore` 已经把这几类挡掉了，别手动 `git add -f`：

- 任何 `.db` / `.db-wal` / `.csv` 导出 —— 里面是采集到的个人信息
- `*_config.json` —— 界面保存的代理账号密码、验证码 Key
- `user_names.json` —— 本地生成的姓名库
- `backup_*/` `dist*/` `build*/` `logs/` `*.exe` `_internal/` `ms-playwright/`
- `test_*.py` `_*.py` —— 一次性验证脚本

代理、2Captcha Key 一律在界面里填，代码和文档里不出现真实账号。

## 四、部署步骤（以 homes 为例）

1. 装依赖：`pip install -r requirements.txt`
2. 打包：`python build_homes.py`（约 80 秒，产物在 `dist_homes\homes经纪人抓取器\`）
3. 把该文件夹整个拷到目标机（或 U 盘中转），双击 `Homes经纪人抓取器.exe`
4. 首次运行会在 EXE 同级自动建库；界面里设并发/等待，导入地区或点「填入 50 个州」
5. 勾选「自动发现下级地区」→ 引擎自己从州扒到县、市、相邻市
6. 抓完点「导出 CSV」，输出在 EXE 同级

TPS 还多两步：填代理 + 2Captcha Key，并确认 `ms-playwright` 里的 Chromium 跟着拷过去了。

## 五、消耗估算（TPS）

### CapSolver / 2Captcha
- 每次验证约 $0.003，1000 次约 $3
- 19 万地址约 $570（假设 100% 需要验证）

### 住宅代理流量
- 每个地址约 1-2MB，19 万地址约 200-400GB
- 按 $0.5/GB 算约 $100-200

### 电费
- 每台约 200W；20 台 × 24h × 7 天 = 672kWh，约 $67/周

realtor 和 homes 都不吃这两项：走本机直连、无验证码。代价是慢 —— 已经确定"慢点就慢点，省钱优先"。

## 六、故障处理与已知坑

- 浏览器崩溃自动重启、断网自动重试、解码失败重试 3 次
- **同一 IP 反复起停会被 Akamai / Kasada 越掐越紧**：一次长跑很顺，跑跑停停就掉速（homes 实测从 368 人/分 掉到 7 人/分）。正式跑请单开一个长会话。
- 翻页 token 各站不同：realtor 是 `/pg-N`，homes 是 `/pN/`；`?page=N` 一律被忽略并返回第一页，容易误判成"翻页坏了"
- homes 的纯数字路径（如 `/33137/`）**不返回 404**，而是返回一个 24 人的通用搜索页；引擎按 canonical 识别这种兜底页并标为「无效地区」，不入库
- realtor 直连并发超过 6 页会被硬封十几分钟
- 拷贝正在使用的 SQLite 库之前**必须先 checkpoint**（`PRAGMA wal_checkpoint(TRUNCATE)`），只拷 `.db` 会丢掉还在 `-wal` 里的行
- TPS 抓到 0 号码基本只有一个原因：2Captcha 没额度，不是命中率低

## 七、多电脑部署

1. 打包 EXE 后整个文件夹拷到每台电脑
2. 每台分不同地址段/地区段，避免重复劳动
3. 或动态领任务：做完一批领下一批
4. 参考：19 万地址 ÷ 20 台 ≈ 每台 1 万地址
