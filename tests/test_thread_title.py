"""Omitted titles may be derived from supplied text without weakening publication checks."""

from __future__ import annotations

import json
from copy import copy, deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_meme_bridge
from astrbot.api import ToolSet
from astrbot.core.provider.register import llm_tools
from test_meme_bridge import (
    IMAGE_URL,
    FakeEvent,
    main_module,
    run,
)

AstrbookPlugin = main_module.AstrbookPlugin
# Re-exporting the existing fixture keeps both suites on the same bridge contract.
integration = test_meme_bridge.integration


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("今天终于解决了这个问题。接下来休息一下。", "今天终于解决了这个问题"),
        ("\n\n# 今天的小收获\n具体过程写在这里。", "今天的小收获"),
        (
            "![不要拿图片说明当标题](https://cdn.example.com/a.png)\n今天完成了小目标。",
            "今天完成了小目标",
        ),
        (
            "[这是一篇学习笔记](https://example.com/notes)。附上详细过程。",
            "这是一篇学习笔记",
        ),
        (
            "```python\nprint('不要拿代码当标题')\n```\n这是代码的说明文字。",
            "这是代码的说明文字",
        ),
    ],
)
def test_automatic_title_is_extracted_from_readable_body_text(
    content: str, expected: str
) -> None:
    assert AstrbookPlugin._title_from_content(content) == expected


def test_title_extraction_caps_long_text_without_rewriting_it() -> None:
    content = "这是一段完整的正文内容" * 20
    title = AstrbookPlugin._title_from_content(content)
    assert 2 <= len(title) <= 60
    assert content.startswith(title.rstrip("…"))


@pytest.mark.parametrize(
    "content",
    [
        "![这段图片说明不能代替正文](https://cdn.example.com/a.png)",
        "![图片](https://cdn.example.com/a(1).png)",
        "https://example.com/notes",
        "<https://example.com/notes>",
        "```python\nprint('only code')\n```",
        "！！！……？？？",
        "---\n***\n___",
    ],
)
def test_title_extraction_does_not_invent_a_title_without_readable_text(
    content: str,
) -> None:
    assert AstrbookPlugin._title_from_content(content) == ""


@pytest.mark.parametrize("title_args", [{}, {"title": None}, {"title": " \n "}])
@pytest.mark.parametrize("response", [{"id": 123}, {"ok": True}])
def test_missing_title_posts_supplied_text_and_reports_the_derived_title(
    title_args: dict, response: dict
) -> None:
    plugin = object.__new__(AstrbookPlugin)
    plugin._make_request = AsyncMock(return_value=response)
    event = FakeEvent()
    content = "今天终于完成了小目标。接下来慢慢整理笔记。"

    result = run(plugin.create_thread(event, content=content, **title_args))

    plugin._make_request.assert_awaited_once_with(
        "POST",
        "/api/threads",
        data={"title": "今天终于完成了小目标", "content": content, "category": "chat"},
    )
    assert "今天终于完成了小目标" in result
    assert "extracted from content" in result
    assert event.get_extra("astrbook_tool_reply_sent") is True


@pytest.mark.parametrize("is_future_task", [False, True])
def test_missing_title_with_selected_meme_searches_uploads_and_posts_in_order(
    integration: SimpleNamespace,
    is_future_task: bool,
) -> None:
    if is_future_task:
        integration.event = test_meme_bridge.cron_event(integration)
    plugin = object.__new__(AstrbookPlugin)
    plugin._meme_bridge = integration.bridge
    plugin._make_request = AsyncMock(return_value={"id": 123})
    content = "今天终于完成了小目标。给自己鼓励一下！"

    async def scenario() -> str:
        search = json.loads(
            await plugin.search_forum_memes(integration.event, query="猫猫捂脸")
        )
        return await plugin.create_thread(
            integration.event,
            content=content,
            category="chat",
            meme_ref=search["candidates"][0]["meme_ref"],
        )

    result = run(scenario())

    integration.magpie.search_meme_candidates.assert_awaited_once()
    expected_event = integration.magpie.search_meme_candidates.await_args.args[0]
    integration.magpie.export_meme_asset.assert_awaited_once_with(
        "emoji_1", expected_event
    )
    integration.ferry.upload_asset.assert_awaited_once()
    assert integration.ferry.upload_asset.await_args.args[0] is expected_event
    plugin._make_request.assert_awaited_once_with(
        "POST",
        "/api/threads",
        data={
            "title": "今天终于完成了小目标",
            "content": f"{content}\n\n![表情包](<{IMAGE_URL}>)",
            "category": "chat",
        },
    )
    assert "今天终于完成了小目标" in result
    integration.asset.release.assert_called_once_with()


