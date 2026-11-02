from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from .auth import valid_any_service_key
from .config import Settings, get_settings
from .jobs import prepare_payload
from .models import OWNER_ID_RE, CreateJobRequest
from .piston import PistonUnavailable
from .registry import runtime_matches
from .sessions import release_session, reserve_session, session_channel
from .security import reserve_websocket_ip_budget

logger = logging.getLogger("codeleaf-runner.interactive")
_ALLOWED_PISTON_TYPES = {"runtime", "stage", "data", "exit", "error"}
_ALLOWED_SIGNALS = {"SIGINT", "SIGTERM", "SIGKILL"}


def piston_websocket_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, "/api/v2/connect", "", ""))


async def connect_piston_websocket(settings: Settings):
    return await websockets.connect(
        piston_websocket_url(settings.piston_base_url),
        open_timeout=settings.piston_connect_timeout_seconds,
        close_timeout=3,
        ping_interval=20,
        ping_timeout=20,
        max_size=settings.interactive_max_message_bytes,
        max_queue=32,
    )


async def _send_signal(upstream: Any, signal: str, lock: asyncio.Lock) -> None:
    async with lock:
        await upstream.send(json.dumps({"type": "signal", "signal": signal}))


async def _send_client_error(websocket: WebSocket, message: str) -> None:
    try:
        await websocket.send_json({"type": "error", "message": message[:300]})
    except Exception:  # noqa: BLE001 - socket may already be closed.
        pass


def _validate_init_message(payload: Any, settings: Settings) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("The first message must be a JSON initialization object.")
    allowed = {"type", "language", "version", "source", "files", "args"}
    unexpected = set(payload) - allowed
    if unexpected:
        raise ValueError("Initialization contains unsupported fields: " + ", ".join(sorted(unexpected)))
    message_type = payload.get("type")
    if message_type is not None and message_type != "init":
        raise ValueError("The first message must have type 'init'.")

    body_data = {key: value for key, value in payload.items() if key != "type"}
    body_data["stdin"] = ""
    try:
        body = CreateJobRequest.model_validate(body_data)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", []))
        raise ValueError(f"Invalid initialization field {location or 'request'}: {first['msg']}") from exc
    if body.owner_id:
        raise ValueError("Do not send owner_id in the initialization message; use the authenticated service header.")
    entry, prepared = prepare_payload(body, settings)
    return entry, prepared


async def _read_client_messages(
    websocket: WebSocket,
    upstream: Any,
    send_lock: asyncio.Lock,
    settings: Settings,
) -> None:
    message_count = 0
    total_input_bytes = 0
    while True:
        raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=settings.interactive_input_idle_timeout_seconds
        )
        if len(raw.encode("utf-8")) > settings.interactive_max_message_bytes:
            raise ValueError("Terminal message exceeded the configured size limit.")
        try:
            packet = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Terminal messages must be valid JSON.") from exc
        if not isinstance(packet, dict):
            raise ValueError("Terminal messages must be JSON objects.")

        message_count += 1
        if message_count > settings.interactive_max_messages:
            raise ValueError("This session exceeded its terminal message limit.")

        packet_type = packet.get("type")
        if packet_type == "data":
            if packet.get("stream") != "stdin":
                raise ValueError("CodeLeaf clients may write only to the stdin stream.")
            data = packet.get("data")
            if not isinstance(data, str) or "\x00" in data:
                raise ValueError("stdin data must be valid text.")
            data_size = len(data.encode("utf-8"))
            total_input_bytes += data_size
            if data_size > settings.interactive_max_input_message_bytes:
                raise ValueError("This stdin message exceeds the configured size limit.")
            if total_input_bytes > settings.interactive_max_total_input_bytes:
                raise ValueError("This session exceeded its total stdin limit.")
        elif packet_type == "signal":
            signal = packet.get("signal")
            if signal not in _ALLOWED_SIGNALS:
                raise ValueError("Only SIGINT, SIGTERM, and SIGKILL are accepted.")
        else:
            raise ValueError("Only stdin data and process signal messages are accepted after initialization.")

        async with send_lock:
            await upstream.send(json.dumps(packet, separators=(",", ":")))
        if packet_type == "signal" and packet.get("signal") == "SIGKILL":
            return "terminated"


