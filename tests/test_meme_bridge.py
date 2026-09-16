from __future__ import annotations

import asyncio
import importlib
import json
import sys
from copy import copy, deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.api.platform import MessageType
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.provider.register import llm_tools

# Use the checkout's real package name, as test_astrbook.py does. A synthetic
# alias would execute AstrBot's adapter registration twice during collection.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT.parent))
package = importlib.import_module(REPO_ROOT.name)
assert Path(package.__file__).resolve() == (REPO_ROOT / "__init__.py").resolve()
bridge_module = importlib.import_module(f"{REPO_ROOT.name}.meme_bridge")
main_module = importlib.import_module(f"{REPO_ROOT.name}.main")
MemeBridge = bridge_module.MemeBridge
MemeBridgeError = bridge_module.MemeBridgeError

HASH_A = "a" * 64
HASH_B = "b" * 64
IMAGE_URL = "https://cdn.example.com/memes/cat.gif"


class FakeEvent:
    def __init__(self) -> None:
        self.extras: dict[str, object] = {}

    def get_extra(self, key: str, default: object = None) -> object:
        return self.extras.get(key, default)

    def set_extra(self, key: str, value: object) -> None:
        self.extras[key] = value

    def get_platform_name(self) -> str:
        return "astrbook"


def run(coroutine):
    return asyncio.run(coroutine)


def candidate(**overrides: object) -> dict:
    return {
        "emoji_id": "emoji_1",
        "hash": HASH_A,
        "scope_mode": "public",
        "desc": "一只捂脸的猫",
        "character": "猫猫",
        "tags": ["捂脸", "尴尬"],
        **overrides,
    }


@pytest.fixture
def integration() -> SimpleNamespace:
    asset = SimpleNamespace(
        sha256=HASH_A,
        metadata={"scope_mode": "public"},
        release=Mock(),
    )
    magpie = SimpleNamespace(
        search_meme_candidates=AsyncMock(return_value=[candidate()]),
        export_meme_asset=AsyncMock(return_value=asset),
    )
    ferry = SimpleNamespace(
        upload_asset=AsyncMock(
            return_value={"success": True, "url": IMAGE_URL, "reused": True}
        )
    )
    stars = {
        bridge_module.MAGPIE: SimpleNamespace(star_cls=magpie, activated=True),
        bridge_module.FERRY: SimpleNamespace(star_cls=ferry, activated=True),
    }
    context = SimpleNamespace(get_registered_star=stars.get)
    return SimpleNamespace(
        asset=asset,
        magpie=magpie,
        ferry=ferry,
        stars=stars,
        context=context,
        bridge=MemeBridge(context, {}),
        event=FakeEvent(),
    )


async def select(integration: SimpleNamespace) -> str:
    results = await integration.bridge.search(integration.event, "猫猫捂脸", {})
    assert len(results) == 1
    return results[0]["meme_ref"]


def test_search_reuses_tag_search_and_exposes_only_safe_public_candidates(
    integration: SimpleNamespace,
) -> None:
    integration.magpie.search_meme_candidates.return_value = [
        candidate(path="/private/library/cat.gif"),
        candidate(emoji_id="emoji_2", scope_mode="local"),
        candidate(emoji_id="emoji_3", hash=""),
        candidate(emoji_id="emoji_4", hash="c" * 32),
        candidate(emoji_id="/arbitrary/file"),
    ]
    results = run(
        integration.bridge.search(
            integration.event,
            "猫猫捂脸",
            {"character": "猫猫", "tag": "尴尬", "scope_mode": "local"},
        )
    )

    integration.magpie.search_meme_candidates.assert_awaited_once_with(
        integration.event,
        "猫猫捂脸",
        filters={"character": "猫猫", "tag": "尴尬", "scope_mode": "public"},
        limit=5,
    )
    assert len(results) == 1
    assert results[0]["character"] == "猫猫"
    assert results[0]["tags"] == ["捂脸", "尴尬"]
    assert results[0]["meme_ref"].startswith("abm_")
    assert "path" not in results[0]
    assert "hash" not in results[0]
    integration.ferry.upload_asset.assert_not_awaited()


