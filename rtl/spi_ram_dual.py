# This file is Copyright (c) 2014 Yann Sionneau <ys@m-labs.hk>
# This file is Copyright (c) 2014-2018 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2013-2014 Robert Jordens <jordens@gmail.com>
# This file is Copyright (c) 2015-2014 Sebastien Bourdeauducq <sb@m-labs.hk>

# License: BSD


from migen import *
from migen.genlib.misc import timeline

from litex.gen import *

from litex.soc.integration.doc import AutoDoc, ModuleDoc
from litex.soc.interconnect import wishbone
from litex.soc.cores.spi import SPIMaster

# SpiRam Quad/Dual/Single (memory-mapped) --------------------------------------------------------

_FAST_READ = 0x0b
_DIOFR = 0xbb
_QIOFR = 0xeb

_WRITE = 0x02
_QIOW = 0x38


def _format_cmd(cmd, spi_width):
    """
    `cmd` is the read instruction. Since everything is transmitted on all
    dq lines (cmd, adr and data), extend/interleave cmd to full pads.dq
    width even if dq1-dq3 are don't care during the command phase:
    For example, for N25Q128, 0xeb is the quad i/o fast read, and
    extended to 4 bits (dq1,dq2,dq3 high) is: 0xfffefeff
    """
    c = 2**(8*spi_width)-1
    for b in range(8):
        if not (cmd>>b)%2:
            c &= ~(1<<(b*spi_width))
    return c


