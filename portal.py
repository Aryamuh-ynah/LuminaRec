from __future__ import annotations

import asyncio
import os
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from dbus_next import BusType, Message, MessageType, Variant
from dbus_next.aio import MessageBus

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"


@dataclass
class PortalStream:
    node_id: int
    fd: int
    width: int
    height: int
    x: int = 0
    y: int = 0


class PortalError(RuntimeError):
    pass


def _unwrap(value: Any) -> Any:
    return value.value if isinstance(value, Variant) else value


class ScreenCastPortal:
    """Owns a persistent D-Bus session so the PipeWire permission FD stays valid."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="portal-dbus")
        self._ready = threading.Event()
        self._bus: MessageBus | None = None
        self._session_path: str | None = None
        self._thread.start()
        if not self._ready.wait(5):
            raise PortalError("Could not connect to the desktop portal D-Bus service.")

    def _thread_main(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect())
        finally:
            self._ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    async def _connect(self) -> None:
        self._bus = await MessageBus(bus_type=BusType.SESSION, negotiate_unix_fd=True).connect()

    @property
    def bus(self) -> MessageBus:
        if self._bus is None:
            raise PortalError("Desktop portal is not connected.")
        return self._bus

    def _sender_component(self) -> str:
        unique = self.bus.unique_name or ""
        if not unique:
            raise PortalError("D-Bus did not assign a unique name.")
        return unique.lstrip(":").replace(".", "_")

    def _request_path(self, token: str) -> str:
        return f"/org/freedesktop/portal/desktop/request/{self._sender_component()}/{token}"

    async def _portal_request(
        self,
        interface: str,
        member: str,
        signature: str,
        body: list[Any],
        token: str,
        timeout: float = 180.0,
    ) -> dict[str, Variant]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[int, dict[str, Variant]]] = loop.create_future()
        expected_path = self._request_path(token)

        def handler(msg: Message) -> bool:
            if (
                msg.message_type == MessageType.SIGNAL
                and msg.path == expected_path
                and msg.interface == REQUEST
                and msg.member == "Response"
            ):
                if not future.done():
                    response, results = msg.body
                    future.set_result((int(response), results))
                return True
            return False

        self.bus.add_message_handler(handler)
        try:
            reply = await self.bus.call(
                Message(
                    destination=PORTAL,
                    path=PORTAL_PATH,
                    interface=interface,
                    member=member,
                    signature=signature,
                    body=body,
                )
            )
            if reply.message_type == MessageType.ERROR:
                raise PortalError(reply.body[0] if reply.body else f"Portal call {member} failed")
            response, results = await asyncio.wait_for(future, timeout=timeout)
            if response == 1:
                raise PortalError("Screen sharing was cancelled.")
            if response != 0:
                raise PortalError(f"Desktop portal denied screen sharing (response {response}).")
            return results
        finally:
            self.bus.remove_message_handler(handler)

    async def _open(self, source_type: int) -> PortalStream:
        create_token = "req" + uuid.uuid4().hex
        session_token = "sess" + uuid.uuid4().hex
        results = await self._portal_request(
            SCREENCAST,
            "CreateSession",
            "a{sv}",
            [{
                "handle_token": Variant("s", create_token),
                "session_handle_token": Variant("s", session_token),
            }],
            create_token,
        )
        session_value = results.get("session_handle")
        if session_value is None:
            raise PortalError("The desktop portal did not return a screencast session.")
        self._session_path = str(_unwrap(session_value))

        select_token = "req" + uuid.uuid4().hex
        await self._portal_request(
            SCREENCAST,
            "SelectSources",
            "oa{sv}",
            [self._session_path, {
                "handle_token": Variant("s", select_token),
                "types": Variant("u", source_type),
                "multiple": Variant("b", False),
                "cursor_mode": Variant("u", 2),
            }],
            select_token,
        )

        start_token = "req" + uuid.uuid4().hex
        started = await self._portal_request(
            SCREENCAST,
            "Start",
            "osa{sv}",
            [self._session_path, "", {"handle_token": Variant("s", start_token)}],
            start_token,
        )
        streams_v = started.get("streams")
        if streams_v is None:
            raise PortalError("No PipeWire stream was returned by the desktop portal.")
        streams = _unwrap(streams_v)
        if not streams:
            raise PortalError("No screen or window was selected.")

        node_id, props = streams[0]
        props = {k: _unwrap(v) for k, v in props.items()}
        size = props.get("size", [0, 0])
        position = props.get("position", [0, 0])
        width, height = int(size[0]), int(size[1])
        x, y = int(position[0]), int(position[1])
        if width <= 0 or height <= 0:
            raise PortalError("The portal did not report a usable stream size.")

        reply = await self.bus.call(
            Message(
                destination=PORTAL,
                path=PORTAL_PATH,
                interface=SCREENCAST,
                member="OpenPipeWireRemote",
                signature="oa{sv}",
                body=[self._session_path, {}],
            )
        )
        if reply.message_type == MessageType.ERROR:
            raise PortalError(reply.body[0] if reply.body else "OpenPipeWireRemote failed")
        if not reply.unix_fds:
            raise PortalError("The portal did not return the PipeWire file descriptor.")
        fd_index = int(reply.body[0]) if reply.body else 0
        try:
            fd = os.dup(reply.unix_fds[fd_index])
        except (IndexError, OSError) as exc:
            raise PortalError(f"Invalid PipeWire file descriptor: {exc}") from exc
        return PortalStream(int(node_id), fd, width, height, x, y)

    def open(self, source_type: int) -> PortalStream:
        future = asyncio.run_coroutine_threadsafe(self._open(source_type), self._loop)
        return future.result(timeout=190)

    async def _close_async(self) -> None:
        if self._session_path:
            try:
                await self.bus.call(
                    Message(
                        destination=PORTAL,
                        path=self._session_path,
                        interface=SESSION,
                        member="Close",
                    )
                )
            except Exception:
                pass
            self._session_path = None
        try:
            self.bus.disconnect()
        except Exception:
            pass

    def close(self) -> None:
        if not self._thread.is_alive():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._close_async(), self._loop)
            future.result(timeout=3)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)
