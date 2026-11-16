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
| `ip_pool.py` | IP 轮换池：吃一批 IP（`ip:port` / `ip:port:user:pass` / `http://user:pass@host:port` 三种写法混着都行），轮着派活、谁被限流谁歇着、连拒 3 次停用，全冷却就等最短那条。homes 引擎用它；纯数据结构不碰网络，别的引擎也能直接复用 |
| `browser_path.py` | 浏览器定位，三级顺序：界面手选的路径 → 本机装的 Chrome/Edge/Brave（注册表 App Paths → 各盘符 Program Files → `C:\Users\*` 所有用户配置 → PATH）→ 交付文件夹自带的 `browser\chrome-win64\chrome.exe`。nodriver 自带的探测只翻 PROGRAMFILES 环境变量，换台机器就报 "could not find chrome browser binary"，所以两个 nodriver 引擎都显式传 `browser_executable_path`。用自带那份时自动加 `--no-sandbox`（非标准安装路径下 Chromium 沙箱会报 "Sandbox cannot access executable"），本机真 Chrome 不动 |
| `bundle_browser.py` | 打包时把本机 `ms-playwright` 里那份 Chromium 塞进交付文件夹的 `browser\`（同盘优先硬链接，不额外占空间；目标机没装任何浏览器也能跑）。源在 `C:\Users\<你>\AppData\Local\ms-playwright\chromium-*\chrome-win64`，没有就先 `python -m playwright install chromium` |
| `build_ui.py` / `build_realtor.py` / `build_homes.py` | 各自打 Windows 免安装 EXE，产物落在 `dist_*`；后两个还会调 `bundle_browser` 带上一份 Chromium，并生成 `启动*.bat` + `浏览器自检.bat` |
| `build_mac.py` + `打包Mac.command` + `.github/workflows/build-mac.yml` | mac 端 `.app`：Windows 交叉打不出来，靠 GitHub Actions 在 mac runner 上构建 |
| `copy_to_usb.py` | 把 `TPS_v1.8.2_UI` 整包拷 U 盘，数据库走 SQLite backup API 做一致快照（不会拷到半截的 `-wal`） |
| `app_tps.py` + `engine_tps182.py` + `engine_fps.py` | **当前交付的那一个界面 = 两核心版**：界面里可选 TPS(1.8.2 内核) 或 FPS(fastpeoplesearch，nodriver 真 Chrome)；两边共用同一个库 `tps_data.db`、同一套年龄/性别/去重口径、同一个 `export_csv()`。详见第九节 |
| `proxy_relay.py` | 本地 CONNECT 代理中继 + 代理模板解析（`{session}` 粘性会话占位），TPS 引擎和两个地址抓取器共用；只用标准库，为了让 nodriver 那条线不必带 playwright |
| `address_pool.py` | 全美地址池：按真实 ZIP 查 Zillow 房产站，只收街道地址，按房价分两档（普通 / 高净值）入库 |
| `pw_engine.py` | PropWire 地址池共用引擎（DataDome 只认真浏览器，走本机网络 + 本机浏览器），被两个地址抓取器入口调用 |
| `app_addr.py` | 两个地址抓取器共用的界面（抓取 / 库存导出 / 失败台账三页签），入口脚本传档位 |
| `普通地址抓取.py` / `高净值地址抓取.py` | 两个地址抓取器的双击入口（一个估价区间、一个门槛上不封顶） |
| `build_addr.py` | 打这两个地址抓取器的 EXE：onedir + `--windowed` + 自带 Chromium |
| `断网断电自动续跑.bat` | 开机自动接着上次那批地址继续跑的启动脚本 |
| `requirements.txt` | 依赖清单 |

**留着但对比的旧版（别改，除非要复现行为差异）**

| 文件 | 用途 |
|---|---|
| `engine_tps.py` | TPS v3.0.0 试验版引擎，后来回到 1.8.2 内核，现由 `engine_core.py` 取代（`app_tps.py` / `build.py` 已经转成当前两核心交付版，见上面那张表） |
| `app_tps_v182.py` | 更早的 1.8.2 简易界面，同样已被 `app_ui.py` 取代 |
| `CHANGELOG_v3.0.0.md` | v3.0.0 那次改了什么，留档 |

日常在改的：`app_tps.py` / `engine_tps182.py` / `engine_fps.py` / `engine_core.py`（两核心这条线）和 `app_ui.py` / `realtor_engine.py` / `app_realtor.py` / `homes_engine.py` / `app_homes.py` / `pw_engine.py` / `app_addr.py`；其余都在仓库历史里躺着，不用拷到别的机器。

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
- **一个 session 参数 = 一个固定住宅出口 IP，换它不要钱**：LinkUp 同一个账号，用户名里
  `-session-a1b2c3` 出口是 47.141.187.7，`-session-d4e5f6` 就是 73.137.13.162（实测）。
  `engine_core.start_relay` 以前只替换 `{session}` 占位符，代理串里没这个占位符就一辈子守着一个 IP ——
  这正是"跑到七万多一直退避进不去"死锁的另一半：IP 被打标之后没有任何自愈路径。
  新版：连续 6 次真 `Access Denied` 就举旗 → 关浏览器 → 换个随机 session 重开 →
  被中断的地区按断点页排回队列继续。桩测（不联网）：全程硬拒绝时 8 轮换出 8 个不同 session，
  4 个地区全部留 pending、一个都不记 failed；连换 8 轮还是一个人抢不到就停手并说明原因
  （那是代理账号本身的问题，继续换只是白烧流量）。界面「出口轮换」可关掉，关掉就退回老行为。
- **省流量模式**：只屏蔽图片/CSS/字体/视频 + 按域名屏蔽第三方统计，两个坑：
  ① Chrome 的 `Network.setBlockedURLs` 只认 URLPattern 写法（`*://*/*.jpg`），
  写成裸 glob `"*.jpg"` 会直接 `ProtocolException: Pattern "*.jpg" failed to parse`（28 条全接受才算挂上）；
  ② 绝不能屏蔽 `*.js` —— 那张 2.4KB 挑战页靠自己的脚本 `location.reload` 才变成 18 万字节的真页面，
  全屏蔽 JS 会把每一页都变成永远过不去的挑战页。第三方脚本只按域名拦。
  实测（同一浏览器同一出口 IP，逐页交替开关）：单页 HTML 182,000~198,000 字节，
  但过代理的量头几页 4~7MB、缓存热了以后 0.2~0.3MB/页；8 页均值 全拉 4.23MB/页 → 屏蔽 3.12MB/页。
  样本小、方差大，只当量级参考。真正的数是引擎自己报的：本地中继按字节计数，
  跑完打一行「过代理流量合计 X MB：每页 Y KB、每人 Z KB」，心跳里也有「过代理N MB」。
