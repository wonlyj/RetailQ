# RetailQ · 第 1 章：纯对话客服

Python 3.12 + FastAPI + LangChain，统一直连 OpenAI Chat Completions 兼容上游。支持 SSE 流式中文客服、客户端携带历史、预算裁剪、售后结构化 JSON。不含工具调用、Agent、查询订单或实际人工转接。

## 安装与配置（Windows PowerShell）

```powershell
py -3.12 -m venv .venv
$ProjectPython = '.\.venv\Scripts\python.exe'
& $ProjectPython --version
& $ProjectPython -m pip install -r requirements-dev.lock
& $ProjectPython -m pip check
Copy-Item .env.example .env  # 仅首次执行；已有 .env 时不要覆盖
```

若 Python 3.12 未注册到 `py`，用本机实际的 Python 3.12 完整路径执行 `-m venv .venv`。开发环境实际使用 3.12.13；不要用系统默认的 3.13 / 3.14。仅运行服务可安装 `requirements.lock`。锁文件来自项目隔离环境的实际解析结果，未安装项目至全局环境。

在本地 `.env` 填写：

```dotenv
OPENAI_BASE_URL=https://your-openai-compatible-service.example/v1
OPENAI_API_KEY=your-local-key
OPENAI_MODEL=your-model-name
STRUCTURED_OUTPUT_METHOD=json_mode
CONTEXT_WINDOW_TOKENS=8192
MAX_OUTPUT_TOKENS=1024
TOKEN_SAFETY_MARGIN=512
MODEL_TIMEOUT_SECONDS=60
```

占位地址 / 模型 / 密钥需替换；不要提交 `.env`。环境变量优先于 `.env`。Ollama 等无鉴权服务可填占位密钥，但其端点仍需支持选定的结构化模式。不同供应商须各自实测：GPT / Claude / DeepSeek / Ollama 的可切换性以端点实际支持 OpenAI Chat Completions、SSE、JSON mode / JSON schema 为前提；不自动改用原生 SDK、网关或工具调用。第三方扩展推理字段不透传。

```powershell
& $ProjectPython -m uvicorn retailq.app:app --host 127.0.0.1 --port 8000
```

另开终端进入项目目录。`GET /health` 只检查存活和配置完整性，不证明模型可用。接口交互文档见 [本机 Swagger](http://127.0.0.1:8000/docs)。

## curl 验收

使用 `curl.exe`，避免 PowerShell 的 `curl` 别名。示例 JSON 文件均为 UTF-8。

```powershell
curl.exe -sS http://127.0.0.1:8000/health
curl.exe -N -sS http://127.0.0.1:8000/api/chat/stream -H 'Content-Type: application/json' --data-binary '@examples/chat-turn1.json'
```

SSE 依次返回 `meta`、若干 `token`、`done`；data 为 JSON，例如：

```text
event: token
data: {"text":"您好"}

event: done
data: {"finish_reason":"stop"}
```

每个 token 事件就是上游一个非空文本增量，可能含多个模型 token。不会等完整回复生成后再分字符输出。错误发 `error` 并终止，没有 `done`；输入错误在流前返回 HTTP 422，缺配置返回 503。`length` 等结束原因原样暴露，不把截断回复当成正常完成。

将第一轮所有 token 文本拼接成完整回复，替换 `examples/chat-turn2.json` 中 assistant 的占位内容，再发送第二轮：

```powershell
curl.exe -N -sS http://127.0.0.1:8000/api/chat/stream -H 'Content-Type: application/json' --data-binary '@examples/chat-turn2.json'
curl.exe -sS http://127.0.0.1:8000/api/after-sales/extract -H 'Content-Type: application/json' --data-binary '@examples/after-sales.json'
```

售后响应示例（实际措辞可能不同）：

```json
{"order_id":"00123","request_type":"exchange","expected_solution":"换成42码"}
```

`request_type` 可为 refund / return_refund / exchange / repair / logistics / other / unknown。订单号或期望方案缺失时为 null；多订单无法判定唯一订单号时也为 null；纯投诉归 other。结构化校验失败为 502，超时为 504，不把模型原文伪装成成功 JSON。

history 只允许从 user 开始、assistant 结束的完整交替轮次，最多 100 条；消息和描述非空，每条最多 8000 Unicode 字符，未知字段拒绝。服务端无会话存储。仅在收到完整 `done` 且 `finish_reason=stop` 后，将该轮完整回复加入下一轮历史。

## 测试、真实 smoke 与 Prompt 评估

```powershell
$ProjectPython = '.\.venv\Scripts\python.exe'
& $ProjectPython -m pytest -q
& $ProjectPython -m scripts.smoke --base-url http://127.0.0.1:8000 --output artifacts/live/smoke.json
& $ProjectPython -m scripts.evaluate --base-url http://127.0.0.1:8000 --output artifacts/evals/ch01/run-01
```

smoke 自动完成真实第一轮 → 将真实回复写入历史 → 第二轮指代 → 售后提取，保存首块 / 总耗时与增量数。自动测试用本机可控协议上游；首 token 及时到达以真实 TCP 和事件屏障验证，取消 / 超时后检查上游释放。自动测试通过不能代替真实模型验收。

标注集包含 12 条对话和 12 条售后描述。评估报告保存于所选 output 目录的 `report.json`，每例都保存实际输出和 rubric；自动检查订单号 / 枚举 / null / 关键事实，人工核对中文语气、职责边界、红线和释义。按实际内容填写每例 `review.status` 为 pass / fail，`review.reason` 为具体理由，再汇总：

```powershell
& $ProjectPython -m scripts.evaluate --review artifacts/evals/ch01/run-01/report.json
```

退出码：0 为全部通过；1 为有失败；2 为模型未配置或仍有待评项。待人工核对的项不算通过。每次调整 Prompt 后使用新目录保存重跑结果。报告中的 configured_model 来自本地配置，不宣称已独立验证上游真实模型身份；报告不含密钥或完整上游端点，`artifacts/` 不提交。

token 预算采用 UTF-8 字节数加消息开销的保守估算，配合输出预留与安全余量；会裁剪旧的完整轮次，保留系统和当前问题。估算不是供应商精确 tokenizer，部署时必须按真实模型上下文窗口调整配置。提取同时预留 schema 开销；超预算直接 422，不截断本轮输入。SDK 不自动重试，避免重复流式内容。

## 过程文件

- 设计：`docs/superpowers/specs/2026-10-22-ch01-pure-chat-design.md`
- 计划：`docs/superpowers/plans/2026-10-22-ch01-pure-chat.md`
- 分阶段开发记录：`dev-notes/ch01.md`

客服人设与红线在 `retailq/prompts/customer_service.txt`，提取规则在 `retailq/prompts/after_sales.txt`。不虚构实时业务信息，退款政策统一“以平台售后规则为准”；“会转人工核实”是指引，不代表本阶段已执行转接。
