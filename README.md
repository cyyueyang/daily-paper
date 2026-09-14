# 飞书 arXiv 论文速读机器人

每天定时从 arXiv 抓取关注方向（LLM / 具身智能 / 世界模型 / RL / Omni 全模态 / Infra）的新论文，调用 DeepSeek 生成中文速读卡片，通过飞书自建机器人推送；回复「下一条」逐篇刷，回复「详细」获取单篇深度解读。

本地常驻运行，飞书 WebSocket 长连接，无需公网 IP、零服务器成本。

## 安装

```bash
uv sync            # 或 pip install -r requirements.txt 思路等价（本项目用 uv 管理）
cp .env.example .env   # 然后按下文填密钥
```

要求 Python ≥ 3.13。

## 配置（.env）

| 配置项 | 说明 |
| --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API key（充值见下） |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书自建应用凭证 |
| `FEISHU_OWNER_OPEN_ID` | 你自己的 open_id，机器人只响应此人 |
| `ARXIV_CATEGORIES` | 抓取分类，默认 `cs.CL,cs.LG,cs.AI,cs.RO` |
| `ARXIV_FETCH_DAYS` | 每日抓取窗口（天），默认 1；首次运行自动补 3 天 |
| `ARXIV_MAX_AGE_DAYS` | 只收最近 N 天内的论文，默认 365（新鲜度兜底） |
| `DAILY_PUSH_TIME` | 每日推送时间，默认 `08:00` |
| `EXTRA_KEYWORDS` | 关键词表（逗号分隔），非空时整体覆盖代码默认词表 |
| `HTTP_PROXY` | 可选代理（arXiv 偶发 429 时使用） |

### DeepSeek 充值

1. 访问 [DeepSeek 开放平台](https://platform.deepseek.com/) → 注册 → 「充值」入口充值（速读卡片每篇约几百 token，成本极低）
2. 「API keys」创建 key，填入 `.env`

### 飞书自建应用创建步骤

1. 访问 [飞书开放平台](https://open.feishu.cn/) → 创建自建应用（个人开发者可用）
2. 添加「机器人」能力
3. 权限管理：开通 `im:message`、`im:message:send_as_bot`
4. 事件订阅：选择「使用长连接接收事件」，订阅 `im.message.receive_v1` 和 `card.action.trigger`（卡片按钮回调）
5. 版本管理与发布：创建版本并发布（个人应用免审核直接可用）
6. 获取自己的 `open_id`：`FEISHU_OWNER_OPEN_ID` 先填任意值启动 `serve`，给机器人随便发条消息，日志里会打出 `忽略非 owner 的消息（open_id=ou_xxx）`，把该值填回 `.env` 重启即可

## 使用

```bash
python -m paperbot fetch      # 手动执行一次抓取+总结（不入定时）
python -m paperbot push       # 手动推送今日汇总
python -m paperbot next       # 在终端打印下一条速读卡片（调试 DeepSeek 输出用，不消费队列）
python -m paperbot serve      # 启动常驻服务（定时任务 + 飞书长连接）
python -m paperbot stats      # 打印统计
```

日常常驻：macOS 已注册 LaunchAgent（`~/Library/LaunchAgents/com.paperbot.daily.plist`），登录自启 + 崩溃自动拉起：

```bash
launchctl kickstart -k gui/$(id -u)/com.paperbot.daily   # 重启（改配置/代码后执行）
launchctl bootout gui/$(id -u)/com.paperbot.daily        # 停止
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.paperbot.daily.plist   # 重新加载
launchctl print gui/$(id -u)/com.paperbot.daily | head   # 查看状态
```

前台调试则直接 `uv run python -m paperbot serve`。**注意：不要与 launchd 实例同时运行**——同一机器人挂两个长连接会重复处理消息。

### 飞书指令

| 指令 | 行为 |
| --- | --- |
| `开始` / `下一条` / `next` / `n` | 发送下一篇速读卡片 |
| `详细` / `detail` / `d` | 当前论文的深度解读（抓 PDF 全文，失败回退摘要），读完自动刷下一篇 |
| `跳过` / `skip` / `s` | 跳过当前篇，自动发下一篇 |
| `收藏` / `star` / `fav` | 收藏当前篇 |
| `列表` / `list` / `ls` | 今日队列概览 |
| `统计` / `stats` | 累计统计与 token 用量 |
| `帮助` / `help` / `?` | 指令说明 |

卡片上的「📖 详细 / ⏭️ 跳过 / ⭐ 收藏」按钮与文字指令等价。

## 运行机制

- **存储**：SQLite（`data/papers.db`），论文状态机 `pending → delivered → read`，`skipped` 可随时深读回 `read`；状态全在 DB，进程重启无损恢复，当日汇总不会重复推送
- **抓取**：arXiv 官方 Atom API，请求间隔 ≥3s，429/超时重试 2 次；以 `arxiv_id`（不含版本号）去重
- **总结**：DeepSeek `deepseek-chat`，速读卡片 `max_tokens=600`，深度解读 `max_tokens=2000`；单篇失败标 `summarize_failed` 不阻塞队列
- **深度解读**：PDF 存 `data/pdfs/` 复用，`pypdf` 提取；全文 >100k 字符截断保留前 60k + 后 20k；结果入库缓存，重复请求不耗 token
- **日志**：`logs/paperbot.log`（1MB×3 轮转）+ 终端输出

## 目录结构

```
src/paperbot/
├── config.py          # pydantic-settings 加载 .env
├── db.py              # SQLAlchemy engine + session
├── models.py          # Paper / State ORM 模型
├── arxiv_client.py    # arXiv 抓取 + 关键词过滤 + 去重
├── llm.py             # DeepSeek 调用 + 两个 prompt 模板
├── queue_service.py   # 状态机、下一条/跳过/收藏逻辑
├── cardfmt.py         # 飞书卡片 JSON 与文案格式化
├── feishu_bot.py      # 长连接、事件处理、指令分发
├── digest.py          # 深度解读（PDF 下载/解析/截断）
├── pipeline.py        # 每日流程编排
├── scheduler.py       # 每日定时任务 + 崩溃补跑
└── __main__.py        # CLI 入口
```

实现参考（许可证兼容的公开仓库，引用处已在代码注释标明出处）：

- [elena-daily-paper-scout](https://github.com/Win7win/elena-daily-paper-scout) — 飞书长连接接入与 `card.action.trigger` 派发模式
- [arxiv_daily_paper_push](https://github.com/NN0202/arxiv_daily_paper_push) — DeepSeek 中文解读 prompt 组织与飞书卡片排版参考

需求规格：[`docs/飞书论文速读机器人-需求规格说明书.md`](docs/飞书论文速读机器人-需求规格说明书.md)