def test_exact_selected_asset_uses_original_event_and_ferry_deduplication(
    integration: SimpleNamespace,
) -> None:
    integration.bridge.config["meme_upload_folder"] = "astrbook/memes"
    integration.ferry.upload_asset.return_value["markdown"] = "untrusted upstream text"

    async def scenario() -> tuple[str, bool]:
        reference = await select(integration)
        return await integration.bridge.attach(integration.event, "论坛正文  ", reference)

    content, reused = run(scenario())
    assert content == f"论坛正文\n\n![表情包](<{IMAGE_URL}>)"
    assert reused is True
    integration.magpie.export_meme_asset.assert_awaited_once_with(
        "emoji_1", integration.event
    )
    integration.ferry.upload_asset.assert_awaited_once_with(
        integration.event,
        integration.asset,
        folder="astrbook/memes",
        compress=False,
        output_format="markdown",
    )
    integration.asset.release.assert_called_once_with()


def test_other_magpie_search_cannot_silently_replace_the_selected_image(
    integration: SimpleNamespace,
) -> None:
    async def scenario() -> None:
        reference = await select(integration)
        # Another search has rebound emoji_1 to a different image.
        integration.asset.sha256 = HASH_B
        with pytest.raises(MemeBridgeError) as error:
            await integration.bridge.attach(integration.event, "正文", reference)
        assert error.value.code == "candidate_changed"

    run(scenario())
    integration.ferry.upload_asset.assert_not_awaited()
    integration.asset.release.assert_called_once_with()


@pytest.mark.parametrize("reason", ["new_search", "other_event", "expired"])
def test_stale_or_foreign_references_never_export(
    integration: SimpleNamespace, reason: str
) -> None:
    async def scenario() -> None:
        reference = await select(integration)
        event = integration.event
        if reason == "new_search":
            await integration.bridge.search(event, "新的语境", {})
        elif reason == "other_event":
            event = FakeEvent()
        else:
            state = event.get_extra(bridge_module.STATE_KEY)
            state.selections[reference].expires_at = 0
        with pytest.raises(MemeBridgeError) as error:
            await integration.bridge.attach(event, "正文", reference)
        assert error.value.code == "candidate_expired"

    run(scenario())
    integration.magpie.export_meme_asset.assert_not_awaited()
    integration.ferry.upload_asset.assert_not_awaited()


def test_provider_reload_invalidates_old_references(integration: SimpleNamespace) -> None:
    async def scenario() -> None:
        reference = await select(integration)
        integration.stars[bridge_module.MAGPIE].star_cls = SimpleNamespace(
            search_meme_candidates=AsyncMock(), export_meme_asset=AsyncMock()
        )
        with pytest.raises(MemeBridgeError) as error:
            await integration.bridge.attach(integration.event, "正文", reference)
        assert error.value.code == "provider_reloaded"

    run(scenario())
    integration.magpie.export_meme_asset.assert_not_awaited()
    integration.ferry.upload_asset.assert_not_awaited()


@pytest.mark.parametrize("metadata", [{"scope_mode": "local"}, {}, None])
def test_scope_is_rechecked_after_export_before_publication(
    integration: SimpleNamespace, metadata: object
) -> None:
    integration.asset.metadata = metadata

    async def scenario() -> None:
        reference = await select(integration)
        with pytest.raises(MemeBridgeError) as error:
            await integration.bridge.attach(integration.event, "正文", reference)
        assert error.value.code == "scope_denied"

    run(scenario())
    integration.ferry.upload_asset.assert_not_awaited()
    integration.asset.release.assert_called_once_with()


@pytest.mark.parametrize(
    ("result", "code"),
    [
        ({"success": False, "code": "quota_exceeded"}, "quota_exceeded"),
        ({"success": False, "code": "token=/secret/path"}, "upload_failed"),
        ({"success": "true", "url": IMAGE_URL}, "upload_failed"),
        (None, "invalid_upload_result"),
        ({"success": True, "url": "http://127.0.0.1/private.png"}, "invalid_url"),
    ],
)
def test_upload_failure_releases_asset_and_preserves_safe_error_codes(
    integration: SimpleNamespace, result: object, code: str
) -> None:
    integration.ferry.upload_asset.return_value = result

    async def scenario() -> None:
        reference = await select(integration)
        with pytest.raises(MemeBridgeError) as error:
            await integration.bridge.attach(integration.event, "正文", reference)
        assert error.value.code == code
        assert "/secret/path" not in str(error.value)

    run(scenario())
    integration.asset.release.assert_called_once_with()


