# 出门也能用：用 Cloudflare Tunnel 把网关放到公网

机器人（xiaozhi 固件）启动时 POST `/xiaozhi/ota/` 拿到 WebSocket 地址，再用 WebSocket 传音频和 MCP。默认两者都指向网关电脑的局域网 IP，所以只有同一 Wi-Fi 下才能用。

Cloudflare Tunnel（`cloudflared`）从网关电脑主动连到 Cloudflare，公网请求经 Cloudflare 转回本机。路由器不用开端口、不需要公网 IP，Cloudflare 负责 TLS 证书，免费套餐即可。WebSocket 在所有套餐都支持；空闲连接会被 Cloudflare 关闭，但网关每 20 秒发一次 WebSocket ping，固件会自动回 pong，所以连接不会被判为空闲。

```text
机器人 ──wss://xiaole.example.com/────► Cloudflare ──► cloudflared ──► 127.0.0.1:8765 (WebSocket)
机器人 ──https://xiaole.example.com/xiaozhi/ota/──► Cloudflare ──► cloudflared ──► 127.0.0.1:8766 (OTA)
```

## 安全设计

| 层 | 做法 |
|---|---|
| 传输 | 公网只有 `https://` / `wss://`，固件用 ESP-IDF 证书包校验 Cloudflare 证书 |
| 路由 | `config.yml` 只把 `/xiaozhi/ota` 和 `/xiaozhi/assets/`（哈希命名的形象包，无秘密）转到 8766，其余路径转到 8765。`/capture`、`/debug/*` 不走隧道 |
| 网关兜底 | 带 `CF-Connecting-IP` 头的请求（即经隧道进来的）在 8766 上只能访问 `/xiaozhi/ota` 和 `/xiaozhi/assets/`，其它一律 404 |
| 共享密钥 | `STACKCHAN_TOKEN` 只在局域网内的 OTA 回复里下发；经隧道的 OTA 回复不含 token。机器人在家里连过一次后 token 已存在 NVS，之后回复里没有 token 也沿用原值。WebSocket 握手没有正确的 `Authorization: Bearer <token>` 直接 401 |
| 设备白名单 | `STACKCHAN_DEVICE_IDS`（机器人 MAC，即 `Device-Id` 头）。设置后其它设备即使有 token 也 403 |

可选的加固（Cloudflare 控制台）：对该主机名加 WAF 自定义规则，要求请求带 `Authorization` 头；加速率限制规则。

## 步骤

### 1. 一次性准备（浏览器里完成）

1. 一个托管在 Cloudflare DNS 的域名（免费套餐）。
2. 网关电脑上登录：

   ```sh
   brew install cloudflared
   cloudflared tunnel login
   ```

   浏览器里选域名授权后，生成 `~/.cloudflared/cert.pem`。

### 2. 建隧道、DNS、开机自启

```sh
cd gateway
scripts/cloudflare_tunnel_setup.sh xiaole.example.com
```

脚本幂等：创建隧道 `xiaole-gateway`、写 `~/.cloudflared/config.yml`、校验 ingress 规则、建 CNAME、以 launch agent 方式随登录启动。日志在 `~/Library/Logs/com.cloudflare.cloudflared.err.log`。

### 3. 网关

`gateway/.env`：

```ini
STACKCHAN_TOKEN=<已有>
STACKCHAN_OTA_WS_URL=wss://xiaole.example.com/
STACKCHAN_DEVICE_IDS=7c:0c:5f:42:41:6c
```

重启网关。验证：

```sh
curl -sS -X POST https://xiaole.example.com/xiaozhi/ota/ -H 'Device-Id: 7c:0c:5f:42:41:6c'   # 有 websocket.url，没有 token
curl -sS -o /dev/null -w '%{http_code}\n' https://xiaole.example.com/debug/status             # 404
```

### 4. 固件

把 OTA 地址改成公网地址，重新编译烧录：

`main/boards/waveshare/esp32-s3-touch-amoled-1.75c-xiaole/config.json`
→ `CONFIG_OTA_URL="https://xiaole.example.com/xiaozhi/ota/"`

不想重刷时，也可以在固件的 Wi-Fi 配网页里改 OTA 地址（存在 NVS 的 `wifi/ota_url`，优先于编译值）。

机器人先在家里连一次（拿到 token），之后带出门即可。

## 开机自动启动

Mac mini 重启后要自动恢复整条链路，需要四样东西随登录拉起：cloudflared、网关、Grok 登录桥、Grok Bot 应用。全部用用户级 launchd 代理 / 登录项，而不是 sudo 守护进程，原因是登录桥要读登录钥匙串、网关的 Mac 控制要用 osascript，都只能在登录会话里运行。

```sh
cd gateway
scripts/install_launch_agents.sh        # 网关 + 登录桥；再执行一次即重装
scripts/install_launch_agents.sh uninstall
```

- `com.xiaole.groki-gateway`：`scripts/run_stackchan_gateway_launchd.sh`，日志 `~/Library/Logs/xiaole-gateway.log`
- `com.xiaole.gbot-login-bridge`：`scripts/run_gbot_login_bridge_launchd.sh`（先导出 `.env` 再启动 node），日志 `~/Library/Logs/xiaole-login-bridge.log`
- `com.cloudflare.cloudflared`：由 `cloudflare_tunnel_setup.sh` 安装
- Grok Bot.app：系统设置 → 通用 → 登录项

改了 `.env` 或代码后重启网关：

```sh
launchctl kickstart -k gui/$(id -u)/com.xiaole.groki-gateway
```

这台机器开着 FileVault，重启后停在磁盘解锁界面，输一次密码后会直接进入登录会话，上面的代理随即启动。不想每次输密码，只能关闭 FileVault 并打开自动登录（系统设置 → 用户与群组），这是安全性换便利的取舍。另外可以让它断电恢复后自动开机：`sudo systemsetup -setrestartpowerfailure on`。

## 取舍

- 用错误 token 做 WebSocket 握手时，有的客户端会看到 Cloudflare 返回的 502 而不是网关的 401（网关日志里仍是 `ESP32 auth rejected`）。网关在拒绝响应里加了 Content-Length 后应显示为真实状态码。

- 在家时流量也绕 Cloudflare，语音往返多几十毫秒。要省这点延迟，可以保留局域网 OTA 地址、只在出门前切换，但那样需要两套配置。
- Cloudflare 发布更新时会重启节点，WebSocket 会断一次；固件自带重连。
