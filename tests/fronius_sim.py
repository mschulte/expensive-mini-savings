"""A simulated Fronius GEN24: the storage model as a register image, with the interface of
modbus_tcp.ModbusTcpClient. Shared by the unit tests and the tests in a test Home Assistant."""

from __future__ import annotations

from custom_components.ems import fronius as fr
from custom_components.ems.modbus_tcp import ModbusError


def raw(value: int) -> int:
    return value & 0xFFFF


class FakeFronius:
    def __init__(self, *, max_charge_w: int = 5000, soc_pct: float = 50.0, header=(124, 24)) -> None:
        block = [0] * fr.STORAGE_COUNT
        block[fr.W_CHA_MAX] = max_charge_w
        block[fr.W_CHA_MAX_SF] = 0
        block[fr.MIN_RSV_PCT] = 500
        block[fr.MIN_RSV_PCT_SF] = raw(-2)
        block[fr.CHA_STATE] = round(soc_pct * 100)
        block[fr.CHA_STATE_SF] = raw(-2)
        block[fr.OUT_W_RTE] = 10000
        block[fr.IN_W_RTE] = 10000
        block[fr.IN_OUT_W_RTE_SF] = raw(-2)
        block[fr.CHA_GRI_SET] = 1
        self.regs = {fr.MODEL_HEADER: header[0], fr.MODEL_HEADER + 1: header[1]}
        self.regs |= {fr.STORAGE_START + i: v for i, v in enumerate(block)}
        self.writes: list[tuple[int, int]] = []
        self.down = False
        """No connection: every request fails."""
        self.stuck = False
        """Writes are acknowledged but change nothing."""
        self.closed = False

    def set(self, offset: int, value: int) -> None:
        """Change a point in the storage block (as the inverter app or a provider would)."""
        self.regs[fr.STORAGE_START + offset] = raw(value)

    def get(self, offset: int) -> int:
        return self.regs[fr.STORAGE_START + offset]

    def storage(self) -> fr.Storage:
        return fr.Storage.decode([self.get(i) for i in range(fr.STORAGE_COUNT)])

    async def read_holding(self, address: int, count: int) -> list[int]:
        if self.down:
            raise ModbusError("timeout")
        return [self.regs.get(a, 0) for a in range(address, address + count)]

    async def write_register(self, address: int, value: int) -> None:
        if self.down:
            raise ModbusError("timeout")
        self.writes.append((address, value))
        if not self.stuck:
            self.regs[address] = raw(value)

    async def close(self) -> None:
        self.closed = True
