from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Callable
from typing import BinaryIO

_CHUNK_SIZE = 65536

_FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class StdioProxy:
    """Transparent byte-level proxy between our stdio and a child MCP server.

    Each direction runs as its own task so a slow reader on one stream can
    never deadlock the others. The proxy writes nothing of its own to stdout:
    that stream must carry only the child's JSON-RPC bytes.
    """

    def __init__(
        self,
        command: list[str],
        # Called with each chunk after it's forwarded; failures are swallowed.
        on_client_bytes: Callable[[bytes], object] | None = None,
        on_server_bytes: Callable[[bytes], object] | None = None,
        # Called when the child subprocess comes up / goes down; failures swallowed.
        on_child_up: Callable[[], object] | None = None,
        on_child_down: Callable[[], object] | None = None,
    ) -> None:
        self._command = command
        self._proc: asyncio.subprocess.Process | None = None
        self._on_client_bytes = on_client_bytes
        self._on_server_bytes = on_server_bytes
        self._on_child_up = on_child_up
        self._on_child_down = on_child_down

    async def run(self) -> int:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            print(f"talyx: command not found: {self._command[0]}", file=sys.stderr)
            return 127

        self._notify(self._on_child_up)
        loop = asyncio.get_running_loop()
        for sig in _FORWARDED_SIGNALS:
            loop.add_signal_handler(sig, self._forward_signal, sig)
        try:
            return await self._pump_until_exit()
        finally:
            for sig in _FORWARDED_SIGNALS:
                loop.remove_signal_handler(sig)
            self._notify(self._on_child_down)

    async def _pump_until_exit(self) -> int:
        assert self._proc is not None
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None

        stdin_reader = await self._connect_stdin()
        # Forward our stdout/stderr through asyncio when it can wrap them.
        # connect_read_pipe put stdin into non-blocking mode; when stdin/stdout/
        # stderr share one terminal that O_NONBLOCK flag is shared across all
        # three, so a plain blocking write of a large message would raise
        # BlockingIOError (EAGAIN). Async writers with drain() handle that. When
        # output is a regular file (redirection) asyncio can't wrap it and
        # _connect_write returns None; blocking writes to a regular file are
        # always safe (regular files ignore O_NONBLOCK), so we fall back to them.
        stdout_writer = await self._connect_write(sys.stdout.buffer)
        stderr_writer = await self._connect_write(sys.stderr.buffer)

        stdin_pump = asyncio.create_task(
            self._pump_to_child(stdin_reader, self._proc.stdin)
        )
        # Child closing its stdout/stderr pipes (EOF) ends these pumps, so all
        # forwarded output is drained before we collect the exit code.
        await asyncio.gather(
            self._pump_to_output(
                self._proc.stdout, stdout_writer, sys.stdout.buffer, self._on_server_bytes
            ),
            self._pump_to_output(self._proc.stderr, stderr_writer, sys.stderr.buffer),
        )
        returncode = await self._proc.wait()

        stdin_pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):  # suppress the CancelledError
            await stdin_pump
        return returncode

    async def _connect_stdin(self) -> asyncio.StreamReader:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=_CHUNK_SIZE, loop=loop)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        return reader

    async def _connect_write(self, pipe: BinaryIO) -> asyncio.StreamWriter | None:
        """Wrap an output pipe for async writing, or None if it's a regular file.

        connect_write_pipe only accepts pipes, sockets and character devices;
        for a regular file (redirected output) it raises ValueError, and the
        caller falls back to plain blocking writes.
        """
        loop = asyncio.get_running_loop()
        try:
            transport, protocol = await loop.connect_write_pipe(
                asyncio.streams.FlowControlMixin, pipe
            )
        except ValueError:
            return None
        return asyncio.StreamWriter(transport, protocol, None, loop)

    # Read talyx's stdin, write to server's stdin
    async def _pump_to_child(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                chunk = await reader.read(_CHUNK_SIZE)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
                self._observe(self._on_client_bytes, chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            # Our stdin hit EOF (client is gone): tell the server likewise.
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                writer.close()

    # Read a child stream, forward to our matching stdout/stderr.
    async def _pump_to_output(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None,
        raw: BinaryIO,
        observe: Callable[[bytes], object] | None = None,
    ) -> None:
        try:
            while True:
                chunk = await reader.read(_CHUNK_SIZE)
                if not chunk:
                    break
                if writer is not None:
                    writer.write(chunk)
                    await writer.drain()
                else:  # regular file: blocking write never raises EAGAIN
                    raw.write(chunk)
                    raw.flush()
                self._observe(observe, chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # our downstream consumer is gone; stop forwarding this stream

    @staticmethod
    def _observe(observe: Callable[[bytes], object] | None, chunk: bytes) -> None:
        """Feed the observer; its failures must never break forwarding."""
        if observe is None:
            return
        try:
            observe(chunk)
        except Exception as exc:  # noqa: BLE001 - observation is best-effort
            print(f"talyx: observer error ignored: {exc!r}", file=sys.stderr)

    @staticmethod
    def _notify(callback: Callable[[], object] | None) -> None:
        """Fire a lifecycle callback; its failures must never break the proxy."""
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:  # noqa: BLE001 - lifecycle hooks are best-effort
            print(f"talyx: lifecycle callback error ignored: {exc!r}", file=sys.stderr)

    def _forward_signal(self, sig: signal.Signals) -> None:
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.send_signal(sig)