async def _read_piston_messages(
    websocket: WebSocket,
    upstream: Any,
    send_lock: asyncio.Lock,
    settings: Settings,
) -> None:
    output_bytes = 0
    while True:
        raw = await upstream.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > settings.interactive_max_message_bytes:
            raise ValueError("Piston returned a terminal message exceeding the configured size limit.")
        try:
            packet = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Piston returned an invalid WebSocket event.") from exc
        if not isinstance(packet, dict) or packet.get("type") not in _ALLOWED_PISTON_TYPES:
            raise RuntimeError("Piston returned an unsupported WebSocket event.")

        packet_type = packet["type"]
        if packet_type == "data":
            if packet.get("stream") not in {"stdout", "stderr"}:
                raise RuntimeError("Piston returned an invalid output stream.")
            data = packet.get("data", "")
            if not isinstance(data, str):
                raise RuntimeError("Piston returned non-text terminal output.")
            output_bytes += len(data.encode("utf-8"))
            if output_bytes > settings.interactive_max_output_bytes:
                await _send_signal(upstream, "SIGKILL", send_lock)
                await _send_client_error(websocket, "Execution stopped because its output exceeded the session limit.")
                return

        await websocket.send_text(json.dumps(packet, separators=(",", ":")))
        if packet_type == "error":
            return
        if packet_type == "exit" and (
            packet.get("stage") == "run"
            or packet.get("code") not in (None, 0)
            or packet.get("signal")
        ):
            return


