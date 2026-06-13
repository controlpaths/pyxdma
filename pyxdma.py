"""pyxdma - A pure-Python user-space driver for the AMD/Xilinx AXI DMA IP.

This module maps the AXI DMA control registers and the reserved DDR buffers
through ``/dev/mem`` and exposes a small, class-based API to run MM2S
(memory-mapped to stream) and S2MM (stream to memory-mapped) transactions
from user space on PetaLinux.

It must be run as root, since it opens ``/dev/mem`` and writes directly to
physical memory. The DDR buffers used for the transfers must be reserved in
the device tree (see the README) so the OS does not use them.
"""

import mmap
import os
import struct
import time

# ---------------------------------------------------------------------------
# AXI DMA register offsets (see AMD PG021)
# ---------------------------------------------------------------------------

# MM2S channel: memory-mapped (DDR) to AXI-Stream
MM2S_CONTROL_REGISTER = 0x00
MM2S_STATUS_REGISTER = 0x04
MM2S_SRC_ADDRESS_REGISTER = 0x18
MM2S_TRNSFR_LENGTH_REGISTER = 0x28

# S2MM channel: AXI-Stream to memory-mapped (DDR)
S2MM_CONTROL_REGISTER = 0x30
S2MM_STATUS_REGISTER = 0x34
S2MM_DST_ADDRESS_REGISTER = 0x48
S2MM_BUFF_LENGTH_REGISTER = 0x58

# ---------------------------------------------------------------------------
# Control register (DMACR) bit masks
# ---------------------------------------------------------------------------
HALT_DMA = 0x00000000
RUN_DMA = 0x00000001
RESET_DMA = 0x00000004
ENABLE_IOC_IRQ = 0x00001000
ENABLE_DELAY_IRQ = 0x00002000
ENABLE_ERR_IRQ = 0x00004000
ENABLE_ALL_IRQ = 0x00007000

# ---------------------------------------------------------------------------
# Status register (DMASR) flags
# ---------------------------------------------------------------------------
IOC_IRQ_FLAG = 1 << 12
IDLE_FLAG = 1 << 1

# Human-readable decoding of the status register.
DMA_STATUS = {
    0x00000000: "STATUS_RUNNING",
    0x00000001: "STATUS_HALTED",
    0x00000002: "STATUS_IDLE",
    0x00000004: "STATUS_RSV",
    0x00000008: "STATUS_SG_INCLDED",
    0x00000010: "STATUS_DMA_INTERNAL_ERR",
    0x00000020: "STATUS_DMA_SLAVE_ERR",
    0x00000040: "STATUS_DMA_DECODE_ERR",
    0x00000080: "STATUS_RSV",
    0x00000100: "STATUS_SG_INTERNAL_ERR",
    0x00000200: "STATUS_SG_SLAVE_ERR",
    0x00000400: "STATUS_SG_DECODE_ERR",
    0x00000800: "STATUS_RSV",
    0x00001000: "STATUS_IOC_IRQ",
    0x00002000: "STATUS_DELAY_IRQ",
    0x00004000: "STATUS_ERR_IRQ",
}


class AxiRegisterBlock:
    """A memory-mapped region of a physical address space.

    Wraps a single ``mmap`` over ``/dev/mem`` and provides word (32-bit) and
    raw byte access relative to the mapped base address. The same class is
    used both for the DMA control registers and for the reserved DDR buffers.
    """

    def __init__(self, fd, base_address, size):
        self._size = size
        self._map = mmap.mmap(
            fd,
            size,
            flags=mmap.MAP_SHARED,
            prot=(mmap.PROT_READ | mmap.PROT_WRITE),
            offset=base_address,
        )

    def write(self, offset, value):
        """Write a 32-bit little-endian word at ``offset``."""
        self._map.seek(offset)
        self._map.write(struct.pack("=I", value))

    def read(self, offset):
        """Read a 32-bit little-endian word from ``offset``."""
        self._map.seek(offset)
        return struct.unpack("=I", self._map.read(4))[0]

    def write_words(self, offset, words):
        """Write a sequence of 32-bit words starting at ``offset``."""
        self._map.seek(offset)
        self._map.write(struct.pack("<%dI" % len(words), *words))

    def read_words(self, offset, count):
        """Read ``count`` 32-bit words starting at ``offset``."""
        self._map.seek(offset)
        return struct.unpack("<%dI" % count, self._map.read(count * 4))

    def close(self):
        self._map.close()


