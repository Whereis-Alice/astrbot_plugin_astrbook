"""Request-local guidance must not change tools in unrelated conversations."""

from types import SimpleNamespace

from astrbot.api import FunctionTool, ToolSet
from astrbot.core.provider.entities import ProviderRequest
from test_astrbook import AstrbookPlugin, FakeEvent, run


def make_request() -> ProviderRequest:
    return ProviderRequest(
        func_tool=ToolSet(
            [
                FunctionTool(name=name, description=name, parameters={})
                for name in (
                    "search_forum_memes",
                    "create_thread",
                    "magpie_search_meme",
                    "magpie_send_meme",
                    "send_message_to_user",
                )
            ]
        )
    )


def test_browse_guidance_changes_only_current_tool_set() -> None:
    plugin = object.__new__(AstrbookPlugin)
    plugin._meme_bridge = SimpleNamespace(available=lambda: True)
    event = FakeEvent()
    event.session_id = "astrbook_browse_system"
    event.set_extra("is_browse_event", True)
    req = make_request()
    original_tools = req.func_tool
    run(plugin.remind_astrbook_tool_reply(event, req))
    assert req.func_tool is not original_tools
    assert {tool.name for tool in req.func_tool.tools} == {
        "search_forum_memes",
        "create_thread",
    }
    assert len(original_tools.tools) == 5
    assert any("meme_ref" in part.text for part in req.extra_user_content_parts)


def test_other_chats_keep_magpie_sending_tools() -> None:
    plugin = object.__new__(AstrbookPlugin)
    plugin._meme_bridge = SimpleNamespace(available=lambda: True)
    event = FakeEvent()
    event.get_platform_name = lambda: "aiocqhttp"
    req = make_request()
    original_tools = req.func_tool
    run(plugin.remind_astrbook_tool_reply(event, req))
    assert req.func_tool is original_tools
    assert len(req.func_tool.tools) == 5
    assert any(
        "search_forum_memes" in part.text for part in req.extra_user_content_parts
    )


def test_unavailable_dependencies_do_not_advertise_meme_workflow() -> None:
    plugin = object.__new__(AstrbookPlugin)
    plugin._meme_bridge = SimpleNamespace(available=lambda: False)
    event = FakeEvent()
    event.get_platform_name = lambda: "aiocqhttp"
    req = make_request()
    run(plugin.remind_astrbook_tool_reply(event, req))
    assert not req.extra_user_content_parts
    assert len(req.func_tool.tools) == 5
