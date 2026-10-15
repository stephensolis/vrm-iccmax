vrm-iccmax
==========

This is a script to raise the ICC_MAX current limit of some PMBus-connected VRM controllers to 255 A through an MCP2221A USB-I2C adapter. It's mainly useful for allowing you to use 210W TDP OEM 2nd gen Xeon Scalable CPUs (eg. 8259CL and 8272CL) on systems limited to 205W by default (eg. Dell 14th Generation PowerEdge, HPE ProLiant Gen10, Cisco UCS M5, and generally most LGA3647 socket motherboards).

Supported controllers are: PXE1610C, MP2955A, TPS53679, TPS53678 (but note I have only tested on a PXE1610C with physical hardware).

It is a Python port of MCP2221a_iccmax_FF by RolloZ170 with various safety/usability improvements, from this thread: [https://forums.servethehome.com/index.php?threads/vrm-modify-icc_max-to-run-high-tdc-oem-cpu.38686/](https://forums.servethehome.com/index.php?threads/vrm-modify-icc_max-to-run-high-tdc-oem-cpu.38686/).

Usage
-----

It's a single-file Python script following PEP723, so if you have `uv` installed, just download `vrm_iccmax.py` and run it with `uv run vrm_iccmax.py`.

There are 2 modes, `scan` and `set`:

```
usage: vrm_iccmax.py scan [-h] [-d N] [--speed HZ] [-v | -q] [START] [END]

Identify PMBus controllers.

positional arguments:
  START             first address (default 0x03) (if only start given, scan only this address)
  END               last address (default 0x77)

options:
  -h, --help        show this help message and exit
  -d N, --device N  MCP2221A index (default 0)
  --speed HZ        I2C clock, 47000-400000 (default 400000)
  -v, --verbose     log every I2C transaction
  -q, --quiet       only print results, warnings, and errors
```

```
usage: vrm_iccmax.py set [-h] [-d N] [--speed HZ] [-v | -q] [-n] [-y] CHIP ADDR [ADDR ...]

Set ICC_MAX to 255 A and commit it to NVM.

positional arguments:
  CHIP              one of: pxe1610c, mp2955a, tps53679, tps53678
  ADDR

options:
  -h, --help        show this help message and exit
  -d N, --device N  MCP2221A index (default 0)
  --speed HZ        I2C clock, 47000-400000 (default 400000)
  -v, --verbose     log every I2C transaction
  -q, --quiet       only print results, warnings, and errors
  -n, --dry-run     only report what would change, no permanent writes
  -y, --yes         commit without asking for confirmation
```

Tests
-----

There is a fairly comprehensive test suite, run it with: `uv run --with pytest,EasyMCP2221 pytest`

License ![License](http://img.shields.io/:license-mit-blue.svg)
-------

    The MIT License (MIT)

    Copyright (c) 2026 Stephen

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
