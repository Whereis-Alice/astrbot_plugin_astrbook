"""Image-hosting and forum outcomes must be logged independently and safely."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import test_meme_bridge
from test_meme_bridge import FakeEvent, main_module, run

integration = test_meme_bridge.integration
AstrbookPlugin = main_module.AstrbookPlugin
SIGNED_URL = "https://cdn.example.com/meme.gif?token=signed-secret-value"
PRIVATE_BODY = "这是一段不应进入运行日志的正文 privacy-marker-123"
AUTH_TOKEN = "Bearer private-auth-token"
UPSTREAM_ERROR = f"Request failed: 500 {AUTH_TOKEN} {PRIVATE_BODY} {SIGNED_URL}"


@pytest.fixture
def captured_logs(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    assert main_module.logger is test_meme_bridge.bridge_module.logger
    calls = []
    messages = []

    def record(level: str):
        def emit(message: str, *args: object, **kwargs: object) -> None:
            calls.append((level, message, args, kwargs))
            messages.append((level, message % args if args else message))

        return emit

    for level in ("info", "warning", "error", "debug"):
        monkeypatch.setattr(main_module.logger, level, Mock(side_effect=record(level)))
    return SimpleNamespace(calls=calls, messages=messages)


def assert_private_values_absent(captured_logs: SimpleNamespace) -> None:
    # Also inspect unformatted arguments: secret values must never reach the
    # logger even if the configured formatter does not display them.
    raw_logs = repr(captured_logs.calls)
    for private in (
        SIGNED_URL,
        "signed-secret-value",
        PRIVATE_BODY,
        "privacy-marker-123",
        AUTH_TOKEN,
        "private-auth-token",
        UPSTREAM_ERROR,
    ):
        assert private not in raw_logs


async def publish_selected_meme(plugin, integration) -> str:
    results = json.loads(
        await plugin.search_forum_memes(integration.event, query="猫猫捂脸")
    )
    return await plugin.create_thread(
        integration.event,
        title="测试帖子",
        content=PRIVATE_BODY,
        meme_ref=results["candidates"][0]["meme_ref"],
    )


@pytest.mark.parametrize(
    ("reused", "expected"),
    [(False, "表情上传图床成功"), (True, "表情缓存复用成功")],
)
def test_image_success_remains_visible_when_subsequent_forum_post_fails(
    integration: SimpleNamespace,
    captured_logs: SimpleNamespace,
    reused: bool,
    expected: str,
) -> None:
    plugin = object.__new__(AstrbookPlugin)
    plugin._meme_bridge = integration.bridge
    plugin._make_request = AsyncMock(return_value={"error": UPSTREAM_ERROR})
    integration.ferry.upload_asset.return_value = {
        "success": True,
        "url": SIGNED_URL,
        "reused": reused,
    }

    run(publish_selected_meme(plugin, integration))

    info = [message for level, message in captured_logs.messages if level == "info"]
    warning = [
        message for level, message in captured_logs.messages if level == "warning"
    ]
    assert sum(expected in message for message in info) == 1
    assert any(expected in message and "尚未发布到论坛" in message for message in info)
    assert any("开始论坛发布" in message for message in info)
    assert not any("论坛发布成功" in message for message in info)
    assert any(
        "论坛发布失败" in message and "code=http_500" in message for message in warning
    )
    assert captured_logs.messages[0][0] == "info"
    assert expected in captured_logs.messages[0][1]
    plugin._make_request.assert_awaited_once()
    assert_private_values_absent(captured_logs)


def test_failed_image_upload_is_logged_without_sending_a_forum_request(
    integration: SimpleNamespace, captured_logs: SimpleNamespace
) -> None:
    plugin = object.__new__(AstrbookPlugin)
    plugin._meme_bridge = integration.bridge
    plugin._make_request = AsyncMock()
    integration.ferry.upload_asset.return_value = {
        "success": False,
        "code": "quota_exceeded",
        "message": UPSTREAM_ERROR,
        "url": SIGNED_URL,
    }

    result = run(publish_selected_meme(plugin, integration))

    assert result.startswith("Error")
    assert any(
        level == "warning"
        and "表情配图失败" in message
        and "code=quota_exceeded" in message
        for level, message in captured_logs.messages
    )
    assert not any("开始论坛发布" in message for _, message in captured_logs.messages)
    plugin._make_request.assert_not_awaited()
    integration.asset.release.assert_called_once_with()
    assert_private_values_absent(captured_logs)


@pytest.mark.parametrize(
    ("method", "arguments", "target"),
    [
        ("create_thread", {"title": "测试标题"}, 0),
        ("reply_thread", {"thread_id": 12}, 12),
        ("reply_floor", {"reply_id": 34}, 34),
    ],
)
def test_valid_forum_receipt_logs_confirmed_publication(
    captured_logs: SimpleNamespace, method: str, arguments: dict, target: int
) -> None:
    plugin = object.__new__(AstrbookPlugin)
    plugin._make_request = AsyncMock(return_value={"id": 456})

    run(getattr(plugin, method)(FakeEvent(), content=PRIVATE_BODY, **arguments))

    confirmed = [
        message
        for level, message in captured_logs.messages
        if level == "info" and "论坛发布成功" in message
    ]
    assert len(confirmed) == 1
    assert f"action={method}" in confirmed[0]
    assert f"target_id={target}" in confirmed[0]
    assert "result_id=456" in confirmed[0]
    assert_private_values_absent(captured_logs)


def test_forum_timeout_logs_unconfirmed_outcome_instead_of_success(
    integration: SimpleNamespace, captured_logs: SimpleNamespace
) -> None:
    plugin = object.__new__(AstrbookPlugin)
    plugin._meme_bridge = integration.bridge
    plugin._make_request = AsyncMock(return_value={"error": "Request timeout"})
    integration.ferry.upload_asset.return_value = {
        "success": True,
        "url": SIGNED_URL,
        "reused": False,
    }

    run(publish_selected_meme(plugin, integration))

    assert any("表情上传图床成功" in message for _, message in captured_logs.messages)
    assert any(
        level == "warning"
        and "论坛发布结果未确认" in message
        and "code=timeout" in message
        for level, message in captured_logs.messages
    )
    assert not any("论坛发布成功" in message for _, message in captured_logs.messages)
    assert_private_values_absent(captured_logs)


def test_unknown_upstream_error_logs_only_a_safe_category(
    captured_logs: SimpleNamespace,
) -> None:
    plugin = object.__new__(AstrbookPlugin)
    plugin._make_request = AsyncMock(
        return_value={"error": f"raw-body {UPSTREAM_ERROR}"}
    )

    run(plugin.create_thread(FakeEvent(), title="测试标题", content=PRIVATE_BODY))

    assert any(
        level == "warning"
        and "结果未确认" in message
        and "code=request_error" in message
        for level, message in captured_logs.messages
    )
    assert_private_values_absent(captured_logs)


@pytest.mark.parametrize(
    "response", [{}, {"text": "<html>proxy response</html>"}, {"id": 0}]
)
def test_missing_receipt_is_not_logged_as_confirmed_success(
    captured_logs: SimpleNamespace,
    response: dict,
) -> None:
    plugin = object.__new__(AstrbookPlugin)
    plugin._make_request = AsyncMock(return_value=response)
    run(plugin.create_thread(FakeEvent(), title="测试标题", content=PRIVATE_BODY))
    assert any(
        level == "warning" and "发布结果未确认" in message
        for level, message in captured_logs.messages
    )
    assert not any("论坛发布成功" in message for _, message in captured_logs.messages)


def test_unconfigured_token_logs_that_no_request_was_sent(
    captured_logs: SimpleNamespace,
) -> None:
    plugin = object.__new__(AstrbookPlugin)
    plugin.token = ""
    run(plugin.create_thread(FakeEvent(), title="测试标题", content=PRIVATE_BODY))
    assert any(
        level == "warning"
        and "未发起论坛请求" in message
        and "code=token_not_configured" in message
        for level, message in captured_logs.messages
    )
