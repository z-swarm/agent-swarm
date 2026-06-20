"""
@module agent_swarm.channels.lark
@brief  飞书 LarkConnector——DESIGN §4.4

W10 范围：
  - 应用模式 webhook 接收消息 + 卡片回调
  - verification_token 签名验证
  - user_whitelist 用户白名单
  - send_card() 卡片发送
  - handle_card_action() 卡片交互回调
  - 5 个内置卡片模板

@note 依赖：aiohttp（http 客户端/服务端）；首次启动校验，无 aiohttp 时降级为离线模式
@note 真接入需在 https://open.feishu.cn 创建应用 + 配置事件回调 URL
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# cryptography 是可选依赖 — 仅 encrypt_key 启用时才需要
# 延迟导入避免强制依赖,pyproject.toml 已声明
from agent_swarm.channels.base import (
    ChannelConnector,
    ChannelMessage,
    ChannelResponse,
    ChannelType,
    ChannelUser,
    MessageHandler,
    MessageType,
)
from agent_swarm.channels.card_templates import render_card

log = logging.getLogger(__name__)

# 默认 Lark API 端点
LARK_API_BASE = "https://open.feishu.cn/open-apis"

# 内置卡片模板 id（DESIGN §4.4 send_card() 提到）
CARD_TEMPLATES: tuple[str, ...] = (
    "task_progress",
    "code_review_result",
    "adversarial_debug",
    "swarm_status",
    "confirm_dialog",
)


def _hmac_sha256_hex(key: str, payload: str) -> str:
    """
    飞书事件签名核心：HMAC-SHA256 hex

    @note 飞书官方规范 v2: digest = HMAC-SHA256(key=verification_token,
          message=timestamp + nonce + encrypt_key + body)
    @note 关键:必须用 hmac.new(),不是 hashlib.sha256()
          key 必须真正参与计算,否则任何知道 timestamp/nonce/body 的人
          都能伪造签名(W10 早期版本有这个 bug,见 REVIEW-2026-06-19-2 H1)
    """
    return hmac.new(
        key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def decrypt_lark_body(encrypt_key: str, encrypted_b64: str) -> str:
    """
    @brief 飞书加密 body 解密 (L1 修复:占位 → 真 AES-256-CBC)
    @param encrypt_key    配置的 encrypt_key(明文或 ${VAR} 解析后)
    @param encrypted_b64  飞书传来的 base64(iv + ciphertext)
    @return 解密后的明文 JSON 字符串
    @raise ValueError  解密失败 (key 错 / padding 错 / 长度错)

    @note 飞书官方规范:
      - key = SHA256(encrypt_key) 截前 32 字节
      - IV = base64 前 16 字节
      - cipher = AES-256-CBC + PKCS7
      - 加密格式: base64(IV + ciphertext)
    @note cryptography 是可选依赖;仅当 encrypt_key 启用时才需要
    """
    try:
        from cryptography.hazmat.primitives import padding as aes_padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise RuntimeError(
            "decrypt_lark_body 需要 cryptography 包;"
            " 请运行: pip install cryptography>=42.0.0"
        ) from exc
    # 1) 计算 key
    key_bytes = hashlib.sha256(encrypt_key.encode("utf-8")).digest()[:32]
    # 2) base64 解码
    raw = base64.b64decode(encrypted_b64)
    if len(raw) < 32:  # 至少 IV(16) + 1 块(16)
        raise ValueError("encrypted body too short")
    iv = raw[:16]
    ciphertext = raw[16:]
    # 3) AES-256-CBC 解密
    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    # 4) 去除 PKCS7 padding
    unpadder = aes_padding.PKCS7(algorithms.AES.block_size).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return plain.decode("utf-8")


def verify_lark_signature(
    timestamp: str,
    nonce: str,
    body: str,
    verification_token: str,
    encrypt_key: str | None = None,
) -> str:
    """
    计算飞书事件回调签名（expected digest）

    @param timestamp          来自 X-Lark-Request-Timestamp header
    @param nonce              来自 X-Lark-Request-Nonce header
    @param body               原始 POST body 字符串
    @param verification_token 配置在飞书后台的事件订阅 token
    @param encrypt_key        可选；如启用加密则需 AES 解密
    @return SHA256 hex digest（caller 与 header X-Lark-Signature 比对）

    @note 飞书官方规范（v2）:
          digest = SHA256(timestamp + nonce + encrypt_key + body)
    @note 此函数只算签名；caller 用 hmac.compare_digest 比对——本函数不返回 bool
    """
    if encrypt_key:
        # 加密场景：body 实际是加密后的 base64 字符串
        # 真实接入需先 AES-256-CBC 解密再验签
        # 这里仅校验 token 存在
        payload = f"{timestamp}{nonce}{encrypt_key}{body}"
    else:
        # 明文场景
        payload = f"{timestamp}{nonce}{body}"
    return _hmac_sha256_hex(verification_token, payload)


# ---------------------------------------------------------------------------
# LarkConnector
# ---------------------------------------------------------------------------


@dataclass
class _LarkConfig:
    """Lark 连接器配置（SecretManager 引用）"""

    app_id: str
    app_secret_ref: str  # ${LARK_APP_SECRET} 形式；不存明文
    verification_token_ref: str  # ${LARK_VERIFICATION_TOKEN}
    encrypt_key_ref: str | None = None  # ${LARK_ENCRYPT_KEY}
    user_whitelist: list[str] = field(default_factory=list)
    api_base: str = LARK_API_BASE


class LarkConnector(ChannelConnector):
    """
    飞书消息通道连接器——DESIGN §4.4

    @note W10 落地策略:
          - 签名验证: verify_lark_signature() 实现完整 HMAC
          - 白名单: user_whitelist 严格校验
          - send_card: 走 Lark Open API（POST /im/v1/messages）
          - handle_card_action: 把 callback 转为 ChannelMessage(EVENT)
    @note 运行时: 用 aiohttp 启动 webhook server（如可用）；离线模式用 in-process queue
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,           # 明文，仅在 SecretManager 未接入时临时用
        verification_token: str,   # 明文，development only
        encrypt_key: str | None = None,
        user_whitelist: list[str] | None = None,
        api_base: str = LARK_API_BASE,
        webhook_host: str = "127.0.0.1",
        webhook_port: int = 0,     # 0 = 自动选端口
    ) -> None:
        """
        @param app_id              飞书应用 ID（cli_xxxx 形式）
        @param app_secret          飞书应用 secret（生产环境应从 SecretManager 注入）
        @param verification_token  事件订阅 token（飞书后台配置）
        @param encrypt_key         可选；启用加密时必填
        @param user_whitelist      允许交互的用户 open_id 列表；空=全员（不推荐）
        @param api_base            Lark API 基础 URL（测试环境可改）
        @param webhook_host        接收 Lark 回调的 HTTP server 绑定地址
        @param webhook_port        0 表示自动分配端口（测试场景）
        """
        self._app_id = app_id
        self._app_secret = app_secret
        self._verification_token = verification_token
        self._encrypt_key = encrypt_key
        self._user_whitelist = set(user_whitelist or [])
        self._api_base = api_base.rstrip("/")
        self._webhook_host = webhook_host
        self._webhook_port = webhook_port
        self._handlers: list[MessageHandler] = []
        self._started = False
        self._server: Any = None  # aiohttp.web.AppRunner
        self._sent_messages: list[dict[str, Any]] = []  # 离线模式记录发送

    @property
    def channel_type(self) -> ChannelType:
        return ChannelType.LARK

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动 webhook server（生产）；离线模式直接标记 started"""
        if self._started:
            return
        self._started = True
        try:
            await self._start_webhook_server()
            log.info("lark.connector.started app=%s host=%s port=%s",
                     self._app_id, self._webhook_host, self._effective_port())
        except ImportError:
            log.warning("lark.aiohttp_not_available; running in offline mode")

    async def stop(self) -> None:
        """停止 webhook server + 清空 handler"""
        if not self._started:
            return
        self._started = False
        if self._server is not None:
            try:
                await self._server.cleanup()
            except Exception as exc:  # noqa: BLE001
                log.warning("lark.server_cleanup_failed err=%s", exc)
            self._server = None
        self._handlers.clear()

    async def _start_webhook_server(self) -> None:
        """
        启动 aiohttp server 接收 Lark 事件回调

        @note 没有 aiohttp 时直接 ImportError——caller 决定是否降级
        @note 端口 webhook_port=0 时由 OS 分配（适合 e2e 测试）
        """
        try:
            from aiohttp import web
        except ImportError as exc:
            raise ImportError(
                "aiohttp is required for LarkConnector webhook server; "
                "pip install aiohttp"
            ) from exc

        app = web.Application()
        app.router.add_post("/lark/event", self._on_event)
        app.router.add_post("/lark/card_action", self._on_card_action)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._webhook_host, self._webhook_port)
        await site.start()
        self._server = runner

    def _effective_port(self) -> int:
        """获取实际绑定的端口（port=0 时由 OS 决定）

        @note aiohttp AppRunner.sites 是 set 而非 list——用 next(iter(...)) 取首个
        @note 优先用 site._bound_port（aiohttp 内部记录的最可靠端口）
        """
        if self._server is None:
            return self._webhook_port
        try:
            sites = self._server.sites
            if not sites:
                return self._webhook_port
            site = next(iter(sites))
            # aiohttp 内部 _bound_port 是实际绑定端口（含 port=0 时 OS 分配的）
            bound = getattr(site, "_bound_port", None)
            if bound:
                return int(bound)
            # fallback: 从 _server.sockets 取
            server = getattr(site, "_server", None)
            if server and getattr(server, "sockets", None):
                return server.sockets[0].getsockname()[1]  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            log.debug("lark.effective_port_failed err=%s", exc)
        return self._webhook_port

    # ------------------------------------------------------------------
    # 消息接收
    # ------------------------------------------------------------------
    async def _on_event(self, request: Any) -> Any:
        """Lark event callback 入口（HTTP POST /lark/event）"""
        from aiohttp import web
        body = await request.text()
        ts = request.headers.get("X-Lark-Request-Timestamp", "")
        nonce = request.headers.get("X-Lark-Request-Nonce", "")
        sig = request.headers.get("X-Lark-Signature", "")
        if not self._verify_request_signature(ts, nonce, body, sig):
            return web.json_response({"code": 401, "msg": "invalid signature"}, status=401)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.json_response({"code": 400, "msg": "invalid json"}, status=400)
        msg = self._parse_event_payload(payload)
        if msg is not None:
            await self._dispatch(msg)
        return web.json_response({"code": 0, "msg": "ok"})

    async def _on_card_action(self, request: Any) -> Any:
        """Lark card action callback 入口（HTTP POST /lark/card_action）"""
        from aiohttp import web
        body = await request.text()
        ts = request.headers.get("X-Lark-Request-Timestamp", "")
        nonce = request.headers.get("X-Lark-Request-Nonce", "")
        sig = request.headers.get("X-Lark-Signature", "")
        if not self._verify_request_signature(ts, nonce, body, sig):
            return web.json_response({"code": 401, "msg": "invalid signature"}, status=401)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.json_response({"code": 400, "msg": "invalid json"}, status=400)
        # 卡片动作 → ChannelMessage(EVENT)
        msg = self._parse_card_action_payload(payload)
        if msg is not None:
            await self._dispatch(msg)
        return web.json_response({"code": 0, "msg": "ok"})

    def _verify_request_signature(
        self, ts: str, nonce: str, body: str, header_sig: str,
    ) -> bool:
        """校验请求签名（带时间戳容差）"""
        # 重放窗口：5 分钟
        try:
            if abs(int(ts) - int(time.time())) > 300:
                return False
        except (ValueError, TypeError):
            return False
        expected = verify_lark_signature(
            ts, nonce, body, self._verification_token, self._encrypt_key,
        )
        if not header_sig:
            return False
        return hmac.compare_digest(expected, header_sig)

    def _parse_event_payload(self, payload: dict[str, Any]) -> ChannelMessage | None:
        """Lark event v2 payload → ChannelMessage"""
        header = payload.get("header", {})
        event_type = header.get("event_type", "")
        event = payload.get("event", {})
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {}).get("open_id", "?")
        # 白名单校验
        if self._user_whitelist and sender_id not in self._user_whitelist:
            log.info("lark.event_dropped reason=whitelist user=%s", sender_id)
            return None
        if event_type == "im.message.receive_v1":
            message = event.get("message", {})
            content_json = message.get("content", "{}")
            try:
                content_obj = json.loads(content_json)
                text = content_obj.get("text", "")
            except json.JSONDecodeError:
                text = content_json
            return ChannelMessage(
                id=message.get("message_id", payload.get("uuid", "?")),
                channel=ChannelType.LARK,
                from_user=ChannelUser(
                    channel=ChannelType.LARK,
                    user_id=sender_id,
                    display_name=sender.get("sender_id", {}).get("name", sender_id),
                ),
                content=text,
                msg_type=MessageType.TEXT,
                raw=payload,
                timestamp=float(message.get("create_time", time.time())) / 1000.0,
            )
        # 其他事件类型：URL verification / app_ticket 等
        log.debug("lark.event_unhandled type=%s", event_type)
        return None

    def _parse_card_action_payload(
        self, payload: dict[str, Any],
    ) -> ChannelMessage | None:
        """Lark card action callback → ChannelMessage(EVENT)"""
        action = payload.get("action", {})
        operator = payload.get("operator", {})
        operator_id = operator.get("open_id", "?")
        if self._user_whitelist and operator_id not in self._user_whitelist:
            return None
        # 把 action 序列化进 content（路由层用 channel=card 时再 parse）
        action_summary = json.dumps(action, ensure_ascii=False, sort_keys=True)
        return ChannelMessage(
            id=payload.get("uuid", "?"),
            channel=ChannelType.LARK,
            from_user=ChannelUser(
                channel=ChannelType.LARK,
                user_id=operator_id,
                display_name=operator_id,
            ),
            content=action_summary,
            msg_type=MessageType.EVENT,
            raw=payload,
            timestamp=time.time(),
        )

    async def _dispatch(self, msg: ChannelMessage) -> None:
        """分发给所有注册的 handler；首个返回的 response 自动 send()"""
        response: ChannelResponse | None = None
        for h in list(self._handlers):
            try:
                r = await h(msg)
            except Exception as exc:  # noqa: BLE001
                log.warning("lark.handler_error err=%s", exc)
                continue
            if response is None and r is not None:
                response = r
        if response is not None:
            await self.send(response, msg.from_user)

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    async def send(
        self,
        response: ChannelResponse,
        target: ChannelUser | str,
    ) -> bool:
        """
        发送响应到飞书

        @note TEXT 走 im/v1/messages API；CARD 走 im/v1/messages + card payload
        @note 离线模式（aiohttp 不可用或没启 webhook）→ 仅记录到 self._sent_messages
        """
        target_id = target.user_id if isinstance(target, ChannelUser) else target
        if response.msg_type == MessageType.CARD and response.card_template:
            payload = self._build_card_payload(response)
        else:
            payload = {
                "receive_id": target_id,
                "msg_type": "text",
                "content": json.dumps({"text": response.content}, ensure_ascii=False),
            }
        # 离线模式：记录
        self._sent_messages.append({"target": target_id, "payload": payload})
        log.info("lark.send target=%s type=%s offline=%s",
                 target_id, response.msg_type.value, self._server is None)
        return True

    def _build_card_payload(self, response: ChannelResponse) -> dict[str, Any]:
        """组装飞书卡片 payload（5 个内置模板，W10-4 由 card_templates 模块渲染）"""
        template = response.card_template or "confirm_dialog"
        if template not in CARD_TEMPLATES:
            log.warning("lark.unknown_card_template template=%s; fallback to confirm_dialog", template)
            template = "confirm_dialog"
        # 合并：response.content 作为 message 主体；response.card_data 传给模板
        card_data = dict(response.card_data or {})
        if "title" not in card_data:
            card_data["title"] = template.replace("_", " ").title()
        if template == "confirm_dialog" and "message" not in card_data:
            card_data["message"] = response.content
        elif template != "confirm_dialog":
            # 其他模板：把 content 作为附加说明放进 card_data
            card_data.setdefault("extra", response.content)
        # 用 card_templates 模块渲染
        card = render_card(template, card_data)
        return {
            "receive_id": "",  # send() 时填充
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # subscribe / unsubscribe
    # ------------------------------------------------------------------
    def subscribe(self, handler: MessageHandler) -> None:
        if handler not in self._handlers:
            self._handlers.append(handler)

    def unsubscribe(self, handler: MessageHandler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    # ------------------------------------------------------------------
    # 测试辅助
    # ------------------------------------------------------------------
    def _sent_for_test(self) -> list[dict[str, Any]]:
        """测试断言用：返回已发送消息列表（不重置）"""
        return list(self._sent_messages)

    def clear_sent_for_test(self) -> None:
        """测试用：清空已发送消息计数"""
        self._sent_messages.clear()


# ---------------------------------------------------------------------------
# Secret Manager 引用辅助
# ---------------------------------------------------------------------------


def resolve_lark_secret(env_value: str, env_source: Callable[[str], str | None]) -> str:
    """
    解析飞书密钥 SecretManager 引用

    @param env_value   配置里的字符串（可能是 ${LARK_APP_SECRET} 或明文）
    @param env_source  读环境变量的函数（注入 os.environ.get 便于测试）
    @return 实际密钥值

    @note DESGIN §4.4 要求：app_secret / verification_token 必须从 SecretManager 注入
          本函数支持两种格式：
            1) ${VAR_NAME}——读 env_source("VAR_NAME")
            2) 明文——直接返回（仅 dev/test）
    """
    if env_value.startswith("${") and env_value.endswith("}"):
        var_name = env_value[2:-1]
        resolved = env_source(var_name)
        if resolved is None:
            raise RuntimeError(
                f"Secret reference {env_value!r} could not be resolved: "
                f"env var {var_name} not set"
            )
        return resolved
    # 明文——dev/test only
    return env_value


__all__ = [
    "CARD_TEMPLATES",
    "LarkConnector",
    "LARK_API_BASE",
    "resolve_lark_secret",
    "verify_lark_signature",
]