def test_title_fallback_never_weakens_failed_image_protection(
    integration: SimpleNamespace,
) -> None:
    plugin = object.__new__(AstrbookPlugin)
    plugin._meme_bridge = integration.bridge
    plugin._make_request = AsyncMock()
    integration.ferry.upload_asset.return_value = {
        "success": False,
        "code": "quota_exceeded",
    }

    async def scenario() -> str:
        search = json.loads(
            await plugin.search_forum_memes(integration.event, query="猫猫捂脸")
        )
        return await plugin.create_thread(
            integration.event,
            content="今天终于完成了小目标。给自己鼓励一下！",
            meme_ref=search["candidates"][0]["meme_ref"],
        )

    result = run(scenario())
    assert result.startswith("Error")
    assert "quota_exceeded" in result
    plugin._make_request.assert_not_awaited()
    integration.asset.release.assert_called_once_with()
    assert integration.event.get_extra("astrbook_tool_reply_sent") is None


@pytest.mark.parametrize("content", [None, "", "   ", "不足五字"])
def test_missing_or_short_body_is_never_filled_from_title_or_meme(
    content: object,
) -> None:
    plugin = object.__new__(AstrbookPlugin)
    plugin._meme_bridge = SimpleNamespace(attach=AsyncMock())
    plugin._make_request = AsyncMock()
    event = FakeEvent()

    run(plugin.create_thread(event, content=content, meme_ref="abm_selected"))

    plugin._meme_bridge.attach.assert_not_awaited()
    plugin._make_request.assert_not_awaited()


@pytest.mark.parametrize(
    "title", ["短", "长" * 101, 12345, {"title": "标题"}, ["标题"]]
)
def test_explicit_invalid_title_is_rejected_before_upload(title: object) -> None:
    plugin = object.__new__(AstrbookPlugin)
    plugin._meme_bridge = SimpleNamespace(attach=AsyncMock())
    plugin._make_request = AsyncMock()
    event = FakeEvent()

    result = run(
        plugin.create_thread(
            event,
            title=title,
            content="这段正文已经足够完整。",
            meme_ref="abm_selected",
        )
    )

    assert "title" in result.lower()
    plugin._meme_bridge.attach.assert_not_awaited()
    plugin._make_request.assert_not_awaited()


def test_unusable_automatic_title_requests_title_without_uploading() -> None:
    plugin = object.__new__(AstrbookPlugin)
    plugin._meme_bridge = SimpleNamespace(attach=AsyncMock())
    plugin._make_request = AsyncMock()

    result = run(
        plugin.create_thread(
            FakeEvent(),
            content="![一张图](https://cdn.example.com/a.png)",
            meme_ref="abm_selected",
        )
    )

    assert "title" in result.lower()
    plugin._meme_bridge.attach.assert_not_awaited()
    plugin._make_request.assert_not_awaited()


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
def test_real_provider_serialization_keeps_content_required_and_title_optional(
    provider: str,
) -> None:
    registered = llm_tools.get_func("create_thread")
    assert registered is not None
    tool = copy(registered)
    tool.parameters = deepcopy(registered.parameters)
    plugin = object.__new__(AstrbookPlugin)
    plugin.context = SimpleNamespace(
        get_llm_tool_manager=lambda: SimpleNamespace(
            get_func=lambda name: tool if name == "create_thread" else None
        )
    )
    plugin._harden_tool_schemas()
    toolset = ToolSet([tool])
    if provider == "openai":
        exported = toolset.openai_schema()[0]["function"]
    elif provider == "anthropic":
        exported = toolset.anthropic_schema()[0]
    else:
        exported = toolset.google_schema()["function_declarations"][0]
    parameters = exported["input_schema" if provider == "anthropic" else "parameters"]

    assert parameters["required"] == ["content"]
    assert parameters["properties"]["title"]["type"] == "string"
    assert "title" not in parameters["required"]
    assert "meme_ref" not in parameters["required"]
    assert exported["description"] == tool.description
    assert "search_forum_memes" in exported["description"]
    assert parameters["properties"]["title"]["description"]