async def handle_interactive_session(websocket: WebSocket) -> None:
    settings = get_settings()
    provided_key = websocket.headers.get("x-runner-key", "")
    owner_id = websocket.headers.get("x-codeleaf-user", "")
    if not valid_any_service_key(provided_key, settings):
        await websocket.close(code=4401, reason="Runner service authentication failed.")
        return
    if not owner_id or not OWNER_ID_RE.fullmatch(owner_id):
        await websocket.close(code=4401, reason="Authenticated CodeLeaf user scope is required.")
        return
    origin = websocket.headers.get("origin", "").rstrip("/")
    if origin and origin not in settings.allowed_websocket_origins:
        await websocket.close(code=4403, reason="WebSocket origin is not allowed.")
        return

    redis = getattr(websocket.app.state, "redis", None)
    piston = getattr(websocket.app.state, "piston", None)
    if redis is None or piston is None:
        await websocket.close(code=1013, reason="Runner services are unavailable.")
        return

    client = websocket.client
    client_ip = str(client.host) if client and client.host else "unknown"
    try:
        allowed = await reserve_websocket_ip_budget(redis, client_ip, settings)
    except Exception:
        await websocket.close(code=1013, reason="Session protection is temporarily unavailable.")
        return
    if not allowed:
        await websocket.close(code=4429, reason="Interactive connection rate limit reached.")
        return

    await websocket.accept()
    session_id = str(uuid4())
    upstream = None
    pubsub = None
    reserved = False
    send_lock = asyncio.Lock()
    try:
        try:
            init_packet = await asyncio.wait_for(
                websocket.receive_json(), timeout=settings.interactive_init_timeout_seconds
            )
            entry, payload = _validate_init_message(init_packet, settings)
        except (asyncio.TimeoutError, ValueError, ValidationError) as exc:
            await _send_client_error(websocket, str(exc) or "Initialization timed out.")
            await websocket.close(code=4400, reason="Invalid session initialization.")
            return
        except WebSocketDisconnect:
            return

        try:
            runtimes = await piston.list_runtimes()
        except PistonUnavailable as exc:
            await _send_client_error(websocket, str(exc))
            await websocket.close(code=1013, reason="Piston is unavailable.")
            return
        runtime = runtime_matches(entry, runtimes)
        if runtime is None:
            await _send_client_error(
                websocket,
                f"{entry['name']} runtime is registered but is not installed on this CodeLeaf Runner node.",
            )
            await websocket.close(code=4409, reason="Runtime is not installed.")
            return
        installed_version = str(runtime.get("version", ""))
        if payload["version"] not in {"*", installed_version}:
            await _send_client_error(
                websocket,
                f"Runtime version {payload['version']!r} is not installed. Available version: {installed_version or 'unknown'}.",
            )
            await websocket.close(code=4400, reason="Runtime version is not installed.")
            return
        payload["version"] = installed_version

        reserved = await reserve_session(
            redis,
            session_id=session_id,
            owner_id=owner_id,
            ttl_seconds=settings.interactive_session_ttl_seconds,
            max_active=settings.max_interactive_sessions_per_user,
        )
        if not reserved:
            await _send_client_error(websocket, "You already have the maximum number of active interactive sessions.")
            await websocket.close(code=4429, reason="Interactive session limit reached.")
            return

        upstream = await connect_piston_websocket(settings)
        pubsub = redis.pubsub()
        await pubsub.subscribe(session_channel(session_id))
        await websocket.send_json({"type": "session", "session_id": session_id})

        init_for_piston = {
            "type": "init",
            "language": payload["piston_language"],
            "version": payload["version"],
            "files": [
                {"name": item["name"], "content": item["content"], "encoding": "utf8"}
                for item in payload["files"]
            ],
            "args": payload.get("args", []),
            "compile_timeout": settings.piston_compile_timeout_ms,
            "run_timeout": settings.piston_run_timeout_ms,
            "compile_cpu_time": settings.piston_compile_cpu_time_ms,
            "run_cpu_time": settings.piston_run_cpu_time_ms,
            "compile_memory_limit": settings.piston_compile_memory_limit_bytes,
            "run_memory_limit": settings.piston_run_memory_limit_bytes,
        }
        await upstream.send(json.dumps(init_for_piston, separators=(",", ":")))

        client_task = asyncio.create_task(
            _read_client_messages(websocket, upstream, send_lock, settings),
            name=f"client-input-{session_id}",
        )
        piston_task = asyncio.create_task(
            _read_piston_messages(websocket, upstream, send_lock, settings),
            name=f"piston-output-{session_id}",
        )
        control_task = asyncio.create_task(
            _watch_termination_with_pubsub(pubsub, websocket, upstream, send_lock),
            name=f"session-control-{session_id}",
        )

        tasks = {client_task, piston_task, control_task}
        try:
            done, pending = await asyncio.wait(
                tasks,
                timeout=settings.interactive_max_session_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await _send_signal(upstream, "SIGKILL", send_lock)
                await _send_client_error(websocket, "Execution session exceeded its maximum duration and was stopped.")
            elif client_task in done and not piston_task.done():
                # Disconnect, input timeout, or explicit SIGKILL ends the process.
                client_action = None
                try:
                    client_action = client_task.result()
                except (WebSocketDisconnect, asyncio.TimeoutError):
                    pass
                except ValueError as exc:
                    await _send_client_error(websocket, str(exc))
                if client_action != "terminated" and not piston_task.done():
                    await _send_signal(upstream, "SIGKILL", send_lock)
                if not piston_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(piston_task), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
            elif control_task in done and not piston_task.done():
                # The control watcher already sent SIGKILL; allow Piston to emit its exit event.
                try:
                    await asyncio.wait_for(asyncio.shield(piston_task), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
            elif piston_task in done:
                try:
                    piston_task.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Piston interactive stream failed (%s)", type(exc).__name__)
                    await _send_client_error(websocket, "Piston closed the interactive output stream unexpectedly.")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await pubsub.aclose()
                pubsub = None
            except Exception:  # noqa: BLE001
                pass
    except WebSocketDisconnect:
        if upstream is not None:
            try:
                await _send_signal(upstream, "SIGKILL", send_lock)
            except Exception:  # noqa: BLE001
                pass
    except asyncio.TimeoutError:
        await _send_client_error(websocket, "Execution session timed out.")
        if upstream is not None:
            try:
                await _send_signal(upstream, "SIGKILL", send_lock)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 - never leak internals to the client.
        logger.exception("Interactive session failed (%s)", type(exc).__name__)
        await _send_client_error(websocket, "The interactive execution session failed.")
        if upstream is not None:
            try:
                await _send_signal(upstream, "SIGKILL", send_lock)
            except Exception:  # noqa: BLE001
                pass
    finally:
        if upstream is not None:
            try:
                await upstream.close()
            except Exception:  # noqa: BLE001
                pass
        if pubsub is not None:
            try:
                await pubsub.aclose()
            except Exception:  # noqa: BLE001
                pass
        if reserved:
            try:
                await release_session(redis, session_id=session_id, owner_id=owner_id)
            except Exception:  # noqa: BLE001
                logger.exception("Could not release interactive session lease %s", session_id)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


async def _watch_termination_with_pubsub(
    pubsub: Any,
    websocket: WebSocket,
    upstream: Any,
    send_lock: asyncio.Lock,
) -> None:
    while True:
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if not message or message.get("type") != "message":
            await asyncio.sleep(0.05)
            continue
        try:
            payload = json.loads(message.get("data") or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("type") == "terminate":
            await _send_signal(upstream, "SIGKILL", send_lock)
            try:
                await websocket.send_json({"type": "control", "status": "termination_requested"})
            except Exception:  # noqa: BLE001
                pass
            return
