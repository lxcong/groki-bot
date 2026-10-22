[English](README.md) | **简体中文**

# Groki Bot 网关

运行在你电脑上的一个 Python 服务，给 [Groki Bot](../README.zh-CN.md) 桌面机器人提供语音能力。它在 Groki Bot 仓库的 `gateway/` 目录；它和固件、Grok Bot 怎么配合，见[架构与通信说明](../docs/architecture.zh-CN.md)。

- **语音对话**：默认通过 Google Gemini Live，也可选择 Grok 登录态语音，用机器人的麦克风和喇叭对话。
- **「Hi Grok」唤醒词**：在电脑上识别，喊到机器人之后网关才把麦克风声音发给所选语音后端。
- **MCP 服务**：Claude Code、Claude Desktop 或其他 MCP 客户端可以让机器人说话、读取它的状态。
- **人脸追踪**：在带摄像头的 Mac 上，机器人转头跟着你的脸（见[人脸追踪](#人脸追踪)）。
- 可选：用语音控制你的 Mac；把难题交给 Claude CLI 回答的 `ask_claude` 语音工具（装了 `claude` 命令行才提供）；Gemini 后端把任务交给 Grok Bot 应用里的智能体、再把回复念出来的 `ask_grokbot` 语音工具（见[可选：把任务交给 Grok Bot](#可选把任务交给-grok-bot)）。

```text
Robot ← XiaoZhi WebSocket :8765 → Gateway ← MCP → Claude / MCP client
Gateway voice → Gemini Live (default) / Grok login voice
Grok voice → localhost:18771 login helper → native Grok Bot
Gemini ask_grokbot → optional localhost:18770 → gbot CLI → Grok Bot
```

## 接入上游 xiaozhi-esp32 固件（小乐）

上游 xiaozhi-esp32 固件（例如小乐板型）不走蓝牙设置页，它在每次开机时向 OTA 地址 POST 系统信息，
并从响应里的 `websocket` 段拿到服务器地址。本网关在 8766 端口提供 `/xiaozhi/ota/`：

- 响应只含 `websocket`（`url` 为 `ws://<设备访问的主机>:WS_PORT/`，设置了 `STACKCHAN_TOKEN` 时附带 `token`）、
  `server_time` 和把设备自身版本原样返回的 `firmware`，不会触发升级，也不含 `mqtt` 段（固件见到 `mqtt` 会改走 MQTT）。
- 主机地址来自设备请求的 Host 头；需要固定时设 `STACKCHAN_OTA_WS_URL=ws://192.168.1.20:8765/`。
- 设备端的唤醒词（如 MultiNet「小乐」）会以 `listen detect` 通知网关，网关端的「Hi Grok」门可以用
  `STACKCHAN_WAKE_WORD=0` 关掉。

设备侧：进入固件的配网页面，「高级」标签里把「自定义 OTA 地址」改成
`http://<本机局域网 IP>:8766/xiaozhi/ota/`，保存后重启；改回原地址即可回到原来的服务器。
（小乐固件把这个地址编译成了默认值，配网页面留空即可。）

对上游固件补齐的小智协议细节（2026-10-20 在小乐板上验证）：
- 用户说的话以 `stt` 发给设备，模型回复的文字随 `tts sentence_start` 发送，设备两边都显示字幕。
- 模型挂断（`end_the_call` / `end_conversation`）、8 秒无人说话或语音会话失效时，网关在再见语音播完后
  关闭设备的 WebSocket；上游固件只有在音频通道关闭后才回到待机并重新开启设备端唤醒词。
- 设备在 `tools/list` 里没有的工具（触摸轮询、舵机、灯光）不再调用，避免设备日志刷 Unknown tool。
- `STACKCHAN_GEMINI_DEVICE_TOOLS=1` 时，语音模型还拿到上游固件自带的设备工具：`get_device_status`、
  `set_device_volume`（设备自己的扬声器音量）、`set_screen_brightness`、`set_screen_theme`，以及小乐板的
  `start_game` / `stop_game`，对应 `self.audio_speaker.set_volume` 等 MCP 工具；「把音量调到 30」「调亮一点」
  「换成深色」「玩抛接球」都能直接操作设备。要控制这台 Mac 的音乐播放和音量，再打开
  `STACKCHAN_MAC_CONTROL=1`（网关要在终端里启动，第一次会弹出允许控制「音乐」的授权）。
- 在设备自己的扬声器上放音乐：`play_music(query)` / `control_music(action)`（`uv sync --all-extras` 会装上解码用的
  PyAV）。曲目来自 `STACKCHAN_MUSIC_DIR`（默认 `~/Music/xiaole`）里的音频文件和 `STACKCHAN_MUSIC_STREAMS` 里的网络电台
  （默认几个 SomaFM 频道）。网关用和朗读一样的方式发送：`tts start`、字幕显示「♪ 曲名」、60 ms Opus 帧、`tts stop`；
  放歌时设备仍在听，用户说话时小乐先回答，回答完音乐继续，说「停」就停。点名字的歌（「放 Golden」）在本地和电台
  里找不到时，会用 yt-dlp 在线搜索第一条结果来播（`STACKCHAN_MUSIC_ONLINE=0` 关闭），搜索大约要 4 秒。
  想让所有「放音乐」都落到设备上，就不要开 `STACKCHAN_MAC_CONTROL`，否则模型可能去控制 Mac 的音乐 App。

### Grok Bot 0.72.0 登录态语音的实测说明

- 登录桥必须在**你自己的终端**里启动：它用 `security` 读钥匙串里的「Grok Bot Safe Storage」，
  第一次会弹授权窗，选「始终允许」；从沙箱或后台服务启动拿不到授权，`/credential` 会一直 502。
- `gbot bots list` 在 0.72.0 上读不到应用会话（agent 清单在云端）。agent ID 可以在 Grok Bot Shell 里
  `ls /home/box/agent-data/agents` 看到，每个目录一个 agent，`profile.json` 里有名字。
- 任务结果回传：0.71.2 通过云端 `voice-call-nudge` outcome 事件；0.72.0 下这条事件不稳定，登录桥改为
  同时监听 agent 对话记录，把标有本次通话（`message.channel` = `voice:<callId>`）的新回复作为结果转发，
  两条来源 15 秒内的同一段文字只转发一次。`STACKCHAN_GBOT_RELAY_ALL_REPLIES=1` 可转发该 agent 的所有新回复。
- `STACKCHAN_GBOT_NATIVE_DEBUG=1` 让登录桥打印每条云端事件的形状（不含正文），用于排查过滤原因。
- 语音通话里的任务和回复带 `voice:` 频道标记，Grok Bot 桌面应用的普通聊天界面不显示它们。

## 需不需要装网关

Groki Bot 固件自己就能对话：刷好固件，在设置页填上自己的 OpenAI 或 Gemini 密钥，不接电脑也能说话。以下情况再装网关：

- 想用「Hi Grok」唤醒词；
- 想让机器人转头跟着你的脸（需要带摄像头的 Mac）；
- 想让 Claude（或其他 MCP 客户端）让机器人说话；
- 想用语音操作电脑（Mac 控制、`ask_claude`）。

**配合 Groki Bot 固件能用的部分**：语音对话、唤醒词、人脸追踪，以及 `speak`、`get_status` 两个 MCP 工具。Groki Bot 固件的 XiaoZhi 客户端声明 `features.mcp=false`，所以驱动硬件的 MCP 工具（`move_head`、`set_led`、`set_avatar`、`take_photo` 等）在这个固件上会返回错误；它们需要实现了 XiaoZhi 设备端 MCP 工具的固件。人脸追踪不走 MCP，它用 XiaoZhi 的 `head` 消息转头，固件能处理这条消息。两个语音后端共用的设备工具（`STACKCHAN_GEMINI_DEVICE_TOOLS=1`）也不需要 MCP：配合这个固件时，`move_head` 以 `head` 消息发送（需要 v0.2.1 之后的固件），`set_avatar` 以表情消息发送，`set_all_leds` 以 `led` 消息发送。摄像头类工具没有这样的通道，在这个固件上不能用。

## 准备

| 项目 | 要求 |
|---|---|
| 机器人 | M5Stack CoreS3 加 Stack-chan 底座，刷好 [Groki Bot 固件](../README.zh-CN.md) |
| 电脑 | macOS（在 Apple Silicon 上测试过）。Linux 预计可以跑语音；Mac 控制只支持 macOS。 |
| 网络 | 机器人和电脑在同一个 Wi-Fi 或局域网 |
| 软件 | [uv](https://docs.astral.sh/uv/)、Git、Opus 音频库（macOS 用 `brew install opus`，Debian/Ubuntu 用 `sudo apt install libopus0`） |
| 密钥 | Gemini Live 用的 Google AI Studio 密钥：<https://aistudio.google.com/apikey> |

## 1. 给机器人刷固件

按 [Groki Bot README 的用法 1](../docs/voice-backends.zh-CN.md#用法-1只刷固件) 操作（用 Chrome 或 Edge 打开网页刷写，可以不填 API 密钥）。机器人开机并连上 Wi-Fi 后回到这里。

## 2. 安装网关

```bash
git clone https://github.com/JosssphZhou/groki-bot.git
cd groki-bot/gateway
uv sync --all-extras
cp .env.example .env
```

打开 `.env`，选择语音后端并设置设备令牌：

- 默认 Gemini：填写 `GEMINI_API_KEY`。Grok 登录态语音按下节配置，不需要这把密钥。
- `STACKCHAN_TOKEN`：一串随机密码，例如用 `openssl rand -hex 16` 生成。建议填写；只有在完全信任的网络里才留空。

注意：PyPI 上名为 `stackchan-mcp` 的包是上游项目（[kisaragi-mochi/stackchan-mcp](https://github.com/kisaragi-mochi/stackchan-mcp)），不含 Gemini 语音和唤醒词。请按上面的方式从本仓库安装。

### Grok Bot 登录态语音（macOS）

默认后端仍是 Gemini。要改用 Grok 语音，请在同一台 Mac 上安装并登录 Grok Bot 桌面应用，并准备好 Node.js。当前接入基于 Grok Bot 0.71.2 的内部接口，后续应用更新、账号权限与使用额度都可能影响可用性。不需要另配 xAI 语音 API 密钥，也不用重新刷写固件。

安装网关后，在 `gateway/.env` 填入自己的智能体名称与 ID：

```dotenv
STACKCHAN_VOICE_BACKEND=gbot
STACKCHAN_GBOT_VOICE_BOT_ID=YOUR_AGENT_ID
STACKCHAN_TOOL_BOT=助手
STACKCHAN_TOOL_BOT_ID=YOUR_AGENT_ID
```

另开一个终端窗口，在 `gateway/` 中启动登录桥并保持运行：

```sh
STACKCHAN_GBOT_VOICE_BOT_ID=YOUR_AGENT_ID \
STACKCHAN_TOOL_BOT=助手 \
STACKCHAN_TOOL_BOT_ID=YOUR_AGENT_ID \
node scripts/gbot_voice_session_bridge.mjs
```

Node 登录桥不会自动读取 `.env`，因此需要如上显式传入相同配置。它读取本机应用的现有登录状态和系统钥匙串，凭据仅保留在内存中，且仅监听 `127.0.0.1:18771`。接着按第 3 步的任意一种方式启动 Python 网关；该后端不需要 `GEMINI_API_KEY`。若要切回 Gemini，将 `STACKCHAN_VOICE_BACKEND` 改回 `gemini`，保留原 Gemini 密钥并重启网关即可。

显式配置智能体名称与匹配的 ID 后，Grok 直接调用原生的 `send_task`、`search_conversations` 与 `end_the_call`，不再增加本地 `ask_grokbot` 包装，也不经过 CLI 转发服务。本地硬件和可选 Mac 工具沿用原有的开关，包括名称仍为 `STACKCHAN_GEMINI_DEVICE_TOOLS` 的设备工具开关。注意会话搜索不等于网页搜索；旧有的 `STACKCHAN_GBOT_VOICE_TOOLS` 开关不会开放未知原生工具。

桌面沙盒环境不可用时，语音仍可正常使用，原生通道会在后台持续重试。在在线状态（presence）和事件流就绪前，任务分配与会话搜索会明确返回暂不可用。受理仅代表系统已接收任务，不代表桌面端聊天界面已可见送达。桌面文字消息订阅仍可能报错，会自动独立重连，断开期间遗漏的消息不会补发。当前版本并未宣称已实现完全的功能一致，也未验证支持纯声学打断。详见[验证范围](GBOT_VOICE_VALIDATION.md)和[延迟测量方法](VOICE_LATENCY_BENCHMARK.md)。

### 唤醒词模型（可选，约 33 MB）

```bash
mkdir -p models/kws && cd models/kws
curl -L -O https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2
tar xf sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2
rm sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2
cd ../..
```

没有模型网关也能用，但机器人连着的时候，麦克风听到的所有声音都会发给所选语音后端。离线检查模型（macOS，会调用系统的 `say` 命令）：

```bash
uv run --all-extras python scripts/kws_offline_repro.py --generate-say
# 命中时输出 hit，没命中时输出 miss
```

### 更换唤醒词

默认唤醒词是「Hi Grok」。唤醒词检测（sherpa-onnx 关键词识别）匹配的是一串读音，换成任何短语都不需要重新训练模型。在 `.env` 里设置后重启网关：

- 改用「Hey Groki」：`STACKCHAN_WAKE_PHRASE=hey groki`（内置，和「Hi Grok」一样带几种元音读法）。
- 自定义短语：`STACKCHAN_WAKE_PHRASE` 填短语，`STACKCHAN_WAKE_KEYWORD` 填它的读音，后面接 `@` 和短语。英文用 CMU 词典音标（ARPAbet，带重音数字）；中文用拼音，声母和韵母分开写，韵母带声调。每个读音都必须出现在模型的 `tokens.txt` 里。例如：

  ```bash
  STACKCHAN_WAKE_PHRASE=小机器人
  STACKCHAN_WAKE_KEYWORD=x iǎo j ī q ì r én @小机器人

  STACKCHAN_WAKE_PHRASE=hello robot
  STACKCHAN_WAKE_KEYWORD=HH AH0 L OW1 R OW1 B AA2 T @hello_robot
  ```

换了唤醒词先离线试一下：`kws_offline_repro.py` 读取同样的设置（`--voice` 换一个 macOS 声音，`--wav` 用自己的录音）。两到三个音节、辅音清楚的短语效果最好；太难唤醒或太容易误触发时，调整 `STACKCHAN_KWS_THRESHOLD` 和 `STACKCHAN_KWS_SCORE`。

## 3. 启动和检查

```bash
uv run --all-extras stackchan-mcp --check     # 检查配置和端口，立即退出
```

然后用下面两种方式之一启动网关。

**A. 让 Claude Code 启动（最简单）**：Claude Code 开着的时候网关就在运行。

```bash
claude mcp add groki -- uv run --all-extras --directory "$(pwd)" stackchan-mcp
```

Claude Desktop 在 `claude_desktop_config.json` 里加入下面的配置（路径换成你克隆的绝对路径）：

```json
{
  "mcpServers": {
    "groki": {
      "command": "uv",
      "args": ["run", "--all-extras", "--directory", "/absolute/path/to/groki-bot/gateway", "stackchan-mcp"]
    }
  }
}
```

**B. 作为后台服务常驻**：不开 MCP 客户端也想和机器人对话时用这种方式。

```bash
./scripts/run_stackchan_gateway_launchd.sh
```

这个脚本让标准输入保持打开，stdio MCP 服务就不会退出。想开机自动启动，可以让 launchd（macOS）或 systemd（Linux）运行它。

8765 和 8766 端口同一时间只能给一个网关用，A 和 B 选一种。

运行起来后，日志里会出现 `server listening on 0.0.0.0:8765`，打开 <http://127.0.0.1:8766/debug/status> 会返回 JSON。

## 4. 让机器人连上网关

在 Groki Bot 蓝牙设置页（<https://josssphzhou.github.io/groki-bot/settings.html>，桌面版 Chrome 或 Edge）的「对话」标签页：

1. 服务商选「XiaoZhi 服务器」。
2. 「XiaoZhi 服务器 URL」填 `ws://<电脑的局域网 IP>:8765`（macOS 用 `ipconfig getifaddr en0` 查 IP，Linux 用 `hostname -I`）。
3. 「XiaoZhi 令牌」填和 `STACKCHAN_TOKEN` 相同的值；`STACKCHAN_TOKEN` 留空时这里也留空。
4. 点「保存并重启」。

这时 `/debug/status` 应显示 `"device": {"connected": true, ...}`。说「Hi Grok」就可以开始对话（设了 `STACKCHAN_WAKE_PHRASE=hey groki` 时说「Hey Groki」，见[更换唤醒词](#更换唤醒词)）。

## 人脸追踪

机器人转头跟着你的脸。Mac 上的一个小程序 [tools/vision-tracker](../tools/vision-tracker/README.md) 用 Apple Vision 看摄像头画面，把人脸位置发到网关的 `http://127.0.0.1:8766/track`；网关把位置换算成头部角度发给机器人。摄像头画面不离开这台 Mac，也不保存。

需要：

- 一台带摄像头的 Mac（macOS 13 或更新）：内置摄像头、Studio Display、用作连续互通相机的 iPhone，或 USB 摄像头都可以。
- CoreS3 加 Stack-chan 底座和两个头部舵机，并且已经连上网关（上面第 4 步）。
- 能处理网关 `head` 消息的固件：v0.2.1 之后的版本，或者从 `main` 编译的固件。旧固件会忽略这条消息，头不会动。
- 编译追踪程序需要 Swift 5.9 或更新（Xcode，或命令行工具：`xcode-select --install`）。

### 编译追踪程序

在仓库根目录运行：

```bash
cd tools/vision-tracker
swift build -c release
```

产物是 `tools/vision-tracker/.build/release/groki-vision-tracker`，网关默认就在这个位置找它。

### 允许使用摄像头

追踪程序第一次打开摄像头时，macOS 会弹窗询问权限，权限记在启动它的那个程序名下：网关在终端里运行时就是这个终端（或 Claude Code 所在的终端、Claude Desktop）。想先把弹窗处理掉，可以在之后启动网关的那个终端里手动运行一次追踪程序，点「允许」，再按 Ctrl-C 结束：

```bash
tools/vision-tracker/.build/release/groki-vision-tracker
```

错过了弹窗，或者网关作为后台服务运行时，到「系统设置 → 隐私与安全性 → 摄像头」里允许对应的程序，再重启网关。没有权限时追踪程序以退出码 2 退出，网关会隔一段时间再试，间隔逐次加长，最长 5 分钟。

### 启动

不需要额外操作：网关启动时自动拉起追踪程序，程序退出了会自动重启。网关一启动，头就开始跟随人脸。追踪程序还没编译时，网关只记一行日志，其余功能照常运行：

```
face tracker unavailable: executable not found at .../tools/vision-tracker/.build/release/groki-vision-tracker (build with: cd tools/vision-tracker && swift build -c release); the head will not follow faces
```

想自己手动运行追踪程序，设 `STACKCHAN_FACE_TRACKER_AUTOSTART=0`，然后运行 `groki-vision-tracker --endpoint http://127.0.0.1:8766/track --fps 8`。

### 确认在工作

1. 网关日志出现 `face tracker started pid=<进程号> endpoint=http://127.0.0.1:8766/track`，追踪程序输出 `Vision tracker camera: <摄像头名>`。
2. 坐到摄像头前，打开 <http://127.0.0.1:8766/debug/status>，看 `face_tracking` 一节：`tracker_running` 是 `true`；摄像头看到你时 `face_reported` 变成 `true`，`last_face_at` 不断更新；`head_follow` 是 `true`，`mode` 是 `idle`。
3. 左右移动：头会转向你并持续跟随，角度在舵机限位之内（默认左右 ±40°，上下 -10° 到 +25°）。摄像头看不到你的脸时，头停止跟随，大约两秒后机器人恢复自己的空闲动作。

### 对话时暂停跟随

对话期间按设计暂停跟随：机器人说话时（`mode` 为 `working`）和唤醒后听你说话时（`mode` 为 `quiet`），网关不发转头指令，免得舵机的声音和动作干扰对话。对话结束后，下一次检测到人脸就恢复跟随。机器人说话时收到的转头指令，固件也会先存着，等这段回复说完再执行。

### 打开和关闭转头跟随

- 用语音（Gemini 或 Grok 网关后端）：说「看着我」打开跟随，说「别看了」关闭。人脸识别在两种状态下都继续运行。
- `STACKCHAN_HEAD_FOLLOW_DEFAULT=0`：网关启动时不跟随，适合没装头部舵机或想让头保持不动的情况。
- `STACKCHAN_FACE_TRACKER_AUTOSTART=0`：完全不启动追踪程序，适合没有摄像头的电脑。

### 选择摄像头

不设置时，追踪程序先选 Studio Display 的摄像头，其次是 iPhone（连续互通相机），再其次是 macOS 列出的第一个摄像头。想指定某个摄像头，把 `STACKCHAN_FACE_TRACKER_CAMERA` 设为它名字里的一段（不区分大小写），例如 `STACKCHAN_FACE_TRACKER_CAMERA=FaceTime`。追踪程序启动时会列出它找到的所有摄像头。

## 可选：把任务交给 Grok Bot

以下是 **Gemini 语音后端的可选 CLI 联动**。Grok 登录态语音直接走原生工具，见 [Grok Bot 登录态语音](#grok-bot-登录态语音macos)，不启动 18770 转发服务。

打开这项功能后，可以说「Hi Grok，帮我查一下明天东京的天气」或「帮我调研一下 X」：Gemini 确认受理后回应，把任务交给 Grok Bot 应用里的智能体，智能体每回一段话，机器人就念一段。闲聊仍由 Gemini 直接回答。完整的消息流程见[架构与通信说明](../docs/architecture.zh-CN.md#4-把任务交给-grok-bot)。

不配置就不会开启。需要在运行网关的同一台电脑上准备：

- 装好并登录 Grok Bot 应用，里面有一个接任务的智能体（或群组）。
- [grok-bot-cli](https://github.com/ScriptedAlchemy/grok-bot-cli) 提供的 `gbot` 命令行工具（需要较新的 Node.js，版本要求见它的 README）：`npm install --global grok-bot-cli`，装好后用 `gbot bots list` 检查。`gbot` 使用应用的登录状态，网关不接触任何 Grok 令牌。

步骤：

1. 从 `gbot bots list` 里选一个智能体名字（也可以新建，例如 `gbot bots create --name 助手`），写进 `.env`：`STACKCHAN_TOOL_BOT=助手`。填了这一项功能才会开启。
2. 另开一个终端（或另设一个后台服务）启动转发服务，保持运行：

   ```bash
   uv run --all-extras stackchan-gbot-proxy
   curl -s http://127.0.0.1:18770/health     # {"ok": true, "service": "gbot-http", ...}
   ```

3. 重启网关，让机器人帮你查点东西。

不接机器人，单独测试转发服务：

```bash
curl -s -X POST http://127.0.0.1:18770/send \
  -H 'Content-Type: application/json' \
  -d '{"text": "用一句话回答：你好", "target": "助手"}'
```

## 配置项

所有配置写在 `.env` 里，启动时读取一次；修改后要重启网关，或者重新连接 MCP 客户端。

| 变量 | 默认值 | 作用 |
|---|---|---|
| `GEMINI_API_KEY` | 无 | 仅 Gemini 后端需要，Grok 登录态语音不需要 |
| `STACKCHAN_GEMINI_VOICE` | `Kore` | Gemini 的音色 |
| `STACKCHAN_GEMINI_MODEL` | `gemini-3.8-live` | Gemini Live 模型（改回 `gemini-3.1-flash-live-preview` 即可退回） |
| `STACKCHAN_TOKEN` | 空 | 与机器人共享的密码，同时保护 `/capture` 和 `/debug/inject-text` |
| `HOST`、`WS_PORT`、`CAPTURE_PORT` | `0.0.0.0`、`8765`、`8766` | 监听地址和端口 |
| `STACKCHAN_WAKE_WORD` | 开 | 设为 `0` 关闭唤醒词 |
| `STACKCHAN_KWS_SCORE` | `7` | 越高越容易唤醒 |
| `STACKCHAN_KWS_THRESHOLD` | `0.05` | 越低越灵敏 |
| `STACKCHAN_KWS_MODEL_DIR` | `./models/kws` | 唤醒词模型位置 |
| `STACKCHAN_WAKE_PHRASE`、`STACKCHAN_WAKE_KEYWORD` | `hi grok` | 唤醒词，见[更换唤醒词](#更换唤醒词)。只设 `STACKCHAN_WAKE_PHRASE=hey groki` 即可改用「Hey Groki」 |
| `STACKCHAN_WAKE_IDLE_S` | `30` | 安静多久后结束这一轮聆听 |
| `STACKCHAN_MAC_CONTROL` | 关 | 设为 `1` 允许语音控制这台 Mac（见「安全」） |
| `STACKCHAN_MUSIC` | 开 | 设为 `0` 时不提供在设备扬声器上放音乐的 `play_music` / `control_music` 工具 |
| `STACKCHAN_MUSIC_DIR` | `~/Music/xiaole` | 本地音乐文件夹（mp3 / m4a / flac / wav / ogg），按文件名点歌 |
| `STACKCHAN_MUSIC_STREAMS` | 三个 SomaFM 电台 | 网络电台列表，`名字=网址;名字=网址`；设为空字符串则不提供电台 |
| `STACKCHAN_MUSIC_ONLINE` | 开 | 本地和电台都没有匹配时，用 yt-dlp 按歌名/歌手在线搜索第一条结果播放；设为 `0` 关闭 |
| `STACKCHAN_FACE_TRACKER_AUTOSTART` | 开 | 设为 `0` 时不启动 Mac 人脸追踪程序（没有摄像头的电脑） |
| `STACKCHAN_FACE_TRACKER_BIN` | `tools/vision-tracker/.build/release/groki-vision-tracker` | 人脸追踪程序的路径 |
| `STACKCHAN_FACE_TRACKER_CAMERA` | 空 | 要用的摄像头名字里的一段；留空按追踪程序的默认顺序选 |
| `STACKCHAN_HEAD_FOLLOW_DEFAULT` | 开 | 设为 `0` 时网关启动后不转头跟随，说「看着我」再打开 |
| `STACKCHAN_GEMINI_DEVICE_TOOLS` | 关 | 设为 `1` 让两个语音后端使用表情、灯光、头部工具。带设备端 MCP 的固件通过 MCP 调用收到；Groki Bot 固件通过 XiaoZhi 的表情、`led`、`head` 消息收到（`head` 需要 v0.2.1 之后的固件） |
| `STACKCHAN_USB_TRANSPORT` | 关 | 设为 `1` 启用 USB 串口控制通道；它会独占 `/dev/cu.usbmodem*` |
| `STACKCHAN_ASK_CLAUDE` | 找到 `claude` 命令行时开启 | 设为 `0` 时即使装了命令行也不提供 `ask_claude` 语音工具 |
| `STACKCHAN_CLAUDE_BIN` | PATH 里的 `claude` | `ask_claude` 使用的 Claude CLI |
| `STACKCHAN_CLAUDE_MODEL` | `claude-sonnet-5` | `ask_claude` 和 Mac 后台任务调用 Claude CLI 时使用的模型 |
| `STACKCHAN_VOICE_BACKEND` | `gemini` | `gbot` 使用 Grok 登录态语音；`xiaozhi` 转发到 XiaoZhi 云服务 |
| `STACKCHAN_GBOT_VOICE_BOT_ID` | 无 | Grok 语音智能体 ID，登录桥与网关必须一致 |
| `STACKCHAN_GBOT_VOICE_URL` | `http://127.0.0.1:18771` | 仅本机 HTTP 登录桥地址 |
| `STACKCHAN_GBOT_VOICE_LANGUAGE` | `zh` | 输入转写语言，如 `en` 或 `auto` |
| `STACKCHAN_GBOT_VOICE_AGENT_NAME` | `Groki` | Grok 语音通话里机器人的名字；存在 `personality.md` 时，文件内容作为通话描述发给 Grok（它是谁、怎么说话） |
| `STACKCHAN_GBOT_VOICE_USER_NAME` | 空 | 可选，Grok 通话里对说话人的称呼 |
| `STACKCHAN_GBOT_VOICE_NAME` | 空（服务端默认，听感同 `ara`） | Grok 音色 id。`scripts/sample_gbot_voices.py` 可以用同一句话给每个音色生成 wav 试听；无效的 id 会静默回落到默认音色 |
| `STACKCHAN_PERSONALITY_FILE` | 本目录的 `personality.md`（存在时） | 追加到 Gemini 指令后面、同时作为 Grok 语音通话描述的人设文字。内置人设很简短：名字叫 Groki，用用户说话的语言回答；这个文件里的内容和它不一致时（名字、语言、风格）以文件为准 |
| `STACKCHAN_TOOL_BOT` | 空（关闭） | 目标智能体名字；启用 Grok 原生桌面联动，或 Gemini 的 `ask_grokbot` |
| `STACKCHAN_TOOL_BOT_ID` | 空 | Grok 原生路径必填，须匹配 `STACKCHAN_GBOT_VOICE_BOT_ID`；Gemini 路径可选 |
| `STACKCHAN_TOOL_BOT_PREFIX` | 内置的朗读提示 | 加在每个任务前面的文字；设为空则原样发送任务（仅 Gemini CLI 路径） |
| `STACKCHAN_GBOT_URL` | `http://127.0.0.1:18770` | 转发服务地址（仅 Gemini CLI 路径） |
| `STACKCHAN_ASK_TIMEOUT` | `110` | 等智能体回复的秒数（仅 Gemini CLI 路径） |
| `STACKCHAN_GBOT_BIN` | PATH 里的 `gbot` | 转发服务使用的 `gbot` 路径（仅 Gemini CLI 路径） |
| `STACKCHAN_GBOT_HTTP_PORT` | `18770` | 转发服务端口（始终只绑定 127.0.0.1）（仅 Gemini CLI 路径） |

## 安全

- **Mac 控制默认关闭。** 设了 `STACKCHAN_MAC_CONTROL=1` 之后，任何在机器人旁边说话的人都能打开应用和网址、调音量、截屏、运行快捷指令，还能启动可能修改网关目录下文件的 `claude -p` 任务。语音提示词要求破坏性操作先口头确认，但这只是给模型的指令，不是强制的权限检查。
- **请设置 `STACKCHAN_TOKEN`。** 不设的话，同一网络里的任何设备都能冒充机器人连上来，能访问 8766 端口的人也能往 `/debug/inject-text` 发文字。
- **Grok Bot 转发服务只绑定 127.0.0.1。** 这台电脑上能访问 18770 端口的任何程序，都能以你的身份给 Grok Bot 智能体发消息。不要把它暴露到网络上。
- **密钥不要进 Git。** `.gitignore` 已经忽略 `.env`。不要把密钥写进脚本、测试文件或 issue。

## 常见问题

| 现象 | 排查 |
|---|---|
| 机器人一直连不上 | 是否在同一网络？防火墙是否允许 8765 入站？地址是否以 `ws://` 开头（不是 `wss://`）并且用的是电脑的局域网 IP？日志出现 `ESP32 auth rejected` 表示 token 不一致。 |
| 连上了但不回答 | 是否填了 `GEMINI_API_KEY`？看 `/debug/status` 里的 `gemini.last_error`。错误里有 `reported as leaked` 表示 Google 已经停用这把密钥，需要新建一把。 |
| 连接 Gemini 超时 | 电脑需要能访问 Google 的 Gemini 接口。在无法直连 Google 的地区，需要让网关通过可用的网络代理运行。 |
| 喊了唤醒词没反应 | 模型是否下载到 `models/kws`？日志里有没有 `KWS ready`？可以试 `STACKCHAN_KWS_THRESHOLD=0.03`，或运行 `scripts/kws_offline_repro.py`。`STACKCHAN_WAKE_WORD=0` 可关闭唤醒词。 |
| 报 `sherpa-onnx unavailable` 或 `Library not loaded: libonnxruntime` | 重新运行 `uv sync --all-extras`，它会安装自带运行库的 `sherpa-onnx-core`。 |
| 报 `Could not find Opus library` | 安装 Opus（`brew install opus` 或 `apt install libopus0`）。 |
| 语音回答“桌面助手暂时连不上”，日志里 `send_task outcome=native_not_ready`、`Grok native presence ... status=409` | 登录桥还把上一通通话记成活动通话（网关被杀掉时没来得及注销）。现在旧通话没有事件流时会被新通话自动接管；若仍 409，重启登录桥（它会在终端里打印 `stale_call_replaced`）。 |
| 设备在听，但没有 `user transcript` | 看每 3 秒一行的 `device mic level`：安静时约 -60 dBFS、说话时应到 -30 dBFS 以上。设 `STACKCHAN_MIC_DUMP=/path/mic.raw` 启动网关可把设备送来的 16 kHz 单声道 s16le 原样存盘分析；固件侧可开 `CONFIG_USE_AUDIO_DEBUGGER` 抓 AFE 之前的双通道原始输入。 |
| 头不跟着人脸转 | 看 `/debug/status` 的 `face_tracking`。是 `null`：网关版本太旧或没有重启。`tracker_running: false`：先编译追踪程序，或在日志里找 `face tracker exited returncode=2`（没有摄像头或没有摄像头权限，见[允许使用摄像头](#允许使用摄像头)）。`face_reported: false`：摄像头没看到人脸。`head_follow: false`：说「看着我」。这些都正常但头不动：固件太旧，不认 `head` 消息，需要更新固件。 |
| 刷固件提示串口被占用 | 如果开了 `STACKCHAN_USB_TRANSPORT`，先停掉网关。 |
| 改了 `.env` 不生效 | 重启网关；用方式 A 时在 Claude Code 里用 `/mcp` 重新连接。 |
| Gemini 路径提示任务没送到 | `stackchan-gbot-proxy` 在运行吗（`curl http://127.0.0.1:18770/health`）？终端里 `gbot bots list` 能用、并且列出了 `STACKCHAN_TOOL_BOT` 里的名字吗？Grok Bot 应用登录了吗？ |

## MCP 工具

| 工具 | 配合 Groki Bot 固件 |
|---|---|
| `get_status` | 可用：网关和机器人的连接状态 |
| `speak(text)` | 可用：网关合成语音，在机器人上播放 |
| `say(text, voice?)` | 需要另外安装 VOICEVOX 引擎 |
| `set_voice_mode` | 进阶功能：把转写的语音送进终端会话，默认关闭 |
| `get_device_info`、`set_volume`、`set_brightness`、`move_head`、`get_head_angles`、`set_avatar`、`set_mouth`、`set_mouth_sequence`、`set_blink`、`set_led`、`set_leds`、`set_all_leds`、`clear_leds`、`set_background_color`、`get_touch_state`、`take_photo`、`install_avatar_assets`、`check_vm_en`、`gpio_test`、`uart_diag` | 需要带 XiaoZhi 设备端 MCP 工具的固件 |

## 开发

```bash
uv sync --all-extras
NO_PROXY="*" uv run --all-extras pytest -q
```

## 致谢与许可证

本网关派生自 [kisaragi-mochi/stackchan-mcp](https://github.com/kisaragi-mochi/stackchan-mcp)，该项目源自 [stack-chan](https://github.com/mongonta0716/stack-chan) 社区。网关使用 [XiaoZhi](https://github.com/78/xiaozhi-esp32) 的 WebSocket 协议，唤醒词使用 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)。

采用 MIT 许可证，见 [LICENSE](LICENSE)。Groki Bot 仓库的其余部分（固件）使用各自的许可证，见 [../LICENSE](../LICENSE) 和 [../THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。`gbot` 命令行（[grok-bot-cli](https://github.com/ScriptedAlchemy/grok-bot-cli)，MIT）是外部工具，不包含在本仓库。
