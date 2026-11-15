"""The Modbus TCP client against a small local Modbus server."""

from __future__ import annotations

import asyncio
import struct

import pytest

from custom_components.ems.modbus_tcp import ModbusError, ModbusTcpClient


class Server:
    """Holding registers in a dict; answers 0x03 and 0x06, exception 2 for unknown addresses."""

    def __init__(self, regs: dict[int, int]) -> None:
        self.regs = regs
        self.requests = 0
        self.drop_next = False
        """Close the connection instead of answering the next request."""

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                tid, _, length, unit = struct.unpack(">HHHB", await reader.readexactly(7))
                pdu = await reader.readexactly(length - 1)
                self.requests += 1
                if self.drop_next:
                    self.drop_next = False
                    writer.close()
                    return
                function, address, value = struct.unpack(">BHH", pdu)
                if function == 3 and all(a in self.regs for a in range(address, address + value)):
                    data = b"".join(struct.pack(">H", self.regs[a]) for a in range(address, address + value))
                    answer = struct.pack(">BB", 3, 2 * value) + data
                elif function == 6 and address in self.regs:
                    self.regs[address] = value
                    answer = pdu
                else:
                    answer = struct.pack(">BB", function | 0x80, 2)
                writer.write(struct.pack(">HHHB", tid, 0, len(answer) + 1, unit) + answer)
                await writer.drain()
        except asyncio.IncompleteReadError:
            writer.close()


def run_with_server(regs: dict[int, int], test):
    async def main():
        server = Server(regs)
        srv = await asyncio.start_server(server.handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        client = ModbusTcpClient("127.0.0.1", port, timeout=2)
        try:
            await test(client, server)
        finally:
            await client.close()
            srv.close()
            await srv.wait_closed()

    asyncio.run(main())


def test_read_and_write():
    async def test(client, server):
        assert await client.read_holding(40343, 2) == [124, 24]
        await client.write_register(40348, 2)
        await client.write_register(40355, -7000 & 0xFFFF)
        assert await client.read_holding(40348, 1) == [2]
        assert server.regs[40355] == 0x10000 - 7000

    run_with_server({40343: 124, 40344: 24, 40348: 0, 40355: 10000}, test)


def test_exception_answer_raises():
    async def test(client, _server):
        with pytest.raises(ModbusError, match="exception 2"):
            await client.read_holding(1, 2)

    run_with_server({}, test)


def test_reconnects_once_after_a_dropped_connection():
    async def test(client, server):
        assert await client.read_holding(40348, 1) == [0]
        server.drop_next = True
        assert await client.read_holding(40348, 1) == [0]
        assert server.requests == 3

    run_with_server({40348: 0}, test)


def test_unreachable_raises_modbus_error():
    async def main():
        client = ModbusTcpClient("127.0.0.1", 1, timeout=1)  # nothing listens on port 1
        with pytest.raises(ModbusError):
            await client.read_holding(40343, 2)
        await client.close()

    asyncio.run(main())
