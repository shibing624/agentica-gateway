# Agentica Gateway

Agentica Gateway 服务，调用 [agentica](https://github.com/shibing624/agentica) SDK。

支持Lark(飞书)、WeCom(企微)、Telegram、Discord、Gradio 等多渠道接入的 AI Agent 网关。

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
MODEL_NAME=glm-4.7-flash

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

```bash
# 开发模式
uvicorn src.main:app --reload --port 8789

# 生产模式
agentica-gateway
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

## 目录结构

```
agentica-gateway/
├── src/
│   ├── __init__.py
│   ├── main.py              # FastAPI 主入口
│   ├── config.py            # 配置管理
│   │
│   ├── services/            # 服务层
│   │   ├── agent_service.py # Agent 服务封装
│   │   ├── channel_manager.py
│   │   ├── router.py        # 消息路由
│   │   └── scheduler.py     # 定时任务
│   │
│   └── channels/            # 渠道实现
│       ├── base.py          # 抽象基类
│       ├── gr.py            # Gradio WebUI
│       ├── feishu.py        # 飞书
│       ├── telegram.py      # Telegram
│       └── discord.py       # Discord
│
├── pyproject.toml
├── requirements.txt
└── .env.example
```

## 飞书机器人配置

1. 在 [飞书开放平台](https://open.feishu.cn) 创建应用
2. 事件与回调 → 订阅方式改为「使用长连接接收事件」
3. 添加事件：`im.message.receive_v1`
4. 权限管理 → 添加 `im:message`、`im:message:send_as_bot`

## 对比 OpenClaw

| 功能 | agentica-gateway | OpenClaw |
|------|------------------|----------|
| 语言 | Python | TypeScript |
| Agent SDK | agentica | 自实现 |
| Gateway | FastAPI + WebSocket | Express + WebSocket |
| 渠道 | Gradio/飞书/Telegram/Discord | Telegram/Discord/Slack |
| 消息路由 | ✅ | ✅ |
| 流式输出 | ✅ | ✅ |
| 定时任务 | APScheduler | node-cron |

## License

MIT
