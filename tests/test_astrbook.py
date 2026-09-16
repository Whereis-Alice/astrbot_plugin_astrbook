from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest
from astrbot.api.platform import AstrBotMessage, MessageType, PlatformMetadata

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_package() -> str:
    """Import the checkout using the same package name pytest will use."""

    # Pytest imports a repository containing ``__init__.py`` as a package during
    # setup. Loading it first under a synthetic alias executes the platform
    # adapter decorator twice and AstrBot rejects the duplicate ``astrbook``
    # registration. Import by the checkout directory's real name instead, so
    # pytest reuses this exact module regardless of what the directory is named.
    repo_parent = str(REPO_ROOT.parent)
    if repo_parent not in sys.path:
        sys.path.insert(0, repo_parent)
    package_name = REPO_ROOT.name
    package_module = importlib.import_module(package_name)
    if Path(package_module.__file__).resolve() != (REPO_ROOT / "__init__.py").resolve():
        raise RuntimeError(
            f"Imported {package_name!r} from an unexpected checkout: "
            f"{package_module.__file__}"
        )
    return package_name


PLUGIN_PACKAGE = _load_plugin_package()
adapter_module = importlib.import_module(
    f"{PLUGIN_PACKAGE}.adapter.astrbook_adapter"
)
forum_memory_module = importlib.import_module(
    f"{PLUGIN_PACKAGE}.adapter.forum_memory"
)
main_module = importlib.import_module(f"{PLUGIN_PACKAGE}.main")

AstrBookAdapter = adapter_module.AstrBookAdapter
_clamp_float = adapter_module._clamp_float
_clamp_int = adapter_module._clamp_int
_normalize_api_base = adapter_module._normalize_api_base
ForumMemory = forum_memory_module.ForumMemory
ASTRBOOK_TOOL_REQUIRED_PARAMS = main_module.ASTRBOOK_TOOL_REQUIRED_PARAMS
AstrbookPlugin = main_module.AstrbookPlugin


class FakeEvent:
    def __init__(self) -> None:
        self.extras: dict[str, object] = {}

    def set_extra(self, key: str, value: object) -> None:
        self.extras[key] = value

    def get_extra(self, key: str, default: object = None) -> object:
        return self.extras.get(key, default)

    def get_platform_name(self) -> str:
        return "astrbook"


def run(coro):
    return asyncio.run(coro)


def test_create_thread_missing_arguments_are_friendly() -> None:
    plugin = object.__new__(AstrbookPlugin)
    event = FakeEvent()

    assert run(plugin.create_thread(event)) == "Error: title is required (2-100 chars)"
    assert run(plugin.create_thread(event, title="标题")) == (
        "Error: content is required (at least 5 chars)"
    )
    assert run(plugin.create_thread(event, title=None, content=None)) == (
        "Error: title is required (2-100 chars)"
    )


def test_create_thread_success_without_response_id_marks_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = object.__new__(AstrbookPlugin)
    event = FakeEvent()

    async def fake_request(method: str, path: str, **kwargs: object) -> dict:
        assert method == "POST"
        assert path == "/api/threads"
        assert kwargs["data"] == {
            "title": "测试主题",
            "content": "这是一段足够长的正文",
            "category": "chat",
        }
        return {"ok": True}

    monkeypatch.setattr(plugin, "_make_request", fake_request)
    result = run(
        plugin.create_thread(
            event,
            title="测试主题",
            content="这是一段足够长的正文",
        )
    )

    assert result == "Thread created successfully"
    assert event.get_extra("astrbook_tool_reply_sent") is True


