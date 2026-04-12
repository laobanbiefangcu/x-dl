<div align="center">

# 🐦 x-dl

**用 [gallery-dl](https://github.com/mikf/gallery-dl) 下载 X / Twitter 媒体，自动推送到 Telegram 频道**

</div>

---

## ✨ 功能

- 🤖 **Bot 模式** — 发推文链接给 Bot，自动下载媒体并推送到频道
- 🔄 **Sync 模式** — 定时同步书签 / 点赞，新内容自动入频道
- 🎬 **超大视频处理** — 自动分割或压缩超过 Telegram 限制的视频
- 🔁 **断线重试** — 网络抖动自动重试，支持 socks5 代理
- 🧹 **去重** — 5 分钟内同一链接不重复下载；Sync 模式通过 archive.db 全局去重

---

## 📋 目录

1. [环境要求](#-环境要求)
2. [安装](#-安装)
3. [获取 Cookies](#-获取-cookies)
4. [配置 .env](#️-配置-env)
5. [Bot 模式](#-bot-模式)
6. [Sync 模式](#-sync-模式)
7. [开机自启](#-开机自启)
8. [常见问题](#-常见问题)

---

## 🛠 环境要求

| 依赖 | 要求 | 用途 |
|------|------|------|
| 🐍 Python | ≥ 3.10 | 运行项目 |
| 🎞 ffmpeg | 任意版本 | 视频分割 / 压缩（可选） |

```bash
# Ubuntu / Debian
sudo apt install python3 python3-venv ffmpeg
```

---

## 📦 安装

```bash
git clone <repo-url> x-dl && cd x-dl

# 🔧 创建虚拟环境并安装依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 📄 初始化配置文件
cp .env.example .env
```

---

## 🍪 获取 Cookies

gallery-dl 需要登录态 cookies 才能访问书签、点赞等私有内容。

**① 安装浏览器扩展**

| 浏览器 | 扩展 |
|--------|------|
| 🌐 Chrome | [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) |
| 🦊 Firefox | [cookies.txt](https://addons.mozilla.org/firefox/addon/cookies-txt/) |

**② 导出**

1. 打开 [x.com](https://x.com) 并确保已登录
2. 点击扩展 → 导出当前网站 cookies → 保存为 `cookies.txt`
3. 将文件路径填入 `.env` 的 `COOKIES_FILE`

> ⚠️ **Cookies 有有效期**，失效后需重新导出。Bot 启动时会在后台自动检测，结果见日志。

---

## ⚙️ 配置 .env

```ini
# 📁 基础
COOKIES_FILE=/path/to/cookies.txt     # cookies 文件完整路径
DOWNLOAD_DIR=downloads                # 媒体下载目录

# 🎯 同步目标（Sync 模式）
SYNC_TARGETS=bookmarks                # bookmarks / likes / 完整 URL，逗号分隔
X_HANDLE=yourhandle                   # 同步点赞时必填（不含 @）

# 🌐 代理（国内服务器推荐填写）
PROXY=socks5://127.0.0.1:7890

# 📬 Telegram
TELEGRAM_BOT_TOKEN=123456:ABC...      # BotFather 获取
TELEGRAM_CHAT_ID=-1001234567890       # 目标频道 / 群组 chat_id
BOT_ALLOWED_CHAT_IDS=                 # 白名单，留空 = 所有人可用，多个用逗号分隔
BOT_MAX_WORKERS=4                     # Bot 并发下载线程数
```

### 🎯 SYNC_TARGETS 示例

```ini
SYNC_TARGETS=bookmarks                 # 📑 只同步书签
SYNC_TARGETS=likes                     # ❤️  只同步点赞
SYNC_TARGETS=bookmarks,likes           # 📑❤️ 同时同步两者
SYNC_TARGETS=https://x.com/user/likes  # 🔗 直接指定 URL
```

---

## 🤖 Bot 模式

### 第一步 — 创建 Bot 🤖

1. 打开 Telegram，找到 [@BotFather](https://t.me/BotFather)
2. 发送 `/newbot`，按提示设置名称和用户名
3. 复制 Token，填入 `.env` 的 `TELEGRAM_BOT_TOKEN`

### 第二步 — 获取频道 chat_id 📢

1. 将 Bot 设为频道管理员（需有发消息权限）
2. 在频道随意发一条消息，然后访问：
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. 在返回 JSON 中找到 `"chat": {"id": -1001234567890}`，填入 `TELEGRAM_CHAT_ID`

### 第三步 — 启动 🚀

```bash
.venv/bin/python bot.py
```

### 使用方式 💬

直接把推文链接发给 Bot，一次可发多个：

```
https://x.com/username/status/1234567890
```

Bot 并发下载，完成后推送媒体到频道，私聊状态实时更新：

```
⏳ 下载中…
       ↓ 完成后原地更新为
✅ 已发送（3 个文件）
```

### 白名单 🔒

只允许特定用户使用时，在 `.env` 中填写 chat_id：

```ini
BOT_ALLOWED_CHAT_IDS=123456789,987654321
```

> 💡 不知道自己的 chat_id？给 Bot 发任意消息，在日志里找 `chat_id=xxxxxxx`。

---

## 🔄 Sync 模式

手动或定时运行，将新增书签 / 点赞推送到 Telegram。  
通过 `data/archive.db` 去重，已推送内容不会重复发送。

```bash
.venv/bin/python sync.py
```

**输出示例：**

```
同步目标: https://x.com/i/bookmarks

→ https://x.com/i/bookmarks
  新增 3 个文件:
  - twitter/bookmark/userA_111_1.mp4
  - twitter/bookmark/userB_222_1.jpg
  - twitter/bookmark/userB_222_2.jpg

本次共下载 3 个文件。
开始发送到 Telegram（共 2 条推文）...
  ✓ 111 (1 个文件)
  ✓ 222 (2 个文件)

全部完成。
```

### ⏰ 定时执行（cron）

```bash
crontab -e
```

```cron
# 每小时同步一次
0 * * * * /root/x-dl/.venv/bin/python /root/x-dl/sync.py >> /root/x-dl/sync.log 2>&1
```

---

## 🚀 开机自启

项目根目录已包含 `x-dl-bot.service`。

> 📝 如果部署路径不是 `/root/x-dl`，先修改 `x-dl-bot.service` 中的 `WorkingDirectory` 和 `ExecStart`。

```bash
# 安装并启用服务
sudo cp x-dl-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable x-dl-bot
sudo systemctl start x-dl-bot
```

**日常管理：**

```bash
systemctl status x-dl-bot        # 📊 查看运行状态
systemctl restart x-dl-bot       # 🔄 重启
journalctl -u x-dl-bot -f        # 📜 实时查看日志
journalctl -u x-dl-bot -n 50     # 📜 查看最近 50 行
```

---

## ❓ 常见问题

<details>
<summary>🔑 <b>下载失败：需要登录 / cookies 已失效</b></summary>
<br>

重新导出 cookies 文件，替换 `COOKIES_FILE` 指向的文件后重启 Bot 即可。

</details>

<details>
<summary>🌐 <b>下载失败：无法获取推文内容</b></summary>
<br>

X 会封锁部分数据中心 IP，在 `.env` 配置代理可解决：

```ini
PROXY=socks5://127.0.0.1:7890
```

</details>

<details>
<summary>🎬 <b>视频超过 Telegram 50 MB 上传限制</b></summary>
<br>

Bot 会自动处理，优先尝试**分割**视频，分割后仍超限则**压缩**。  
需要安装 ffmpeg，相关开关在 `.env` 中：

```ini
TELEGRAM_SPLIT_OVERSIZED_VIDEO=true
TELEGRAM_COMPRESS_OVERSIZED_VIDEO=true
```

</details>

<details>
<summary>🧹 <b>同一链接发了两次，Bot 只处理了一次</b></summary>
<br>

正常行为。Bot 有 **5 分钟去重窗口**，同一链接短时间内只会下载一次，防止重复推送。

</details>

<details>
<summary>📜 <b>如何查看完整运行日志</b></summary>
<br>

```bash
journalctl -u x-dl-bot -f
```

正常启动日志如下：

```
[01:23:47] 已连接: @your_bot (Bot Name)
[01:23:47] 正在检测 cookies 有效性（后台）…
[01:23:47] Bot 已启动，并发线程数: 4，正在监听消息…
[01:23:48] [cookies] ✓ 认证有效
[01:24:01] 收到消息 chat_id=123456789 from=@user: 'https://x.com/...'
[01:24:05]   [https://x.com/...] 下载完成 1 个文件，发送中…
[01:24:07]   [https://x.com/...] 发送成功 → -1001234567890
```

</details>