def test_upload_exception_does_not_leak_credentials_and_releases_asset(
    integration: SimpleNamespace,
) -> None:
    integration.ferry.upload_asset.side_effect = RuntimeError("token=secret-value")

    async def scenario() -> None:
        reference = await select(integration)
        with pytest.raises(MemeBridgeError) as error:
            await integration.bridge.attach(integration.event, "正文", reference)
        assert error.value.code == "asset_failed"
        assert "secret-value" not in str(error.value)

    run(scenario())
    integration.asset.release.assert_called_once_with()


def test_cancellation_during_upload_releases_the_asset(
    integration: SimpleNamespace,
) -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        async def upload(*args: object, **kwargs: object) -> None:
            started.set()
            await asyncio.Event().wait()

        integration.ferry.upload_asset.side_effect = upload
        reference = await select(integration)
        task = asyncio.create_task(
            integration.bridge.attach(integration.event, "正文", reference)
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    integration.asset.release.assert_called_once_with()


def test_upload_timeout_releases_the_asset(
    integration: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge_module, "UPLOAD_TIMEOUT", 0.01)

    async def upload(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    integration.ferry.upload_asset.side_effect = upload

    async def scenario() -> None:
        reference = await select(integration)
        with pytest.raises(MemeBridgeError) as error:
            await integration.bridge.attach(integration.event, "正文", reference)
        assert error.value.code == "asset_timeout"

    run(scenario())
    integration.asset.release.assert_called_once_with()


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///private/meme.gif",
        "//cdn.example.com/meme.gif",
        "http://127.0.0.1/meme.gif",
        "http://[::1]/meme.gif",
        "http://192.168.1.1/meme.gif",
        "http://localhost/meme.gif",
        "https://cdn.local/meme.gif",
        "https://cdn.internal/meme.gif",
        "https://user:password@cdn.example.com/meme.gif",
        "https://cdn.example.com:bad/meme.gif",
        "https://cdn.example.com/a\nb.gif",
        "https://cdn.example.com/a\\b.gif",
        None,
    ],
)
def test_invalid_image_urls_cannot_be_embedded(url: object) -> None:
    with pytest.raises(MemeBridgeError) as error:
        MemeBridge._image_markdown(url)
    assert error.value.code == "invalid_url"


def test_markdown_delimiters_are_encoded_without_rewriting_signed_url() -> None:
    markdown = MemeBridge._image_markdown(
        "https://cdn.example.com/cat(1)<x>.gif?sig=a%2Fb&expires=99"
    )
    assert markdown == (
        "![表情包](<https://cdn.example.com/cat%281%29%3Cx%3E.gif"
        "?sig=a%2Fb&expires=99>)"
    )


@pytest.mark.parametrize("name", [bridge_module.MAGPIE, bridge_module.FERRY])
@pytest.mark.parametrize("state", ["missing", "disabled", "outdated"])
def test_missing_disabled_or_outdated_plugins_fail_before_search(
    integration: SimpleNamespace, name: str, state: str
) -> None:
    if state == "missing":
        integration.stars.pop(name)
    elif state == "disabled":
        integration.stars[name].activated = False
    else:
        integration.stars[name].star_cls = object()

    assert integration.bridge.available() is False
    with pytest.raises(MemeBridgeError) as error:
        run(integration.bridge.search(integration.event, "开心", {}))
    expected = "plugin_outdated" if state == "outdated" else "plugin_unavailable"
    assert error.value.code == expected
    integration.magpie.search_meme_candidates.assert_not_awaited()
    integration.ferry.upload_asset.assert_not_awaited()


def test_integration_can_be_disabled_without_loading_other_plugins(
    integration: SimpleNamespace,
) -> None:
    integration.bridge.config["meme_enabled"] = False
    integration.context.get_registered_star = Mock(side_effect=AssertionError)
    assert integration.bridge.available() is False
    with pytest.raises(MemeBridgeError) as error:
        run(integration.bridge.search(integration.event, "开心", {}))
    assert error.value.code == "integration_disabled"
    integration.context.get_registered_star.assert_not_called()


POST_CASES = [
    ("create_thread", {"title": "论坛标题", "content": "这是论坛正文"}, "/api/threads"),
    ("reply_thread", {"thread_id": 12, "content": "这是论坛正文"}, "/api/threads/12/replies"),
    ("reply_floor", {"reply_id": 34, "content": "这是论坛正文"}, "/api/replies/34/sub_replies"),
]