class AxiDma:
    """User-space driver for a single AXI DMA IP in direct register mode.

    Parameters mirror the addresses defined in the Vivado block design
    (Address Editor) and the buffers reserved in the device tree:

    * ``base_address`` / ``size``: AXI-Lite control register block of the IP.
    * ``src_address``: reserved DDR buffer read by the MM2S channel.
    * ``dst_address``: reserved DDR buffer written by the S2MM channel.
    * ``buffer_size``: size mapped for each DDR buffer.
    """

    def __init__(
        self,
        base_address=0x80000000,
        size=0x10000,
        src_address=0x0E000000,
        dst_address=0x0F000000,
        buffer_size=0x10000,
    ):
        self._src_address = src_address
        self._dst_address = dst_address

        self._fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self.regs = AxiRegisterBlock(self._fd, base_address, size)
        self.src = AxiRegisterBlock(self._fd, src_address, buffer_size)
        self.dst = AxiRegisterBlock(self._fd, dst_address, buffer_size)

    # -- low-level channel control -----------------------------------------

    def reset(self):
        """Soft-reset both channels."""
        self.regs.write(MM2S_CONTROL_REGISTER, RESET_DMA)
        self.regs.write(S2MM_CONTROL_REGISTER, RESET_DMA)

    def halt(self):
        """Halt both channels."""
        self.regs.write(MM2S_CONTROL_REGISTER, HALT_DMA)
        self.regs.write(S2MM_CONTROL_REGISTER, HALT_DMA)

    def enable_interrupts(self, mask=ENABLE_ALL_IRQ):
        """Enable the IOC/delay/error interrupts on both channels."""
        self.regs.write(MM2S_CONTROL_REGISTER, mask)
        self.regs.write(S2MM_CONTROL_REGISTER, mask)

    def configure(self):
        """Reset, halt, enable interrupts and program the buffer addresses."""
        self.reset()
        self.halt()
        self.enable_interrupts()
        self.regs.write(MM2S_SRC_ADDRESS_REGISTER, self._src_address)
        self.regs.write(S2MM_DST_ADDRESS_REGISTER, self._dst_address)

    def run(self):
        """Start both channels. They stay idle until a length is written."""
        self.regs.write(MM2S_CONTROL_REGISTER, RUN_DMA)
        self.regs.write(S2MM_CONTROL_REGISTER, RUN_DMA)

    # -- status ------------------------------------------------------------

    def mm2s_status(self):
        """Return the MM2S status register decoded as a string."""
        status = self.regs.read(MM2S_STATUS_REGISTER)
        return DMA_STATUS[status & 0x4FFF]

    def s2mm_status(self):
        """Return the S2MM status register decoded as a string."""
        status = self.regs.read(S2MM_STATUS_REGISTER)
        return DMA_STATUS[status & 0x4FFF]

    def s2mm_done(self):
        """True once the S2MM channel signals idle or interrupt-on-complete."""
        status = self.regs.read(S2MM_STATUS_REGISTER)
        return bool(status & (IOC_IRQ_FLAG | IDLE_FLAG))

    # -- data movement -----------------------------------------------------

    def write_src(self, words):
        """Write a list of 32-bit words into the source buffer."""
        self.src.write_words(0, words)

    def read_dst(self, count):
        """Read ``count`` 32-bit words from the destination buffer."""
        return self.dst.read_words(0, count)

    def transfer(self, words, timeout=1.0):
        """Run a full loopback transfer and return the received words.

        Writes ``words`` to the source buffer, arms the S2MM (receiver) and
        triggers the MM2S (transmitter) by programming the transfer length,
        then waits for completion and reads the destination buffer back.
        """
        nbytes = len(words) * 4

        self.write_src(words)

        # Arm the receiver before triggering the transmitter.
        self.regs.write(S2MM_BUFF_LENGTH_REGISTER, nbytes)
        self.regs.write(MM2S_TRNSFR_LENGTH_REGISTER, nbytes)

        deadline = time.time() + timeout
        while not self.s2mm_done():
            if time.time() > deadline:
                raise TimeoutError(
                    "S2MM did not complete (status: %s)" % self.s2mm_status()
                )
            time.sleep(0.001)

        return self.read_dst(len(words))

    # -- lifecycle ---------------------------------------------------------

    def close(self):
        self.regs.close()
        self.src.close()
        self.dst.close()
        os.close(self._fd)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
