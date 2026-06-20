"""单元测试：channels/lark.py——DESIGN §4.4 飞书连接器"""

from __future__ import annotations

import json

import pytest

from agent_swarm.channels.base import (
    ChannelMessage,
    ChannelResponse,
    ChannelType,
    ChannelUser,
    MessageType,
)
from agent_swarm.channels.lark import (
    CARD_TEMPLATES,
    LARK_API_BASE,
    LarkConnector,
    _hmac_sha256_hex,
    resolve_lark_secret,
    verify_lark_signature,
)

# ---------------------------------------------------------------------------
# verify_lark_signature / resolve_lark_secret
# ---------------------------------------------------------------------------


def test_verify_lark_signature_basic() -> None:
    """签名验证：相同输入产生相同哈希"""
    sig1 = verify_lark_signature("1700000000", "nonce1", '{"x":1}', "token-abc")
    sig2 = verify_lark_signature("1700000000", "nonce1", '{"x":1}', "token-abc")
    assert sig1 == sig2


def test_verify_lark_signature_different_input_changes_hash() -> None:
    """不同输入产生不同哈希"""
    sig1 = verify_lark_signature("1700000000", "nonce1", '{"x":1}', "token-abc")
    sig2 = verify_lark_signature("1700000000", "nonce2", '{"x":1}', "token-abc")
    assert sig1 != sig2


def test_verify_lark_signature_with_encrypt_key() -> None:
    """加密场景：encrypt_key 影响签名"""
    sig1 = verify_lark_signature("1700000000", "nonce1", "body", "tok", None)
    sig2 = verify_lark_signature("1700000000", "nonce1", "body", "tok", "enc")
    assert sig1 != sig2


# ---------------------------------------------------------------------------
# H1 回归测试:HMAC 必须真正使用 key(REVIEW-2026-06-19-2 §3.1)
# ---------------------------------------------------------------------------


def test_hmac_sha256_hex_different_keys_produce_different_signatures() -> None:
    """核心安全属性:不同 key 必须产生不同签名
    @note 早期 W10 bug: _hmac_sha256_hex 接收 key 但完全不用,任何 key 产生相同 hash
    """
    sig_a = _hmac_sha256_hex("correct-token", "1700000000nonce1body")
    sig_b = _hmac_sha256_hex("wrong-token", "1700000000nonce1body")
    sig_c = _hmac_sha256_hex("", "1700000000nonce1body")
    assert sig_a != sig_b, "key 必须影响签名"
    assert sig_a != sig_c, "空 key 与有效 key 必须产生不同签名"
    assert sig_b != sig_c, "wrong key 与空 key 必须产生不同签名"


def test_verify_lark_signature_changes_with_token() -> None:
    """verify_lark_signature 端到端:不同 token 必须产生不同签名"""
    sig1 = verify_lark_signature("1700000000", "nonce1", '{"x":1}', "token-abc")
    sig2 = verify_lark_signature("1700000000", "nonce1", '{"x":1}', "token-xyz")
    assert sig1 != sig2


def test_verify_lark_signature_resists_forgery_without_token() -> None:
    """攻击者不知道 token 时,无法算出与正确 token 相同的签名"""
    real_sig = verify_lark_signature("1700000000", "nonce1", '{"x":1}', "real-token")
    # 攻击者用一个错误 token
    forged_sig = verify_lark_signature("1700000000", "nonce1", '{"x":1}', "attacker-guess")
    assert forged_sig != real_sig, "攻击者不应能伪造签名"


def test_hmac_sha256_hex_uses_hmac_not_plain_sha256() -> None:
    """_hmac_sha256_hex 必须用 hmac.new(),不是 hashlib.sha256()
    @note 防止 W10 那种'key 被丢弃'的 bug 再次出现
    """
    import hashlib
    import hmac
    key = "my-token"
    payload = "1700000000nonce1body"
    # 正确 HMAC 结果
    expected = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    actual = _hmac_sha256_hex(key, payload)
    # 错误(纯 SHA256)结果
    wrong = hashlib.sha256(payload.encode()).hexdigest()
    assert actual == expected, f"必须用 hmac.new(); got {actual} != {expected}"
    assert actual != wrong, f"不能是纯 SHA256; got {actual} == {wrong}"


def test_resolve_lark_secret_env_reference() -> None:
    """SecretManager 引用 ${VAR} → 读 env"""
    env = {"LARK_APP_SECRET": "secret-value"}
    out = resolve_lark_secret("${LARK_APP_SECRET}", env.get)
    assert out == "secret-value"


def test_resolve_lark_secret_missing_env_raises() -> None:
    """${VAR} 引用但 env 缺 → raise"""
    with pytest.raises(RuntimeError, match="could not be resolved"):
        resolve_lark_secret("${MISSING_VAR}", lambda k: None)


def test_resolve_lark_secret_plaintext_passthrough() -> None:
    """明文（非 ${...}）直接返回（dev/test only）"""
    assert resolve_lark_secret("plain-text-secret", lambda k: None) == "plain-text-secret"


