"""
@module tests.e2e.test_w10_lark_e2e
@brief  W10 飞书连接器端到端验证（DESIGN §17.2 ①）

W10 DoD（DESIGN §17.2 ①）：
  ① 飞书连接器签名验证 + 卡片交互在真实 Lark 工作区可用
  ② 事件白名单 + 限流
  ③ 5 个内置卡片模板全部可渲染
  ④ ChannelAdapter 路由 + 鉴权 + 限流
  ⑤ SecretManager 引用（${VAR_NAME} 形式）

@note mock Lark server 跑通（不连真 Lark）— 真接入需 app_id + app_secret + 飞书后台配置
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import aiohttp
import pytest
import yaml

from agent_swarm.channels.adapter import ChannelAdapter
from agent_swarm.channels.base import (
    ChannelConnector,
    ChannelMessage,
    ChannelResponse,
    ChannelType,
    MessageType,
)
from agent_swarm.channels.card_templates import render_card
from agent_swarm.channels.lark import (
    LarkConnector,
    resolve_lark_secret,
    verify_lark_signature,
)


class _StubConnector(ChannelConnector):
    """测试用 connector——记录 _started 状态"""
    def __init__(self, ct: ChannelType) -> None:
        self._ct = ct
        self._started = False
    @property
    def channel_type(self) -> ChannelType:
        return self._ct
    async def start(self) -> None:
        self._started = True
    async def stop(self) -> None:
        self._started = False
    async def send(self, response, target) -> bool: return True
    def subscribe(self, handler) -> None: ...
    def unsubscribe(self, handler) -> None: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_event_payload(text: str, sender_open_id: str = "ou_user1") -> dict:
    """构造飞书 event v2 payload（receive_v1 文本消息）"""
    return {
        "uuid": f"evt_{int(time.time() * 1000)}",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": sender_open_id, "name": "Test User"}},
            "message": {
                "message_id": f"om_{int(time.time() * 1000)}",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "create_time": str(int(time.time() * 1000)),
            },
        },
    }


def _build_card_action_payload(action_value: str, operator: str = "ou_user1") -> dict:
    return {
        "uuid": f"act_{int(time.time() * 1000)}",
        "operator": {"open_id": operator},
        "action": {
            "tag": "button",
            "value": {"action": action_value},
            "form_value": {},
        },
    }


async def _post_event(
    session: aiohttp.ClientSession, url: str, body: str,
    timestamp: str, nonce: str, signature: str,
) -> aiohttp.ClientResponse:
    headers = {
        "Content-Type": "application/json",
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": signature,
    }
    resp = await session.post(url, data=body, headers=headers)
    return resp


# ---------------------------------------------------------------------------
# ① 端到端：mock Lark server + LarkConnector + ChannelAdapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_lark_event_to_handler_dispatch() -> None:
    """端到端：模拟 Lark POST /lark/event → 验证 → 派发到 handler → 自动 send 回复"""
    token = "verify-tok-abc"
    c = LarkConnector(
        app_id="cli_test",
        app_secret="secret",
        verification_token=token,
        user_whitelist=["ou_user1"],
        webhook_host="127.0.0.1",
        webhook_port=0,
    )
    await c.start()
    try:
        port = c._effective_port()
        assert port > 0, "webhook server should bind on auto-assigned port"
        url = f"http://127.0.0.1:{port}/lark/event"

        # 注入业务 handler
        async def echo_handler(msg: ChannelMessage) -> ChannelResponse:
            return ChannelResponse(content=f"echo: {msg.content}")

        c.subscribe(echo_handler)

        # 构造合法签名
        body = json.dumps(_build_event_payload("hello from mock"))
        ts = str(int(time.time()))
        nonce = "n1"
        sig = verify_lark_signature(ts, nonce, body, token)

        async with aiohttp.ClientSession() as session:
            resp = await _post_event(session, url, body, ts, nonce, sig)
            assert resp.status == 200
            data = await resp.json()
            assert data["code"] == 0

        # 验证 handler 被调用 + 离线模式记录发送
        await asyncio.sleep(0.1)  # 给 _dispatch 时间跑完
        sent = c._sent_for_test()
        assert any("echo: hello from mock" in str(s.get("payload", "")) for s in sent), \
            f"handler response should be sent back: {sent}"
    finally:
        await c.stop()


@pytest.mark.asyncio
async def test_e2e_lark_rejects_invalid_signature() -> None:
    """端到端：签名错误 → 401"""
    c = LarkConnector(
        app_id="cli", app_secret="s", verification_token="tok",
        webhook_host="127.0.0.1", webhook_port=0,
    )
    await c.start()
    try:
        port = c._effective_port()
        url = f"http://127.0.0.1:{port}/lark/event"
        body = json.dumps(_build_event_payload("x"))
        ts = str(int(time.time()))
        nonce = "n"
        wrong_sig = "definitely-not-the-right-signature"

        async with aiohttp.ClientSession() as session:
            resp = await _post_event(session, url, body, ts, nonce, wrong_sig)
            assert resp.status == 401
    finally:
        await c.stop()


@pytest.mark.asyncio
async def test_e2e_lark_card_action_to_event() -> None:
    """端到端：卡片按钮 action → ChannelMessage(EVENT)"""
    c = LarkConnector(
        app_id="cli", app_secret="s", verification_token="tok",
        webhook_host="127.0.0.1", webhook_port=0,
    )
    received: list[ChannelMessage] = []

    async def capture(msg: ChannelMessage) -> ChannelResponse:
        received.append(msg)
        return ChannelResponse(content="got it")

    c.subscribe(capture)
    await c.start()
    try:
        port = c._effective_port()
        url = f"http://127.0.0.1:{port}/lark/card_action"
        body = json.dumps(_build_card_action_payload("approve"))
        ts = str(int(time.time()))
        nonce = "n"
        sig = verify_lark_signature(ts, nonce, body, "tok")

        async with aiohttp.ClientSession() as session:
            resp = await _post_event(session, url, body, ts, nonce, sig)
            assert resp.status == 200

            await asyncio.sleep(0.1)
            assert len(received) == 1
            assert received[0].msg_type == MessageType.EVENT
            assert "approve" in received[0].content
    finally:
        await c.stop()


@pytest.mark.asyncio
async def test_e2e_lark_event_whitelist_via_http() -> None:
    """端到端：白名单用户被拦，handler 收不到"""
    c = LarkConnector(
        app_id="cli", app_secret="s", verification_token="tok",
        user_whitelist=["ou_allowed"],
        webhook_host="127.0.0.1", webhook_port=0,
    )
    received: list[ChannelMessage] = []

    async def capture(msg):
        received.append(msg)
        return ChannelResponse(content="x")

    c.subscribe(capture)
    await c.start()
    try:
        port = c._effective_port()
        url = f"http://127.0.0.1:{port}/lark/event"
        body = json.dumps(_build_event_payload("hi", sender_open_id="ou_blocked"))
        ts = str(int(time.time()))
        nonce = "n"
        sig = verify_lark_signature(ts, nonce, body, "tok")

        async with aiohttp.ClientSession() as session:
            resp = await _post_event(session, url, body, ts, nonce, sig)
            assert resp.status == 200

            await asyncio.sleep(0.1)
            assert len(received) == 0, "白名单外的消息应被丢弃"
    finally:
        await c.stop()


# ---------------------------------------------------------------------------
# ② ChannelAdapter 全链路 e2e
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_channel_adapter_full_path() -> None:
    """ChannelAdapter 完整路径：connector event → 限流 → 鉴权 → handler → 发送回 connector"""
    c = LarkConnector(
        app_id="cli", app_secret="s", verification_token="tok",
        user_whitelist=["ou_usr"], webhook_host="127.0.0.1", webhook_port=0,
    )
    adapter = ChannelAdapter(
        messages_per_minute=100, sessions_per_hour=10,
        user_whitelist={"ou_usr"},
    )
    adapter.register_connector(c)

    async def bot_handler(msg: ChannelMessage) -> ChannelResponse:
        return ChannelResponse(
            content=f"received: {msg.content}",
            msg_type=MessageType.CARD,
            card_template="confirm_dialog",
            card_data={"title": "Bot", "message": "Got your message"},
        )

    adapter.set_handler(bot_handler)
    await c.start()
    try:
        port = c._effective_port()
        url = f"http://127.0.0.1:{port}/lark/event"
        body = json.dumps(_build_event_payload("ping", sender_open_id="ou_usr"))
        ts = str(int(time.time()))
        nonce = "n"
        sig = verify_lark_signature(ts, nonce, body, "tok")

        async with aiohttp.ClientSession() as session:
            resp = await _post_event(session, url, body, ts, nonce, sig)
            assert resp.status == 200

        await asyncio.sleep(0.3)  # 等 _dispatch + send 走完
        # 验证：handler 跑过 + connector 收到 send 调用
        # confirm_dialog 模板把 response.content 写到 card_data.message
        sent = c._sent_for_test()
        sent_str = " ".join(str(s.get("payload", "")) for s in sent)
        assert "received" in sent_str or "Got" in sent_str, (
            f"handler response 应被 send 回来: {sent}"
        )
    finally:
        await c.stop()


@pytest.mark.asyncio
async def test_e2e_channel_adapter_rate_limit_via_http() -> None:
    """ChannelAdapter 限流：超出 messages_per_minute → denied"""
    c = LarkConnector(
        app_id="cli", app_secret="s", verification_token="tok",
        user_whitelist=["ou_usr"], webhook_host="127.0.0.1", webhook_port=0,
    )
    adapter = ChannelAdapter(
        messages_per_minute=2, sessions_per_hour=10,
        user_whitelist={"ou_usr"},
    )
    adapter.register_connector(c)

    async def always_ok(msg):
        return ChannelResponse(content="ok")

    adapter.set_handler(always_ok)
    await c.start()
    try:
        port = c._effective_port()
        url = f"http://127.0.0.1:{port}/lark/event"

        async with aiohttp.ClientSession() as session:
            # 前 2 个通过
            for i in range(2):
                body = json.dumps(_build_event_payload(f"msg-{i}", sender_open_id="ou_usr"))
                ts = str(int(time.time()))
                nonce = f"n{i}"
                sig = verify_lark_signature(ts, nonce, body, "tok")
                resp = await _post_event(session, url, body, ts, nonce, sig)
                assert resp.status == 200
            # 第 3 个应被限流（限流响应也会被 send，但内容是 denied）
            body = json.dumps(_build_event_payload("msg-3", sender_open_id="ou_usr"))
            ts = str(int(time.time()))
            nonce = "n3"
            sig = verify_lark_signature(ts, nonce, body, "tok")
            resp = await _post_event(session, url, body, ts, nonce, sig)
            assert resp.status == 200

            await asyncio.sleep(0.1)
            sent = c._sent_for_test()
        # 找 rate_limited 响应
        assert any("rate_limited" in str(s.get("payload", "")) for s in sent), \
            f"应有 rate_limited 响应: {sent}"
    finally:
        await c.stop()


# ---------------------------------------------------------------------------
# ③ SecretManager 引用（DESIGN §4.4 强制要求）
# ---------------------------------------------------------------------------


def test_secret_manager_reference_required_for_production() -> None:
    """DESIGN §4.4：app_secret 必须从 SecretManager 注入（明文仅 dev/test）"""
    env = {"LARK_APP_SECRET": "real-secret-from-vault"}
    out = resolve_lark_secret("${LARK_APP_SECRET}", env.get)
    assert out == "real-secret-from-vault"


def test_secret_manager_missing_raises() -> None:
    """${VAR} 引用但 env 缺 → fail-fast（不要 fallback 明文）"""
    with pytest.raises(RuntimeError, match="could not be resolved"):
        resolve_lark_secret("${MISSING_LARK_SECRET}", lambda k: None)


# ---------------------------------------------------------------------------
# ④ 5 个卡片模板端到端
# ---------------------------------------------------------------------------


def test_all_five_templates_render_with_valid_lark_structure() -> None:
    """5 个模板 → 渲染后 payload 是合法 Lark 卡片结构"""
    cases = [
        ("task_progress", {"title": "P", "tasks": [
            {"id": "T1", "title": "x", "status": "completed"},
        ]}),
        ("code_review_result", {"findings": [], "verdict": "approve"}),
        ("adversarial_debug", {"round_no": 1, "max_rounds": 3, "survivors": ["h0"]}),
        ("swarm_status", {"state": "running", "agents": []}),
        ("confirm_dialog", {"title": "OK?", "message": "Continue?"}),
    ]
    for tmpl, data in cases:
        card = render_card(tmpl, data)
        assert "header" in card
        assert "elements" in card
        assert isinstance(card["elements"], list) and len(card["elements"]) >= 1
        # header 必须含 title.tag == "plain_text"
        assert card["header"]["title"]["tag"] == "plain_text"
        # template 必须在合法颜色集合
        assert card["header"]["template"] in {"blue", "green", "orange", "red", "grey"}


# ---------------------------------------------------------------------------
# ⑤ example YAML 配置可被加载（DESIGN §4.6）
# ---------------------------------------------------------------------------


def test_example_yaml_lark_config_loads() -> None:
    """example/w10_lark.yaml 配置可被 yaml.safe_load 解析"""
    p = Path("examples/w10_lark.yaml")
    if not p.exists():
        pytest.skip("examples/w10_lark.yaml not present (will be added in W10-5)")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "channels" in cfg
    assert "lark" in cfg["channels"]
    lark_cfg = cfg["channels"]["lark"]
    # app_secret / verification_token 必须用 ${VAR} 引用（DESIGN §4.4）
    assert lark_cfg["app_secret"].startswith("${")
    assert lark_cfg["verification_token"].startswith("${")
    # user_whitelist 应存在
    assert "user_whitelist" in lark_cfg


# ---------------------------------------------------------------------------
# ⑥ ChannelAdapter start_all / stop_all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_adapter_start_all_starts_all_connectors() -> None:
    """start_all() 启动所有注册的 connector（不同 channel_type）"""
    c_lark = LarkConnector(app_id="c1", app_secret="s", verification_token="t",
                           webhook_host="127.0.0.1", webhook_port=0)
    c_sdk = _StubConnector(ChannelType.SDK)
    a = ChannelAdapter()
    a.register_connector(c_lark)
    a.register_connector(c_sdk)
    await a.start_all()
    try:
        assert c_lark._started is True
        assert c_sdk._started is True
    finally:
        await a.stop_all()
        assert c_lark._started is False
        assert c_sdk._started is False