- **本机直连被硬拒绝时不硬撞**：实测 1 个标签页撞 700 秒 —— 0 页 0 人，退避从 2 次涨到 7 次，
  只会把封窗口越拖越长。新版直连最多重开 4 轮浏览器，中间递增打盹 5/10/15 分钟（期间一个请求都不发），
  4 轮还不行就停下并提示"过几小时再点开始，会接着上次那页跑"。
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

## 九、两核心交付版（`新年龄修复版_两核心覆盖包`）= 现在在用的这一个

一个界面 + 一个库，两个核心在界面上选：

| 核心 | 目标站 | 过站方式 | 验证码 | 说明 |
|---|---|---|---|---|
| TPS | truepeoplesearch.com | Playwright + stealth | **要**（2Captcha，关了 0 号码） | 抽取算法与 1.8.2 逐字节一致，只带已授权的修复 |
| FPS | fastpeoplesearch.com | nodriver + 真 Chrome（有头） | **不需要**，Cloudflare 托管挑战自己过（实测 0s / 148s / 162s 三种） | 个人页电话直出，解码费 0；无头 / Playwright 都卡在挑战页，已判死 |

源码分工：`engine_tps182.py` 是两个核心共用的库层（建表、`stats()`、`export_csv()`、年龄清洗），
`engine_fps.py` 只管 FPS 的抓页面 + 解析，界面 `app_tps.py`。FPS 直接 `export_csv = tps.export_csv`，
所以**两个核心导出来的表格式一模一样**，改口径只改一处。