# ---------------------------------------------------------------------------
# LarkConnector 构造 + 属性
# ---------------------------------------------------------------------------


def _make_connector(user_whitelist: list[str] | None = None) -> LarkConnector:
    return LarkConnector(
        app_id="cli_test",
        app_secret="secret",
        verification_token="verify-tok",
        user_whitelist=user_whitelist,
    )


def test_lark_connector_channel_type() -> None:
    """LarkConnector.channel_type 必须是 LARK"""
    assert _make_connector().channel_type == ChannelType.LARK


def test_lark_connector_card_templates_constant() -> None:
    """5 个内置模板：DESIGN §4.4 列举"""
    assert "task_progress" in CARD_TEMPLATES
    assert "code_review_result" in CARD_TEMPLATES
    assert "adversarial_debug" in CARD_TEMPLATES
    assert "swarm_status" in CARD_TEMPLATES
    assert "confirm_dialog" in CARD_TEMPLATES
    assert len(CARD_TEMPLATES) == 5


def test_lark_connector_lark_api_base() -> None:
    """默认 API 基础 URL 是飞书生产端点"""
    assert LARK_API_BASE == "https://open.feishu.cn/open-apis"


# ---------------------------------------------------------------------------
# 事件解析 + 白名单
# ---------------------------------------------------------------------------


def test_parse_event_payload_text_message() -> None:
    """正常 text 消息 → ChannelMessage"""
    c = _make_connector()
    payload = {
        "uuid": "evt_1",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_abc", "name": "Alice"}},
            "message": {
                "message_id": "om_123",
                "content": json.dumps({"text": "hello world"}),
                "create_time": "1700000000000",
            },
        },
    }
    msg = c._parse_event_payload(payload)
    assert msg is not None
    assert msg.content == "hello world"
    assert msg.from_user.user_id == "ou_abc"
    assert msg.from_user.display_name == "Alice"
    assert msg.msg_type == MessageType.TEXT
    assert msg.timestamp == pytest.approx(1700000000.0)


def test_parse_event_payload_whitelist_drops_user() -> None:
    """不在白名单的用户消息被丢弃"""
    c = _make_connector(user_whitelist=["ou_allowed"])
    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_blocked"}},
            "message": {"content": json.dumps({"text": "hi"}), "create_time": "1700000000000"},
        },
    }
    msg = c._parse_event_payload(payload)
    assert msg is None


def test_parse_event_payload_whitelist_allows_user() -> None:
    """白名单用户消息被收下"""
    c = _make_connector(user_whitelist=["ou_allowed"])
    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_allowed"}},
            "message": {"content": json.dumps({"text": "hi"}), "create_time": "1700000000000"},
        },
    }
    msg = c._parse_event_payload(payload)
    assert msg is not None
    assert msg.content == "hi"


def test_parse_event_payload_empty_whitelist_allows_all() -> None:
    """空白名单 = 允许所有用户"""
    c = _make_connector(user_whitelist=[])  # 空
    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_any"}},
            "message": {"content": json.dumps({"text": "x"}), "create_time": "1700000000000"},
        },
    }
    msg = c._parse_event_payload(payload)
    assert msg is not None


def test_parse_event_payload_unhandled_event_type() -> None:
    """未处理事件类型 → None（不报错）"""
    c = _make_connector()
    payload = {
        "header": {"event_type": "app_ticket"},
        "event": {},
    }
    assert c._parse_event_payload(payload) is None


def test_parse_event_payload_invalid_json_content() -> None:
    """content 不是合法 JSON → 整段当文本"""
    c = _make_connector()
    payload = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": "u"}},
            "message": {"content": "not-json-{", "create_time": "1700000000000"},
        },
    }
    msg = c._parse_event_payload(payload)
    assert msg is not None
    assert msg.content == "not-json-{"


# ---------------------------------------------------------------------------
# 卡片动作解析
# ---------------------------------------------------------------------------


def test_parse_card_action_payload_approve() -> None:
    """卡片 action（approve 按钮）→ ChannelMessage(EVENT)"""
    c = _make_connector()
    payload = {
        "uuid": "act_1",
        "operator": {"open_id": "ou_user"},
        "action": {
            "tag": "button",
            "value": {"action": "approve"},
            "form_value": {},
        },
    }
    msg = c._parse_card_action_payload(payload)
    assert msg is not None
    assert msg.msg_type == MessageType.EVENT
    assert "approve" in msg.content  # action JSON 序列化进 content
    assert msg.from_user.user_id == "ou_user"


def test_parse_card_action_whitelist_drops() -> None:
    """白名单外的卡片操作丢弃"""
    c = _make_connector(user_whitelist=["ou_allowed"])
    payload = {
        "operator": {"open_id": "ou_blocked"},
        "action": {"value": {"action": "deny"}},
    }
    assert c._parse_card_action_payload(payload) is None