@pytest.mark.parametrize(("method", "arguments", "path"), POST_CASES)
def test_forum_tools_attach_the_chosen_meme_before_posting(
    method: str, arguments: dict, path: str
) -> None:
    plugin = object.__new__(main_module.AstrbookPlugin)
    event = FakeEvent()
    attached = f"这是论坛正文\n\n![表情包](<{IMAGE_URL}>)"
    plugin._meme_bridge = SimpleNamespace(attach=AsyncMock(return_value=(attached, True)))
    plugin._make_request = AsyncMock(return_value={"ok": True})

    result = run(getattr(plugin, method)(event, **arguments, meme_ref="abm_selected"))

    plugin._meme_bridge.attach.assert_awaited_once_with(
        event, arguments["content"], "abm_selected"
    )
    request = plugin._make_request.await_args
    assert request.args == ("POST", path)
    assert request.kwargs["data"]["content"] == attached
    assert not result.startswith("Error")
    assert event.get_extra("astrbook_tool_reply_sent") is True


@pytest.mark.parametrize(("method", "arguments", "path"), POST_CASES)
def test_invalid_post_body_never_triggers_upload(
    method: str, arguments: dict, path: str
) -> None:
    plugin = object.__new__(main_module.AstrbookPlugin)
    event = FakeEvent()
    plugin._meme_bridge = SimpleNamespace(attach=AsyncMock())
    plugin._make_request = AsyncMock()
    invalid = {**arguments, "content": ""}

    run(getattr(plugin, method)(event, **invalid, meme_ref="abm_selected"))

    plugin._meme_bridge.attach.assert_not_awaited()
    plugin._make_request.assert_not_awaited()


@pytest.mark.parametrize(("method", "arguments", "path"), POST_CASES)
def test_failed_meme_upload_never_publishes_a_partial_post(
    method: str, arguments: dict, path: str
) -> None:
    plugin = object.__new__(main_module.AstrbookPlugin)
    event = FakeEvent()
    plugin._meme_bridge = SimpleNamespace(
        attach=AsyncMock(side_effect=MemeBridgeError("quota_exceeded", "上传额度不足"))
    )
    plugin._make_request = AsyncMock()

    result = run(getattr(plugin, method)(event, **arguments, meme_ref="abm_selected"))

    assert result.startswith("Error")
    assert "quota_exceeded" in result
    plugin._make_request.assert_not_awaited()
    assert event.get_extra("astrbook_tool_reply_sent") is None


def test_search_tool_returns_structured_candidates() -> None:
    plugin = object.__new__(main_module.AstrbookPlugin)
    event = FakeEvent()
    candidates = [{"meme_ref": "abm_selected", "desc": "猫猫捂脸"}]
    plugin._meme_bridge = SimpleNamespace(search=AsyncMock(return_value=candidates))

    result = json.loads(run(plugin.search_forum_memes(event, query="猫猫捂脸")))

    assert result["candidates"] == candidates
    assert isinstance(result["instruction"], str) and result["instruction"]
    assert plugin._meme_bridge.search.await_args.args[:2] == (event, "猫猫捂脸")


def cron_event(
    integration: SimpleNamespace,
    *,
    session_id: str = "887766",
    sender_id: str | None = "12345",
    is_group: bool = True,
    unique_session: bool = False,
    platform_name: str = "aiocqhttp",
) -> CronMessageEvent:
    message_type = MessageType.GROUP_MESSAGE if is_group else MessageType.FRIEND_MESSAGE
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(name=platform_name))
    integration.context.get_platform_inst = Mock(return_value=platform)
    integration.context.get_config = Mock(
        return_value={"platform_settings": {"unique_session": unique_session}}
    )
    session = MessageSession("qq-adapter-1", message_type, session_id)
    payload = {"session": str(session), "note": "用猫猫捂脸表情回复论坛"}
    if sender_id is not None:
        payload["sender_id"] = sender_id
    event = CronMessageEvent(
        context=integration.context,
        session=session,
        message=payload["note"],
        extras={"cron_payload": payload},
        message_type=message_type,
    )
    event.role = "admin"
    return event


