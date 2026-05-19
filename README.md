<div align="center">

# 🐦 x-dl

**用 [gallery-dl](https://github.com/mikf/gallery-dl) 下载 X / Twitter 媒体，自动推送到 Telegram 频道**

</div>

---

## ✨ 功能

- 🤖 **Bot 模式** — 发推文链接给 Bot，自动下载并推送（X / Twitter / Pixiv / Instagram / Weibo / Reddit / Bilibili / YouTube）
- 🔄 **内置定时同步** — bot 自己跑 scheduler，不再依赖 systemd timer
- 📌 **订阅** — `/subscribe @user` 自动跟踪新推文
- 🔍 **检索** — `/search 关键词` 在历史推文中查找
- 🔁 **失败重试** — 自动入队，`/retry_failed` 一键重发
- 🎬 **超大视频处理** — 自动分割或压缩；自建 local bot-api 可解锁 2 GB 上传
- 🧹 **去重** — URL（持久化，默认 7 天）+ 媒体 md5 + gallery-dl archive 三层
- 💾 **磁盘配额** — 超过 `DOWNLOAD_DIR_MAX_GB` 自动清理旧文件
- 🌐 **Webhook / 长轮询** 双模式；socks5/http 代理
- 📊 **结构化日志** — loguru，stdout + 滚动文件
- 🚦 **多频道路由** — 不同 chat_id 路由到不同目标频道

---

## 📋 目录

1. [环境要求](#-环境要求)
2. [安装](#-安装)
3. [获取 Cookies](#-获取-cookies)
4. [配置 .env](#️-配置-env)
5. [Bot 模式](#-bot-模式)
6. [Sync 命令行](#-sync-命令行可选)
7. [Webhook 模式（可选）](#-webhook-模式可选)
8. [自建 local bot-api（解锁 2GB 上传）](#-自建-local-bot-api解锁-2gb-上传)
9. [开机自启](#-开机自启)
10. [常见问题](#-常见问题)

---

## 🛠 环境要求

| 依赖 | 要求 | 用途 |
|------|------|------|
| 🐍 Python | ≥ 3.10 | 运行项目 |
| 🎞 ffmpeg | 任意版本 | 视频分割 / 压缩（可选） |

```bash
sudo apt install python3 python3-venv ffmpeg
```

---

## 📦 安装

```bash
git clone https://github.com/laobanbiefangcu/x-dl.git && cd x-dl

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
```

> 💡 v2 升级提示：本版本数据迁到 `data/xdl.db`（sqlite）。原有的 `data/archive.db` 仍兼容，不会丢失下载记录。

---

## 🍪 获取 Cookies

gallery-dl 需要登录态 cookies 才能访问书签 / 点赞等私有内容。

1. 浏览器装 **Get cookies.txt LOCALLY** 扩展
2. 在 [x.com](https://x.com) 登录后，导出当前网站 cookies 为 `cookies.txt`
3. 将路径填入 `.env` 的 `COOKIES_FILE`

> ⚠️ cookies 有效期约 30 天。Bot 每 12 小时自动检查，临近到期会在 Telegram 提醒。

---

## ⚙️ 配置 .env

完整选项见 `.env.example`。核心：

```ini
COOKIES_FILE=/path/to/cookies.txt
DOWNLOAD_DIR=downloads
SYNC_TARGETS=bookmarks                # bookmarks / likes / URL
X_HANDLE=yourhandle                   # 同步点赞时必填
PROXY=socks5://127.0.0.1:7890

TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=-1001234567890
BOT_ALLOWED_CHAT_IDS=                 # 留空 = 对所有人开放
BOT_MAX_WORKERS=4

# 内置 scheduler，0 = 关闭，需 systemd timer 时设 0
SYNC_INTERVAL_MINUTES=60
SUBSCRIPTION_INTERVAL_MINUTES=60

URL_DEDUP_TTL_DAYS=7
MEDIA_HASH_DEDUP=true
DOWNLOAD_DIR_MAX_GB=0                 # 0 = 不限

LOG_FILE=data/bot.log
LOG_LEVEL=INFO
```

### 多频道路由

不同来源 chat_id 转发到不同频道：

```ini
ROUTES_JSON={"123456789":"-1001111111111","987654321":"-1002222222222"}
```

未命中时 fallback 到 `TELEGRAM_CHAT_ID`。

---

## 🤖 Bot 模式

```bash
.venv/bin/python bot.py
```

**支持的 URL**

| 平台 | 示例 |
|------|------|
| X / Twitter | `https://x.com/user/status/123` |
| Pixiv | `https://www.pixiv.net/artworks/123` |
| Instagram | `https://www.instagram.com/p/abc` |
| 微博 / Reddit / Bilibili / YouTube | 同形式 URL |

**命令列表**

| 命令 | 说明 |
|------|------|
| `/sync [likes\|bookmarks]` | 同步书签/点赞，默认全部 |
| `/subscribe @user` | 订阅某 X 用户的新推 |
| `/unsubscribe @user` | 取消订阅 |
| `/subs` | 列出订阅 |
| `/search 关键词` | 检索历史推文（FTS5 全文搜索） |
| `/retry_failed` | 重试所有失败链接 |
| `/disk_cleanup` | 立即触发磁盘配额清理 |
| `/clear` | 把本地媒体发完后清空 archive |
| `/status` | cookies / 同步 / 磁盘 / 订阅 / 失败队列概览 |
| `/restart` | 主动退出，依赖 systemd 自动重启 |

---

## 🔄 Sync 命令行（可选）

仍保留 CLI 入口，适合手动跑或外部 cron。Bot 模式下用 `SYNC_INTERVAL_MINUTES` 即可，**无需 systemd timer**。

```bash
.venv/bin/python sync.py
```

---

## 🌐 Webhook 模式（可选）

适合公网服务器、希望事件即时响应。配置后启动 bot.py 即自动用 webhook 而非长轮询。

```ini
WEBHOOK_URL=https://your-domain.com
WEBHOOK_LISTEN=0.0.0.0
WEBHOOK_PORT=8443
WEBHOOK_PATH=/tg/webhook
WEBHOOK_SECRET=随便填一段随机字符串
```

前面通常套 Caddy / Nginx：
```
your-domain.com {
    reverse_proxy /tg/webhook 127.0.0.1:8443
}
```

---

## 📤 自建 local bot-api（解锁 2GB 上传）

官方 bot API 单文件 50 MB。自建 [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) 可上传到 2 GB：

```bash
docker run -d --restart unless-stopped \
  -p 127.0.0.1:8081:8081 \
  -e TELEGRAM_API_ID=<your id> -e TELEGRAM_API_HASH=<your hash> \
  --name tgbotapi aiogram/telegram-bot-api:latest
```

然后在 `.env`：
```ini
TELEGRAM_API_BASE=http://127.0.0.1:8081
TELEGRAM_MAX_UPLOAD_BYTES=2147483648
```

切换后旧 bot token 需要执行一次 [`logOut`](https://core.telegram.org/bots/api#logout) 才能换到本地服务器。

---

## 🚀 开机自启

```bash
sudo cp x-dl-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now x-dl-bot
```

> 📝 部署路径不是 `/root/x-dl` 时，先改 service 文件里的 `WorkingDirectory` 和 `ExecStart`。

**老用户：移除 systemd timer**

v2 起 bot 内置定时同步，不再需要 `x-dl-sync.timer`：

```bash
sudo systemctl disable --now x-dl-sync.timer
sudo rm /etc/systemd/system/x-dl-sync.timer
sudo systemctl daemon-reload
```

`x-dl-sync.service` 仍保留，便于手动 `systemctl start x-dl-sync` 触发一次同步。

**日常管理**

```bash
systemctl status x-dl-bot
systemctl restart x-dl-bot
journalctl -u x-dl-bot -f                # 实时日志（也可看 data/bot.log）
```

---

## ❓ 常见问题

<details>
<summary>🔑 <b>下载失败：需要登录 / cookies 已失效</b></summary>

重新导出 cookies 文件，替换 `COOKIES_FILE` 指向的文件后 `/restart` 即可。

</details>

<details>
<summary>🌐 <b>下载失败：无法获取推文内容</b></summary>

X 会封锁部分数据中心 IP，在 `.env` 配置代理可解决：

```ini
PROXY=socks5://127.0.0.1:7890
```

</details>

<details>
<summary>🎬 <b>视频过大</b></summary>

Bot 默认先尝试**分割**视频，分割后仍超限则**压缩**。彻底解决：自建 local bot-api 提到 2GB（见上文）。

</details>

<details>
<summary>🗃 <b>数据文件说明</b></summary>

| 文件 | 说明 |
|------|------|
| `data/archive.db` | gallery-dl 自己的 sqlite，记录已下载的 URL（避免重复下载） |
| `data/xdl.db` | x-dl 的 sqlite：URL 去重、推文元数据 + FTS、订阅、失败队列、媒体 hash |
| `data/bot.log` | 滚动日志，超 10 MB 压缩归档 |
| `downloads/` | 媒体下载目录 |

</details>

<details>
<summary>🔁 <b>从 v1 升级</b></summary>

- 直接 `git pull && .venv/bin/pip install -r requirements.txt` 即可
- 老 `BOT_MAX_WORKERS` 等配置全部兼容
- 新增配置项（`SYNC_INTERVAL_MINUTES`、`ROUTES_JSON`、`DOWNLOAD_DIR_MAX_GB` 等）见 `.env.example`
- 如启用内置 scheduler，记得 `systemctl disable x-dl-sync.timer`

</details>
