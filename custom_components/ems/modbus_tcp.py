"""A minimal asyncio Modbus TCP client: read holding registers (0x03), write single register (0x06).

Enough for the Fronius storage model and no dependency to install. One persistent connection; an operation
that fails on the connection reconnects once and retries once. Requests are serialised (one at a time)."""

from __future__ import annotations

import asyncio
import contextlib
import struct

TIMEOUT_S = 5.0
READ_HOLDING = 0x03
WRITE_SINGLE = 0x06


class ModbusError(Exception):
    """The device answered with an exception or something malformed, or could not be reached."""


class ModbusTcpClient:
    def __init__(self, host: str, port: int = 502, unit_id: int = 1, timeout: float = TIMEOUT_S) -> None:
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tid = 0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _connect(self) -> None:
        if self._writer is None:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )

    async def _request(self, pdu: bytes) -> bytes:
        async with self._lock:
            for attempt in (1, 2):
                try:
                    await self._connect()
                    return await asyncio.wait_for(self._exchange(pdu), self.timeout)
                except (OSError, TimeoutError, asyncio.IncompleteReadError) as err:
                    await self.close()
                    if attempt == 2:
                        raise ModbusError(f"{self.host}:{self.port}: {err or type(err).__name__}") from err
        raise AssertionError("unreachable")

    async def _exchange(self, pdu: bytes) -> bytes:
        assert self._reader is not None and self._writer is not None
        self._tid = (self._tid + 1) & 0xFFFF
        self._writer.write(struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, self.unit_id) + pdu)
        await self._writer.drain()
        while True:
            tid, protocol, length, _unit = struct.unpack(">HHHB", await self._reader.readexactly(7))
            body = await self._reader.readexactly(length - 1)
            if tid == self._tid and protocol == 0:
                break  # an answer to an earlier, timed-out request is skipped
        function = body[0]
        if function == pdu[0] | 0x80:
            raise ModbusError(f"Modbus exception {body[1]} (function {pdu[0]})")
        if function != pdu[0]:
            raise ModbusError(f"unexpected function {function} in answer to {pdu[0]}")
        return body

    async def read_holding(self, address: int, count: int) -> list[int]:
        body = await self._request(struct.pack(">BHH", READ_HOLDING, address, count))
        if len(body) < 2 or body[1] != 2 * count or len(body) != 2 + 2 * count:
            raise ModbusError(f"malformed answer reading {count} registers at {address}")
        return list(struct.unpack(f">{count}H", body[2:]))

    async def write_register(self, address: int, value: int) -> None:
        pdu = struct.pack(">BHH", WRITE_SINGLE, address, value & 0xFFFF)
        body = await self._request(pdu)
        if body != pdu:
            raise ModbusError(f"write {value} to {address} not confirmed")