class SpiRamDualQuad(Module, AutoDoc, ModuleDoc):
    def __init__(self, pads_1, pads_2, dummy=5, endianness="big"):
        """
        Simple SPI flash.
        Supports multi-bit pseudo-parallel reads (aka Dual or Quad I/O Fast
        Read). Only supports mode0 (cpol=0, cpha=0).
        """
        self.bus = bus = wishbone.Interface()
        spi_width = len(pads_1.dq)
        assert spi_width >= 2
        assert len(pads_1.dq) == len(pads_2.dq)

        self.intro = ModuleDoc(title="SPI RAM", body="""
        This device contains paired SPI RAM chips.  These are run in quad-SPI
        mode (but not QPI), giving a total of 8 bits of parallel data.

        This module performs memory-mapping by translating Wishbone calls into
        SPI commands.  Reads get transformed into Quad-Fast-Read commands, and
        writes get transformed into Quad-Write.
        """)

        self.reads = ModuleDoc(title="Data Reads", body="""
        The SPI chip uses the following protocol to perform chip reads:

        .. wavedrom::
            :caption: Fast Read Operation

            {
                "signal": [
                ["RAM0",
                    {  "name": 'CLK',     "wave": 'xp.....................x', "data": ''   },
                    {  "name": 'CS',      "wave": '10......................', "data": ''   },
                    {  "name": 'SI',      "wave": 'x1..0101.222222xxxxxx22x', "data": '20 16 12 8 4 0 8 0'},
                    {  "name": 'SO',      "wave": 'xxxxxxxxx222222xxxxxx22x', "data": '21 17 13 9 5 1 9 1'},
                    {  "name": 'D2',      "wave": 'xxxxxxxxx222222xxxxxx22x', "data": '22 18 14 10 6 2 10 2'},
                    {  "name": 'D3',      "wave": 'xxxxxxxxx222222xxxxxx22x', "data": '23 19 15 11 7 3 11 3'},
                    {  "name": 'meaning', "wave": 'x2.......2.....2.....2.x', "data": ['cmd: 0xEB', 'address', 'HI-Z', 'data'] },
                    ],
                    {},
                    ["RAM1",
                    {  "name": 'CLK',     "wave": 'xp.....................x', "data": ''   },
                    {  "name": 'CS',      "wave": '10......................', "data": ''   },
                    {  "name": 'SI',      "wave": 'x1..0101.222222xxxxxx22x', "data": '20 16 12 8 4 0 12 4'},
                    {  "name": 'SO',      "wave": 'xxxxxxxxx222222xxxxxx22x', "data": '21 17 13 9 5 1 13 5'},
                    {  "name": 'D2',      "wave": 'xxxxxxxxx222222xxxxxx22x', "data": '22 18 14 10 6 2 14 6'},
                    {  "name": 'D3',      "wave": 'xxxxxxxxx222222xxxxxx22x', "data": '23 19 15 11 7 3 15 7'},
                    {  "name": 'meaning', "wave": 'x2.......2.....2.....2.x', "data": ['cmd: 0xEB', 'address', 'HI-Z', 'data'] },
                    ]
                ],
                "head": { tick: -1 },
                "foot": { tick: -1 }
            }

        This is a ``SPI Fast Read`` operation.  Any time something accesses the Wishbone bus,
        it is transformed into an operation such as this.  The address is striped across the four
        data lines, which is followed by a period of "dummy clock cycles" while the SPI device
        fetches the data.  Finally, the data is made available.

        There are two optimizations made here.  First, and most obvious, bits are striped
        across two devices.  The address and command is the same for both chips, but the
        actual bits are different.  This gets us 8 bits per clock cycle.

        Second, if you attempt to read from two consecutive addresses, the ``CS`` line is not
        deasserted and we simply continue to read data from the device.  This avoids about
        20 clock cycles of overhead when doing sequential reads.
        """)

        self.write_doc = ModuleDoc(title="Data Writes", body="""
        The following protocol is used when performing Wishbone writes:

        .. wavedrom::
            :caption: SPI Quad Write

            {
                "signal": [
                    ["RAM0",
                        {  "name": 'CLK',     "wave": 'xp...............x', "data": ''   },
                        {  "name": 'CS',      "wave": '10................', "data": ''   },
                        {  "name": 'SI',      "wave": 'x0.1..0..22222222x', "data": '20 16 12 8 4 0 8 0'},
                        {  "name": 'SO',      "wave": 'xxxxxxxxx22222222x', "data": '21 17 13 9 5 1 9 1'},
                        {  "name": 'D2',      "wave": 'xxxxxxxxx22222222x', "data": '22 18 14 10 6 2 10 2'},
                        {  "name": 'D3',      "wave": 'xxxxxxxxx22222222x', "data": '23 19 15 11 7 3 11 3'},
                        {  "name": 'meaning', "wave": 'x2.......2.....2.x', "data": ['cmd: 0x38', 'address', 'data'] },
                    ],
                    {},
                    ["RAM1",
                        {  "name": 'CLK',     "wave": 'xp...............x', "data": ''   },
                        {  "name": 'CS',      "wave": '10................', "data": ''   },
                        {  "name": 'SI',      "wave": 'x0.1..0..22222222x', "data": '20 16 12 8 4 0 8 0'},
                        {  "name": 'SO',      "wave": 'xxxxxxxxx22222222x', "data": '21 17 13 9 5 1 13 5'},
                        {  "name": 'D2',      "wave": 'xxxxxxxxx22222222x', "data": '22 18 14 10 6 2 14 6'},
                        {  "name": 'D3',      "wave": 'xxxxxxxxx22222222x', "data": '23 19 15 11 7 3 15 7'},
                        {  "name": 'meaning', "wave": 'x2.......2.....2.x', "data": ['cmd: 0x38', 'address', 'data'] },
                    ]
                ],
                "head":{ tick:-1 },
                "foot":{tick: -1}
            }

        Like Wishbone reads, there are two optimizations in use: Dual-chip operation
        (yielding 8-bit parallel output), and continuous-write mode where the ``CS``
        line is not deasserted at the end of a write in case the subsequent write is
        to the following address.
        """)
        # # #

        cs_n = Signal(reset=1)
        clk = Signal()
        dq_oe = Signal()
        wbone_width = len(bus.dat_r)

        read_cmd_params = {
            4: (_format_cmd(_QIOFR, 4), 4*8),
            2: (_format_cmd(_DIOFR, 2), 2*8),
            1: (_format_cmd(_FAST_READ, 1), 1*8)
        }
        read_cmd, cmd_width = read_cmd_params[spi_width]
        write_cmd_params = {
            4: _format_cmd(_QIOW, 4),
            1: _format_cmd(_WRITE, 1),
        }
        write_cmd = write_cmd_params[spi_width]
        addr_width = 24

        dq1 = TSTriple(spi_width)
        dq2 = TSTriple(spi_width)
        self.specials.dq1 = dq1.get_tristate(pads_1.dq)
        self.specials.dq2 = dq2.get_tristate(pads_2.dq)

        sr = Signal(max(cmd_width, addr_width, wbone_width))
        if endianness == "big":
            self.comb += bus.dat_r.eq(sr)
        else:
            self.comb += bus.dat_r.eq(reverse_bytes(sr))

        # This signal goes HIGH when the DQ outputs of both RAM chips
        # should be ganged together.  This is used for writing addresses
        # and commands.
        gang_outputs = Signal()

        self.comb += [
            pads_1.clk.eq(clk),
            pads_1.cs_n.eq(cs_n),
            pads_2.clk.eq(clk),
            pads_2.cs_n.eq(cs_n),
            dq1.o.eq(sr[-spi_width:]),
            If(gang_outputs,
                dq2.o.eq(sr[-spi_width:]),
            ).Else(
                dq2.o.eq(sr[-(spi_width*2):-spi_width]),
            ),
            dq1.oe.eq(dq_oe),
            dq2.oe.eq(dq_oe),
        ]

        self.submodules.fsm = fsm = FSM()
        cycle_counter = Signal(5, reset_less=True)
        cycle_counter_reset = Signal()
        cycle_counter_ce = Signal()
        is_write = Signal()
        next_addr = Signal(32)
        self.sync += \
            If(cycle_counter_reset,
                cycle_counter.eq(0)
            ).Elif(cycle_counter_ce,
                cycle_counter.eq(cycle_counter + 1)
            )

        fsm.act("IDLE",
            cycle_counter_reset.eq(1),
            If(bus.cyc & bus.stb,
                NextState("SEND_CMD"),
                If(bus.we,
                    NextValue(sr, write_cmd),
                ).Else(
                    NextValue(sr, read_cmd),
                ),
            )
        )
        fsm.act("SEND_CMD",
            cycle_counter_ce.eq(1),
            dq_oe.eq(1),
            cs_n.eq(0),
            clk.eq(ClockSignal()),
            gang_outputs.eq(1),

            # Cache the write value so we can speed up sequences
            NextValue(is_write, bus.we),

            NextValue(sr, Cat(Signal(cmd_width - wbone_width + spi_width), sr)),
            If(cycle_counter == cmd_width//spi_width - 1,
                cycle_counter_reset.eq(1),
                # Strip off the bottom address bit, since we're striping across two chips.
                NextValue(sr, bus.adr << 1),
                # However, Litex Wishbone addresses are missing the bottom two bits,
                # so the next address to read is just bus.adr + 1.
                NextValue(next_addr, bus.adr + 1),
                NextState("SEND_ADDR"),
            ),
        )
        fsm.act("SEND_ADDR",
            cycle_counter_ce.eq(1),
            dq_oe.eq(1),
            cs_n.eq(0),
            clk.eq(ClockSignal()),
            gang_outputs.eq(1),
            NextValue(sr, Cat(Signal(cmd_width - wbone_width + spi_width), sr)),
            If(cycle_counter == addr_width//spi_width - 1,
                cycle_counter_reset.eq(1),
                If(is_write,
                    NextState("SEND_DATA"),
                    NextValue(sr, bus.dat_w),
                ).Else(
                    NextState("RECV_DATA_DUMMY"),
                ),
            ),
        )
        fsm.act("SEND_DATA",
            cycle_counter_ce.eq(1),
            dq_oe.eq(1),
            cs_n.eq(0),
            clk.eq(ClockSignal()),
            NextValue(sr, Cat(Signal(cmd_width - wbone_width + (spi_width * 2)), sr)),
            If(cycle_counter == wbone_width//spi_width//2 - 1,
                cycle_counter_reset.eq(1),
                NextState("WAIT_SEND_MORE"),
                bus.ack.eq(1),
            ),
        )
        fsm.act("RECV_DATA_DUMMY",
            cycle_counter_ce.eq(1),
            dq_oe.eq(0),
            cs_n.eq(0),
            clk.eq(ClockSignal()),
            If(cycle_counter == dummy,
                cycle_counter_reset.eq(1),
                NextState("RECV_DATA"),
            ),
        )
        fsm.act("RECV_DATA",
            cycle_counter_ce.eq(1),
            dq_oe.eq(0),
            cs_n.eq(0),
            clk.eq(ClockSignal()),
            NextValue(sr, Cat(dq1.i, dq2.i, sr[:-spi_width*2])),
            If(cycle_counter == wbone_width//spi_width//2,
                cycle_counter_reset.eq(1),
                NextState("WAIT_RECV_MORE"),
                bus.ack.eq(1),
            ),
        )
        fsm.act("WAIT_SEND_MORE",
            cs_n.eq(0),
            If(bus.cyc & bus.stb,
                NextState("IDLE"),
                # If the next command is a write, and the address
                # is the next address, jump immediately to SEND_DATA
                If(bus.adr == next_addr,
                    If(bus.we,
                        NextValue(next_addr, bus.adr + 1),
                        NextState("SEND_DATA")
                    )
                )
            )
        )
        fsm.act("WAIT_RECV_MORE",
            cs_n.eq(0),
            If(bus.cyc & bus.stb,
                NextState("IDLE"),
                # If the next command is a read, and the address
                # is the next address, jump immediately to RECV_DATA
                If(bus.adr == next_addr,
                    If(~bus.we,
                        NextValue(next_addr, bus.adr + 1),
                        NextState("RECV_DATA"),
                    )
                )
            )
        )