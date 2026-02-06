# Agentica Gateway

Agentica Gateway 服务，调用 [agentica](https://github.com/shibing624/agentica) SDK。

支持Lark/Feishu(飞书)、WeCom(企微)、Telegram、Discord、Gradio 等多渠道接入的 AI Agent 网关。

## 特性

- **FastAPI 服务** - REST API + WebSocket Gateway
- **多渠道支持** - Gradio / Lark / WeCom / Telegram / Discord
- **调用 agentica SDK** - Agent / Memory / Skills / Tools
- **消息路由** - 多 Agent 路由支持
- **定时任务** - APScheduler Cron 调度
- **流式输出** - WebSocket 实时推送

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                   agentica-gateway                       │
│                                                          │
│  ┌─────────────────────────────────────────────────┐    │
│  │              FastAPI Application                 │    │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────────────┐│    │
│  │  │ REST API │ │WebSocket │ │   Webhooks       ││    │
│  │  │ /api/*   │ │ /ws      │ │   /webhook/*     ││    │
│  │  └────┬─────┘ └────┬─────┘ └────────┬─────────┘│    │
│  └───────┼────────────┼────────────────┼──────────┘    │
│          │            │                │                │
│  ┌───────▼────────────▼────────────────▼──────────┐    │
│  │              Core Services                      │    │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐       │    │
│  │  │ Channels │ │  Router  │ │Scheduler │       │    │
│  │  │ Manager  │ │          │ │          │       │    │
│  │  └────┬─────┘ └────┬─────┘ └──────────┘       │    │
│  └───────┼────────────┼──────────────────────────┘    │
│          │            │                                │
│  ┌───────▼────────────▼──────────────────────────┐    │
│  │              agentica SDK                      │    │
│  │  Agent | Workspace | Skills | Memory | Tools  │    │
│  └────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

## 安装

```bash
pip install -e .

# 可选：安装 Telegram/Discord 支持
pip install -e ".[telegram]"
pip install -e ".[discord]"
pip install -e ".[all]"
```

## 配置

复制 `.env.example` 为 `.env` 并填写：

```bash
# 服务配置
HOST=0.0.0.0
PORT=8789

# 模型配置
MODEL_PROVIDER=zhipuai   # zhipuai / openai / deepseek
MODEL_NAME=glm-4.7-flash # glm-4.7-flash is free

# Gradio WebUI
GRADIO_ENABLED=true
GRADIO_PORT=7863

# 飞书
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_ALLOWED_USERS=ou_xxx,ou_yyy

# Telegram（可选）
TELEGRAM_BOT_TOKEN=xxx

# Discord（可选）
DISCORD_BOT_TOKEN=xxx
```

## 启动

### 开发模式

```bash
uvicorn src.main:app --reload --port 8789
```

### 生产模式

#### 方式一：Gunicorn + Uvicorn（推荐）

```bash
# 安装 gunicorn
pip install gunicorn

# 启动（4 个 worker 进程）
gunicorn src.main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8789
```

#### 方式二：直接运行

```bash
agentica-gateway
```

#### 方式三：Docker Compose

```bash
# 构建并启动
docker-compose up -d

# 查看日志
docker-compose logs -f
```

## API

### REST API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| GET | `/api/status` | 系统状态 |
| POST | `/api/chat` | 发送消息 |
| POST | `/api/memory` | 保存记忆 |
| GET | `/api/sessions` | 列出会话 |
| GET | `/api/channels` | 渠道状态 |
| POST | `/api/send` | 发送到渠道 |

### WebSocket Gateway

```
ws://localhost:8789/ws

# 连接
→ {"type":"req","id":"1","method":"connect","params":{"auth":{"token":"..."}}}
← {"type":"res","id":"1","ok":true,"payload":{"type":"hello-ok"}}

# 聊天（流式）
→ {"type":"req","id":"2","method":"agent","params":{"message":"你好"}}
← {"type":"event","event":"agent.content","payload":{"delta":"你好"}}
← {"type":"res","id":"2","ok":true,"payload":{"content":"你好！"}}
```


## 飞书机器人配置


### 配置

1. 在 [飞书开放平台](https://open.feishu.cn) 创建自建应用
2. 在凭证页面获取 App ID 和 App Secret
3. 开启所需权限（见下方）
4. **配置事件订阅**（见下方）⚠️ 重要
5. 配置插件：

#### 必需权限

| 权限 | 范围 | 说明 |
|------|------|------|
| `contact:user.base:readonly` | 用户信息 | 获取用户基本信息（用于解析发送者姓名，避免群聊/私聊把不同人当成同一说话者） |
| `im:message` | 消息 | 发送和接收消息 |
| `im:message.p2p_msg:readonly` | 私聊 | 读取发给机器人的私聊消息 |
| `im:message.group_at_msg:readonly` | 群聊 | 接收群内 @机器人 的消息 |
| `im:message:send_as_bot` | 发送 | 以机器人身份发送消息 |
| `im:resource` | 媒体 | 上传和下载图片/文件 |

#### 可选权限

| 权限 | 范围 | 说明 |
|------|------|------|
| `im:message.group_msg` | 群聊 | 读取所有群消息（敏感） |
| `im:message:readonly` | 读取 | 获取历史消息 |
| `im:message:update` | 编辑 | 更新/编辑已发送消息 |
| `im:message:recall` | 撤回 | 撤回已发送消息 |
| `im:message.reactions:read` | 表情 | 查看消息表情回复 |

#### 文档工具权限

使用飞书文档工具（`feishu_doc_*`）需要以下权限：

| 权限 | 说明 |
|------|------|
| `docx:document` | 创建/编辑文档 |
| `docx:document:readonly` | 读取文档 |
| `docx:document.block:convert` | Markdown 转 blocks（write/append 必需） |
| `drive:drive` | 上传图片到文档 |
| `drive:drive:readonly` | 列出文件夹 |

#### 事件订阅 ⚠️

> **这是最容易遗漏的配置！** 如果机器人能发消息但收不到消息，请检查此项。

在飞书开放平台的应用后台，进入 **事件与回调** 页面：

1. **事件配置方式**：选择 **使用长连接接收事件**（推荐）
2. **添加事件订阅**，勾选以下事件：

| 事件 | 说明 |
|------|------|
| `im.message.receive_v1` | 接收消息（必需） |
| `im.message.message_read_v1` | 消息已读回执 |
| `im.chat.member.bot.added_v1` | 机器人进群 |
| `im.chat.member.bot.deleted_v1` | 机器人被移出群 |

3. 确保事件订阅的权限已申请并通过审核

## License

Apache-2.0