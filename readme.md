# pyxdma

A pure-Python, user-space driver for the **AMD/Xilinx AXI DMA** IP, designed to
run on PetaLinux on Zynq and Zynq UltraScale+ MPSoC devices.

The DMA (Direct Memory Access) is one of the most useful peripherals in any
embedded system: it moves data between a peripheral — or the FPGA fabric — and
DDR memory without involving the processor. In fields like video processing, AI
acceleration or SDR (Software Defined Radio), the DMA is a key building block.

This repository turns the procedural example from the article
[Creating a Python driver for the AXI DMA IP](https://controlpaths.com/2025/03/02/drivers-ip-python/)
into a small, reusable, class-based driver, plus the PetaLinux configuration
needed to use it.

## Table of contents

- [How it works](#how-it-works)
- [PetaLinux setup](#petalinux-setup)
  - [Python packages](#python-packages)
  - [Reserved memory (device tree)](#reserved-memory-device-tree)
- [The driver](#the-driver)
  - [`AxiRegisterBlock`](#axiregisterblock)
  - [`AxiDma`](#axidma)
  - [Register and status definitions](#register-and-status-definitions)
- [Usage](#usage)
- [Examples](#examples)
- [References](#references)

## How it works

The AXI DMA exposes an AXI-Lite register block that is mapped into the
processor's address space (you can find its base address in the Vivado block
design **Address Editor**). The registers are split into two channels:

- **MM2S** (memory-mapped to stream): reads from DDR and drives the AXI-Stream
  master — i.e. it sends data to the FPGA.
- **S2MM** (stream to memory-mapped): receives from the AXI-Stream slave and
  writes into DDR — i.e. it gets data from the FPGA.

Each channel has a control register (DMACR), a status register (DMASR), an
address register and a length register. Writing the length register *triggers*
the transfer.

From user space, the driver uses Python's `mmap` over `/dev/mem` to map both the
AXI-Lite register block and the DDR buffers, and `struct` to pack/unpack the
32-bit words. Because it touches physical memory directly, **the driver must run
as root**.

## PetaLinux setup

### Python packages

To run Python (and, optionally, Jupyter for interactive debugging) on the
target, add the following packages. Edit
`os/project-spec/meta-user/conf/user-rootfsconfig` and append:

```
CONFIG_packagegroup-python3-jupyter
CONFIG_python3-mmap
```

`packagegroup-python3-jupyter` brings in Python 3 and Jupyter; `python3-mmap`
adds the `mmap` module the driver relies on to map physical memory. Then enable
them in the rootfs configuration, under **user-packages**:

```shell
petalinux-config -c rootfs
```

Rebuild and deploy. To debug interactively, launch a Jupyter server on the board
(as root, headless, listening on all interfaces) and connect from your host:

```shell
zuboard-py:~$ sudo jupyter-notebook --allow-root --no-browser --ip=0.0.0.0
```

### Reserved memory (device tree)

The MM2S source buffer and the S2MM destination buffer live in DDR. The OS must
not use those ranges, so they have to be reserved in the device tree. Edit
`os/project-spec/meta-user/recipes-bsp/device-tree/files/system-user.dtsi` and
add a `reserved-memory` node:

```dts
reserved-memory {
   #address-cells = <2>;
   #size-cells = <2>;
   ranges;

   reserved: buffer@0 {
      no-map;
      reg = <0x0 0x0e000000 0x0 0x02000000>;
   };
};

reserved-mem@0 {
   compatible = "xlnx,reserved-memory";
   memory-region = <&reserved>;
};
```

This reserves the range `0x0e000000`–`0x10000000` (32 MB). The `no-map`
attribute keeps the OS from touching it. Note that on the 64-bit MPSoC,
addresses and sizes are expressed as two 32-bit cells (`#address-cells = <2>`,
`#size-cells = <2>`). Adjust the base address and size to match your design, and
pass the same source/destination addresses to the driver.

## The driver

The driver lives in a single module, [`pyxdma.py`](pyxdma.py), so it can simply
be copied to the board. It exposes two classes.

### `AxiRegisterBlock`

A thin wrapper around a single `mmap` of a physical address range. It is used
both for the DMA control registers and for the reserved DDR buffers, and it
provides generic AXI access that is not specific to the DMA — the same class
could drive any other AXI-Lite peripheral.

| Method | Description |
|---|---|
| `write(offset, value)` | Write a 32-bit word at `offset`. |
| `read(offset)` | Read a 32-bit word from `offset`. |
| `write_words(offset, words)` | Write a sequence of 32-bit words. |
| `read_words(offset, count)` | Read `count` 32-bit words. |
| `close()` | Release the mapping. |

### `AxiDma`

The high-level driver for one AXI DMA IP, in *direct register mode* (no
scatter-gather). The constructor opens `/dev/mem` and maps the control block and
both DDR buffers:

```python
AxiDma(
    base_address=0x80000000,  # AXI-Lite control block (Vivado Address Editor)
    size=0x10000,             # control block size
    src_address=0x0E000000,   # reserved DDR buffer read by MM2S
    dst_address=0x0F000000,   # reserved DDR buffer written by S2MM
    buffer_size=0x10000,      # size mapped for each DDR buffer
)
```

| Method | Description |
|---|---|
| `configure()` | Reset, halt, enable interrupts and program the source/destination addresses. |
| `reset()` / `halt()` | Soft-reset / halt both channels. |
| `enable_interrupts(mask)` | Enable IOC/delay/error interrupts (defaults to all). |
| `run()` | Start both channels (they stay idle until a length is written). |
| `mm2s_status()` / `s2mm_status()` | Return the decoded status string of each channel. |
| `s2mm_done()` | `True` once S2MM signals idle or interrupt-on-complete. |
| `write_src(words)` / `read_dst(count)` | Write/read the DDR buffers directly. |
| `transfer(words, timeout=1.0)` | Run a full loopback transfer and return the received words. |
| `close()` | Release all mappings and close `/dev/mem`. |

`AxiDma` is also a context manager, so mappings are released automatically:

```python
with AxiDma() as dma:
    ...
```

### Register and status definitions

The module exposes the register offsets, the DMACR control bits and the DMASR
flags as module-level constants (`MM2S_CONTROL_REGISTER`, `RUN_DMA`,
`ENABLE_ALL_IRQ`, …), taken from the
[AXI DMA Product Guide (PG021)](https://docs.amd.com/r/en-US/pg021_axi_dma). The
`DMA_STATUS` dictionary maps the status register to a verbose, human-readable
string.

## Usage

A minimal loopback test (the MM2S output is wired back to the S2MM input in the
FPGA design):

```python
from pyxdma import AxiDma

with AxiDma(
    base_address=0x80000000,
    src_address=0x0E000000,
    dst_address=0x0F000000,
) as dma:
    dma.configure()
    dma.run()

    data = [0xDEADBEEF, 0x33001111, 0x22223333]
    received = dma.transfer(data)

    for word in received:
        print(hex(word))
```

Run it as root on the target:

```shell
zuboard-py:~$ sudo python3 loopback.py
```

If you prefer the step-by-step flow (write data, arm the receiver, trigger the
transmitter, poll the status, read back), you can call the lower-level methods
directly instead of `transfer()`.

## Examples

The [`examples/`](examples/) folder contains ready-to-run scripts for different
boards (for instance the **ZUBoard**), each with its own addresses and the
matching device-tree snippet.

## References

- [Creating a Python driver for the AXI DMA IP](https://controlpaths.com/2025/03/02/drivers-ip-python/) — the original article on controlpaths.com.
- [AXI DMA LogiCORE IP Product Guide (PG021)](https://docs.amd.com/r/en-US/pg021_axi_dma).
