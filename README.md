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
| 源码 | `engine_core.py` + `app_ui.py` | `realtor_engine.py` + `app_realtor.py` | `homes_engine.py` + `app_homes.py` |
| 打包 | `build_ui.py` / `build.py` | `build_realtor.py` | `build_homes.py` |
| 库文件 | `tps_data.db` | `realtor_data.db` | `homes_data.db` |
| 实测速度 | 取决于代理与解码 | 受 Kasada 限制，慢但稳 | 本机直连 4 页：**328–368 人/分** |

共用的东西：`engine_core.py`（姓名性别判定 + 姓名库 + TPS 引擎内核，homes / realtor 的引擎也 import 它做性别过滤）、`user_names.json`（45,443 条姓名的性别库，本地生成，**不入库**）。

## 二、仓库里每个文件拿来干嘛

**正在用的（改这些）**

| 文件 | 用途 |
|---|---|
| `engine_core.py` | TPS 引擎内核 + 姓名性别判定；homes / realtor 引擎共用它的性别过滤 |
| `app_ui.py` | TPS 主用版界面（带可调阈值），入口就是它 |
| `realtor_engine.py` / `app_realtor.py` | realtor.com 引擎 + 界面 |
| `homes_engine.py` / `app_homes.py` | homes.com 引擎 + 界面 |
| `browser_path.py` | 浏览器定位，三级顺序：界面手选的路径 → 本机装的 Chrome/Edge/Brave（注册表 App Paths → 各盘符 Program Files → `C:\Users\*` 所有用户配置 → PATH）→ 交付文件夹自带的 `browser\chrome-win64\chrome.exe`。nodriver 自带的探测只翻 PROGRAMFILES 环境变量，换台机器就报 "could not find chrome browser binary"，所以两个 nodriver 引擎都显式传 `browser_executable_path`。用自带那份时自动加 `--no-sandbox`（非标准安装路径下 Chromium 沙箱会报 "Sandbox cannot access executable"），本机真 Chrome 不动 |
| `bundle_browser.py` | 打包时把本机 `ms-playwright` 里那份 Chromium 塞进交付文件夹的 `browser\`（同盘优先硬链接，不额外占空间；目标机没装任何浏览器也能跑）。源在 `C:\Users\<你>\AppData\Local\ms-playwright\chromium-*\chrome-win64`，没有就先 `python -m playwright install chromium` |
| `build_ui.py` / `build_realtor.py` / `build_homes.py` | 各自打 Windows 免安装 EXE，产物落在 `dist_*`；后两个还会调 `bundle_browser` 带上一份 Chromium，并生成 `启动*.bat` + `浏览器自检.bat` |
| `build_mac.py` + `打包Mac.command` + `.github/workflows/build-mac.yml` | mac 端 `.app`：Windows 交叉打不出来，靠 GitHub Actions 在 mac runner 上构建 |
| `copy_to_usb.py` | 把 `TPS_v1.8.2_UI` 整包拷 U 盘，数据库走 SQLite backup API 做一致快照（不会拷到半截的 `-wal`） |
| `requirements.txt` | 依赖清单 |

**留着但对比的旧版（别改，除非要复现行为差异）**

| 文件 | 用途 |
|---|---|
| `engine_tps.py` / `app_tps.py` / `build.py` | TPS v3.0.0 试验版，后来回到 1.8.2 内核，现由 `engine_core.py` + `app_ui.py` 取代 |
| `app_tps_v182.py` | 更早的 1.8.2 简易界面，同样已被 `app_ui.py` 取代 |
| `CHANGELOG_v3.0.0.md` | v3.0.0 那次改了什么，留档 |

只有 `engine_core.py` / `app_ui.py` / `realtor_engine.py` / `app_realtor.py` / `homes_engine.py` / `app_homes.py` 是日常在改的；其余都在仓库历史里躺着，不用拷到别的机器。

## 三、交付出去的东西是什么

打包产物都落在各自 `dist_*` 目录，再拷到桌面顶层变成可双击的成品文件夹。每个文件夹都是**自包含**的：EXE + `_internal` + `user_names.json` + 自己的 `.db`
+ `browser\`（homes / realtor 自带的那份 Chromium，约 447 MB），整文件夹拷 U 盘就能在别的机器跑，
目标机一个浏览器都没装也不影响。

| 桌面文件夹 | 是什么 |
|---|---|
| `Homes经纪人抓取器\` | homes.com 抓取器成品（最新，带兜底页校验 + 自带 Chromium + `浏览器自检.bat`） |
| `Realtor经纪人抓取器\` | realtor.com 抓取器成品（同样自带 Chromium） |
| `TPS_v1.8.2_UI\` | **TPS 唯一在用版**：1.8.2 引擎 + 可调阈值界面，内置 Chromium（`ms-playwright`） |
| `phonedb\` | 号码库数据目录（不是软件），源码在另一个仓库 [phonedb](https://github.com/ananan28/phonedb) |
| `tps-scraper\` | **就是这个仓库**：全部源码 |

历史打包件 `TPS_v1.8.2\` `TPS_v2.1.6\` `TPS_v2.1.7\` `TPS抓取器-macOS\` 已于 2026-11-10 清掉（里面只有几十条测试数据，EXE 可用 `build*.py` 重打，mac 端由 GitHub Actions 自动出 `.app`）。桌面只留 `TPS_v1.8.2_UI\` 一份成品。

## 四、绝不入库的东西

`.gitignore` 已经把这几类挡掉了，别手动 `git add -f`：

- 任何 `.db` / `.db-wal` / `.csv` 导出 —— 里面是采集到的个人信息
- `*_config.json` —— 界面保存的代理账号密码、验证码 Key
- `user_names.json` —— 本地生成的姓名库
- `backup_*/` `dist*/` `build*/` `logs/` `*.exe` `_internal/` `ms-playwright/`
- `test_*.py` `_*.py` —— 一次性验证脚本

代理、2Captcha Key 一律在界面里填，代码和文档里不出现真实账号。

## 五、部署步骤（以 homes 为例）

1. 装依赖：`pip install -r requirements.txt`
2. 打包：`python build_homes.py`（约 80 秒，产物在 `dist_homes\homes经纪人抓取器\`）
3. 把该文件夹整个拷到目标机（或 U 盘中转），双击 `Homes经纪人抓取器.exe`
   - 换机器起不了浏览器时，双击同目录的 `浏览器自检.bat`，它会生成 `browser_check.txt`，
     里面写清楚「实际会用哪个、本机装了几个、自带的那份在不在、查过多少个位置」
4. 首次运行会在 EXE 同级自动建库；界面里设并发/等待，导入地区或点「填入 50 个州」
5. 勾选「自动发现下级地区」→ 引擎自己从州扒到县、市、相邻市
6. 抓完点「导出 CSV」，输出在 EXE 同级

TPS 还多两步：填代理 + 2Captcha Key，并确认 `ms-playwright` 里的 Chromium 跟着拷过去了。

## 六、消耗估算（TPS）

### CapSolver / 2Captcha
- 每次验证约 $0.003，1000 次约 $3
- 19 万地址约 $570（假设 100% 需要验证）

### 住宅代理流量
- 每个地址约 1-2MB，19 万地址约 200-400GB
- 按 $0.5/GB 算约 $100-200

### 电费
- 每台约 200W；20 台 × 24h × 7 天 = 672kWh，约 $67/周

realtor 走本机直连、无验证码。代价是慢 —— 已经确定"慢点就慢点，省钱优先"。
**homes 从 2026-11-10 起不再直连**：本机（泰国出口）打 homes.com 任何路径都是边缘 403 `Access Denied`，
连 `robots.txt` 都拒；换美国住宅 IP 才通。所以 homes 界面新增「代理出口 / 密码」两栏，
日志启动时会打印当前出口 IP 供核对。

## 七、故障处理与已知坑

- 浏览器崩溃自动重启、断网自动重试、解码失败重试 3 次
- **homes 目录页会先回一张 2.4KB 的 "Powered and protected by Privacy" 挑战页**（`chlgeId` + 自带
  `location.reload`），它自己 4~25 秒后变成 18 万字节的真页面。旧版把这张壳页当"被拦"，每试一次就
  把退避往上顶，而恢复计数只有"成功一页"才降 —— 被拦时永远没有成功，于是冷却顶死在 600 秒、
  所有标签页到点一起冲，看起来就是"跑到七万多一直退避进不去"。新版：挑战页原地等、不 trip；
  只有真 `Access Denied` 才退避；冷却按 180 秒自动衰减；并发请求加 0~3 秒抖动错开；
  起跑前先打一次首页拿 cookie（实测冷启动直打目录页要 24 秒，预热后第 1 次轮询就过）。
- **中间页卡片不满 48 张不等于尾页**：USB 那台机器的库里有地区 `Page 1 of 13` 却只抓到 144/240 张卡片就被判"完成"，实测 `granby-co` 更狠 —— 第 1 页只渲染出 38 张，旧代码 `len(cards) < PER_PAGE` 直接 `break`，9 页只跑了 1 页。现在：只要 `page < pages_total` 且这页不满 48 张，就原地重进 2 次；还缺就把地区排回 pending 并记下断点页（日志前缀 ↪，心跳多一项「空页N次」），不再谎称抓完。`pages_total` 只有 1 页时短页就是真尾页，避免永久排队。
- **设置和日志的位置**：`app_homes.py` 原先用 `Path(__file__)` 定位，打包后 `__file__` 落在 `_internal\` 里，于是 `homes_config.json`、`logs\` 写进了 `_internal`，库却写在 EXE 同级 —— 重打包会把设置和日志一起清掉。现在统一取 `eng.BASE`（EXE 同级）。
- **同一 IP 反复起停会被 Akamai / Kasada 越掐越紧**：一次长跑很顺，跑跑停停就掉速（homes 实测从 368 人/分 掉到 7 人/分）。正式跑请单开一个长会话。
- 翻页 token 各站不同：realtor 是 `/pg-N`，homes 是 `/pN/`；`?page=N` 一律被忽略并返回第一页，容易误判成"翻页坏了"
- homes 的纯数字路径（如 `/33137/`）**不返回 404**，而是返回一个 24 人的通用搜索页；引擎按 canonical 识别这种兜底页并标为「无效地区」，不入库
- realtor 直连并发超过 6 页会被硬封十几分钟
- 拷贝正在使用的 SQLite 库之前**必须先 checkpoint**（`PRAGMA wal_checkpoint(TRUNCATE)`），只拷 `.db` 会丢掉还在 `-wal` 里的行
- 目标机报 `could not find chrome browser binary` 时别再依赖 nodriver 自动探测：`browser_path.py` 按
  手选路径 → 注册表 App Paths / 各盘符 Program Files / 所有用户配置 / PATH → 自带的 `browser\chrome-win64`
  三级往下找，显式传给 `uc.start(browser_executable_path=...)`。用户那台没装 Chrome 的电脑走的就是第三级。
  界面日志会写明用的是哪一个（本机已装 / 软件自带 / 手动指定），因为自带那份是干净 Chromium，
  过 Akamai 不如真 Chrome，能装 Chrome 就还是装
- TPS 抓到 0 号码基本只有一个原因：2Captcha 没额度，不是命中率低

## 八、多电脑部署

1. 打包 EXE 后整个文件夹拷到每台电脑
2. 每台分不同地址段/地区段，避免重复劳动
3. 或动态领任务：做完一批领下一批
4. 参考：19 万地址 ÷ 20 台 ≈ 每台 1 万地址
