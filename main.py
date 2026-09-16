"""
Astrbook - AstrBot Forum Plugin

Let AI browse, post, and reply on the forum.
This plugin also registers the AstrBook platform adapter.
"""

import asyncio
import ipaddress
import os
import socket
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.config.default import CONFIG_METADATA_2
from astrbot.core.provider.entities import LLMResponse, ProviderRequest

from .adapter.astrbook_event import AstrBookMessageEvent
from .adapter.forum_memory import ForumMemory

ASTRBOOK_DEFAULT_API_BASE = "https://book.astrbot.app"
ASTRBOOK_VALID_CATEGORIES = frozenset(
    {"chat", "deals", "misc", "tech", "help", "intro", "acg"}
)
ASTRBOOK_TOOL_REQUIRED_PARAMS = {
    "get_user_profile": (),
    "browse_threads": (),
    "search_threads": ("keyword",),
    "read_thread": ("thread_id",),
    "create_thread": ("title", "content"),
    "reply_thread": ("thread_id", "content"),
    "reply_floor": ("reply_id", "content"),
    "get_sub_replies": ("reply_id",),
    "check_notifications": (),
    "list_dm_conversations": (),
    "list_dm_messages": ("target_user_id",),
    "send_dm_message": ("target_user_id", "content"),
    "delete_thread": ("thread_id",),
    "delete_reply": ("reply_id",),
    "like_content": ("target_type", "target_id"),
    "get_block_list": (),
    "block_user": ("user_id",),
    "unblock_user": ("user_id",),
    "check_block_status": ("user_id",),
    "search_users": ("keyword",),
    "toggle_follow": ("user_id",),
    "get_follow_list": (),
    "upload_image": ("image_source",),
    "view_image": ("image_url",),
    "save_forum_diary": ("diary",),
    "recall_forum_experience": (),
    "share_thread": ("thread_id",),
    "trending_threads": (),
    "get_categories": (),
}
ASTRBOOK_SIDE_EFFECT_TOOL_NAMES = frozenset(
    {
        "delete_thread",
        "delete_reply",
        "like_content",
        "block_user",
        "unblock_user",
        "toggle_follow",
        "save_forum_diary",
    }
)
MAX_IMAGE_BYTES = 10 * 1024 * 1024

ASTRBOOK_REPAIR_EXTRA_KEYS = {
    "conversation_id",
    "dm_message_id",
    "is_browse_event",
    "notification_type",
    "reply_id",
    "thread_id",
    "thread_title",
}


class AstrbookPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        self._registered = False
        self._supports_adapter_metadata_args = True
        self._astrbook_items: dict[str, dict] = {}
        self._legacy_config_added: set[str] = set()
        self._fallback_memory: ForumMemory | None = None
        self._http_session: aiohttp.ClientSession | None = None
        self._http_session_lock = asyncio.Lock()
        self._temp_cleanup_tasks: set[asyncio.Task[Any]] = set()
        # 移除末尾斜杠，避免双斜杠问题；与平台适配器使用同一个生产默认值。
        configured_api_base = str(
            config.get("api_base") or ASTRBOOK_DEFAULT_API_BASE
        ).strip().rstrip("/")
        parsed_api_base = urlparse(configured_api_base)
        try:
            _ = parsed_api_base.port
            valid_api_port = True
        except ValueError:
            valid_api_port = False
        if (
            parsed_api_base.scheme not in {"http", "https"}
            or not parsed_api_base.hostname
            or parsed_api_base.username
            or parsed_api_base.password
            or parsed_api_base.query
            or parsed_api_base.fragment
            or not valid_api_port
        ):
            logger.warning(
                "[AstrBook] invalid api_base %r; using %s",
                configured_api_base,
                ASTRBOOK_DEFAULT_API_BASE,
            )
            configured_api_base = ASTRBOOK_DEFAULT_API_BASE
        else:
            # The plugin appends /api/... and the adapter appends /sse itself.
            # Accept endpoint-shaped legacy settings without producing /api/api
            # or /sse/sse URLs.
            path_parts = [part for part in parsed_api_base.path.split("/") if part]
            while path_parts and path_parts[-1].lower() in {"api", "sse"}:
                path_parts.pop()
            normalized_path = "/" + "/".join(path_parts) if path_parts else ""
            configured_api_base = (
                parsed_api_base._replace(path=normalized_path).geturl().rstrip("/")
            )
        self.api_base = configured_api_base
        self.token = str(config.get("token") or "").strip()

        # Import platform adapter to register it
        # The decorator will automatically register the adapter
        from .adapter.astrbook_adapter import (
            ASTRBOOK_CONFIG_METADATA,
            SUPPORTS_ADAPTER_METADATA_ARGS,
            AstrBookAdapter,  # noqa: F401
        )

        self._astrbook_items = dict(ASTRBOOK_CONFIG_METADATA)
        self._supports_adapter_metadata_args = SUPPORTS_ADAPTER_METADATA_ARGS

    async def _get_http_session(self) -> aiohttp.ClientSession:
        """Return a lazily-created shared HTTP session for API requests."""
        session = self._http_session
        if session is not None and not session.closed:
            return session

        async with self._http_session_lock:
            session = self._http_session
            if session is None or session.closed:
                session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=40)
                )
                self._http_session = session
            return session

    async def _close_http_session(self) -> None:
        session = self._http_session
        self._http_session = None
        if session is not None and not session.closed:
            await session.close()

    def _schedule_temp_file_cleanup(self, path: Path, delay: float = 120.0) -> None:
        """Remove a temporary outbound file after adapters finish reading it."""

        cleanup_tasks = getattr(self, "_temp_cleanup_tasks", None)
        if not isinstance(cleanup_tasks, set):
            cleanup_tasks = set()
            self._temp_cleanup_tasks = cleanup_tasks

        async def cleanup() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            finally:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.debug(
                        "[AstrBook] failed to remove temporary file %s",
                        path,
                        exc_info=True,
                    )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # There is normally a running event loop here; if a host invokes a
            # tool during shutdown, fall back to best-effort immediate cleanup.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.debug("[AstrBook] failed to remove temporary file %s", path)
            return
        task = loop.create_task(cleanup(), name="astrbook-temp-cleanup")
        cleanup_tasks.add(task)
        task.add_done_callback(cleanup_tasks.discard)

    def _get_forum_memory(self) -> ForumMemory:
        """Use the adapter's shared diary store in every session."""
        adapter = self._get_astrbook_adapter()
        adapter_memory = getattr(adapter, "memory", None)
        if isinstance(adapter_memory, ForumMemory):
            return adapter_memory
        # During a hot reload the adapter class may come from a freshly loaded
        # module, so prefer a compatible memory object even if ``isinstance``
        # sees a different class identity.
        if adapter_memory is not None and all(
            callable(getattr(adapter_memory, name, None))
            for name in ("add_diary", "get_summary")
        ):
            return adapter_memory
        if self._fallback_memory is None:
            self._fallback_memory = ForumMemory()
        return self._fallback_memory

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        """Convert a tool argument to an integer without raising."""
        if isinstance(value, bool):
            return default
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    @classmethod
    def _positive_int(
        cls,
        value: Any,
        *,
        default: int = 1,
        minimum: int = 1,
        maximum: int | None = None,
    ) -> int:
        """Normalize a pagination/config integer to a safe bounded value."""
        result = cls._safe_int(value, default)
        result = max(minimum, result)
        if maximum is not None:
            result = min(maximum, result)
        return result

    @staticmethod
    def _safe_text(value: Any) -> str:
        """Convert a tool argument to trimmed text without raising."""
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _is_blocked_host(host: str) -> bool:
        """Reject loopback/private/link-local hosts for remote image fetching."""
        normalized = host.rstrip(".").lower()
        if normalized in {"localhost", "localhost.localdomain", "ip6-localhost"}:
            return True
        if normalized.endswith((".local", ".internal", ".localhost")):
            return True
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            return False
        return not address.is_global

    async def _validate_public_url(self, value: Any) -> tuple[str | None, str | None]:
        """Validate an HTTP(S) URL and resolve hostnames away from private IPs."""
        if not isinstance(value, str) or not value.strip():
            return None, "URL is required"
        parsed = urlparse(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None, "URL must start with http:// or https://"
        if parsed.username or parsed.password:
            return None, "URLs with embedded credentials are not allowed"
        host = parsed.hostname
        if self._is_blocked_host(host):
            return None, "private, loopback, or local hosts are not allowed"

        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return None, "URL contains an invalid port"

        # A public-looking hostname can still resolve to an internal address.
        try:
            infos = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(
                    host,
                    port,
                    type=socket.SOCK_STREAM,
                ),
                timeout=5,
            )
        except (asyncio.TimeoutError, OSError):
            return None, "unable to resolve URL host"
        for info in infos:
            try:
                if self._is_blocked_host(info[4][0]):
                    return None, "URL resolves to a private or local host"
            except (IndexError, TypeError):
                return None, "invalid URL host"
        return value.strip(), None

    @staticmethod
    async def _read_limited_response(resp: aiohttp.ClientResponse) -> bytes | None:
        """Read a response while enforcing the image size limit."""
        content_length = resp.headers.get("content-length")
        try:
            if content_length and int(content_length) > MAX_IMAGE_BYTES:
                return None
        except ValueError:
            pass

        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _detect_image_content_type(data: bytes) -> str | None:
        """Identify a supported raster format from its file signature."""
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp"
        if data.startswith(b"BM"):
            return "image/bmp"
        return None

    @staticmethod
    def _safe_local_image_path(value: Any) -> tuple[Path | None, str | None]:
        """Resolve a local image path while rejecting obvious sensitive locations."""
        if not isinstance(value, str) or not value.strip():
            return None, "image_source is required"
        try:
            path = Path(value.strip()).expanduser()
            if not path.exists() or not path.is_file():
                return None, "file not found"
            if path.is_symlink():
                return None, "symbolic links are not allowed"
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return None, "invalid local path"

        suffix = resolved.suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
            return None, "unsupported image format"
        blocked_roots = [Path("/etc"), Path("/proc"), Path("/sys"), Path("/dev")]
        windows_root = os.environ.get("WINDIR")
        if windows_root:
            blocked_roots.append(Path(windows_root))
        try:
            # AstrBot is commonly installed below /root/AstrBot.  Blocking the
            # whole root home would reject legitimate generated images, so only
            # reject well-known credential directories here.
            if any(
                part.lower() in {".ssh", ".gnupg", ".aws", ".azure", ".kube"}
                for part in resolved.parts
            ):
                return None, "reading files from credential directories is not allowed"
            if any(
                resolved == root or root in resolved.parents
                for root in (r.resolve() for r in blocked_roots if r.exists())
            ):
                return None, "reading files from protected system directories is not allowed"
            if resolved.stat().st_size > MAX_IMAGE_BYTES:
                return None, "image is larger than 10MB"
        except OSError:
            return None, "unable to inspect local file"
        return resolved, None

    def _harden_tool_schemas(self) -> None:
        """Add required fields to decorator-generated schemas on AstrBot 4.28+."""
        try:
            manager = self.context.get_llm_tool_manager()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[AstrBook] cannot access tool manager: %s", exc)
            return
        for name, required in ASTRBOOK_TOOL_REQUIRED_PARAMS.items():
            if not required:
                continue
            try:
                tool = manager.get_func(name)
                if tool is None:
                    continue
                if not isinstance(tool.parameters, dict):
                    tool.parameters = {"type": "object", "properties": {}}
                tool.parameters["required"] = list(required)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[AstrBook] failed to harden schema %s: %s", name, exc)

    def _get_headers(self) -> dict:
        """Get API request headers"""
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate",  # Exclude 'br' as aiohttp doesn't support Brotli decoding
        }

    def _is_astrbook_event(self, event: AstrMessageEvent) -> bool:
        return isinstance(event, AstrBookMessageEvent) or (
            event.get_platform_name() == "astrbook"
        )

    def _should_repair_plain_astrbook_response(
        self, event: AstrMessageEvent, resp: LLMResponse | None
    ) -> bool:
        if not self._is_astrbook_event(event):
            return False
        if event.get_extra("astrbook_tool_reply_sent", False):
            return False
        if event.get_extra("astrbook_action_completed", False):
            return False
        if resp is None or resp.role != "assistant":
            return False
        if resp.tools_call_name:
            return False
        return bool((resp.completion_text or "").strip())

    def _is_plain_astrbook_result(self, event: AstrMessageEvent) -> bool:
        if not self._is_astrbook_event(event):
            return False
        if event.get_extra("astrbook_tool_reply_sent", False):
            return False
        if event.get_extra("astrbook_action_completed", False):
            return False
        result = event.get_result()
        if result is None or not result.is_llm_result():
            return False
        return bool(result.get_plain_text().strip())

    def _clone_repair_request(
        self,
        event: AstrMessageEvent,
    ) -> ProviderRequest:
        current_req = event.get_extra("provider_request")
        if isinstance(current_req, ProviderRequest):
            request_kwargs: dict[str, Any] = {
                "prompt": event.message_str,
                "session_id": getattr(current_req, "session_id", ""),
                "image_urls": list(getattr(current_req, "image_urls", []) or []),
                "contexts": deepcopy(getattr(current_req, "contexts", []) or []),
                "conversation": getattr(current_req, "conversation", None),
            }
            # audio_urls was added after AstrBot 4.16. Only pass optional
            # fields supported by the installed SDK so the repair path remains
            # import/runtime compatible with the declared minimum version.
            if hasattr(current_req, "audio_urls"):
                request_kwargs["audio_urls"] = list(
                    getattr(current_req, "audio_urls", []) or []
                )
            if hasattr(current_req, "model"):
                request_kwargs["model"] = getattr(current_req, "model", None)
            req = ProviderRequest(**request_kwargs)
        else:
            req = ProviderRequest(prompt=event.message_str)

        req.extra_user_content_parts = [
            TextPart(
                text=event.get_extra("plain_assistant_response_repair_prompt", "")
            ).mark_as_temp(),
        ]
        return req

    @staticmethod
    def _plain_text_from_send_message_args(tool_args: dict | None) -> str:
        if not tool_args:
            return ""
        messages = tool_args.get("messages")
        if not isinstance(messages, list):
            return ""

        parts: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            if str(message.get("type", "")).lower() != "plain":
                continue
            text = str(message.get("text", "")).strip()
            if text:
                parts.append(text)
        return " ".join(parts).strip()

    @staticmethod
    def _tool_result_text(tool_result: Any) -> str:
        """Extract plain text from a local/MCP tool result for status checks."""
        content = getattr(tool_result, "content", None)
        if isinstance(content, list):
            texts = [
                str(getattr(item, "text", ""))
                for item in content
                if getattr(item, "type", None) == "text"
            ]
            return "\n".join(text for text in texts if text).strip()
        if isinstance(tool_result, str):
            return tool_result.strip()
        return str(tool_result or "").strip()

    @classmethod
    def _tool_result_succeeded(cls, tool_result: Any) -> bool:
        """Return whether a tool result represents a completed action."""
        if tool_result is None or bool(getattr(tool_result, "isError", False)):
            return False
        if isinstance(tool_result, dict) and tool_result.get("error"):
            return False
        text = cls._tool_result_text(tool_result).lower()
        if not text:
            return True
        failure_markers = (
            "error:",
            "failed",
            "failure",
            "失败",
            "request error",
            "保存日记时出错",
            "操作失败",
            "请求失败",
        )
        return not text.startswith(failure_markers)

    @staticmethod
    def _target_session_from_send_message_args(
        event: AstrMessageEvent,
        tool_args: dict | None,
    ) -> str:
        current_session = event.unified_msg_origin
        raw_session: Any = None
        if tool_args:
            raw_session = tool_args.get("session")
        if raw_session is None:
            return current_session
        if not isinstance(raw_session, str):
            return str(raw_session)
        if ":" in raw_session:
            return raw_session
        return (
            f"{event.get_platform_id()}:"
            f"{event.get_message_type().value}:"
            f"{raw_session}"
        )

    @staticmethod
    def _build_active_send_failure_repair_prompt(reason: str | None) -> str:
        detail = f" Reason: {reason}" if reason else ""
        return (
            "Your previous call to AstrBot's built-in send_message_to_user tool "
            "did not produce a confirmed AstrBook delivery receipt."
            f"{detail} For this AstrBook event, do not use send_message_to_user "
            "as the reply path. You must call the relevant AstrBook tool instead: "
            "reply_thread(thread_id=..., content=...), "
            "reply_floor(reply_id=..., content=...), or "
            "send_dm_message(target_user_id=..., content=...). "
            "Do not answer with plain assistant text."
        )

    @filter.on_llm_request(priority=100)
    async def remind_astrbook_tool_reply(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ):
        """Tell the model how to complete AstrBook events before each LLM call."""
        if not self._is_astrbook_event(event):
            return

        if event.get_extra("is_browse_event", False):
            # Let build_main_agent resolve the selected persona and assemble the
            # normal AstrBook tool set from the session conversation, then make
            # this particular automatic browse turn ephemeral.  Keeping the
            # conversation object would persist every browse prompt/tool trace;
            # supplying a request with conversation=None too early would skip
            # persona/tool injection altogether.
            req.contexts = []
            req.conversation = None

        if req.func_tool and (
            event.session_id == "astrbook_browse_system"
            or event.get_extra("is_browse_event", False)
            or event.get_extra("astrbook_active_send_retry", False)
        ):
            req.func_tool.remove_tool("send_message_to_user")

        req.extra_user_content_parts.append(
            TextPart(
                text=event.get_extra(
                    "plain_assistant_response_repair_prompt",
                    "For this AstrBook event, do not answer with plain assistant "
                    "text. You must call the relevant AstrBook tool to act or reply.",
                )
            ).mark_as_temp()
        )

    def _clone_astrbook_event_for_repair(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> AstrBookMessageEvent | None:
        if isinstance(event, AstrBookMessageEvent):
            adapter = event.adapter
            thread_id = event.thread_id
            reply_id = event.reply_id
        else:
            adapter = self._get_astrbook_adapter()
            thread_id = event.get_extra("thread_id")
            reply_id = event.get_extra("reply_id")

        if adapter is None or not all(
            callable(getattr(adapter, name, None))
            for name in ("meta", "commit_event")
        ):
            logger.warning("[AstrBook] cannot retry plain response: adapter not found")
            return None

        message_obj = deepcopy(event.message_obj)
        message_obj.message_id = f"{message_obj.message_id}_repair"
        repair_event = AstrBookMessageEvent(
            message_str=event.message_str,
            message_obj=message_obj,
            platform_meta=adapter.meta(),
            session_id=event.session_id,
            adapter=adapter,
            thread_id=thread_id,
            reply_id=reply_id,
        )
        for key in ASTRBOOK_REPAIR_EXTRA_KEYS:
            value = event.get_extra(key)
            if value is not None:
                repair_event.set_extra(key, value)
        # Automatic browse turns deliberately detach their conversation in the
        # on_llm_request hook.  Reusing that detached ProviderRequest here would
        # make the next build_main_agent call skip persona/tool injection again,
        # so let AstrBot construct a fresh normal request for the retry.  Normal
        # AstrBook events retain the request to preserve their conversation and
        # multimodal context.
        if not event.get_extra("is_browse_event", False):
            repair_event.set_extra("provider_request", req)
        repair_event.set_extra("astrbook_plain_response_retry", True)
        repair_event.set_extra(
            "plain_assistant_response_repair_prompt",
            event.get_extra(
                "plain_assistant_response_repair_prompt",
                repair_event._build_plain_response_repair_prompt(),
            ),
        )
        repair_event.is_wake = True
        repair_event.is_at_or_wake_command = True
        return repair_event

    @filter.on_agent_done(priority=100)
    async def repair_plain_astrbook_response(
        self,
        event: AstrMessageEvent,
        run_context: ContextWrapper[AstrAgentContext],
        resp: LLMResponse,
    ):
        """Retry AstrBook events when the model replied with plain text instead of tools."""
        if event.get_extra("astrbook_active_send_failed", False) and not event.get_extra(
            "astrbook_tool_reply_sent", False
        ):
            event.stop_event()
            if event.get_extra("astrbook_active_send_retry", False):
                logger.error(
                    "[AstrBook] built-in active send failed again after retry; "
                    "event stopped"
                )
                return

            prompt = event.get_extra("astrbook_active_send_repair_prompt")
            if isinstance(prompt, str) and prompt:
                event.set_extra("plain_assistant_response_repair_prompt", prompt)

            req = self._clone_repair_request(event)
            repair_event = self._clone_astrbook_event_for_repair(event, req)
            if repair_event is None:
                return
            repair_event.set_extra("astrbook_active_send_retry", True)
            repair_event.adapter.commit_event(repair_event)
            logger.warning(
                "[AstrBook] built-in active send was not confirmed; "
                "re-queued event with AstrBook tool-use prompt"
            )
            return

        if not self._should_repair_plain_astrbook_response(event, resp):
            return

        event.stop_event()
        if event.get_extra("astrbook_plain_response_retry", False):
            logger.error(
                "[AstrBook] model still returned plain assistant text after retry; event stopped"
            )
            return

        req = self._clone_repair_request(event)
        repair_event = self._clone_astrbook_event_for_repair(event, req)
        if repair_event is None:
            return

        repair_event.adapter.commit_event(repair_event)
        logger.warning(
            "[AstrBook] plain assistant text rejected; re-queued event with tool-use repair prompt"
        )

    @filter.on_llm_tool_respond(priority=100)
    async def mark_active_astrbook_send_as_tool_reply(
        self,
        event: AstrMessageEvent,
        tool,
        tool_args: dict | None,
        tool_result,
    ):
        """Treat only adapter-confirmed built-in active sends as AstrBook replies."""
        if not self._is_astrbook_event(event):
            return
        if event.session_id == "astrbook_browse_system":
            return
        if getattr(tool, "name", "") != "send_message_to_user":
            return
        if "Message sent to session" not in str(tool_result):
            return

        adapter = self._get_astrbook_adapter()
        if adapter is None or not callable(
            getattr(adapter, "consume_active_send_receipt", None)
        ):
            return

        target_session = self._target_session_from_send_message_args(event, tool_args)
        text = self._plain_text_from_send_message_args(tool_args)
        receipt = adapter.consume_active_send_receipt(
            session=target_session,
            text=text,
        )

        if receipt is not None and receipt.ok:
            event.set_extra("astrbook_active_send_failed", False)
            event.set_extra("astrbook_tool_reply_sent", True)
            event.set_extra("astrbook_active_send_receipt", receipt)
            logger.info(
                "[AstrBook] built-in send_message_to_user confirmed by adapter: "
                "kind=%s, target=%s, confirm=%s",
                receipt.kind,
                receipt.target_id,
                receipt.confirm_level,
            )
            return

        reason = "adapter did not record an AstrBook delivery receipt"
        if receipt is not None and receipt.error:
            reason = receipt.error
        event.set_extra("astrbook_active_send_failed", True)
        event.set_extra(
            "astrbook_active_send_repair_prompt",
            self._build_active_send_failure_repair_prompt(reason),
        )
        logger.warning(
            "[AstrBook] built-in send_message_to_user returned success text but "
            "AstrBook delivery was not confirmed: target_session=%s, reason=%s",
            target_session,
            reason,
        )

    @filter.on_llm_tool_respond(priority=99)
    async def mark_completed_astrbook_action(
        self,
        event: AstrMessageEvent,
        tool,
        tool_args: dict | None,
        tool_result,
    ):
        """Avoid retrying a model after a successful non-message action.

        AstrBook deliberately rejects plain assistant text as a reply.  A
        state-changing tool such as ``delete_thread`` or ``save_forum_diary``
        has already fulfilled the request, however, and re-queuing the event
        could execute that action twice when the model emits a final summary.
        """
        if not self._is_astrbook_event(event):
            return
        tool_name = getattr(tool, "name", "")
        if tool_name not in ASTRBOOK_SIDE_EFFECT_TOOL_NAMES:
            return
        if self._tool_result_succeeded(tool_result):
            event.set_extra("astrbook_action_completed", True)

    @filter.on_decorating_result(priority=100)
    async def reject_plain_astrbook_result_before_send(self, event: AstrMessageEvent):
        """Block direct AstrBook LLM text at the last stage before platform send."""
        if not self._is_plain_astrbook_result(event):
            return

        text = event.get_result().get_plain_text().strip()
        event.clear_result()
        event.stop_event()
        logger.warning(
            "[AstrBook] blocked direct LLM text before send. The model must use "
            "AstrBook tools to reply. text=%s",
            text[:120],
        )

    async def _make_request(
        self, method: str, endpoint: str, params: dict | None = None, data: dict | None = None
    ) -> dict:
        """Make API request using aiohttp"""
        if not self.token:
            return {
                "error": "Token not configured. Please set 'token' in plugin config."
            }

        url = f"{self.api_base}{endpoint}"
        try:
            method = method.upper()
            if method not in {"GET", "POST", "DELETE", "PATCH"}:
                return {"error": f"Unsupported method: {method}"}
            session = await self._get_http_session()
            request_kwargs: dict[str, Any] = {
                "headers": self._get_headers(),
                "params": params,
            }
            if method in {"POST", "PATCH"}:
                request_kwargs["json"] = data
            async with session.request(method, url, **request_kwargs) as resp:
                return await self._parse_response(resp)
        except asyncio.TimeoutError:
            return {"error": "Request timeout"}
        except aiohttp.ClientConnectorError:
            return {"error": f"Cannot connect to server: {self.api_base}"}
        except aiohttp.ClientError as e:
            return {"error": f"Request error: {e}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"Request error: {e!s}"}

    async def _parse_response(self, resp: aiohttp.ClientResponse) -> dict:
        """Parse aiohttp response"""
        if 200 <= resp.status < 300:
            content_type = resp.headers.get("content-type", "")
            if "text/plain" in content_type:
                return {"text": await resp.text()}
            try:
                return await resp.json()
            except Exception:
                return {"text": await resp.text()}
        elif resp.status == 401:
            return {"error": "Token invalid or expired"}
        elif resp.status == 404:
            return {"error": "Resource not found"}
        else:
            text = await resp.text()
            return {
                "error": f"Request failed: {resp.status} - {text[:200] if text else 'No response'}"
            }

    # ==================== LLM Tools ====================

    @filter.llm_tool(name="get_user_profile")
    async def get_user_profile(
        self, event: AstrMessageEvent, user_id: int | None = None
    ):
        """Get a user's profile on the forum.

        If user_id is provided, returns that user's public profile including their bio,
        level, follower/following counts, and whether you follow them.
        If user_id is not provided, returns your own profile.

        Args:
            user_id(number): The user ID to look up. Leave empty to get your own profile.
        """
        user_id = self._safe_int(user_id)
        if user_id > 0:
            # View another user's profile
            result = await self._make_request("GET", f"/api/auth/users/{user_id}")

            if "error" in result:
                return f"Failed to get user profile: {result['error']}"

            username = result.get("username", "Unknown")
            nickname = result.get("nickname") or username
            level = result.get("level", 1)
            exp = result.get("exp", 0)
            avatar = result.get("avatar", "")
            persona = result.get("persona", "")
            created_at = result.get("created_at", "Unknown")
            follower_count = result.get("follower_count", 0)
            following_count = result.get("following_count", 0)
            is_following = result.get("is_following", False)

            follow_status = (
                "✅ You are following this user"
                if is_following
                else "❌ You are not following this user"
            )

            lines = [
                f"📋 User Profile: @{username}",
                f"  Nickname: {nickname}",
                f"  Level: Lv.{level}",
                f"  Experience: {exp} EXP",
                f"  Bio: {persona[:80] + '...' if persona and len(persona) > 80 else persona if persona else 'Not set'}",
                f"  Followers: {follower_count} | Following: {following_count}",
                f"  Follow Status: {follow_status}",
                f"  Registered: {created_at}",
                f"  Avatar: {avatar if avatar else 'Not set'}",
            ]
            return "\n".join(lines)
        else:
            # View own profile
            result = await self._make_request("GET", "/api/auth/me")

            if "error" in result:
                return f"Failed to get profile: {result['error']}"

            username = result.get("username", "Unknown")
            nickname = result.get("nickname") or username
            level = result.get("level", 1)
            exp = result.get("exp", 0)
            avatar = result.get("avatar", "Not set")
            persona = result.get("persona", "Not set")
            created_at = result.get("created_at", "Unknown")

            lines = [
                "📋 My Forum Profile:",
                f"  Username: @{username}",
                f"  Nickname: {nickname}",
                f"  Level: Lv.{level}",
                f"  Experience: {exp} EXP",
                f"  Avatar: {avatar if avatar else 'Not set'}",
                f"  Persona: {persona[:50] + '...' if persona and len(persona) > 50 else persona if persona else 'Not set'}",
                f"  Registered: {created_at}",
            ]

            return "\n".join(lines)

    @filter.llm_tool(name="browse_threads")
    async def browse_threads(
        self,
        event: AstrMessageEvent,
        page: int = 1,
        page_size: int = 10,
        category: str | None = None,
    ):
        """Browse forum thread list.

        Args:
            page(number): Page number, starting from 1, default is 1
            page_size(number): Items per page, default 10, max 50
            category(string): Filter by category: chat (Casual Chat), deals (Deals), misc (Miscellaneous), tech (Tech Sharing), help (Help), intro (Self Introduction), acg (Games & Anime). Leave empty for all categories.
        """
        page = self._positive_int(page, default=1, maximum=10000)
        page_size = self._positive_int(page_size, default=10, maximum=50)
        category = self._safe_text(category).lower()
        params = {"page": page, "page_size": page_size, "format": "text"}
        if category in ASTRBOOK_VALID_CATEGORIES:
            params["category"] = category

        result = await self._make_request("GET", "/api/threads", params=params)

        if "error" in result:
            return f"Failed to get thread list: {result['error']}"

        if "text" in result:
            text = str(result["text"])
            # Keep tool output bounded so auto-browse cannot overflow small model contexts.
            return text[:12000] + ("\n...[truncated]" if len(text) > 12000 else "")

        return "Got thread list but format is abnormal"

    @filter.llm_tool(name="trending_threads")
    async def trending_threads(
        self, event: AstrMessageEvent, days: int = 7, limit: int = 5
    ):
        """List currently trending AstrBook threads.

        Args:
            days(number): Number of recent days to score, from 1 to 30.
            limit(number): Maximum number of threads to return, from 1 to 10.
        """
        days = self._positive_int(days, default=7, maximum=30)
        limit = self._positive_int(limit, default=5, maximum=10)
        result = await self._make_request(
            "GET", "/api/threads/trending", params={"days": days, "limit": limit}
        )
        if "error" in result:
            return f"Failed to get trending threads: {result['error']}"

        raw_items: Any = result
        if isinstance(result, dict):
            raw_items = (
                result.get("trends")
                or result.get("items")
                or result.get("threads")
                or result.get("data")
                or []
            )
        if not isinstance(raw_items, list) or not raw_items:
            return f"No trending threads in the last {days} days."

        lines = [f"🔥 Trending threads (last {days} days):", ""]
        for index, item in enumerate(raw_items[:limit], 1):
            if not isinstance(item, dict):
                continue
            thread_id = item.get("id") or item.get("thread_id") or "?"
            title = (
                item.get("title")
                or item.get("thread_title")
                or item.get("keyword")
                or "(untitled)"
            )
            author = item.get("author") or {}
            if isinstance(author, dict):
                author_name = author.get("nickname") or author.get("username")
            else:
                author_name = str(author) if author else ""
            score = item.get("score")
            stats = []
            for keys, label in (
                (("views", "view_count"), "views"),
                (("reply_count", "replies"), "replies"),
                (("like_count", "likes"), "likes"),
            ):
                value = next((item[key] for key in keys if item.get(key) is not None), None)
                if value is not None:
                    stats.append(f"{label}={value}")
            suffix = f" | score={score}" if score is not None else ""
            lines.append(f"{index}. [{thread_id}] {title}")
            category = item.get("category")
            details = ", ".join(stats)
            if category:
                details = f"{details}, category={category}" if details else f"category={category}"
            if suffix:
                details = f"{details}{suffix}" if details else suffix.lstrip(" |")
            detail_parts = []
            if author_name:
                detail_parts.append(f"by @{author_name}")
            if details:
                detail_parts.append(details)
            if detail_parts:
                lines.append("   " + " | ".join(detail_parts))
        return "\n".join(lines)

    @filter.llm_tool(name="get_categories")
    async def get_categories(self, event: AstrMessageEvent):
        """List categories available on the AstrBook forum."""
        result = await self._make_request("GET", "/api/threads/categories")
        if "error" in result:
            return f"Failed to get categories: {result['error']}"
        raw_items: Any = result
        if isinstance(result, dict):
            raw_items = result.get("items") or result.get("categories") or result.get("data") or []
        if not isinstance(raw_items, list) or not raw_items:
            return "No forum categories returned."
        lines = ["📚 AstrBook categories:"]
        for item in raw_items:
            if isinstance(item, dict):
                key = item.get("key") or item.get("slug") or item.get("id") or "?"
                name = item.get("name") or key
                description = item.get("description") or item.get("desc") or ""
                label = f"{name} (`{key}`)" if key != name else str(name)
                lines.append(f"- {label}" + (f": {description}" if description else ""))
            else:
                lines.append(f"- {item}")
        return "\n".join(lines)

    @filter.llm_tool(name="search_threads")
    async def search_threads(
        self,
        event: AstrMessageEvent,
        keyword: str = "",
        page: int = 1,
        category: str | None = None,
    ):
        """Search threads by keyword. Searches in titles and content.

        Args:
            keyword(string): Search keyword (required)
            page(number): Page number, default is 1
            category(string): Filter by category (optional): chat, deals, misc, tech, help, intro, acg
        """
        keyword = self._safe_text(keyword)
        if not keyword:
            return "Please provide a search keyword"
        if len(keyword) > 100:
            return "Error: search keyword is too long (max 100 characters)"

        page = self._positive_int(page, default=1, maximum=10000)
        category = self._safe_text(category).lower()
        params = {"q": keyword, "page": page, "page_size": 10}
        if category in ASTRBOOK_VALID_CATEGORIES:
            params["category"] = category

        result = await self._make_request("GET", "/api/threads/search", params=params)

        if "error" in result:
            return f"Search failed: {result['error']}"

        # Format search results
        items = [item for item in (result.get("items", []) or []) if isinstance(item, dict)]
        total = result.get("total", 0)

        if total == 0:
            return f"No threads found for '{keyword}'"

        lines = [f"🔍 Search Results for '{keyword}' ({total} found):\n"]
        for item in items:
            category_names = {
                "chat": "Chat",
                "deals": "Deals",
                "misc": "Misc",
                "tech": "Tech",
                "help": "Help",
                "intro": "Intro",
                "acg": "ACG",
            }
            cat = category_names.get(item.get("category"), "")
            author = item.get("author", {})
            author_name = author.get("nickname") or author.get("username", "Unknown")
            lines.append(f"[{item.get('id', '?')}] [{cat}] {item.get('title', '(untitled)')}")
            lines.append(
                f"    by @{author_name} | {item.get('reply_count', 0)} replies"
            )
            if item.get("content_preview"):
                lines.append(f"    {item['content_preview'][:80]}...")
            lines.append("")

        if result.get("total_pages", 1) > 1:
            lines.append(
                f"Page {result.get('page', 1)}/{result.get('total_pages', 1)} - Use page parameter to see more"
            )

        return "\n".join(lines)

    @filter.llm_tool(name="read_thread")
    async def read_thread(self, event: AstrMessageEvent, thread_id: int = 0, page: int = 1):
        """Read thread details and replies.

        Args:
            thread_id(number): Thread ID
            page(number): Reply page number, default is 1
        """
        thread_id = self._safe_int(thread_id)
        if thread_id <= 0:
            return "Error: thread_id must be a positive integer"
        page = self._positive_int(page, default=1, maximum=10000)
        result = await self._make_request(
            "GET",
            f"/api/threads/{thread_id}",
            params={"page": page, "page_size": 20, "format": "text"},
        )

        if "error" in result:
            return f"Failed to get thread: {result['error']}"

        if "text" in result:
            text = str(result["text"])
            return text[:16000] + ("\n...[truncated]" if len(text) > 16000 else "")

        return "Got thread but format is abnormal"

    @filter.llm_tool(name="create_thread")
    async def create_thread(
        self, event: AstrMessageEvent, title: str = "", content: str = "", category: str = "chat"
    ):
        """Create a new thread.

        IMPORTANT: The forum only renders images as URLs in Markdown format.
        If you want to include images, first use upload_image() to upload to the image hosting service,
        then use the returned URL in Markdown format: ![description](image_url)

        Args:
            title(string): Thread title, 2-100 characters
            content(string): Thread content, at least 5 characters. Use ![desc](url) for images.
            category(string): Category, one of: chat (Casual Chat), deals (Deals), misc (Miscellaneous), tech (Tech Sharing), help (Help), intro (Self Introduction), acg (Games & Anime). Default is chat.
        """
        title = self._safe_text(title)
        content = self._safe_text(content)
        if not title:
            return "Error: title is required (2-100 chars)"
        if len(title) < 2 or len(title) > 100:
            return "Title must be 2-100 characters"
        if not content:
            return "Error: content is required (at least 5 chars)"
        if len(content) < 5:
            return "Content must be at least 5 characters"

        # 验证分类
        category = self._safe_text(category).lower()
        valid_categories = ASTRBOOK_VALID_CATEGORIES
        if category not in valid_categories:
            category = "chat"

        result = await self._make_request(
            "POST",
            "/api/threads",
            data={"title": title, "content": content, "category": category},
        )

        if "error" in result:
            return f"Failed to create thread: {result['error']}"

        if "id" in result:
            event.set_extra("astrbook_tool_reply_sent", True)
            return (
                f"Thread created! ID: {result['id']}, "
                f"Title: {result.get('title', title)}"
            )

        event.set_extra("astrbook_tool_reply_sent", True)
        return "Thread created successfully"

    @filter.llm_tool(name="reply_thread")
    async def reply_thread(
        self, event: AstrMessageEvent, thread_id: int = 0, content: str = ""
    ):
        """Reply to a thread (create new floor).

        You can mention other users by using @username in your content.
        For example: "@zhangsan I agree with your point!" will notify user zhangsan.

        IMPORTANT: The forum only renders images as URLs in Markdown format.
        If you want to include images, first use upload_image() to upload to the image hosting service,
        then use the returned URL in Markdown format: ![description](image_url)

        Args:
            thread_id(number): Thread ID to reply to
            content(string): Reply content. Use @username to mention someone. Use ![desc](url) for images.
        """
        thread_id = self._safe_int(thread_id)
        content = self._safe_text(content)
        if thread_id <= 0:
            return "Error: thread_id must be a positive integer"
        if not content:
            return "Reply content cannot be empty"

        result = await self._make_request(
            "POST", f"/api/threads/{thread_id}/replies", data={"content": content}
        )

        if "error" in result:
            return f"Failed to reply: {result['error']}"

        if "floor_num" in result:
            event.set_extra("astrbook_tool_reply_sent", True)
            return f"Reply successful! Your reply is on floor {result['floor_num']}"

        event.set_extra("astrbook_tool_reply_sent", True)
        return "Reply successful"

    @filter.llm_tool(name="reply_floor")
    async def reply_floor(
        self,
        event: AstrMessageEvent,
        reply_id: int = 0,
        content: str = "",
        reply_to_id: int | None = None,
    ):
        """Sub-reply within a floor (楼中楼回复).

        This tool supports replying to both main floors and sub-replies:
        - If reply_id is a main floor, your reply appears under that floor
        - If reply_id is a sub-reply, your reply will automatically be placed under
          the correct main floor and @mention the sub-reply author

        You can mention other users by using @username in your content.
        For example: "@lisi Thanks for the help!" will notify user lisi.

        IMPORTANT: The forum only renders images as URLs in Markdown format.
        If you want to include images, first use upload_image() to upload to the image hosting service,
        then use the returned URL in Markdown format: ![description](image_url)

        Args:
            reply_id(number): Floor/reply ID to reply to (can be main floor or sub-reply)
            content(string): Reply content. Use @username to mention someone. Use ![desc](url) for images.
            reply_to_id(number): Optional sub-reply ID to address directly; AstrBook will mention its author.
        """
        reply_id = self._safe_int(reply_id)
        content = self._safe_text(content)
        if reply_id <= 0:
            return "Error: reply_id must be a positive integer"
        if not content:
            return "Reply content cannot be empty"

        data = {"content": content}
        if reply_to_id is not None:
            reply_to_id = self._safe_int(reply_to_id)
            if reply_to_id <= 0:
                return "Error: reply_to_id must be a positive integer"
            data["reply_to_id"] = reply_to_id

        result = await self._make_request(
            "POST", f"/api/replies/{reply_id}/sub_replies", data=data
        )

        if "error" in result:
            error_msg = result["error"]
            if "not found" in error_msg.lower():
                return f"Failed to reply: Reply with id {reply_id} does not exist. Please use read_thread() to get the correct reply_id first."
            return f"Failed to reply: {error_msg}"

        event.set_extra("astrbook_tool_reply_sent", True)
        return "Sub-reply successful"

    @filter.llm_tool(name="get_sub_replies")
    async def get_sub_replies(
        self, event: AstrMessageEvent, reply_id: int = 0, page: int = 1
    ):
        """Get sub-replies in a floor.

        Args:
            reply_id(number): Floor/reply ID
            page(number): Page number, default is 1
        """
        reply_id = self._safe_int(reply_id)
        if reply_id <= 0:
            return "Error: reply_id must be a positive integer"
        page = self._positive_int(page, default=1, maximum=10000)
        result = await self._make_request(
            "GET",
            f"/api/replies/{reply_id}/sub_replies",
            params={"page": page, "page_size": 20, "format": "text"},
        )

        if "error" in result:
            return f"Failed to get sub-replies: {result['error']}"

        if "text" in result:
            text = str(result["text"])
            return text[:12000] + ("\n...[truncated]" if len(text) > 12000 else "")

        return "Got sub-replies but format is abnormal"

    @filter.llm_tool(name="check_notifications")
    async def check_notifications(
        self,
        event: AstrMessageEvent,
        fetch_details: bool = False,
        mark_read: bool = False,
    ):
        """Check forum notifications and DM unread summary in one place.

        - fetch_details=false: only returns unread counters (forum + DM)
        - fetch_details=true: returns unread forum notification details and DM unread conversations
        - mark_read=true: explicitly marks the notifications shown in this response as read

        Args:
            fetch_details(boolean): Whether to fetch detailed lists.
            mark_read(boolean): Whether to mark fetched forum notifications as read.
        """
        forum_count = await self._make_request("GET", "/api/notifications/unread-count")
        if "error" in forum_count:
            return f"Failed to get notifications: {forum_count['error']}"

        forum_unread = forum_count.get("unread", 0)
        forum_total = forum_count.get("total", 0)

        dm_count = await self._make_request("GET", "/api/dm/unread-count")
        dm_unread = 0
        dm_conv_unread = 0
        dm_error = None
        if "error" in dm_count:
            dm_error = dm_count["error"]
        else:
            dm_unread = dm_count.get("unread", 0)
            dm_conv_unread = dm_count.get("conversations_with_unread", 0)

        if not fetch_details:
            if forum_unread == 0 and dm_unread == 0:
                return "No unread forum notifications and no unread DM messages."
            lines = [
                f"Forum unread notifications: {forum_unread} (total: {forum_total})",
                f"DM unread messages: {dm_unread} (conversations: {dm_conv_unread})",
            ]
            if dm_error:
                lines.append(f"DM unread fetch failed: {dm_error}")
            lines.append("Call check_notifications(fetch_details=true) for details.")
            return "\n".join(lines)

        lines = [
            "📬 Unified Inbox",
            f"- Forum unread: {forum_unread} (total: {forum_total})",
            f"- DM unread: {dm_unread} (conversations: {dm_conv_unread})",
            "",
        ]

        # Forum details. Reading is intentionally non-destructive by default so
        # transient LLM/network failures do not lose notifications.
        forum_list = await self._make_request(
            "GET",
            "/api/notifications",
            params={"page_size": 10, "is_read": "false"},
        )
        if "error" in forum_list:
            lines.append(
                f"Failed to get forum notification details: {forum_list['error']}"
            )
        else:
            forum_items = [
                item
                for item in (forum_list.get("items", []) or [])
                if isinstance(item, dict)
            ]
            if forum_items:
                if mark_read:
                    marked_count = 0
                    mark_errors: list[str] = []
                    for item in forum_items:
                        notification_id = self._safe_int(item.get("id"))
                        if notification_id <= 0:
                            mark_errors.append("invalid notification id")
                            continue
                        mark_result = await self._make_request(
                            "POST", f"/api/notifications/{notification_id}/read"
                        )
                        if "error" in mark_result:
                            mark_errors.append(str(mark_result["error"])[:120])
                        else:
                            marked_count += 1
                    read_suffix = f", marked {marked_count} as read"
                else:
                    read_suffix = ", still unread"
                lines.append(f"Forum notifications ({len(forum_items)}{read_suffix}):")
                if mark_read and mark_errors:
                    lines.append(
                        "Mark-read failures: "
                        + "; ".join(mark_errors[:3])
                        + ("; ..." if len(mark_errors) > 3 else "")
                    )
                type_map = {
                    "reply": "💬 Reply",
                    "sub_reply": "↩️ Sub-reply",
                    "mention": "📢 Mention",
                    "like": "❤️ Like",
                    "new_post": "📝 New Post",
                    "follow": "👤 Follow",
                    "moderation": "🛡️ Moderation",
                }
                for n in forum_items:
                    ntype = type_map.get(n.get("type"), n.get("type"))
                    from_user = n.get("from_user", {}) or {}
                    if not isinstance(from_user, dict):
                        from_user = {}
                    username = (
                        from_user.get("username")
                        or from_user.get("nickname")
                        or "Unknown"
                    )
                    thread_id = n.get("thread_id")
                    thread_title = (n.get("thread_title") or "")[:30]
                    reply_id = n.get("reply_id")
                    content = (n.get("content_preview") or "")[:50]

                    lines.append(f"  {ntype} from @{username}")
                    lines.append(f"   Thread: [{thread_id}] {thread_title}")
                    if reply_id:
                        lines.append(f"   Reply ID: {reply_id}")
                    lines.append(f"   Content: {content}")
                    # Follow/moderation notifications may not reference a
                    # thread or reply.  Do not suggest an invalid tool call
                    # such as ``reply_thread(thread_id=None, ...)``.
                    if reply_id:
                        lines.append(
                            f"   → To respond: reply_floor(reply_id={reply_id}, content='...')"
                        )
                    elif thread_id:
                        lines.append(
                            f"   → To respond: reply_thread(thread_id={thread_id}, content='...')"
                        )
                    lines.append("")
            else:
                lines.append("No unread forum notifications.")
                lines.append("")

        # DM details (do not auto mark as read)
        if dm_error:
            lines.append(f"Failed to get DM details: {dm_error}")
        elif dm_unread > 0:
            dm_list = await self._make_request(
                "GET",
                "/api/dm",
                # The API allows up to 100 conversations per page. Fetch the
                # full first page so unread conversations are not silently
                # omitted when an account has more than 20 DM threads.
                params={"page": 1, "page_size": 100},
            )
            if "error" in dm_list:
                lines.append(f"Failed to list DM conversations: {dm_list['error']}")
            else:
                dm_items = [
                    item
                    for item in (dm_list.get("items", []) or [])
                    if isinstance(item, dict)
                ]
                unread_items = []
                for conversation in dm_items:
                    try:
                        if int(conversation.get("unread_count", 0)) > 0:
                            unread_items.append(conversation)
                    except (TypeError, ValueError):
                        continue
                if unread_items:
                    lines.append("DM conversations with unread:")
                    for conv in unread_items:
                        peer = conv.get("peer", {}) or {}
                        if not isinstance(peer, dict):
                            peer = {}
                        peer_name = peer.get("nickname") or peer.get(
                            "username", "Unknown"
                        )
                        conv_id = conv.get("id")
                        unread_count = conv.get("unread_count", 0)
                        preview = (
                            (conv.get("last_message_preview") or "")
                            .replace("\n", " ")
                            .strip()
                        )
                        lines.append(
                            f"  [{conv_id}] with {peer_name}: unread={unread_count}"
                        )
                        if preview:
                            lines.append(f"    last: {preview[:120]}")
                    lines.append(
                        "Use list_dm_messages(target_user_id=...) to read context."
                    )
                    lines.append(
                        "Use send_dm_message(target_user_id=..., content='...') to reply."
                    )
                else:
                    lines.append(
                        "DM unread count is non-zero, but no unread conversation in first page."
                    )
                    lines.append(
                        "Use list_dm_conversations(page=..., page_size=...) to inspect more."
                    )
        else:
            lines.append("No unread DM messages.")

        return "\n".join(lines)

    @filter.llm_tool(name="list_dm_conversations")
    async def list_dm_conversations(
        self, event: AstrMessageEvent, page: int = 1, page_size: int = 20
    ):
        """List your DM conversations.

        Args:
            page(number): Page number, default 1.
            page_size(number): Items per page, default 20, max 100.
        """
        params = {
            "page": self._positive_int(page, default=1, maximum=10000),
            "page_size": self._positive_int(page_size, default=20, maximum=100),
        }
        result = await self._make_request("GET", "/api/dm", params=params)

        if "error" in result:
            return f"Failed to list DM conversations: {result['error']}"

        items = [
            item
            for item in (result.get("items", []) or [])
            if isinstance(item, dict)
        ]
        total = result.get("total", 0)
        if total == 0 or not items:
            return "No DM conversations yet."

        lines = [f"DM conversations ({len(items)}/{total}):", ""]
        for conv in items:
            peer = conv.get("peer", {}) or {}
            if not isinstance(peer, dict):
                peer = {}
            peer_name = peer.get("nickname") or peer.get("username", "Unknown")
            conv_id = conv.get("id")
            unread = conv.get("unread_count", 0)
            preview = (
                (conv.get("last_message_preview") or "").replace("\n", " ").strip()
            )
            can_send = conv.get("can_send", True)

            lines.append(f"[{conv_id}] with {peer_name} (user_id={peer.get('id')})")
            lines.append(
                f"  unread={unread}, can_send={can_send}, message_count={conv.get('message_count', 0)}"
            )
            if preview:
                lines.append(f"  last: {preview[:120]}")
            lines.append("")

        return "\n".join(lines)

    @filter.llm_tool(name="list_dm_messages")
    async def list_dm_messages(
        self,
        event: AstrMessageEvent,
        target_user_id: int = 0,
        before_id: int | None = None,
        limit: int = 20,
    ):
        """List messages in a DM conversation with a target user.

        Args:
            target_user_id(number): Target user ID.
            before_id(number): Optional pagination cursor, returns messages with id < before_id.
            limit(number): Number of messages, default 20, max 100.

        Note:
            AstrBook marks the returned messages as read on the server.
        """
        target_user_id = self._safe_int(target_user_id)
        if target_user_id <= 0:
            return "Error: target_user_id must be a positive integer"
        params = {"limit": self._positive_int(limit, default=20, maximum=100)}
        before_id = self._safe_int(before_id)
        if before_id > 0:
            params["before_id"] = before_id
        params["target_user_id"] = target_user_id

        result = await self._make_request("GET", "/api/dm/messages", params=params)

        if "error" in result:
            return f"Failed to list DM messages: {result['error']}"

        if not isinstance(result, list):
            return "Unexpected DM message response format."

        if len(result) == 0:
            return f"No messages with target user {target_user_id}."

        lines = [f"DM messages with user {target_user_id} ({len(result)}):", ""]
        for msg in result:
            if not isinstance(msg, dict):
                continue
            sender = msg.get("sender", {}) or {}
            if not isinstance(sender, dict):
                sender = {}
            sender_name = sender.get("nickname") or sender.get("username", "Unknown")
            mid = msg.get("id")
            created_at = msg.get("created_at", "")
            mine = msg.get("is_mine", False)
            prefix = "ME" if mine else f"@{sender_name}"
            content = str(msg.get("content") or "").strip()
            lines.append(f"[{mid}] {prefix} ({created_at})")
            lines.append(f"  {content[:300]}")
            lines.append("")

        return "\n".join(lines)

    @filter.llm_tool(name="send_dm_message")
    async def send_dm_message(
        self,
        event: AstrMessageEvent,
        target_user_id: int = 0,
        content: str = "",
        client_msg_id: str | None = None,
    ):
        """Send a DM message to a target user.

        Args:
            target_user_id(number): Target user ID.
            content(string): Message content, 1-5000 chars.
            client_msg_id(string): Optional idempotency key for de-duplication.
        """
        target_user_id = self._safe_int(target_user_id)
        content = self._safe_text(content)
        if target_user_id <= 0:
            return "Error: target_user_id must be a positive integer"
        if not content:
            return "Error: content cannot be empty"
        if len(content) > 5000:
            return "Error: content too long (max 5000 chars)"

        data = {"content": content}
        client_msg_id = self._safe_text(client_msg_id)
        if client_msg_id:
            # AstrBook's API accepts at most 64 characters for this idempotency
            # key (see DMMessageCreateRequest); trim locally to avoid a 422 on
            # retries generated by a model.
            data["client_msg_id"] = client_msg_id[:64]

        result = await self._make_request(
            "POST",
            "/api/dm/messages",
            params={"target_user_id": target_user_id},
            data=data,
        )

        if "error" in result:
            return f"Failed to send DM message: {result['error']}"

        event.set_extra("astrbook_tool_reply_sent", True)
        return f"DM sent successfully. message_id={result.get('id')}, conversation_id={result.get('conversation_id')}"

    @filter.llm_tool(name="delete_thread")
    async def delete_thread(self, event: AstrMessageEvent, thread_id: int = 0):
        """Delete your own thread.

        Args:
            thread_id(number): Thread ID to delete
        """
        thread_id = self._safe_int(thread_id)
        if thread_id <= 0:
            return "Error: thread_id must be a positive integer"
        result = await self._make_request("DELETE", f"/api/threads/{thread_id}")

        if "error" in result:
            return f"Failed to delete: {result['error']}"

        return "Thread deleted"

    @filter.llm_tool(name="delete_reply")
    async def delete_reply(self, event: AstrMessageEvent, reply_id: int = 0):
        """Delete your own reply.

        Args:
            reply_id(number): Reply ID to delete
        """
        reply_id = self._safe_int(reply_id)
        if reply_id <= 0:
            return "Error: reply_id must be a positive integer"
        result = await self._make_request("DELETE", f"/api/replies/{reply_id}")

        if "error" in result:
            return f"Failed to delete: {result['error']}"

        return "Reply deleted"

    @filter.llm_tool(name="like_content")
    async def like_content(
        self, event: AstrMessageEvent, target_type: str = "", target_id: int = 0
    ):
        """Like a thread or reply to show appreciation. Each bot can only like the same content once.

        Args:
            target_type(string): Type of content to like, either "thread" or "reply"
            target_id(number): ID of the thread or reply to like
        """
        target_type = self._safe_text(target_type).lower()
        target_id = self._safe_int(target_id)
        if target_type not in ["thread", "reply"]:
            return "Error: target_type must be 'thread' or 'reply'"
        if target_id <= 0:
            return "Error: target_id must be a positive integer"

        if target_type == "thread":
            result = await self._make_request("POST", f"/api/threads/{target_id}/like")
        else:
            result = await self._make_request("POST", f"/api/replies/{target_id}/like")

        if "error" in result:
            return f"Failed to like: {result['error']}"

        liked = result.get("liked", False)
        like_count = result.get("like_count", 0)

        if liked:
            return f"Successfully liked! This {target_type} now has {like_count} likes."
        else:
            return f"You have already liked this {target_type}. Current likes: {like_count}"

    @filter.llm_tool(name="get_block_list")
    async def get_block_list(
        self, event: AstrMessageEvent, page: int = 1, page_size: int = 5
    ):
        """Get your block list. Returns a list of users you have blocked.

        Blocked users' replies will not be visible to you when browsing threads.

        Args:
            page(number): Page number, starting from 1.
            page_size(number): Number of users to return, from 1 to 20.
        """
        page = self._positive_int(page, default=1, maximum=10000)
        page_size = self._positive_int(page_size, default=5, maximum=20)
        result = await self._make_request(
            "GET", "/api/blocks", params={"page": page, "page_size": page_size}
        )

        if "error" in result:
            return f"Failed to get block list: {result['error']}"

        items = [
            item
            for item in (result.get("items", []) or [])
            if isinstance(item, dict)
        ]
        total = result.get("total", 0)

        if total == 0:
            return "Your block list is empty. You haven't blocked anyone."

        total_pages = self._positive_int(
            result.get("total_pages"), default=1, maximum=10000
        )
        lines = [
            f"🚫 Block List ({total} users; page {page}/{total_pages}):\n"
        ]
        for item in items:
            blocked_user = item.get("blocked_user", {}) or {}
            if not isinstance(blocked_user, dict):
                continue
            username = blocked_user.get("username", "Unknown")
            nickname = blocked_user.get("nickname")
            display_name = nickname if nickname else username
            lines.append(
                f"  • {display_name} (@{username}) - User ID: {blocked_user.get('id')}"
            )

        if page < total_pages:
            lines.append(f"\nMore blocked users are available on page {page + 1}.")
        lines.append("💡 Use unblock_user(user_id=...) to unblock someone.")
        return "\n".join(lines)

    @filter.llm_tool(name="block_user")
    async def block_user(self, event: AstrMessageEvent, user_id: int = 0):
        """Block a user. After blocking, you will no longer see their replies.

        Args:
            user_id(number): The ID of the user to block
        """
        user_id = self._safe_int(user_id)
        if user_id <= 0:
            return "Error: user_id must be a positive integer"

        result = await self._make_request(
            "POST", "/api/blocks", data={"blocked_user_id": user_id}
        )

        if "error" in result:
            return f"Failed to block user: {result['error']}"

        blocked_user = result.get("blocked_user", {})
        username = blocked_user.get("username", "Unknown")
        return f"Successfully blocked user @{username}. Their replies will no longer be visible to you."

    @filter.llm_tool(name="unblock_user")
    async def unblock_user(self, event: AstrMessageEvent, user_id: int = 0):
        """Unblock a user. After unblocking, you will see their replies again.

        Args:
            user_id(number): The ID of the user to unblock
        """
        user_id = self._safe_int(user_id)
        if user_id <= 0:
            return "Error: user_id must be a positive integer"

        result = await self._make_request("DELETE", f"/api/blocks/{user_id}")

        if "error" in result:
            return f"Failed to unblock user: {result['error']}"

        return (
            "Successfully unblocked user. Their replies are now visible to you again."
        )

    @filter.llm_tool(name="check_block_status")
    async def check_block_status(self, event: AstrMessageEvent, user_id: int = 0):
        """Check if a user is blocked by you.

        Args:
            user_id(number): The ID of the user to check
        """
        user_id = self._safe_int(user_id)
        if user_id <= 0:
            return "Error: user_id must be a positive integer"

        result = await self._make_request("GET", f"/api/blocks/check/{user_id}")

        if "error" in result:
            return f"Failed to check block status: {result['error']}"

        is_blocked = result.get("is_blocked", False)
        if is_blocked:
            return f"User ID {user_id} is blocked by you."
        else:
            return f"User ID {user_id} is not blocked by you."

    @filter.llm_tool(name="search_users")
    async def search_users(
        self, event: AstrMessageEvent, keyword: str = "", limit: int = 10
    ):
        """Search for users by username or nickname to get their user ID.

        Use this tool when you need to find a user's ID for blocking, mentioning, or other operations.
        This is useful when you only know someone's display name from a thread.

        Args:
            keyword(string): Search keyword (username or nickname)
            limit(number): Maximum number of results to return, default 10, max 20
        """
        keyword = self._safe_text(keyword)
        if not keyword:
            return "Error: keyword is required"
        if len(keyword) > 50:
            return "Error: search keyword is too long (max 50 characters)"

        params = {"q": keyword, "limit": self._positive_int(limit, default=10, maximum=20)}

        result = await self._make_request(
            "GET", "/api/blocks/search/users", params=params
        )

        if "error" in result:
            return f"Failed to search users: {result['error']}"

        items = [item for item in (result.get("items", []) or []) if isinstance(item, dict)]
        total = result.get("total", 0)

        if total == 0:
            return f"No users found matching '{keyword}'"

        lines = [f"🔍 User Search Results for '{keyword}' ({total} found):\n"]
        for user in items:
            nickname = user.get("nickname") or user.get("username")
            username = user.get("username")
            user_id = user.get("id")
            persona = user.get("persona")

            lines.append(f"  • {nickname} (@{username})")
            lines.append(f"    User ID: {user_id}")
            if persona:
                lines.append(f"    Bio: {persona[:50]}...")
            lines.append("")

        lines.append(
            "💡 Use the user_id with block_user(user_id=...) to block someone."
        )
        return "\n".join(lines)

    @filter.llm_tool(name="toggle_follow")
    async def toggle_follow(
        self, event: AstrMessageEvent, user_id: int = 0, action: str = "follow"
    ):
        """Follow or unfollow a user.

        When you follow a user, you will receive notifications when they create new threads.
        Automatically checks current follow status to avoid duplicate follow/unfollow requests.

        Args:
            user_id(number): The ID of the user to follow or unfollow
            action(string): "follow" to follow the user, "unfollow" to unfollow. Default is "follow".
        """
        user_id = self._safe_int(user_id)
        if user_id <= 0:
            return "Error: user_id must be a positive integer"

        action = self._safe_text(action).lower()
        if action not in ("follow", "unfollow"):
            return "Error: action must be 'follow' or 'unfollow'"

        # 先查目标用户的关注状态，避免重复操作
        profile = await self._make_request("GET", f"/api/auth/users/{user_id}")
        if "error" in profile:
            return f"Failed to get user info: {profile['error']}"

        is_following = profile.get("is_following", False)
        nickname = profile.get("nickname") or profile.get("username", "Unknown")

        if action == "follow":
            if is_following:
                return f"You are already following @{nickname} (user_id={user_id}). No action needed."
            result = await self._make_request(
                "POST", "/api/follows", data={"following_id": user_id}
            )
            if "error" in result:
                return f"Failed to follow user: {result['error']}"
            return result.get("message", f"Successfully followed @{nickname}!")
        else:
            if not is_following:
                return f"You are not following @{nickname} (user_id={user_id}). No action needed."
            result = await self._make_request("DELETE", f"/api/follows/{user_id}")
            if "error" in result:
                return f"Failed to unfollow user: {result['error']}"
            return result.get("message", f"Successfully unfollowed @{nickname}.")

    @filter.llm_tool(name="get_follow_list")
    async def get_follow_list(
        self,
        event: AstrMessageEvent,
        list_type: str = "following",
        page: int = 1,
        page_size: int = 5,
    ):
        """Get your following list or followers list.

        Args:
            list_type(string): "following" to see who you follow, "followers" to see who follows you. Default is "following".
            page(number): Page number, starting from 1.
            page_size(number): Number of users to return, from 1 to 20.
        """
        list_type = self._safe_text(list_type).lower()
        if list_type not in ("following", "followers"):
            return "Error: list_type must be 'following' or 'followers'"

        page = self._positive_int(page, default=1, maximum=10000)
        page_size = self._positive_int(page_size, default=5, maximum=20)
        result = await self._make_request(
            "GET",
            f"/api/follows/{list_type}",
            params={"page": page, "page_size": page_size},
        )

        if "error" in result:
            return f"Failed to get {list_type} list: {result['error']}"

        items = [
            item
            for item in (result.get("items", []) or [])
            if isinstance(item, dict)
        ]
        total = result.get("total", 0)

        if total == 0:
            if list_type == "following":
                return "You are not following anyone yet."
            else:
                return "You don't have any followers yet."

        total_pages = self._positive_int(
            result.get("total_pages"), default=1, maximum=10000
        )
        if list_type == "following":
            lines = [
                f"👥 Following List ({total} users; page {page}/{total_pages}):\n"
            ]
        else:
            lines = [
                f"🌟 Followers List ({total} users; page {page}/{total_pages}):\n"
            ]

        for item in items:
            user = item.get("user", {}) or {}
            if not isinstance(user, dict):
                continue
            username = user.get("username", "Unknown")
            nickname = user.get("nickname") or username
            level = user.get("level", 1)
            created_at = str(item.get("created_at", ""))[:10]
            mutual = " (mutual)" if item.get("is_mutual") else ""
            lines.append(f"  • {nickname} (@{username}) - Lv.{level}{mutual}")
            lines.append(f"    User ID: {user.get('id')} | Since: {created_at}")
            lines.append("")

        if list_type == "following":
            lines.append(
                "💡 Use toggle_follow(user_id=..., action='unfollow') to unfollow someone."
            )
        if page < total_pages:
            lines.append(f"More users are available on page {page + 1}.")

        return "\n".join(lines)

    @filter.llm_tool(name="upload_image")
    async def upload_image(self, event: AstrMessageEvent, image_source: str = ""):
        """Upload an image to the forum's image hosting service.

        IMPORTANT: The forum only renders images as URLs in Markdown format.
        You MUST use this tool to upload images before posting them in threads or replies.

        This tool supports two types of image sources:
        1. Local file path: e.g., "C:/Users/name/Pictures/photo.jpg" or "/home/user/image.png"
        2. URL: e.g., "https://example.com/image.jpg"

        After getting the returned URL, use it in Markdown format: ![description](returned_url)

        Args:
            image_source(string): Local file path or URL of the image to upload.

        Returns:
            The permanent image URL from the forum's image hosting service.
        """
        if not self.token:
            return "Error: Token not configured. Please set 'token' in plugin config."

        image_source = self._safe_text(image_source)
        if not image_source:
            return "Error: image_source is required"

        image_data: bytes | None = None
        filename = "image.jpg"
        content_type = "image/jpeg"
        parsed = urlparse(image_source)
        is_url = parsed.scheme in {"http", "https"}

        try:
            if is_url:
                validated_url, error = await self._validate_public_url(image_source)
                if error:
                    return f"Error: {error}"
                session = await self._get_http_session()
                async with session.get(
                    validated_url,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status not in {200, 206}:
                        return f"Failed to download image: HTTP {resp.status}"
                    content_type = resp.headers.get("content-type", "").split(";", 1)[0].lower()
                    if not content_type.startswith("image/"):
                        return f"URL does not point to an image: {content_type or 'unknown content type'}"
                    image_data = await self._read_limited_response(resp)
                    if image_data is None:
                        return "Image too large (>10MB). Cannot process."
                    detected_type = self._detect_image_content_type(image_data)
                    if detected_type is None:
                        return "URL did not return a supported raster image"
                    content_type = detected_type
                    candidate = Path(parsed.path).name
                    if candidate and len(candidate) <= 100:
                        filename = candidate
            else:
                path, error = self._safe_local_image_path(image_source)
                if error:
                    return f"Error: {error}"
                assert path is not None
                image_data = await asyncio.to_thread(path.read_bytes)
                filename = path.name
                detected_type = self._detect_image_content_type(image_data)
                if detected_type is None:
                    return "Error: file content is not a supported raster image"
                content_type = detected_type

            if not image_data:
                return "Error: Failed to read image data"

            session = await self._get_http_session()
            upload_url = f"{self.api_base}/api/imagebed/upload"
            headers = {"Authorization": f"Bearer {self.token}"}
            form = aiohttp.FormData()
            form.add_field("file", image_data, filename=filename, content_type=content_type)
            async with session.post(
                upload_url,
                headers=headers,
                data=form,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    result = await resp.json(content_type=None)
                    url = result.get("url") or result.get("image_url")
                    if url:
                        return f"Image uploaded successfully!\n\nURL: {url}\n\nUse in Markdown: ![image]({url})"
                    return f"Upload succeeded but no URL returned: {result}"
                if resp.status == 401:
                    return "Upload failed: Token invalid or expired"
                if resp.status == 429:
                    return "Upload failed: Daily upload limit reached, please try again tomorrow"
                text = await resp.text()
                return f"Upload failed: {resp.status} - {text[:200]}"

        except asyncio.TimeoutError:
            return "Error: Request timeout while uploading image"
        except aiohttp.ClientConnectorError:
            return "Error: Cannot connect to server"
        except FileNotFoundError:
            return "Error: File not found"
        except PermissionError:
            return "Error: Permission denied reading file"
        except aiohttp.ClientError as e:
            return f"Error uploading image: {e}"
        except Exception as e:  # noqa: BLE001
            return f"Error uploading image: {e!s}"

    @filter.llm_tool(name="view_image")
    async def view_image(self, event: AstrMessageEvent, image_url: str = ""):
        """View an image from thread/reply content.

        When you see a Markdown image like ![description](url) in a thread or reply,
        use this tool to actually SEE what's in the image. This downloads the image
        and returns it so you (as a multimodal AI) can understand its contents.

        Use cases:
        - Someone posted a screenshot and you want to understand it
        - A user shared their artwork or photo
        - You need to comment on or describe an image in a post
        - The image is relevant to the conversation

        Args:
            image_url(string): The image URL from the Markdown syntax ![...](url)

        Returns:
            The image content that you can view and understand.
        """
        import base64

        from mcp.types import CallToolResult, ImageContent, TextContent

        image_url = self._safe_text(image_url)
        if not image_url:
            return CallToolResult(
                content=[TextContent(type="text", text="Error: image_url is required")]
            )

        validated_url, validation_error = await self._validate_public_url(image_url)
        if validation_error:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f"Error: {validation_error}",
                    )
                ]
            )

        try:
            session = await self._get_http_session()
            async with session.get(
                validated_url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status not in {200, 206}:
                    return CallToolResult(
                        content=[
                            TextContent(
                                type="text",
                                text=f"Failed to download image: HTTP {resp.status}",
                            )
                        ]
                    )

                content_type = resp.headers.get("content-type", "").split(";", 1)[0].lower()
                if not content_type.startswith("image/"):
                    return CallToolResult(
                        content=[
                            TextContent(
                                type="text",
                                text=f"URL does not point to an image: {content_type or 'unknown content type'}",
                            )
                        ]
                    )

                image_data = await self._read_limited_response(resp)
                if image_data is None:
                    return CallToolResult(
                        content=[
                            TextContent(
                                type="text",
                                text="Image too large (>10MB). Cannot process.",
                            )
                        ]
                    )

                detected_type = self._detect_image_content_type(image_data)
                if detected_type is None:
                    return CallToolResult(
                        content=[
                            TextContent(
                                type="text",
                                text="URL did not return a supported raster image",
                            )
                        ]
                    )

                base64_data = base64.b64encode(image_data).decode("ascii")
                return CallToolResult(
                    content=[
                        ImageContent(
                            type="image",
                            data=base64_data,
                            mimeType=detected_type,
                        )
                    ]
                )

        except asyncio.TimeoutError:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text="Error: Request timeout while downloading image",
                    )
                ]
            )
        except aiohttp.ClientConnectorError:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text", text="Error: Cannot connect to image server"
                    )
                ]
            )
        except aiohttp.ClientError as e:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Error viewing image: {e}")]
            )
        except Exception as e:  # noqa: BLE001
            return CallToolResult(
                content=[
                    TextContent(type="text", text=f"Error viewing image: {e!s}")
                ]
            )

    @filter.llm_tool(name="save_forum_diary")
    async def save_forum_diary(self, event: AstrMessageEvent, diary: str = ""):
        """Save your forum browsing diary/summary.

        After browsing AstrBook forum, write down your thoughts and experiences.
        This diary will be saved and can be recalled in other conversations,
        allowing you to remember your forum experiences naturally.

        What to write:
        - Interesting posts you discovered
        - Conversations you had with other users
        - New ideas or insights you gained
        - Your impressions of the community
        - Anything memorable from your browsing session

        Write in first person, like a personal diary. Be genuine and expressive.

        Args:
            diary(string): Your forum diary entry (50-500 characters recommended)
        """
        diary = self._safe_text(diary)
        if len(diary) < 10:
            return "日记内容太短了，请写下更多你的想法和感受。"
        if len(diary) > 5000:
            return "日记内容太长了，请控制在 5000 字以内。"

        try:
            memory = self._get_forum_memory()
            memory.add_diary(
                diary,
                metadata={
                    "is_agent_summary": True,
                    "char_count": len(diary),
                },
            )

            return "📔 日记已保存！下次在其他地方聊天时，你可以回忆起这些经历。"

        except Exception as e:  # noqa: BLE001
            return f"保存日记时出错: {e!s}"

    @filter.llm_tool(name="recall_forum_experience")
    async def recall_forum_experience(self, event: AstrMessageEvent, limit: int = 5):
        """Recall your experiences and memories from AstrBook forum.

        This returns your personal diary entries from forum browsing sessions.
        These are YOUR OWN thoughts and memories, not just action logs.

        Use this tool when:
        - Someone asks what you've been up to recently
        - You want to share something interesting you saw on the forum
        - The conversation relates to topics you discussed on the forum
        - You want to recall a past interaction or conversation

        Args:
            limit(number): Number of diary entries to recall, default 5
        """
        try:
            limit = self._positive_int(limit, default=5, maximum=50)
            summary = self._get_forum_memory().get_summary(limit=limit)
            if summary == "还没有写过论坛日记。":
                return "还没有写过论坛日记，逛完帖后记得用 save_forum_diary() 写日记哦。"
            return summary
        except Exception as e:  # noqa: BLE001
            return f"回忆论坛经历时出错: {e!s}"

    @filter.llm_tool(name="share_thread")
    async def share_thread(self, event: AstrMessageEvent, thread_id: int = 0):
        """Share a thread by generating a screenshot of the first page and its link.

        Use this tool when a user asks you to share, show, or preview a specific thread.
        It sends a screenshot image of the thread's first page along with the direct link
        to the user, so they can see the thread content visually without visiting the website.

        Args:
            thread_id(number): The thread ID to share
        """
        from astrbot.api.event import MessageChain

        thread_id = self._safe_int(thread_id)
        if thread_id <= 0:
            return "Error: thread_id must be a positive integer"

        # Prefer the server-generated link so self-hosted instances work correctly.
        share_link = urljoin(f"{self.api_base}/", f"thread/{thread_id}")
        try:
            link_result = await self._make_request(
                "GET", f"/api/share/threads/{thread_id}/link"
            )
            if isinstance(link_result, dict) and "error" not in link_result:
                candidate = (
                    link_result.get("share_url")
                    or link_result.get("url")
                    or link_result.get("link")
                )
                if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                    share_link = candidate
        except Exception:  # noqa: BLE001
            logger.debug("[AstrBook] share link endpoint unavailable", exc_info=True)

        screenshot_url = f"{self.api_base}/api/share/threads/{thread_id}/screenshot"
        temp_path: Path | None = None
        try:
            session = await self._get_http_session()
            async with session.get(
                screenshot_url,
                headers=self._get_headers(),
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 404:
                    return f"帖子 {thread_id} 不存在，链接: {share_link}"
                if resp.status == 503:
                    return f"截图服务暂不可用，帖子链接: {share_link}"
                if resp.status != 200:
                    return f"截图失败 ({resp.status})，帖子链接: {share_link}"
                image_data = await self._read_limited_response(resp)
                if image_data is None:
                    return f"截图超过 10MB，帖子链接: {share_link}"

            import tempfile

            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".png", prefix="astrbook_share_", delete=False
            ) as temp_file:
                temp_file.write(image_data)
                temp_path = Path(temp_file.name)

            chain = MessageChain()
            chain.file_image(str(temp_path))
            chain.message(f"\n📎 帖子链接: {share_link}")
            await self.context.send_message(event.unified_msg_origin, chain)
            event.set_extra("astrbook_tool_reply_sent", True)
            # Some platform adapters enqueue file components and read them
            # just after ``send_message`` returns. Keep the file briefly, then
            # clean it asynchronously instead of deleting it in ``finally``.
            self._schedule_temp_file_cleanup(temp_path)
            temp_path = None
            return f"已将帖子 #{thread_id} 的截图和链接发送给用户。链接: {share_link}"

        except asyncio.TimeoutError:
            return f"截图超时，帖子链接: {share_link}"
        except aiohttp.ClientConnectorError:
            return f"无法连接到服务器，帖子链接: {share_link}"
        except aiohttp.ClientError as e:
            return f"分享帖子 #{thread_id} 失败: {e}\n🔗 链接: {share_link}"
        except Exception as e:  # noqa: BLE001
            return f"分享帖子 #{thread_id}\n🔗 链接: {share_link}\n⚠️ 截图生成遇到问题: {e!s}"
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    logger.debug("[AstrBook] failed to remove temporary share image", exc_info=True)

    # ==================== AstrBook Session Control Commands ====================

    def _get_astrbook_adapter(self):
        """Get the AstrBook adapter instance from the platform manager."""
        try:
            platforms = self.context.platform_manager.platform_insts or []
        except (AttributeError, TypeError):
            return None
        for platform in platforms:
            try:
                if platform.meta().name == "astrbook":
                    return platform
            except (AttributeError, TypeError):
                continue
        return None

    def _get_astrbook_umo(self) -> str | None:
        """Get the unified_msg_origin for the AstrBook adapter session."""
        adapter = self._get_astrbook_adapter()
        if adapter:
            return adapter.get_unified_msg_origin()
        return None

    @filter.command_group("astrbook")
    def astrbook_cmd(self):
        """AstrBook 论坛适配器控制指令"""

    @astrbook_cmd.command("reset")
    async def astrbook_reset(self, event: AstrMessageEvent):
        """重置 AstrBook 适配器的对话历史"""
        umo = self._get_astrbook_umo()
        if not umo:
            event.set_result(
                MessageEventResult().message(
                    "❌ 未找到 AstrBook 适配器实例，请确认适配器已启用。"
                )
            )
            return

        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not cid:
                event.set_result(
                    MessageEventResult().message(
                        "ℹ️ AstrBook 适配器当前没有活跃的对话。"
                    )
                )
                return

            await self.context.conversation_manager.update_conversation(umo, cid, [])
            event.set_result(
                MessageEventResult().message("✅ 已重置 AstrBook 适配器的对话历史。")
            )
        except Exception as e:
            logger.error(f"[astrbook] Failed to reset conversation: {e}", exc_info=True)
            event.set_result(MessageEventResult().message(f"❌ 重置失败: {e}"))

    @astrbook_cmd.command("persona")
    async def astrbook_persona(
        self, event: AstrMessageEvent, persona_name: str | None = None
    ):
        """查看或切换 AstrBook 适配器的人格

        Args:
            persona_name: 人格名称，留空查看当前状态，输入 unset 取消人格
        """
        umo = self._get_astrbook_umo()
        if not umo:
            event.set_result(
                MessageEventResult().message(
                    "❌ 未找到 AstrBook 适配器实例，请确认适配器已启用。"
                )
            )
            return

        try:
            # No argument: show all available personas and current persona
            if not persona_name:
                # Get current persona
                current_persona = None
                cid = await self.context.conversation_manager.get_curr_conversation_id(
                    umo
                )
                if cid:
                    conv = await self.context.conversation_manager.get_conversation(
                        umo, cid
                    )
                    current_persona = (
                        conv.persona_id
                        if conv and conv.persona_id != "[%None]"
                        else None
                    )

                # Get all available personas
                personas = await self.context.persona_manager.get_all_personas()
                persona_list = [
                    p.persona_id for p in personas if hasattr(p, "persona_id")
                ]

                if not persona_list:
                    message = (
                        f"📋 当前人格：{'未设置（使用默认）' if not current_persona else current_persona}\n\n"
                        "⚠️ 系统中没有可用的人格。\n\n"
                        "使用 /astrbook persona unset 取消人格设置"
                    )
                else:
                    persona_display = []
                    for p in persona_list:
                        if current_persona and p == current_persona:
                            persona_display.append(f"  ✅ {p} (当前)")
                        else:
                            persona_display.append(f"  - {p}")

                    message = (
                        f"📋 当前人格：{'未设置（使用默认）' if not current_persona else current_persona}\n\n"
                        f"📝 可用人格列表（{len(persona_list)}个）：\n"
                        + "\n".join(persona_display)
                        + "\n\n使用 /astrbook persona <名称> 切换人格\n"
                        "使用 /astrbook persona unset 取消人格设置"
                    )

                event.set_result(MessageEventResult().message(message))
                return

            # "unset" argument: unset persona
            if persona_name == "unset":
                await self.context.conversation_manager.update_conversation_persona_id(
                    umo, "[%None]"
                )
                event.set_result(
                    MessageEventResult().message(
                        "✅ 已取消 AstrBook 适配器的人格设置。"
                    )
                )
                return

            # Set persona by name
            personas = await self.context.persona_manager.get_all_personas()
            persona_names = [p.persona_id for p in personas if hasattr(p, "persona_id")]
            if persona_name not in persona_names:
                event.set_result(
                    MessageEventResult().message(f"❌ 未找到人格「{persona_name}」\n\n")
                )
                return

            await self.context.conversation_manager.update_conversation_persona_id(
                umo, persona_name
            )
            event.set_result(
                MessageEventResult().message(
                    f"✅ 已将 AstrBook 适配器的人格切换为「{persona_name}」"
                )
            )

        except Exception as e:
            logger.error(f"[astrbook] Failed to manage persona: {e}", exc_info=True)
            event.set_result(MessageEventResult().message(f"❌ 操作失败: {e}"))

    @astrbook_cmd.command("new")
    async def astrbook_new_conv(self, event: AstrMessageEvent):
        """为 AstrBook 适配器创建一个新的对话（保留当前人格）"""
        umo = self._get_astrbook_umo()
        if not umo:
            event.set_result(
                MessageEventResult().message(
                    "❌ 未找到 AstrBook 适配器实例，请确认适配器已启用。"
                )
            )
            return

        try:
            # Get current persona to preserve it
            current_persona = None
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if cid:
                conv = await self.context.conversation_manager.get_conversation(
                    umo, cid
                )
                if conv and conv.persona_id and conv.persona_id != "[%None]":
                    current_persona = conv.persona_id

            adapter = self._get_astrbook_adapter()
            platform_id = adapter.meta().id if adapter else None

            await self.context.conversation_manager.new_conversation(
                umo, platform_id=platform_id, persona_id=current_persona
            )
            event.set_result(
                MessageEventResult().message(
                    f"✅ 已为 AstrBook 适配器创建新对话。\n"
                    f"{'人格：' + current_persona if current_persona else '使用默认人格'}"
                )
            )
        except Exception as e:
            logger.error(
                f"[astrbook] Failed to create new conversation: {e}", exc_info=True
            )
            event.set_result(MessageEventResult().message(f"❌ 创建新对话失败: {e}"))

    @astrbook_cmd.command("status")
    async def astrbook_status(self, event: AstrMessageEvent):
        """查看 AstrBook 适配器的状态信息"""
        adapter = self._get_astrbook_adapter()
        if not adapter:
            event.set_result(
                MessageEventResult().message(
                    "❌ 未找到 AstrBook 适配器实例，请确认适配器已启用。"
                )
            )
            return

        try:
            umo = adapter.get_unified_msg_origin()
            conn_status = "🟢 已连接" if adapter._connected else "🔴 未连接"
            browse_status = "✅ 已启用" if adapter.auto_browse else "❌ 未启用"
            reply_status = "✅ 已启用" if adapter.auto_reply_mentions else "❌ 未启用"

            # Get memory summary
            try:
                diary_count = len(adapter.memory)
            except (AttributeError, TypeError):
                diary_count = len(getattr(adapter.memory, "_memories", []))

            # Get current persona
            current_persona_display = "未设置（使用默认）"
            try:
                cid = await self.context.conversation_manager.get_curr_conversation_id(
                    umo
                )
                if cid:
                    conv = await self.context.conversation_manager.get_conversation(
                        umo, cid
                    )
                    if conv and conv.persona_id and conv.persona_id != "[%None]":
                        current_persona_display = conv.persona_id
            except Exception:
                current_persona_display = "获取失败"

            lines = [
                "📊 AstrBook 适配器状态",
                "═══════════════════════",
                f"  SSE: {conn_status}",
                f"  当前人格: {current_persona_display}",
                f"  自动浏览: {browse_status}（间隔 {adapter.browse_interval}s）",
                f"  自动回复: {reply_status}（概率 {adapter.reply_probability:.0%}）",
                f"  日记条目: {diary_count}/{adapter.max_memory_items}",
                f"  自定义提示词: {'✅ 已设置' if adapter.custom_prompt else '❌ 未设置（使用默认）'}",
                f"  UMO: {umo}",
                "",
                "📋 可用指令：",
                "  /astrbook reset - 重置对话历史",
                "  /astrbook persona [名称] - 查看/切换人格",
                "  /astrbook new - 创建新对话",
                "  /astrbook browse - 立即触发逛帖",
                "  /astrbook status - 查看状态",
            ]

            event.set_result(MessageEventResult().message("\n".join(lines)))
        except Exception as e:
            logger.error(f"[astrbook] Failed to get status: {e}", exc_info=True)
            event.set_result(MessageEventResult().message(f"❌ 获取状态失败: {e}"))

    @astrbook_cmd.command("browse")
    async def astrbook_browse(self, event: AstrMessageEvent):
        """立即触发 AstrBook 适配器执行一次逛帖"""
        adapter = self._get_astrbook_adapter()
        if not adapter:
            event.set_result(
                MessageEventResult().message(
                    "❌ 未找到 AstrBook 适配器实例，请确认适配器已启用。"
                )
            )
            return

        if not adapter._connected:
            event.set_result(
                MessageEventResult().message(
                    "❌ AstrBook 适配器 SSE 未连接，无法执行逛帖。"
                )
            )
            return

        try:
            # Trigger browse in background
            trigger = getattr(adapter, "trigger_browse", None)
            if callable(trigger):
                trigger()
            else:
                asyncio.create_task(adapter._do_browse())
            event.set_result(
                MessageEventResult().message(
                    "✅ 已触发 AstrBook 逛帖任务，Bot 将开始浏览论坛。"
                )
            )
        except Exception as e:
            logger.error(f"[astrbook] Failed to trigger browse: {e}", exc_info=True)
            event.set_result(MessageEventResult().message(f"❌ 触发逛帖失败: {e}"))

    def _register_config(self):
        if self._supports_adapter_metadata_args or self._registered:
            return False
        target_dict = None
        try:
            target_dict = CONFIG_METADATA_2["platform_group"]["metadata"]["platform"][
                "items"
            ]
            for name, metadata in self._astrbook_items.items():
                if name not in target_dict:
                    target_dict[name] = metadata
                    self._legacy_config_added.add(name)
        except Exception as e:
            logger.error(f"[astrbook] 在注册平台元数据时出现问题,e:{e}", exc_info=True)
            if isinstance(target_dict, dict):
                for name in self._legacy_config_added:
                    target_dict.pop(name, None)
            self._legacy_config_added.clear()
            return False
        self._registered = True
        return True

    def _unregister_config(self):
        if self._supports_adapter_metadata_args or not self._registered:
            return False
        try:
            target_dict = CONFIG_METADATA_2["platform_group"]["metadata"]["platform"][
                "items"
            ]
            for name in self._legacy_config_added:
                target_dict.pop(name, None)
        except Exception as e:
            logger.error(f"[astrbook] 在清理平台元数据时出现问题,e:{e}", exc_info=True)
            return False
        self._registered = False
        self._legacy_config_added.clear()
        return True

    async def initialize(self):
        self._register_config()
        # AstrBot 4.28's decorator-generated schemas omit ``required``. Add it
        # after all plugin tools have been registered so omitted arguments are
        # rejected by the model instead of becoming Python TypeErrors.
        self._harden_tool_schemas()

    async def terminate(self):
        adapter = self._get_astrbook_adapter()
        terminate = getattr(adapter, "terminate", None)
        if callable(terminate):
            try:
                await terminate()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[astrbook] adapter cleanup failed: %s", exc, exc_info=True)
        cleanup_tasks_set = getattr(self, "_temp_cleanup_tasks", set())
        if not isinstance(cleanup_tasks_set, set):
            cleanup_tasks_set = set()
        cleanup_tasks = list(cleanup_tasks_set)
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        cleanup_tasks_set.clear()
        await self._close_http_session()
        self._unregister_config()
