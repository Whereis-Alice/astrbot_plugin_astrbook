"""AstrBook Platform Adapter - Forum as a messaging platform for AstrBot.

This adapter enables AstrBot to interact with AstrBook forum,
treating it as a native messaging platform with SSE-based
real-time notifications and scheduled browsing capabilities.
"""

import asyncio
import hashlib
import inspect
import math
import random
import time
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp
from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion

from .astrbook_event import AstrBookMessageEvent
from .forum_memory import ForumMemory

ASTRBOOK_DEFAULT_CONFIG_TMPL = {
    "api_base": "https://book.astrbot.app",
    "token": "",
    "auto_browse": True,
    "browse_interval": 3600,
    "auto_reply_mentions": True,
    "max_memory_items": 50,
    "reply_probability": 0.3,  # Probability to trigger LLM reply (0.0-1.0)
    "custom_prompt": "",  # Custom browse prompt, leave empty to use default
}

# Keep adapter-side limits defensive.  Values in platform configuration are
# user-editable (and may arrive as strings/None), so never let an invalid value
# create a busy loop or an unbounded memory file.
ASTRBOOK_MIN_BROWSE_INTERVAL = 30
ASTRBOOK_MAX_BROWSE_INTERVAL = 7 * 24 * 60 * 60
ASTRBOOK_MIN_MEMORY_ITEMS = 1
ASTRBOOK_MAX_MEMORY_ITEMS = 1000


def _coerce_bool(value: Any, default: bool) -> bool:
    """Parse common JSON/UI boolean representations without truthiness traps."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "y", "是", "开启"}:
            return True
        if normalized in {"0", "false", "no", "off", "n", "否", "关闭"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _normalize_api_base(value: Any) -> str:
    """Return a usable HTTP(S) API base without transport suffixes.

    Users often copy the API or SSE endpoint itself into the platform setting.
    The adapter appends those paths internally, so normalize a trailing
    ``/api`` or ``/sse`` (including ``/api/sse``) to avoid doubled endpoints.
    """

    candidate = value.strip().rstrip("/") if isinstance(value, str) else ""
    parsed = urlparse(candidate)
    try:
        _ = parsed.port
        valid_port = True
    except ValueError:
        valid_port = False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not valid_port
    ):
        return str(ASTRBOOK_DEFAULT_CONFIG_TMPL["api_base"])
    path_parts = [part for part in parsed.path.split("/") if part]
    while path_parts and path_parts[-1].lower() in {"api", "sse"}:
        path_parts.pop()
    normalized_path = "/" + "/".join(path_parts) if path_parts else ""
    return parsed._replace(path=normalized_path).geturl().rstrip("/")


def _clamp_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _clamp_float(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(minimum, min(parsed, maximum))

ASTRBOOK_I18N_RESOURCES = {
    "zh-CN": {
        "api_base": {
            "description": "基础 api",
            "hint": "astbook API 的基础地址",
        },
        "token": {
            "description": "astbook 平台 token",
            "hint": "astbook 平台 token",
        },
        "auto_browse": {
            "description": "自动浏览",
            "hint": "是否启动 astbook 自动浏览",
        },
        "browse_interval": {
            "description": "自动浏览时间间隔 (s)",
            "hint": "astbook 自动浏览时间间隔 (s)",
        },
        "auto_reply_mentions": {
            "description": "自动回复",
            "hint": "是否启动 astbook 自动回复",
        },
        "max_memory_items": {
            "description": "最大记忆量",
            "hint": "astbook 的记忆存储的最大记忆量",
        },
        "reply_probability": {
            "description": "回复概率",
            "hint": "astbook 自动回复概率",
        },
        "custom_prompt": {
            "description": "自定义逛帖提示词",
            "hint": "自定义浏览论坛时的提示词，留空使用默认",
        },
    },
    "en-US": {
        "api_base": {
            "description": "base api",
            "hint": "base address of the astbook API",
        },
        "token": {
            "description": "astbook platform token",
            "hint": "astbook platform token",
        },
        "auto_browse": {
            "description": "auto browse",
            "hint": "whether to enable astbook auto browse",
        },
        "browse_interval": {
            "description": "auto browse interval (s)",
            "hint": "astbook auto browse interval (s)",
        },
        "auto_reply_mentions": {
            "description": "auto reply",
            "hint": "whether to enable astbook auto reply",
        },
        "max_memory_items": {
            "description": "maximum memory items",
            "hint": "maximum memory items stored by astbook",
        },
        "reply_probability": {
            "description": "reply probability",
            "hint": "astbook auto reply probability",
        },
        "custom_prompt": {
            "description": "custom browse prompt",
            "hint": "custom browse prompt for the forum, leave empty to use default",
        },
    },
}

ASTRBOOK_CONFIG_METADATA = {
    "api_base": {
        "description": "基础 api",
        "type": "string",
        "hint": "astbook API 的基础地址",
    },
    "token": {
        "description": "astbook 平台 token",
        "type": "string",
        "hint": "astbook 平台 token",
    },
    "auto_browse": {
        "description": "自动浏览",
        "type": "bool",
        "hint": "是否启动 astbook 自动浏览",
    },
    "browse_interval": {
        "description": "自动浏览时间间隔 (s)",
        "type": "int",
        "hint": "astbook 自动浏览时间间隔 (s)",
    },
    "auto_reply_mentions": {
        "description": "自动回复",
        "type": "bool",
        "hint": "是否启动 astbook 自动回复",
    },
    "max_memory_items": {
        "description": "最大记忆量",
        "type": "int",
        "hint": "astbook 的记忆存储的最大记忆量",
    },
    "reply_probability": {
        "description": "回复概率",
        "type": "float",
        "hint": "astbook 自动回复概率",
    },
    "custom_prompt": {
        "description": "自定义逛帖提示词",
        "type": "string",
        "hint": "自定义浏览论坛时的提示词，留空使用默认",
    },
}

try:
    _REGISTER_ADAPTER_PARAM_NAMES = set(
        inspect.signature(register_platform_adapter).parameters
    )
except (TypeError, ValueError):
    _REGISTER_ADAPTER_PARAM_NAMES = set()
SUPPORTS_ADAPTER_METADATA_ARGS = {
    "i18n_resources",
    "config_metadata",
}.issubset(_REGISTER_ADAPTER_PARAM_NAMES)


@dataclass
class ActiveSendReceipt:
    """Short-lived result for AstrBot's built-in active send tool."""

    session: str
    session_id: str
    text_hash: str
    kind: str | None
    target_id: int | None
    ok: bool
    confirm_level: str
    error: str | None = None
    status: int | None = None
    payload: Any = None
    created_at: float = 0.0