@pytest.mark.parametrize(
    ("session_id", "unique_session", "expected_group"),
    [
        ("887766", False, "887766"),
        ("12345_887766", True, "887766"),
        ("12345_887766", False, "887766"),
    ],
)
def test_real_cron_group_identity_is_recovered_without_mutating_original(
    integration: SimpleNamespace,
    session_id: str,
    unique_session: bool,
    expected_group: str,
) -> None:
    event = cron_event(
        integration, session_id=session_id, unique_session=unique_session
    )
    original_message = event.message_obj
    original_sender = event.message_obj.sender
    original_group = event.get_group_id()
    original_sender_id = event.get_sender_id()

    async def scenario() -> None:
        results = await integration.bridge.search(event, "猫猫捂脸", {})
        view = integration.magpie.search_meme_candidates.await_args.args[0]
        assert isinstance(view, CronMessageEvent)
        assert view is not event
        assert view.message_obj is not original_message
        assert view.message_obj.sender is not original_sender
        assert view.session is event.session
        assert view.role == event.role == "admin"
        assert view.get_platform_name() == "cron"
        assert view.get_sender_id() == "12345"
        assert view.get_group_id() == expected_group
        assert view.get_extra() is event.get_extra()
        # Magpie stores its per-turn candidates in extras. Search and export
        # must see the same event view and the same shared candidate state.
        view.set_extra("magpie_candidates_probe", "present")
        await integration.bridge.attach(event, "正文", results[0]["meme_ref"])
        integration.magpie.export_meme_asset.assert_awaited_once_with("emoji_1", view)
        assert integration.ferry.upload_asset.await_args.args[0] is view
        assert event.get_extra("magpie_candidates_probe") == "present"
        # A second search in this execution also reuses the normalized view.
        await integration.bridge.search(event, "猫猫开心", {})
        assert integration.magpie.search_meme_candidates.await_args.args[0] is view

    run(scenario())
    assert event.message_obj is original_message
    assert event.message_obj.sender is original_sender
    assert event.get_group_id() == original_group
    assert event.get_sender_id() == original_sender_id == session_id
    assert event.role == "admin"
    integration.asset.release.assert_called_once_with()


def test_real_private_cron_can_resolve_sender_without_inventing_group(
    integration: SimpleNamespace,
) -> None:
    event = cron_event(
        integration, session_id="12345", sender_id="12345", is_group=False
    )

    async def scenario() -> None:
        results = await integration.bridge.search(event, "猫猫捂脸", {})
        view = integration.magpie.search_meme_candidates.await_args.args[0]
        assert view.get_sender_id() == "12345"
        assert not view.get_group_id()
        await integration.bridge.attach(event, "正文", results[0]["meme_ref"])
        assert integration.ferry.upload_asset.await_args.args[0] is view

    run(scenario())
    integration.context.get_platform_inst.assert_not_called()


def test_telegram_cron_group_is_preserved_with_unique_session_enabled(
    integration: SimpleNamespace,
) -> None:
    event = cron_event(
        integration,
        platform_name="telegram",
        session_id="-100112233",
        unique_session=True,
    )
    run(integration.bridge.search(event, "猫猫捂脸", {}))
    view = integration.magpie.search_meme_candidates.await_args.args[0]
    assert view.get_group_id() == "-100112233"
    assert view.get_sender_id() == "12345"


@pytest.mark.parametrize(
    "disabled_plugin",
    ["astrbot_plugin_astrbook", bridge_module.MAGPIE, bridge_module.FERRY],
)
@pytest.mark.parametrize("stage", ["search", "upload"])
def test_session_plugin_set_is_enforced_before_search_and_upload(
    integration: SimpleNamespace, disabled_plugin: str, stage: str
) -> None:
    event = cron_event(integration)
    enabled_plugins = ["astrbot_plugin_astrbook", bridge_module.MAGPIE, bridge_module.FERRY]
    disabled_plugins = [name for name in enabled_plugins if name != disabled_plugin]

    async def scenario() -> None:
        reference = None
        if stage == "upload":
            result = await integration.bridge.search(event, "猫猫捂脸", {})
            reference = result[0]["meme_ref"]
        integration.context.get_config.return_value = {"plugin_set": disabled_plugins}
        with pytest.raises(MemeBridgeError) as error:
            if reference is None:
                await integration.bridge.search(event, "猫猫捂脸", {})
            else:
                await integration.bridge.attach(event, "正文", reference)
        assert error.value.code == "plugin_disabled_in_session"
        assert disabled_plugin in str(error.value)

    run(scenario())
    if stage == "search":
        integration.magpie.search_meme_candidates.assert_not_awaited()
    integration.context.get_config.assert_any_call(umo=event.unified_msg_origin)
    integration.magpie.export_meme_asset.assert_not_awaited()
    integration.ferry.upload_asset.assert_not_awaited()