### 界面上三个数分别是什么（这块口径 2026-11-15 定过，别再改回去）

以前界面写「结果数 / 号码数」，他看着像虚报：一屏显示 42 人 / 219 个号，导出的表只有 37 行。
实测对平过，219 是真的（一人最多挂 12 个号），37 是因为**导出改成一人一行、只给第一个常用号**，
外加 5 行被年龄拦掉。现在三个标签直接对着数：

- **人数** = `results` 表行数（一个活人一行）
- **导出条数** = `deliver` = 按界面上的年龄范围**现在真能导出来的行数**，和 CSV 的行数必须一模一样
- **全部号码** = 所有人名下号码的个数合计（一人能有十几个），只是库里的存量，**不代表导出会给出这么多行**

三条硬规矩：
1. **显示的就是他会拿到的东西**，不报好看但拿不到的数（「我的意思是显示要准确 不要报假数据」）。
2. 导出**一人一行、只写第一个常用号**（页面上排最前那个）。其余号仍在库里，他要全导随时再改。
3. 导出和统计共用同一个年龄判定 `engine_tps182._passes_age()`，两边数出来的必须相等 —— 用 `_age_cell()`
   清洗：非数字留空、>120 也留空（实测 `413 岁` 是 `(413)` 电话区号被当年龄读），
   **留空 = 不知道年龄 = 不参与拦**，不因为读错一个数把整个人丢掉。
   「导出速度」也改成按 `deliver` 算，单位写「条/分钟」。

### 覆盖安装：`覆盖升级_双击我.bat`