@pytest.mark.parametrize(
    ("method", "keyword", "expected"),
    [
        (
            "search_threads",
            "t" * 101,
            "Error: search keyword is too long (max 100 characters)",
        ),
        (
            "search_users",
            "u" * 51,
            "Error: search keyword is too long (max 50 characters)",
        ),
    ],
)
def test_search_keyword_limits_reject_before_request(
    method: str,
    keyword: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = object.__new__(AstrbookPlugin)
    event = FakeEvent()

    async def unexpected_request(*args: object, **kwargs: object) -> dict:
        pytest.fail(f"overlong keyword unexpectedly reached HTTP: {args}, {kwargs}")

    monkeypatch.setattr(plugin, "_make_request", unexpected_request)
    assert run(getattr(plugin, method)(event, keyword=keyword)) == expected


def test_send_dm_message_truncates_client_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = object.__new__(AstrbookPlugin)
    event = FakeEvent()
    captured: dict[str, object] = {}

    async def fake_request(method: str, path: str, **kwargs: object) -> dict:
        captured.update(method=method, path=path, **kwargs)
        return {"id": 9, "conversation_id": 12}

    monkeypatch.setattr(plugin, "_make_request", fake_request)
    result = run(
        plugin.send_dm_message(
            event,
            target_user_id=7,
            content="hello",
            client_msg_id="x" * 80,
        )
    )

    assert result == "DM sent successfully. message_id=9, conversation_id=12"
    assert captured["params"] == {"target_user_id": 7}
    assert captured["data"] == {"content": "hello", "client_msg_id": "x" * 64}
    assert event.get_extra("astrbook_tool_reply_sent") is True


def test_required_tool_schema_map_covers_legacy_required_arguments() -> None:
    expected = {
        "create_thread": ("title", "content"),
        "reply_thread": ("thread_id", "content"),
        "reply_floor": ("reply_id", "content"),
        "send_dm_message": ("target_user_id", "content"),
    }
    for name, required in expected.items():
        assert ASTRBOOK_TOOL_REQUIRED_PARAMS[name] == required


def test_successful_side_effect_tool_does_not_trigger_duplicate_repair() -> None:
    plugin = object.__new__(AstrbookPlugin)
    event = FakeEvent()
    event.session_id = "astrbook_thread_12"

    class Tool:
        name = "delete_thread"

    run(plugin.mark_completed_astrbook_action(event, Tool(), {}, "Thread deleted"))
    assert event.get_extra("astrbook_action_completed") is True

    response = type(
        "Response",
        (),
        {"role": "assistant", "tools_call_name": None, "completion_text": "done"},
    )()
    assert plugin._should_repair_plain_astrbook_response(event, response) is False


def test_failed_side_effect_tool_remains_repairable() -> None:
    plugin = object.__new__(AstrbookPlugin)
    event = FakeEvent()
    event.session_id = "astrbook_thread_12"

    class Tool:
        name = "delete_thread"

    run(plugin.mark_completed_astrbook_action(event, Tool(), {}, "Failed to delete"))
    assert event.get_extra("astrbook_action_completed") is None


def test_trending_and_categories_use_api_response_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = object.__new__(AstrbookPlugin)
    event = FakeEvent()

    async def fake_request(method: str, path: str, **kwargs: object) -> dict:
        if path == "/api/threads/trending":
            return {
                "trends": [
                    {
                        "thread_id": 12,
                        "keyword": "AI agents",
                        "reply_count": 3,
                        "view_count": 40,
                        "like_count": 2,
                        "category": "tech",
                        "score": 9.5,
                    }
                ]
            }
        if path == "/api/threads/categories":
            return [{"key": "tech", "name": "技术分享"}]
        raise AssertionError(path)

    monkeypatch.setattr(plugin, "_make_request", fake_request)
    trending = run(plugin.trending_threads(event))
    categories = run(plugin.get_categories(event))
    assert "AI agents" in trending
    assert "view_count" not in trending
    assert "views=40" in trending
    assert "tech" in categories and "技术分享" in categories


def test_check_notifications_marks_only_displayed_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit mark_read must not clear notifications outside this page."""

    plugin = object.__new__(AstrbookPlugin)
    event = FakeEvent()
    calls: list[tuple[str, str, dict[str, object]]] = []

    async def fake_request(
        method: str,
        path: str,
        **kwargs: object,
    ) -> dict:
        calls.append((method, path, kwargs))
        if path == "/api/notifications/unread-count":
            return {"unread": 3, "total": 3}
        if path == "/api/dm/unread-count":
            return {"unread": 0, "conversations_with_unread": 0}
        if path == "/api/notifications":
            return {
                "items": [
                    {
                        "id": 11,
                        "type": "reply",
                        "from_user": {"username": "alice"},
                        "thread_id": 1,
                        "thread_title": "A thread",
                        "reply_id": 101,
                        "content_preview": "hello",
                    },
                    {
                        "id": 12,
                        "type": "like",
                        "from_user": {"username": "bob"},
                        "thread_id": 2,
                        "thread_title": "Another thread",
                        "content_preview": "liked",
                    },
                ],
                "total": 3,
            }
        if path in {
            "/api/notifications/11/read",
            "/api/notifications/12/read",
        }:
            return {"ok": True}
        raise AssertionError(f"unexpected request: {method} {path} {kwargs}")

    monkeypatch.setattr(plugin, "_make_request", fake_request)
    result = run(plugin.check_notifications(event, fetch_details=True, mark_read=True))

    assert "marked 2 as read" in result
    assert [path for _, path, _ in calls if path.endswith("/read")] == [
        "/api/notifications/11/read",
        "/api/notifications/12/read",
    ]
    assert "/api/notifications/read-all" not in [path for _, path, _ in calls]


def test_reply_floor_can_target_a_sub_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = object.__new__(AstrbookPlugin)
    event = FakeEvent()
    captured: dict[str, object] = {}

    async def fake_request(method: str, path: str, **kwargs: object) -> dict:
        captured.update(method=method, path=path, **kwargs)
        return {"id": 8}

    monkeypatch.setattr(plugin, "_make_request", fake_request)
    result = run(
        plugin.reply_floor(
            event,
            reply_id=3,
            content="我也赞同",
            reply_to_id=4,
        )
    )
    assert result == "Sub-reply successful"
    assert captured["data"] == {"content": "我也赞同", "reply_to_id": 4}


def test_check_notifications_does_not_suggest_reply_for_unlinked_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Follow/moderation notices can have null thread and reply IDs."""
    plugin = object.__new__(AstrbookPlugin)
    event = FakeEvent()

    async def fake_request(method: str, path: str, **kwargs: object) -> dict:
        if path == "/api/notifications/unread-count":
            return {"unread": 1, "total": 1}
        if path == "/api/dm/unread-count":
            return {"unread": 0, "conversations_with_unread": 0}
        if path == "/api/notifications":
            return {
                "items": [
                    {
                        "id": 7,
                        "type": "follow",
                        "thread_id": None,
                        "reply_id": None,
                        "from_user": {"nickname": "Alice"},
                        "content_preview": "关注了你",
                    }
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(plugin, "_make_request", fake_request)
    result = run(plugin.check_notifications(event, fetch_details=True))

    assert "Alice" in result
    assert "thread_id=None" not in result
    assert "reply_floor(reply_id=None" not in result
    assert "reply_thread(" not in result


def test_browse_hook_keeps_persona_tools_but_detaches_history() -> None:
    plugin = object.__new__(AstrbookPlugin)
    event = FakeEvent()
    event.set_extra("is_browse_event", True)

    class Request:
        def __init__(self) -> None:
            self.contexts = [{"role": "user", "content": "old browse history"}]
            self.conversation = object()
            self.func_tool = None
            self.extra_user_content_parts: list[object] = []

    request = Request()
    run(plugin.remind_astrbook_tool_reply(event, request))
    assert request.contexts == []
    assert request.conversation is None


def test_browse_repair_rebuilds_request_with_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """A browse retry must not reuse the detached ProviderRequest."""
    adapter = object.__new__(AstrBookAdapter)
    adapter._metadata = PlatformMetadata(
        name="astrbook", description="AstrBook", id="astrbook"
    )
    message = AstrBotMessage()
    message.message_id = "browse-1"
    message.type = MessageType.FRIEND_MESSAGE
    message.self_id = "astrbook"
    message.sender = type("Sender", (), {"user_id": "system", "nickname": "System"})()
    event = adapter_module.AstrBookMessageEvent(
        "browse",
        message,
        adapter.meta(),
        "astrbook_browse_system",
        adapter,
    )
    event.set_extra("is_browse_event", True)
    plugin = object.__new__(AstrbookPlugin)
    monkeypatch.setattr(plugin, "_get_astrbook_adapter", lambda: adapter)

    repair = plugin._clone_astrbook_event_for_repair(event, object())
    assert repair is not None
    assert repair.get_extra("provider_request") is None


def test_sse_parser_accepts_crlf_and_data_without_space() -> None:
    adapter = object.__new__(AstrBookAdapter)
    received: list[dict] = []

    async def handle(data: dict) -> None:
        received.append(data)

    adapter._handle_message = handle
    run(
        adapter._parse_sse_block(
            'event: message\r\ndata:{"type":"connected","user_id":7}\r\n'
        )
    )
    assert received == [{"type": "connected", "user_id": 7}]


def test_forum_memory_is_bounded_and_skips_corrupt_records(tmp_path: Path) -> None:
    memory = ForumMemory(max_items=2, storage_dir=tmp_path)
    memory.add_diary("first diary")
    memory.add_diary("second diary")
    memory.add_diary("third diary")

    reloaded = ForumMemory(max_items=2, storage_dir=tmp_path)
    assert [item.content for item in reloaded.get_diaries()] == [
        "third diary",
        "second diary",
    ]

    path = tmp_path / "forum_memory.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    records.insert(0, {"memory_type": "diary", "content": "bad timestamp"})
    path.write_text(json.dumps(records), encoding="utf-8")
    repaired = ForumMemory(max_items=10, storage_dir=tmp_path)
    assert all(item.content != "bad timestamp" for item in repaired.get_diaries())


def test_forum_memory_merges_split_legacy_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = tmp_path / "astrbot_plugin_astrbook"
    legacy = tmp_path / "astrbot-plugin-astrbook"
    auto = tmp_path / "auto"
    canonical.mkdir()
    legacy.mkdir()
    auto.mkdir()
    canonical_record = {
        "memory_type": "diary",
        "content": "canonical entry",
        "timestamp": "2026-09-15T10:00:00+00:00",
    }
    legacy_record = {
        "memory_type": "diary",
        "content": "legacy entry",
        "timestamp": "2026-09-16T10:00:00+00:00",
    }
    (canonical / "forum_memory.json").write_text(
        json.dumps([canonical_record]), encoding="utf-8"
    )
    (legacy / "forum_memory.json").write_text(
        json.dumps([legacy_record]), encoding="utf-8"
    )

    def fake_data_dir(name: str | None = None) -> Path:
        if name == "astrbot_plugin_astrbook":
            return canonical
        if name == "astrbot-plugin-astrbook":
            return legacy
        if name == "astrbook":
            return tmp_path / "unused"
        return auto

    monkeypatch.setattr(
        forum_memory_module.StarTools,
        "get_data_dir",
        staticmethod(fake_data_dir),
    )
    merged = ForumMemory(max_items=10)
    assert [item.content for item in merged.get_diaries()] == [
        "legacy entry",
        "canonical entry",
    ]
    persisted = json.loads((canonical / "forum_memory.json").read_text("utf-8"))
    assert {item["content"] for item in persisted} == {
        "canonical entry",
        "legacy entry",
    }


def test_limits_and_private_url_validation() -> None:
    assert _clamp_int(-1, default=3600, minimum=30, maximum=100) == 30
    assert _clamp_int("bad", default=3600, minimum=30, maximum=100) == 100
    assert _clamp_float(2.0, default=0.3, minimum=0.0, maximum=1.0) == 1.0
    assert _clamp_float(float("nan"), default=0.3, minimum=0.0, maximum=1.0) == 0.3
    assert _normalize_api_base("https://example.com/") == "https://example.com"
    assert _normalize_api_base("https://example.com/api") == "https://example.com"
    assert _normalize_api_base("https://example.com/api/sse/") == "https://example.com"
    assert _normalize_api_base("https://example.com/forum/api") == "https://example.com/forum"
    assert _normalize_api_base("javascript:alert(1)") == "https://book.astrbot.app"

    plugin = object.__new__(AstrbookPlugin)
    url, error = run(plugin._validate_public_url("http://127.0.0.1/image.png"))
    assert url is None
    assert error and "private" in error


@pytest.mark.parametrize(
    ("method", "args", "expected"),
    [
        ("reply_thread", (), "Error: thread_id must be a positive integer"),
        ("reply_floor", (), "Error: reply_id must be a positive integer"),
        ("read_thread", (), "Error: thread_id must be a positive integer"),
    ],
)
def test_other_required_ids_do_not_raise(method: str, args: tuple, expected: str) -> None:
    plugin = object.__new__(AstrbookPlugin)
    event = FakeEvent()
    result = run(getattr(plugin, method)(event, *args))
    assert result == expected