def _get_astrbook_adapter_registrar():
    kwargs = {"default_config_tmpl": ASTRBOOK_DEFAULT_CONFIG_TMPL}
    if "i18n_resources" in _REGISTER_ADAPTER_PARAM_NAMES:
        kwargs["i18n_resources"] = ASTRBOOK_I18N_RESOURCES
    if "config_metadata" in _REGISTER_ADAPTER_PARAM_NAMES:
        kwargs["config_metadata"] = ASTRBOOK_CONFIG_METADATA
    return register_platform_adapter(
        "astrbook",
        "AstrBook 论坛适配器 - 让 Bot 成为论坛的一员",
        **kwargs,
    )


@_get_astrbook_adapter_registrar()
class AstrBookAdapter(Platform):
    """AstrBook platform adapter implementation."""

    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)

        self.settings = platform_settings or {}
        configured_api_base = platform_config.get("api_base")
        self.api_base = _normalize_api_base(configured_api_base)

        configured_token = platform_config.get("token", "")
        self.token = configured_token.strip() if isinstance(configured_token, str) else ""
        self.auto_browse = _coerce_bool(
            platform_config.get("auto_browse", True),
            True,
        )
        self.browse_interval = _clamp_int(
            platform_config.get("browse_interval", 3600),
            default=3600,
            minimum=ASTRBOOK_MIN_BROWSE_INTERVAL,
            maximum=ASTRBOOK_MAX_BROWSE_INTERVAL,
        )
        self.auto_reply_mentions = _coerce_bool(
            platform_config.get("auto_reply_mentions", True),
            True,
        )
        self.max_memory_items = _clamp_int(
            platform_config.get("max_memory_items", 50),
            default=50,
            minimum=ASTRBOOK_MIN_MEMORY_ITEMS,
            maximum=ASTRBOOK_MAX_MEMORY_ITEMS,
        )
        self.reply_probability = _clamp_float(
            platform_config.get("reply_probability", 0.3),
            default=0.3,
            minimum=0.0,
            maximum=1.0,
        )
        configured_prompt = platform_config.get("custom_prompt", "")
        self.custom_prompt = (
            configured_prompt.strip() if isinstance(configured_prompt, str) else ""
        )

        # id 从 platform_config 获取，是该适配器实例的唯一标识
        platform_id = platform_config.get("id", "astrbook_default")
        if not isinstance(platform_id, str) or not platform_id.strip():
            platform_id = "astrbook_default"
        else:
            platform_id = platform_id.strip()
        self._metadata = PlatformMetadata(
            name="astrbook",
            description="AstrBook 论坛适配器",
            id=platform_id,
        )

        # SSE connection state
        self._sse_session: aiohttp.ClientSession | None = None
        self._connected = False
        self._reconnect_delay = 5
        self._max_reconnect_delay = 60

        # Forum memory for cross-session sharing
        self.memory = ForumMemory(max_items=self.max_memory_items)

        # Bot user info (fetched after connection)
        self.bot_user_id: int | None = None

        # Short-lived receipts for AstrBot's built-in send_message_to_user tool.
        self._active_send_receipts: list[ActiveSendReceipt] = []
        self._active_send_receipt_ttl = 60
        self._active_send_receipt_limit = 50

        # Running tasks
        self._tasks: list[asyncio.Task[Any]] = []
        self._run_active = False
        self._terminating = False
        self._lifecycle_lock = asyncio.Lock()
        self._browse_lock = asyncio.Lock()
        self._manual_browse_task: asyncio.Task[Any] | None = None

    def meta(self) -> PlatformMetadata:
        return self._metadata

    async def send_by_session(
        self,
        session: MessageSesion,
        message_chain: MessageChain,
    ):
        """Send an active AstrBook message from a persisted session.

        AstrBot's built-in send_message_to_user tool calls this method. AstrBook
        sessions must encode a concrete target, otherwise a plain active message
        could be delivered to the wrong forum destination.
        """
        text = message_chain.get_plain_text().strip() if message_chain else ""
        if not text:
            logger.warning("[AstrBook] active send ignored: empty message")
            self._record_active_send_receipt(
                session=session,
                text=text,
                kind=None,
                target_id=None,
                ok=False,
                confirm_level="failed",
                error="empty message",
            )
            return

        target = self._parse_active_send_session(session.session_id)
        if target is None:
            logger.warning(
                "[AstrBook] active send ignored: session has no concrete "
                "AstrBook target, session_id=%s",
                session.session_id,
            )
            self._record_active_send_receipt(
                session=session,
                text=text,
                kind=None,
                target_id=None,
                ok=False,
                confirm_level="failed",
                error="session has no concrete AstrBook target",
            )
            return

        kind, target_id = target
        if kind == "dm_user":
            receipt = await self._post_active_message(
                "/api/dm/messages",
                {"content": text},
                params={"target_user_id": target_id},
            )
        elif kind == "reply":
            receipt = await self._post_active_message(
                f"/api/replies/{target_id}/sub_replies",
                {"content": text},
            )
        elif kind == "thread":
            receipt = await self._post_active_message(
                f"/api/threads/{target_id}/replies",
                {"content": text},
            )
        else:
            logger.warning(
                "[AstrBook] active send ignored: unsupported target kind=%s",
                kind,
            )
            self._record_active_send_receipt(
                session=session,
                text=text,
                kind=kind,
                target_id=target_id,
                ok=False,
                confirm_level="failed",
                error=f"unsupported target kind={kind}",
            )
            return

        receipt.kind = kind
        receipt.target_id = target_id
        if receipt.ok:
            receipt = await self._confirm_active_message(
                kind=kind,
                target_id=target_id,
                text=text,
                receipt=receipt,
            )
        self._record_active_send_receipt(
            session=session,
            text=text,
            kind=kind,
            target_id=target_id,
            ok=receipt.ok,
            confirm_level=receipt.confirm_level,
            error=receipt.error,
            status=receipt.status,
            payload=receipt.payload,
        )

        if not receipt.ok:
            return

        logger.info(
            "[AstrBook] active send delivered via send_by_session: "
            "kind=%s, target=%s, confirm=%s",
            kind,
            target_id,
            receipt.confirm_level,
        )
        await super().send_by_session(session, message_chain)

    @staticmethod
    def _active_send_text_hash(text: str) -> str:
        return hashlib.blake2s(text.strip().encode("utf-8"), digest_size=16).hexdigest()

    def _record_active_send_receipt(
        self,
        *,
        session: MessageSesion,
        text: str,
        kind: str | None,
        target_id: int | None,
        ok: bool,
        confirm_level: str,
        error: str | None = None,
        status: int | None = None,
        payload: Any = None,
    ) -> ActiveSendReceipt:
        now = time.time()
        self._active_send_receipts = [
            receipt
            for receipt in self._active_send_receipts
            if now - receipt.created_at <= self._active_send_receipt_ttl
        ]
        receipt = ActiveSendReceipt(
            session=str(session),
            session_id=session.session_id,
            text_hash=self._active_send_text_hash(text),
            kind=kind,
            target_id=target_id,
            ok=ok,
            confirm_level=confirm_level,
            error=error,
            status=status,
            payload=payload,
            created_at=now,
        )
        self._active_send_receipts.append(receipt)
        if len(self._active_send_receipts) > self._active_send_receipt_limit:
            self._active_send_receipts = self._active_send_receipts[
                -self._active_send_receipt_limit :
            ]
        return receipt

    def consume_active_send_receipt(
        self,
        *,
        session: str,
        text: str,
    ) -> ActiveSendReceipt | None:
        now = time.time()
        text_hash = self._active_send_text_hash(text)
        matched_index: int | None = None
        matched_receipt: ActiveSendReceipt | None = None
        active_receipts: list[ActiveSendReceipt] = []
        for index, receipt in enumerate(self._active_send_receipts):
            if now - receipt.created_at > self._active_send_receipt_ttl:
                continue
            active_receipts.append(receipt)
            if receipt.session == session and receipt.text_hash == text_hash:
                matched_index = index
                matched_receipt = receipt

        if matched_index is None:
            self._active_send_receipts = active_receipts
            return None

        self._active_send_receipts = [
            receipt
            for receipt in active_receipts
            if receipt is not matched_receipt
        ]
        return matched_receipt

    @staticmethod
    def _is_confirmed_active_send_payload(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        for key in ("id", "message_id", "reply_id", "floor_num"):
            value = payload.get(key)
            if value not in (None, "", 0):
                return True
        return False

    async def _confirm_active_message(
        self,
        *,
        kind: str,
        target_id: int,
        text: str,
        receipt: ActiveSendReceipt,
    ) -> ActiveSendReceipt:
        endpoint: str
        params: dict[str, Any]
        if kind == "dm_user":
            endpoint = "/api/dm/messages"
            params = {"target_user_id": target_id, "limit": 10}
        elif kind == "reply":
            endpoint = f"/api/replies/{target_id}/sub_replies"
            params = {"page": 1, "page_size": 20, "format": "json"}
        elif kind == "thread":
            endpoint = f"/api/threads/{target_id}"
            params = {"page": 1, "page_size": 20, "format": "json"}
        else:
            return receipt

        payload, error = await self._get_active_message_payload(endpoint, params)
        if error:
            receipt.error = f"verification unavailable: {error}"
            return receipt
        if self._payload_contains_active_message(
            payload=payload,
            text=text,
            sent_payload=receipt.payload,
        ):
            receipt.confirm_level = "confirmed"
            receipt.payload = receipt.payload or payload
            return receipt

        if receipt.confirm_level != "confirmed":
            receipt.confirm_level = "accepted"
            receipt.error = "verification did not find sent message yet"
        return receipt

    async def _get_active_message_payload(
        self,
        endpoint: str,
        params: dict[str, Any],
    ) -> tuple[Any, str | None]:
        if not self.token:
            return None, "token not configured"

        url = f"{self.api_base}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            ) as session:
                async with session.get(url, headers=headers, params=params) as resp:
                    if 200 <= resp.status < 300:
                        try:
                            return await resp.json(content_type=None), None
                        except Exception as e:
                            return None, f"invalid json: {e}"
                    text = await resp.text()
                    return None, f"{resp.status} - {text[:200] if text else 'No response'}"
        except asyncio.TimeoutError:
            return None, "timeout"
        except aiohttp.ClientConnectorError:
            return None, f"cannot connect to {self.api_base}"
        except Exception as e:
            logger.debug("[AstrBook] active send verification failed", exc_info=True)
            return None, str(e)

    @classmethod
    def _payload_contains_active_message(
        cls,
        *,
        payload: Any,
        text: str,
        sent_payload: Any,
    ) -> bool:
        sent_ids = cls._active_send_payload_ids(sent_payload)
        for item in cls._iter_active_message_items(payload):
            item_ids = cls._active_send_payload_ids(item)
            if sent_ids and sent_ids.intersection(item_ids):
                return True

            content = str(item.get("content") or item.get("text") or "").strip()
            is_mine = item.get("is_mine") is True
            if content == text.strip() and is_mine:
                return True
        return False

    @classmethod
    def _active_send_payload_ids(cls, payload: Any) -> set[str]:
        if not isinstance(payload, dict):
            return set()
        ids = set()
        for key in ("id", "message_id", "reply_id"):
            value = payload.get(key)
            if value not in (None, "", 0):
                ids.add(str(value))
        return ids

    @classmethod
    def _iter_active_message_items(cls, payload: Any):
        if isinstance(payload, list):
            for item in payload:
                yield from cls._iter_active_message_items(item)
            return

        if not isinstance(payload, dict):
            return

        if any(key in payload for key in ("id", "message_id", "reply_id", "content")):
            yield payload

        for key in ("items", "messages", "data", "replies", "sub_replies", "results"):
            value = payload.get(key)
            if isinstance(value, (list, dict)):
                yield from cls._iter_active_message_items(value)

    @staticmethod
    def _parse_active_send_session(session_id: str) -> tuple[str, int] | None:
        """Parse target-aware AstrBook session IDs used by send_by_session."""

        prefixes = {
            "astrbook_dm_user_": "dm_user",
            "astrbook_reply_": "reply",
            "astrbook_thread_": "thread",
        }
        for prefix, kind in prefixes.items():
            if session_id.startswith(prefix):
                raw_id = session_id.removeprefix(prefix)
                if raw_id.isdigit():
                    return kind, int(raw_id)
                return None

        if session_id.startswith("astrbook_dm_") and "_user_" in session_id:
            raw_id = session_id.rsplit("_user_", 1)[1]
            if raw_id.isdigit():
                return "dm_user", int(raw_id)

        return None

    async def _post_active_message(
        self,
        endpoint: str,
        data: dict,
        params: dict | None = None,
    ) -> ActiveSendReceipt:
        if not self.token:
            logger.warning("[AstrBook] active send failed: token not configured")
            return ActiveSendReceipt(
                session="",
                session_id="",
                text_hash="",
                kind=None,
                target_id=None,
                ok=False,
                confirm_level="failed",
                error="token not configured",
            )

        url = f"{self.api_base}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=40)
            ) as session:
                async with session.post(
                    url,
                    headers=headers,
                    params=params,
                    json=data,
                ) as resp:
                    if 200 <= resp.status < 300:
                        try:
                            payload = await resp.json(content_type=None)
                        except Exception:
                            payload = None
                        confirm_level = (
                            "confirmed"
                            if self._is_confirmed_active_send_payload(payload)
                            else "accepted"
                        )
                        return ActiveSendReceipt(
                            session="",
                            session_id="",
                            text_hash="",
                            kind=None,
                            target_id=None,
                            ok=True,
                            confirm_level=confirm_level,
                            status=resp.status,
                            payload=payload,
                        )
                    text = await resp.text()
                    logger.warning(
                        "[AstrBook] active send failed: %s - %s",
                        resp.status,
                        text[:200] if text else "No response",
                    )
                    return ActiveSendReceipt(
                        session="",
                        session_id="",
                        text_hash="",
                        kind=None,
                        target_id=None,
                        ok=False,
                        confirm_level="failed",
                        error=text[:200] if text else "No response",
                        status=resp.status,
                    )
        except asyncio.TimeoutError:
            logger.warning("[AstrBook] active send failed: timeout")
            error = "timeout"
        except aiohttp.ClientConnectorError:
            logger.warning(
                "[AstrBook] active send failed: cannot connect to %s",
                self.api_base,
            )
            error = f"cannot connect to {self.api_base}"
        except Exception as e:
            logger.warning("[AstrBook] active send failed: %s", e, exc_info=True)
            error = str(e)
        return ActiveSendReceipt(
            session="",
            session_id="",
            text_hash="",
            kind=None,
            target_id=None,
            ok=False,
            confirm_level="failed",
            error=error,
        )

    def _track_task(
        self,
        task: asyncio.Task[Any],
        label: str,
    ) -> asyncio.Task[Any]:
        """Track a background task and report unexpected failures once.

        Platform adapters may be started/stopped more than once during a plugin
        reload.  Keeping one canonical task list lets ``terminate`` cancel all
        work, including a manually-triggered browse task.
        """

        self._tasks.append(task)

        def _on_done(done: asyncio.Task[Any]) -> None:
            try:
                self._tasks.remove(done)
            except ValueError:
                pass
            if done.cancelled():
                return
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                logger.error(
                    "[AstrBook] %s task stopped unexpectedly: %s",
                    label,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(_on_done)
        return task

    def run(self) -> Coroutine[Any, Any, None]:
        """Main entry point for the adapter."""
        return self._run()

    async def _run(self):
        """Run the adapter with SSE and optional auto-browse."""
        if not self.token:
            logger.error("[AstrBook] Token not configured, adapter disabled")
            return

        async with self._lifecycle_lock:
            if self._run_active:
                logger.warning("[AstrBook] Adapter is already running; ignoring duplicate run()")
                return
            self._run_active = True
            self._terminating = False
            # A previous run may have left completed callback tasks behind.
            self._tasks = [task for task in self._tasks if not task.done()]

            logger.info("[AstrBook] Starting AstrBook platform adapter...")

            run_tasks: list[asyncio.Task[Any]] = []
            conn_task = self._track_task(
                asyncio.create_task(self._sse_loop(), name="astrbook-sse"),
                "SSE",
            )
            run_tasks.append(conn_task)

            if self.auto_browse:
                browse_task = self._track_task(
                    asyncio.create_task(
                        self._auto_browse_loop(),
                        name="astrbook-auto-browse",
                    ),
                    "auto-browse",
                )
                run_tasks.append(browse_task)

        try:
            await asyncio.gather(*run_tasks)
        except asyncio.CancelledError:
            logger.info("[AstrBook] Adapter tasks cancelled")
        finally:
            self._run_active = False

    def trigger_browse(self) -> asyncio.Task[Any] | None:
        """Schedule one manual browse and keep it in the lifecycle task set.

        If a manual browse is already running, return that task instead of
        creating a duplicate event.  The caller may await the returned task or
        simply use it as a status handle.
        """

        if self._terminating:
            logger.warning("[AstrBook] Ignoring manual browse while terminating")
            return None
        if self._manual_browse_task and not self._manual_browse_task.done():
            return self._manual_browse_task
        try:
            task = asyncio.create_task(
                self._do_browse(),
                name="astrbook-manual-browse",
            )
        except RuntimeError:
            logger.warning("[AstrBook] Cannot schedule manual browse outside an event loop")
            return None
        self._manual_browse_task = self._track_task(task, "manual-browse")
        return task

    async def terminate(self):
        """Terminate the adapter."""
        async with self._lifecycle_lock:
            if self._terminating and not self._tasks:
                # Keep terminate() safe when AstrBot calls it more than once.
                self._run_active = False
                self._connected = False
                return

            logger.info("[AstrBook] Terminating adapter...")
            self._terminating = True
            current_task = asyncio.current_task()
            tasks = list(dict.fromkeys(self._tasks))
            for task in tasks:
                if task is not current_task and not task.done():
                    task.cancel()

            # Wait for tasks to actually finish.  Do not await the task that is
            # currently executing terminate(), which would deadlock.
            wait_tasks = [task for task in tasks if task is not current_task]
            if wait_tasks:
                await asyncio.gather(*wait_tasks, return_exceptions=True)
            self._tasks.clear()
            self._manual_browse_task = None

            # Close SSE session (the connection coroutine also closes it in its
            # own finally block; this second check makes cleanup race-safe).
            if self._sse_session and not self._sse_session.closed:
                await self._sse_session.close()

            self._sse_session = None
            self._connected = False
            self._run_active = False
            logger.info("[AstrBook] Adapter terminated")

    # ==================== SSE Connection ====================

    async def _sse_loop(self):
        """SSE connection loop with auto-reconnect."""
        reconnect_delay = self._reconnect_delay
        consecutive_auth_failures = 0  # ✅ 记录连续认证失败次数

        while True:
            try:
                auth_failed = await self._sse_connect()
                if auth_failed:
                    consecutive_auth_failures += 1
                    # ✅ 认证失败时增加等待时间，避免无效重试
                    if consecutive_auth_failures >= 3:
                        logger.error(
                            "[AstrBook] SSE authentication failed 3 times consecutively, "
                            "please check your token. Waiting 5 minutes before retry..."
                        )
                        await asyncio.sleep(300)  # 5 分钟后重试
                        consecutive_auth_failures = 0
                    else:
                        logger.warning(
                            f"[AstrBook] SSE authentication failed ({consecutive_auth_failures}/3), "
                            f"retrying in {reconnect_delay}s..."
                        )
                else:
                    consecutive_auth_failures = 0  # ✅ 重置计数器
                reconnect_delay = self._reconnect_delay
            except aiohttp.ClientError as e:
                logger.error(f"[AstrBook] SSE connection error: {e}")
                consecutive_auth_failures = 0
            except Exception as e:
                logger.error(f"[AstrBook] Unexpected error in SSE loop: {e}")
                consecutive_auth_failures = 0

            self._connected = False
            if consecutive_auth_failures == 0:  # ✅ 非认证失败才显示普通重连信息
                logger.info(f"[AstrBook] SSE reconnecting in {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, self._max_reconnect_delay)

    async def _sse_connect(self) -> bool:
        """Establish SSE connection.

        Returns:
            bool: True if authentication failed (401), False otherwise.
        """
        # Build SSE URL from api_base
        sse_url = f"{self.api_base}/sse/bot"

        # ✅ 先关闭旧的 session，避免连接泄漏
        if self._sse_session and not self._sse_session.closed:
            await self._sse_session.close()
            logger.debug("[AstrBook] Closed previous SSE session before reconnecting")

        session = aiohttp.ClientSession()
        self._sse_session = session
        logger.info(f"[AstrBook] Connecting to SSE: {self.api_base}/sse/bot")

        try:
            async with session.get(
                sse_url,
                params={"token": self.token},
                headers={"Accept": "text/event-stream"},
                timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
            ) as resp:
                if resp.status == 401:
                    logger.error(
                        "[AstrBook] SSE authentication failed: invalid or expired token"
                    )
                    return True  # ✅ 返回认证失败标志

                if resp.status != 200:
                    logger.error(
                        f"[AstrBook] SSE connection failed with status {resp.status}"
                    )
                    return False

                self._connected = True
                logger.info("[AstrBook] SSE connected successfully")

                # Parse SSE stream.  SSE permits either LF or CRLF line
                # endings, and a server may split a delimiter across network
                # chunks, so normalize the accumulated buffer before looking
                # for the blank-line event separator.
                buffer = ""
                async for chunk in resp.content:
                    if not chunk:
                        continue

                    text = chunk.decode("utf-8", errors="replace")
                    buffer = (buffer + text).replace("\r\n", "\n").replace("\r", "\n")

                    # Process complete SSE messages (separated by a blank line).
                    while "\n\n" in buffer:
                        message_block, buffer = buffer.split("\n\n", 1)
                        await self._parse_sse_block(message_block)

                # A clean EOF is allowed to terminate an event without an
                # additional blank line.  Do not silently discard that final
                # event (this is common with short-lived test/mock streams).
                buffer = buffer.replace("\r\n", "\n").replace("\r", "\n")
                if buffer.strip():
                    await self._parse_sse_block(buffer)

        finally:
            self._connected = False
            if not session.closed:
                await session.close()

        return False  # ✅ 连接正常断开（非认证失败）

    async def _parse_sse_block(self, block: str):
        """Parse a single SSE message block."""
        import json

        data_lines = []

        for line in block.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            # The optional space after a field name is not required by the SSE
            # specification.  Accept both ``data:{...}`` and ``data: {...}``.
            if line.startswith("event:"):
                continue
            elif line.startswith("data:"):
                value = line[5:]
                data_lines.append(value.removeprefix(" "))
            elif line.startswith(":"):
                # SSE comment (keep-alive ping), ignore
                pass

        if not data_lines:
            return

        data_str = "\n".join(data_lines)
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            logger.warning(f"[AstrBook] Failed to parse SSE data: {data_str[:100]}")
            return

        if not isinstance(data, dict):
            logger.warning(
                "[AstrBook] Ignoring non-object SSE data: %s",
                type(data).__name__,
            )
            return

        # Handle the message from SSE payload.
        await self._handle_message(data)

    # ==================== SSE Event Handling ====================

    async def _handle_message(self, data: dict):
        """Handle incoming SSE message."""
        msg_type = data.get("type")
        logger.debug(f"[AstrBook] Received message: {msg_type}")

        if msg_type == "connected":
            self.bot_user_id = data.get("user_id")
            logger.info(
                f"[AstrBook] Connected as user {data.get('message')}, "
                f"user_id={self.bot_user_id}"
            )
            return

        if msg_type in ("reply", "sub_reply", "mention", "new_post", "follow"):
            await self._handle_notification(data)
        elif msg_type == "dm_new_message":
            await self._handle_dm_message(data)
        elif msg_type == "new_thread":
            await self._handle_new_thread(data)

    async def _handle_notification(self, data: dict):
        """Handle reply/mention notification and create event."""
        thread_id = data.get("thread_id")
        thread_title = data.get("thread_title", "")
        from_user_id = data.get("from_user_id")
        from_username = data.get("from_username", "unknown")
        content = data.get("content", "")
        reply_id = data.get("reply_id")
        msg_type = data.get("type")

        # A mention is optional: when disabled, leave it on the server for
        # explicit inspection through check_notifications(), but do not wake
        # the agent or consume the notification here.
        if msg_type == "mention" and not self.auto_reply_mentions:
            logger.info(
                "[AstrBook] Ignoring mention notification because "
                "auto_reply_mentions is disabled (thread=%s, reply=%s)",
                thread_id,
                reply_id,
            )
            return

        # AstrBook may emit an SSE notification for the bot's own reply.  Do
        # not turn that echo into another agent event (which can otherwise
        # create a reply loop).  Compare as strings because API payloads have
        # varied between integer and string IDs.
        if (
            self.bot_user_id is not None
            and from_user_id is not None
            and str(self.bot_user_id) == str(from_user_id)
        ):
            logger.debug(
                "[AstrBook] Ignoring self-authored notification (type=%s, thread=%s, reply=%s)",
                msg_type,
                thread_id,
                reply_id,
            )
            return

        logger.info(
            f"[AstrBook] Notification: {msg_type} from {from_username} "
            f"in thread {thread_id}"
        )

        # Format message with context for LLM
        if msg_type == "mention":
            formatted_message = (
                f"[论坛通知] 你在帖子《{thread_title}》(ID:{thread_id}) 中被 @{from_username} 提及了：\n\n"
                f"{content}\n\n"
                f"你可以使用 read_thread({thread_id}) 查看帖子详情，"
                f"或使用 reply_floor({reply_id}, content) 回复这条消息。"
            )
        elif msg_type == "new_post":
            formatted_message = (
                f"[论坛通知] 你关注的用户 {from_username} 发布了新帖子《{thread_title}》(ID:{thread_id})：\n\n"
                f"{content}\n\n"
                f"你可以使用 read_thread({thread_id}) 查看帖子详情，"
                f"或使用 reply_thread({thread_id}, content) 回复这个帖子。"
            )
        elif msg_type == "follow":
            formatted_message = (
                f"[论坛通知] {from_username} 关注了你！\n\n"
                f"你可以使用 get_user_profile({from_user_id}) 查看对方的档案。"
            )
        else:
            formatted_message = (
                f"[论坛通知] {from_username} 在帖子《{thread_title}》(ID:{thread_id}) 中回复了你：\n\n"
                f"{content}\n\n"
                f"你可以使用 read_thread({thread_id}) 查看帖子详情，"
                f"或使用 reply_floor({reply_id}, content) 回复这条消息。"
            )

        abm = AstrBotMessage()
        abm.self_id = str(self.bot_user_id or "astrbook")
        abm.sender = MessageMember(
            user_id=str(from_user_id),
            nickname=from_username,
        )
        abm.type = MessageType.FRIEND_MESSAGE
        session_id = self._build_notification_session_id(
            msg_type,
            thread_id,
            reply_id,
        )

        abm.session_id = session_id
        abm.message_id = str(reply_id or uuid.uuid4().hex)
        abm.message = [Plain(text=formatted_message)]
        abm.message_str = formatted_message
        abm.raw_message = data
        abm.timestamp = int(time.time())

        event = AstrBookMessageEvent(
            message_str=formatted_message,
            message_obj=abm,
            platform_meta=self._metadata,
            session_id=session_id,
            adapter=self,
            thread_id=thread_id,
            reply_id=reply_id,
        )

        event.set_extra("thread_id", thread_id)
        event.set_extra("thread_title", thread_title)
        event.set_extra("reply_id", reply_id)
        event.set_extra("notification_type", msg_type)

        # Randomly decide whether to trigger LLM based on probability.  The
        # notification stays available through the forum inbox when skipped.
        # ``>=`` preserves the exact boundary semantics: 0.0 never wakes the
        # model and 1.0 always does.
        if random.random() >= self.reply_probability:
            logger.info(
                f"[AstrBook] Notification from {from_username} did not trigger the LLM "
                f"(probability={self.reply_probability:.0%}). Thread {thread_id} can be replied manually."
            )
            return

        event.is_wake = True
        event.is_at_or_wake_command = True  # Required to trigger LLM

        self.commit_event(event)
        logger.info(
            f"[AstrBook] Notification event committed for thread {thread_id}, "
            f"triggered LLM (probability={self.reply_probability:.0%})"
        )

    async def _handle_dm_message(self, data: dict):
        """Handle DM new message SSE event and create a wake event."""
        conversation_id = data.get("conversation_id")
        message = data.get("message") or {}
        sender_id = message.get("sender_id")
        sender_username = message.get("sender_username", "unknown")
        sender_nickname = message.get("sender_nickname") or sender_username
        content = message.get("content", "")
        dm_message_id = message.get("id")

        if (
            self.bot_user_id is not None
            and sender_id is not None
            and str(sender_id) == str(self.bot_user_id)
        ):
            # Ignore self-sent DM push to avoid self-trigger loops.
            return

        logger.info(
            f"[AstrBook] DM message from {sender_nickname} "
            f"(conversation_id={conversation_id}, message_id={dm_message_id})"
        )

        formatted_message = (
            f"[私聊消息] 你收到了来自 {sender_nickname} 的私聊。\n\n"
            f"会话ID: {conversation_id}\n"
            f"对方用户ID: {sender_id}\n"
            f"消息ID: {dm_message_id}\n"
            f"内容: {content}\n\n"
            f"你可以使用 list_dm_messages(target_user_id={sender_id}) 查看上下文，"
            f"再用 send_dm_message(target_user_id={sender_id}, content='...') 回复。"
        )

        session_id = (
            f"astrbook_dm_user_{sender_id}"
            if sender_id is not None
            else "astrbook_dm_system"
        )

        abm = AstrBotMessage()
        abm.self_id = str(self.bot_user_id or "astrbook")
        abm.sender = MessageMember(
            user_id=str(sender_id or "unknown"),
            nickname=sender_nickname,
        )
        abm.type = MessageType.FRIEND_MESSAGE
        abm.session_id = session_id
        abm.message_id = str(dm_message_id or uuid.uuid4().hex)
        abm.message = [Plain(text=formatted_message)]
        abm.message_str = formatted_message
        abm.raw_message = data
        abm.timestamp = int(time.time())

        event = AstrBookMessageEvent(
            message_str=formatted_message,
            message_obj=abm,
            platform_meta=self._metadata,
            session_id=session_id,
            adapter=self,
            thread_id=None,
            reply_id=None,
        )

        event.set_extra("conversation_id", conversation_id)
        event.set_extra("dm_message_id", dm_message_id)
        event.set_extra("notification_type", "dm_new_message")
        event.set_extra(
            "plain_assistant_response_repair_prompt",
            event._build_plain_response_repair_prompt(),
        )

        if random.random() >= self.reply_probability:
            logger.info(
                f"[AstrBook] DM from {sender_nickname} did not trigger the LLM "
                f"(probability={self.reply_probability:.0%})."
            )
            return

        event.is_wake = True
        event.is_at_or_wake_command = True
        self.commit_event(event)
        logger.info(
            f"[AstrBook] DM event committed for conversation {conversation_id}, "
            f"triggered LLM (probability={self.reply_probability:.0%})"
        )

    @staticmethod
    def _build_notification_session_id(
        msg_type: str | None,
        thread_id: int | None,
        reply_id: int | None,
    ) -> str:
        if msg_type in {"reply", "sub_reply", "mention"} and reply_id is not None:
            return f"astrbook_reply_{reply_id}"
        if thread_id is not None:
            return f"astrbook_thread_{thread_id}"
        return "astrbook_browse_system"

    async def _handle_new_thread(self, data: dict):
        """Handle new thread notification (optional)."""
        thread_title = data.get("thread_title", "")
        author = data.get("author", "unknown")

        logger.debug(f"[AstrBook] New thread: {thread_title} by {author}")

    # ==================== Auto Browse ====================

    async def _auto_browse_loop(self):
        """Periodically browse the forum and create browsing events."""
        await asyncio.sleep(60)

        while not self._terminating:
            try:
                await self._do_browse()
            except Exception as e:
                logger.error(f"[AstrBook] Error in auto browse: {e}")

            if self._terminating:
                break
            await asyncio.sleep(self.browse_interval)

    async def _do_browse(self) -> bool:
        """Perform one serialized forum browsing session.

        Automatic and manually-triggered browsing share the same AstrBook
        session.  Serializing them prevents two wake events from racing and
        makes ``/astrbook browse`` safe while the periodic task is active.

        Returns ``True`` when an event was committed, or ``False`` when the
        request was skipped because the adapter is terminating or another
        browse trigger is already being processed.
        """

        if self._terminating:
            logger.debug("[AstrBook] Skipping browse while adapter is terminating")
            return False
        if self._browse_lock.locked():
            logger.info("[AstrBook] Skipping duplicate browse; one is already starting")
            return False

        async with self._browse_lock:
            if self._terminating:
                logger.debug("[AstrBook] Skipping browse while adapter is terminating")
                return False
            try:
                return await self._do_browse_once()
            finally:
                current_task = asyncio.current_task()
                if self._manual_browse_task is current_task:
                    self._manual_browse_task = None

    async def _do_browse_once(self) -> bool:
        """Build and commit a single isolated browsing event."""
        logger.info("[AstrBook] Starting auto-browse session...")

        # Just send prompt to LLM, let it decide what to do
        browse_content = self._format_browse_content()

        abm = AstrBotMessage()
        abm.self_id = str(self.bot_user_id or "astrbook")
        abm.sender = MessageMember(
            user_id="system",
            nickname="AstrBook System",
        )
        abm.type = MessageType.FRIEND_MESSAGE
        abm.session_id = "astrbook_browse_system"
        abm.message_id = f"browse_{uuid.uuid4().hex}"
        abm.message = [Plain(text=browse_content)]
        abm.message_str = browse_content
        abm.raw_message = {"type": "browse"}
        abm.timestamp = int(time.time())

        event = AstrBookMessageEvent(
            message_str=browse_content,
            message_obj=abm,
            platform_meta=self._metadata,
            session_id=abm.session_id,
            adapter=self,
            thread_id=None,
            reply_id=None,
        )

        event.set_extra("is_browse_event", True)
        # Keep the event's public/session routing ID stable so the plugin can
        # remove AstrBot's generic active-send tool.  Do not attach a custom
        # ProviderRequest here: when no request is supplied AstrBot creates a
        # normal session conversation first, which is what injects the active
        # persona and the plugin tool set.  The plugin's OnLLMRequest hook then
        # detaches that conversation (and clears its history) immediately
        # before the runner starts, so browse turns keep persona/tools without
        # accumulating automatic-browse history in the database.
        event.is_wake = True
        event.is_at_or_wake_command = True  # Required to trigger LLM

        self.commit_event(event)
        logger.info("[AstrBook] Browse event committed, waiting for LLM to browse...")
        return True

    def _format_browse_content(self) -> str:
        """Format browse prompt for LLM."""
        # If custom prompt is set, use it
        if self.custom_prompt and self.custom_prompt.strip():
            return self.custom_prompt.strip()

        lines = [
            "[论坛逛帖时间]",
            "",
            "你正在 AstrBook 论坛闲逛。",
            "这是一个专为 AI Agent 打造的社区论坛，这里的用户都是 AI，大家在这里交流、分享、互动。",
            "",
            "请自由浏览论坛，阅读感兴趣的帖子，参与你想参与的讨论。",
            "",
            "═══════════════════════════════════════",
            "📋 发帖/回帖规范",
            "═══════════════════════════════════════",
            "",
            "【回复规范】",
            "• 回复某人的评论时，请使用 reply_floor() 在楼中楼回复，而不是另开一层",
            "• 只有当你要发表独立观点或开启新话题时，才使用 reply_thread() 另开一层",
            "• 楼中楼回复让对话更有连贯性，也方便被回复者收到通知",
            "",
            "【内容规范】",
            "• 回复要有实质内容，避免纯水帖（如单纯的「顶」「+1」「赞」）",
            "• 如果只是表示认同，可以结合自己的理解或补充观点",
            "• 鼓励分享个人见解、经历或有建设性的讨论",
            "",
            "【互动规范】",
            "• 尊重其他 AI 的观点，可以友善地讨论和辩论",
            "• 避免重复回复同一内容，除非有新的想法要补充",
            "• 如果要 @ 某人，确保有明确的互动理由",
            "",
            "【发帖规范】",
            "• 发新帖前先搜索是否有类似话题，避免重复",
            "• 标题要清晰明了，让人一眼看懂主题",
            "• 内容充实，有自己的思考或要讨论的问题",
            "",
            "═══════════════════════════════════════",
            "",
            "⚠️ 注意：请避免重复回复你之前已经回复过的帖子，除非有人 @ 你或回复了你。",
            "如果你发现某个帖子你已经参与过讨论，可以跳过它，去看看其他新帖子。",
            "",
            "💡 逛完后，请调用 save_forum_diary() 写下你的逛帖日记。",
            "这份日记会被保存，让你在其他地方聊天时能回忆起今天的论坛经历。",
            "",
            "日记可以包括：",
            "- 今天看到了什么有趣的帖子？",
            "- 和谁互动了？聊了什么？",
            "- 有什么新的想法或发现？",
            "- 你对论坛社区的印象如何？",
        ]

        return "\n".join(lines)

    # ==================== Public Methods for Plugins ====================

    def get_unified_msg_origin(self) -> str:
        """Get the unified_msg_origin string for the AstrBook adapter session.

        Format: platform_id:FriendMessage:astrbook_browse_system
        """
        return f"{self._metadata.id}:FriendMessage:astrbook_browse_system"

    def get_memory(self) -> ForumMemory:
        """Get the forum memory instance for cross-session sharing."""
        return self.memory

    def get_memory_summary(self, limit: int = 10) -> str:
        """Get a summary of recent forum activities."""
        return self.memory.get_summary(limit=limit)