给已经在用老版本的机器做原地升级用的（**只覆盖程序，绝不动 `tps_data.db` 和 `logs\`**）。
三道闸，任一不过就停手并提示，一个文件都不写：

1. 目标文件夹里**没有 `TPS抓取器.exe`** → 认定选错目录，停（防他把包路径当成目标）。
2. 有 `TPS抓取器` 进程**且跑的就是目标那一份**（按 `Get-Process` 的 `.Path` 比，读不到路径按最坏情况算）→ 停，
   只跑在别处的只提示不拦。别用 WMI `Win3_Process.ExecutablePath`，沙箱实测那条不稳。
3. 拷完比 `TPS抓取器.exe` 的**字节数**，不等 → 停并让他重拷。

bat 正文是 **GBK**、里面不加 `chcp`（这台机 ACP=936，加了 `chcp 65001` 中文提示会乱码）。
交付包本身**不要压缩**，直接给解压好的文件夹，而且**不许混进 `.db` / `logs`**。

### 抓完一个地址界面那行怎么读

`✓ …：入库N人 / 出表N条（每人1个常用号）/ 库内全部号M / 流量…` —— 入库数和出表条数刻意做成同一个数，
不一致就说明导出漏人了；`M` 只是库存号码数。

### 限流页 / 拉黑页：认出签名就换出口，不等（11-16 定案）

FPS 页型判定只看一个签名：**正文里有没有 `_cf_chl_opt`**。

- 有 = 会自过的真 Cloudflare 挑战（归档 11 份真挑战页 11/11 都带）⇒ 等或解都是对的。
- 没有 = 「再等多久都原样吐回来」的死页，只有换出口能救：
  `Rate Limited - FastPeopleSearch.com` / `Rate Limit Exceeded` / `allotted time limit` ⇒ `ratelimited`；
  `Sorry, you have been blocked` + `you are unable to access` ⇒ `hardblock`；
  `ERR_TOO_MANY_REDIRECTS` 这类断连页 ⇒ `neterror`。

旧判定把归档里 31 份硬页的 29 份当 `loading`、2 份当 `challenge`，于是每页白等
`challenge_wait 200 秒 × (fps_rotations+1=3) ≈ 600 秒` —— 这就是「换台机器一开机就成片卡死」的真相。
现在撞到硬页先重读一次确认（防半加载时撞字样误判），确认了抛 `FpsNeedNewIp`（FPS）/
`DetailHardBlocked`（TPS 详情页），**整副浏览器关掉重开**才换得到出口：实测同一副浏览器里
`relay.set_session()` 换 5 次 sid，出口 IP 一次没变（1/5 唯一，隧道复用，改用户名只对新建隧道生效）。
TPS 详情页撞到硬页也不再「跳过此人」——那是出口被废，不等于查无此人。

代理串的粘性部分由程序拼：`-sid-{session}-t-<分钟>`，`{session}` 每条车道唯一。
坑：界面上贴**带横杠的写死 sid**（如 `-sid-AB-C123-t-30`）时，旧正则 `-sid-[^-]*-t-\d+` 匹配不上、
整条原样保留 ⇒ 20 条车道共用一个出口 IP ⇒ 一开机就撞限流。现已改成 `-sid-.+?(?:-t-\d+|$)`，
粘没粘 sid 都会被换成每车道唯一（实测粘性并发 4/8/12/16/20：唯一 IP 数 = 通的车道数，20 并发 20/20）。

实测：同一被限流出口 + 同一地址，新版 54.4 秒认出并抛错、54.8 秒收工，旧版 107.8 秒一次没认出来。
产量对平（4 个配对样本）人数/号码数条条相等，用时与流量随出口 IP 抖（两臂互有胜负），无系统性倒退。

### 界面尺寸与「挪出屏幕」（11-16 紧凑版）

- 窗口默认 `min(1240, 屏幕可用宽) × min(700, 可用高-20)`，左栏定宽最多 680px：
  以前一最大化，配置那几栏被拉到 1900px，FPS 那行长说明直接被屏幕切掉、日志区挤没。
- 「配置 / 地址导入 / 控制 / 统计」四个勾选框是**真折叠**（`app_tps.py` 的 `mount(group, inner_layout)`）。
  PySide6 6.12 里 checkable 的 `QGroupBox` 取消勾选**不会**藏子控件（实测取消后子控件仍可见、高度一点没变），
  所以必须把内容装进一个 `QWidget` 容器、用 `toggled → setVisible` 才收得起来。
  两个坑都要踩：① 只 `setVisible(False)` 外层布局不重算，框子继续占着展开时的 208px 空白，
  必须补 `group.updateGeometry()`；② 左栏末尾的 `addStretch()` 要给因子 1（`addStretch(1)`），
  全为 0 时 Qt 把多余高度平均分给每个框，收起来照样一片空灰。
  离屏实测四框高度：默认 437px、四段全展开 565px、全收起 124px（每框 31px）。
- FPS 的「挪出屏幕」＝Chrome 窗口 `--window-position=-32000,-32000` 挪到屏幕外，**不是无头**
  （无头已判死：连换 3 个出口 IP 各等满 200 秒 0 张卡片）。同时加 `CalculateNativeWinOcclusion`
  等三个 flag，防止窗口看不见就被降频。
  同地址同代理线 A/B：挪出屏幕 12 卡片/5 人/18 号/62.0 秒/11.0MB vs 窗口可见 12 卡片/5 人/18 号/55.7 秒/9.2MB，
  产出完全一致。真要让机器不卡还得把并发往下调——一个车道一个 Chrome，实测每个吃 1.2~1.7GB。