# ---------------------------------------------------------------------------
# 发送：TEXT 路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_text_message_records_in_offline_mode() -> None:
    """离线模式：send() 记录到 _sent_messages，不调 Lark API"""
    c = _make_connector()
    # 不调 start() → _server = None → 离线模式
    target = ChannelUser(channel=ChannelType.LARK, user_id="ou_1", display_name="u")
    resp = ChannelResponse(content="hi", msg_type=MessageType.TEXT)
    ok = await c.send(resp, target)
    assert ok is True
    sent = c._sent_for_test()
    assert len(sent) == 1
    assert sent[0]["target"] == "ou_1"
    assert sent[0]["payload"]["msg_type"] == "text"
    assert "hi" in sent[0]["payload"]["content"]


@pytest.mark.asyncio
async def test_send_with_string_target() -> None:
    """send() 接受字符串 target（直接用 open_id）"""
    c = _make_connector()
    resp = ChannelResponse(content="hi")
    ok = await c.send(resp, "ou_2")
    assert ok is True
    assert c._sent_for_test()[0]["target"] == "ou_2"


# ---------------------------------------------------------------------------
# 发送：CARD 路径（5 个内置模板）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_confirm_dialog_card_has_buttons() -> None:
    """confirm_dialog 模板：payload 含按钮元素"""
    c = _make_connector()
    target = ChannelUser(channel=ChannelType.LARK, user_id="u", display_name="u")
    resp = ChannelResponse(
        content="Deploy to production?",
        msg_type=MessageType.CARD,
        card_template="confirm_dialog",
        card_data={"title": "Confirm", "actions": [
            {"text": "Yes", "value": "approve", "type": "primary"},
            {"text": "No", "value": "deny", "type": "danger"},
        ]},
    )
    await c.send(resp, target)
    sent = c._sent_for_test()
    assert len(sent) == 1
    payload = sent[0]["payload"]
    assert payload["msg_type"] == "interactive"
    card = json.loads(payload["content"])
    assert "elements" in card
    # 找 action 元素
    action_elems = [e for e in card["elements"] if e.get("tag") == "action"]
    assert len(action_elems) == 1
    assert len(action_elems[0]["actions"]) == 2


@pytest.mark.asyncio
async def test_send_task_progress_card() -> None:
    """task_progress 模板：基本结构正确"""
    c = _make_connector()
    target = ChannelUser(channel=ChannelType.LARK, user_id="u", display_name="u")
    resp = ChannelResponse(
        content="3/5 tasks done",
        msg_type=MessageType.CARD,
        card_template="task_progress",
        card_data={"title": "Swarm Progress"},
    )
    await c.send(resp, target)
    sent = c._sent_for_test()
    card = json.loads(sent[0]["payload"]["content"])
    assert "header" in card
    assert card["header"]["title"]["content"] == "Swarm Progress"


@pytest.mark.asyncio
async def test_send_unknown_card_template_falls_back() -> None:
    """未知模板 → fallback 到 confirm_dialog"""
    c = _make_connector()
    target = ChannelUser(channel=ChannelType.LARK, user_id="u", display_name="u")
    resp = ChannelResponse(
        content="hi", msg_type=MessageType.CARD,
        card_template="unknown_template_xyz",
        card_data={"title": "X"},
    )
    await c.send(resp, target)
    sent = c._sent_for_test()
    card = json.loads(sent[0]["payload"]["content"])
    # fallback 后应含 action 元素
    action_elems = [e for e in card["elements"] if e.get("tag") == "action"]
    assert len(action_elems) == 1


# ---------------------------------------------------------------------------
# subscribe / unsubscribe
# ---------------------------------------------------------------------------


def test_subscribe_unsubscribe() -> None:
    """subscribe 多次注册同一 handler 只算一次；unsubscribe 移除"""
    c = _make_connector()
    async def h(msg: ChannelMessage) -> ChannelResponse:
        return ChannelResponse(content="ok")

    c.subscribe(h)
    c.subscribe(h)  # 重复注册
    assert len(c._handlers) == 1  # 去重

    c.unsubscribe(h)
    assert len(c._handlers) == 0


# ---------------------------------------------------------------------------
# 启动 / 停止
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_stop_idempotent() -> None:
    """start/stop 幂等：多次调用不报错"""
    c = _make_connector()
    await c.start()
    await c.start()  # 第二次不报错
    assert c._started is True
    await c.stop()
    await c.stop()  # 第二次不报错
    assert c._started is False


# ---------------------------------------------------------------------------
# 签名验证（带时间戳容差）
# ---------------------------------------------------------------------------


def test_request_signature_rejects_old_timestamp() -> None:
    """时间戳超出 5 分钟窗口 → 拒绝"""
    import time
    c = _make_connector()
    # 10 分钟前的时间戳
    old_ts = str(int(time.time()) - 600)
    assert c._verify_request_signature(old_ts, "n", "body", "any-sig") is False


def test_request_signature_rejects_malformed_timestamp() -> None:
    """时间戳非法 → 拒绝"""
    c = _make_connector()
    assert c._verify_request_signature("not-a-number", "n", "body", "any-sig") is False


def test_request_signature_rejects_empty_signature_header() -> None:
    """header 中无签名 → 拒绝"""
    import time
    c = _make_connector()
    ts = str(int(time.time()))
    assert c._verify_request_signature(ts, "n", "body", "") is False