@pytest.mark.parametrize(
    "options",
    [
        {"sender_id": None},
        {"sender_id": None, "is_group": False},
        {"session_id": "12345", "platform_name": "dingtalk"},
        {"session_id": "ambiguous_room", "platform_name": "dingtalk", "unique_session": True},
    ],
)
def test_real_cron_rejects_unrecoverable_group_identity_before_dependency_calls(
    integration: SimpleNamespace, options: dict
) -> None:
    event = cron_event(integration, **options)
    with pytest.raises(MemeBridgeError) as error:
        run(integration.bridge.search(event, "猫猫捂脸", {}))
    assert error.value.code == "cron_identity_missing"
    integration.magpie.search_meme_candidates.assert_not_awaited()
    integration.magpie.export_meme_asset.assert_not_awaited()
    integration.ferry.upload_asset.assert_not_awaited()


def test_next_cron_execution_cannot_reuse_previous_candidate_reference(
    integration: SimpleNamespace,
) -> None:
    first_event = cron_event(integration)
    next_event = cron_event(integration)
    assert str(first_event.session) == str(next_event.session)

    async def scenario() -> None:
        results = await integration.bridge.search(first_event, "猫猫捂脸", {})
        with pytest.raises(MemeBridgeError) as error:
            await integration.bridge.attach(next_event, "正文", results[0]["meme_ref"])
        assert error.value.code == "candidate_expired"

    run(scenario())
    integration.magpie.export_meme_asset.assert_not_awaited()
    integration.ferry.upload_asset.assert_not_awaited()


@pytest.mark.parametrize(("method", "arguments", "path"), POST_CASES)
def test_future_task_can_search_then_publish_without_chat_request_hook(
    integration: SimpleNamespace, method: str, arguments: dict, path: str
) -> None:
    plugin = object.__new__(main_module.AstrbookPlugin)
    plugin._meme_bridge = integration.bridge
    plugin._make_request = AsyncMock(return_value={"ok": True})
    event = cron_event(integration, session_id="12345_887766", unique_session=True)

    async def scenario() -> str:
        result = json.loads(await plugin.search_forum_memes(event, query="猫猫捂脸"))
        reference = result["candidates"][0]["meme_ref"]
        return await getattr(plugin, method)(event, **arguments, meme_ref=reference)

    result = run(scenario())
    assert not result.startswith("Error")
    request = plugin._make_request.await_args
    assert request.args == ("POST", path)
    assert request.kwargs["data"]["content"] == (
        f"{arguments['content']}\n\n![表情包](<{IMAGE_URL}>)"
    )
    view = integration.magpie.search_meme_candidates.await_args.args[0]
    assert view.get_sender_id() == "12345"
    assert view.get_group_id() == "887766"
    assert integration.magpie.export_meme_asset.await_args.args == ("emoji_1", view)
    assert integration.ferry.upload_asset.await_args.args == (view, integration.asset)
    assert event.get_extra("astrbook_tool_reply_sent") is True
    integration.asset.release.assert_called_once_with()


def test_real_astrbot_tool_schemas_advertise_search_and_optional_meme_reference() -> None:
    names = ("search_forum_memes", "create_thread", "reply_thread", "reply_floor")
    # Copy SDK-decorated tools so this test can exercise schema hardening
    # without modifying the process-global AstrBot tool registry.
    tools = {}
    for name in names:
        registered = llm_tools.get_func(name)
        assert registered is not None
        tool = copy(registered)
        tool.parameters = deepcopy(registered.parameters)
        tools[name] = tool
    plugin = object.__new__(main_module.AstrbookPlugin)
    plugin.context = SimpleNamespace(
        get_llm_tool_manager=lambda: SimpleNamespace(get_func=tools.get)
    )
    plugin._harden_tool_schemas()

    search_parameters = tools["search_forum_memes"].parameters
    assert search_parameters["required"] == ["query"]
    assert search_parameters["properties"]["query"]["type"] == "string"
    for name in names[1:]:
        schema = tools[name].parameters
        assert schema["properties"]["meme_ref"]["type"] == "string"
        assert "meme_ref" not in schema["required"]
        assert "content" in schema["required"]
        assert "search_forum_memes" in tools[name].description
