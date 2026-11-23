# Beacon: logic depth, area and timing screens before synthesis

Beacon counts the logic levels of every register-to-register path of an RTL
design as Vivado counts them on an FPGA family (LUTs, carry chains, DSP cells,
distributed-RAM reads), or in cell stages for an ASIC library (ASAP7). It also
counts the LUTs, flip-flops, distributed and block RAM, UltraRAM and DSPs the
design maps to. It reads any elaborated [CIRCT](https://github.com/orcaslab/circt)
netlist (HW, Comb, Seq) and takes seconds to minutes. It names the RTL units
whose paths are too deep for the target clock, so they can be fixed before a
synthesis run.

It is used by:

- [Vortex](https://github.com/vortexgpgpu/vortex): its DUT catalog is screened
  on every RTL change (`ci/logic_gate.py`, `make -C hw/syn/xilinx/dut <dut>-beacon`).
- [FoldSim](https://github.com/orcaslab/foldsim): FoldSim links Beacon's
  passes. It sizes a fold's pipeline stages by mapped depth
  (`stage-levels-mapped`), prices a class's area (`--degree-estimator
  beacon-area`), and screens a fold's SLR crossings (`beacon-estimate-timing
  crossings=`).

The design document is [docs/beacon.md](docs/beacon.md).

## What is here

| path | what |
|---|---|
| `lib/Beacon/Beacon.cpp` | `beacon-map`: maps a netlist to a target's cells (CIRCT Synth's cut-based LUT mapper, carry blocks, boundary arrivals) |
| `lib/Beacon/EstimateTiming.cpp` | `beacon-estimate-timing`: levels of every end point, paths, ports, SLR crossings. Also the mapping's preparation passes `beacon-lower-equality`, `beacon-lower-constant-division`, `beacon-fold-zero-width`, `beacon-keep-modules` and `beacon-boundary-arrivals` |
| `lib/Beacon/EstimateArea.cpp` | `beacon-estimate-area`: LUTs, flip-flops, memories and DSPs per instance |
| `include/beacon/` | targets (`Beacon.h`: versal, virtexuplus, virtexuplusHBM, zynquplus, asap7), memory pin loads (`MemoryLoads.h`), pass declarations |
| `tools/beacon-opt/` | `beacon-opt`, the passes with CIRCT's |
| `tools/beacon/beacon.py` | `beacon`, the driver: `run`, `summary`, `summarize`, `paths`, `diff`, `validate`, `ports`, `buffers`, `calibrate`, `score`, `ties`, `asic-summary`, `version` |
| `tools/beacon/beacon_summary.py` | the gate's tables (`beacon summary`) |
| `tools/beacon/beacon_terms.py` | routing terms per end point: levels, fanout and SLR crossings, fitted on placed builds |
| `tools/beacon/db/` | the path database: `xilinx_path_export.tcl` exports a Vivado checkpoint's paths, and `beacon_db.py` ingests them and refits the models |
| `tools/beacon/calibrations/` | V80 timing terms and SLR regions, the U55C learned calibration |
| `examples/mac/` | the quick-start demo: a multiply-accumulate screened before and after pipelining |
| `test/` | lit tests (`check-beacon`) |

## Quick start: build and run a demo

Beacon builds against [orcaslab/circt](https://github.com/orcaslab/circt)
`main`, which carries the CIRCT changes it uses: the Synth `LongestPathAnalysis`,
the LUT mapper's arrival times and carry blocks, `LowerWordToBits`, and the
`circt-verilog` front end. Steps 1 to 4 build that CIRCT; skip them if you
already have a build of it (step 4 of its README). Tested on Ubuntu 24.04
(x86-64) with clang 18, CMake 3.31, Ninja 1.11 and Python 3.12.

The CIRCT commit this Beacon was tested with is recorded as the submodule
`third_party/circt`; `git submodule status` prints it. The build does not read
the submodule: it takes CIRCT from `-DCIRCT_DIR`, so the steps below work on a
plain clone. To build exactly that commit, check it out in step 2
(`git -C circt checkout <commit>`). Do not clone Beacon with `--recursive`:
that fetches CIRCT and its LLVM submodule as well.

**1. Prerequisites.**

```sh
sudo apt-get install -y git cmake ninja-build clang lld python3 ccache
```

CMake 3.20 or newer, clang 16+ or GCC 11+, and Python 3 for the `beacon`
driver, which is tested with 3.12. `beacon calibrate` and `beacon score` also
need `numpy` and `scikit-learn`. Without `lld`, set `LINKER=gold` in step 3.

**2. Clone CIRCT and its LLVM.**

```sh
mkdir -p ~/orcaslab && cd ~/orcaslab
git clone https://github.com/orcaslab/circt.git
cd circt
git submodule update --init --depth 1 llvm
```

**3. Build LLVM and MLIR.**

```sh
J=$(nproc); LINKER=lld
cmake -G Ninja -S llvm/llvm -B llvm/build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ -DLLVM_USE_LINKER=$LINKER \
  -DLLVM_ENABLE_PROJECTS=mlir -DLLVM_TARGETS_TO_BUILD=host \
  -DLLVM_ENABLE_ASSERTIONS=ON -DLLVM_INSTALL_UTILS=ON -DLLVM_CCACHE_BUILD=ON
ninja -C llvm/build -j$J
```

**4. Build CIRCT** with its SystemVerilog front end (`circt-verilog`).

```sh
cmake -G Ninja -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ -DLLVM_USE_LINKER=$LINKER \
  -DMLIR_DIR=$PWD/llvm/build/lib/cmake/mlir -DLLVM_DIR=$PWD/llvm/build/lib/cmake/llvm \
  -DLLVM_EXTERNAL_LIT=$PWD/llvm/build/bin/llvm-lit \
  -DCIRCT_SLANG_FRONTEND_ENABLED=ON -DLLVM_ENABLE_ASSERTIONS=ON -DLLVM_CCACHE_BUILD=ON \
  -DCMAKE_CXX_SCAN_FOR_MODULES=OFF   # slang's C++20 module scan needs clang-scan-deps
ninja -C build -j$J
export CIRCT=$PWD LLVM_BUILD=$PWD/llvm/build
export PATH=$CIRCT/build/bin:$PATH
```

**5. Build Beacon** as an out-of-tree MLIR project, and run its tests.

```sh
cd ~/orcaslab
git clone https://github.com/orcaslab/beacon.git
cd beacon
cmake -G Ninja -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ -DLLVM_USE_LINKER=$LINKER \
  -DCIRCT_DIR=$CIRCT/build/lib/cmake/circt \
  -DMLIR_DIR=$LLVM_BUILD/lib/cmake/mlir -DLLVM_DIR=$LLVM_BUILD/lib/cmake/llvm \
  -DLLVM_EXTERNAL_LIT=$LLVM_BUILD/bin/llvm-lit
ninja -C build                     # build/bin: beacon-opt, beacon and its tools
ninja -C build check-beacon        # lit tests
export PATH=$PWD/build/bin:$PATH
```

To install instead: `cmake --install build --prefix <prefix>` puts `beacon`,
`beacon-opt`, the helper scripts and the calibrations in `<prefix>/bin`.

**6. Run the demo.** [`examples/mac`](examples/mac) holds `mac.sv`, a
multiply-accumulate whose product and add share one cycle (`PIPE=0`), or take
one cycle each (`PIPE=1`). `demo.sh` screens both for the Versal family:

```sh
examples/mac/demo.sh               # writes examples/mac/out/
```

Expected output:

```
PIPE=0: end points by levels: 0:73 9:3 10:5 11:8 12:8 13:8 14:8
        mac on versal: 27 LUTs (27 logic, 0 distributed RAM), 73 flip-flops, 0.0 block RAMs, 0 UltraRAMs, 1 DSPs
PIPE=1: end points by levels: 0:74 1:34 2:1 3:5 4:8 5:8 6:8 7:8
        mac on versal: 27 LUTs (27 logic, 0 distributed RAM), 106 flip-flops, 0.0 block RAMs, 0 UltraRAMs, 1 DSPs

Deepest paths of PIPE=0 (12 levels or more is a timing risk at 300 MHz on Versal):
...
== <top>: 14 levels, 1 paths

  14 levels, 24 d bits
  from a_q[0]
  to   acc.d[32]
      14 lut    fo 0     mac.sv:10                comb.truth_table[0]
      13 carry  fo 1     mac.sv:35                comb.add[32]
       1 dsp    fo 1     mac.sv:30                comb.mul[0]
       0 reg    fo 32    mac.sv:12                a_q[0]
```

How to read it:
- **`end points by levels`** is a histogram: `14:8` means 8 end-point bits sit
  14 logic levels from their nearest registers.
- **`PIPE=0`**: 24 bits of `acc` reach 12 to 14 levels through the DSP
  multiplier, a 13-level carry chain for the add, and a LUT. On a V80 at
  300 MHz, 12 levels or more is the fix list.
- **`PIPE=1`**: registering the product halves the depth to 7 levels. It costs
  33 flip-flops and no LUTs.
- **`paths`** walks the path op by op, back to `mac.sv` source lines. The
  lines come from `circt-verilog --mlir-print-debuginfo`.

## Use

```sh
circt-verilog --ir-hw --mlir-print-debuginfo --top=Top design.sv -o design.mlir   # or any HW/Comb/Seq netlist
beacon run design.mlir runs/top top=Top target=versal      # map, count levels and area
beacon summarize runs/top --min-levels 12                  # deep end points by RTL unit
beacon paths runs/top --unit lsu_unit                      # one unit's paths, op by op
beacon summary runs/top --top Top                          # the gate's tables
```

`beacon` runs the `beacon-opt` installed beside it, or the one named by
`$BEACON_OPT`. Targets: `versal`, `virtexuplus`, `virtexuplusHBM`,
`zynquplus` (FPGA LUT levels) and `asap7` (ASIC cell stages).

A project that has found MLIR and CIRCT itself can also build Beacon as a
subproject (`add_subdirectory`). FoldSim does that with its `third_party/beacon`
submodule.

## Related repositories

[orcaslab/circt](https://github.com/orcaslab/circt) ·
[orcaslab/foldsim](https://github.com/orcaslab/foldsim) ·
[orcaslab/firesim](https://github.com/orcaslab/firesim) ·
[orcaslab/slash](https://github.com/orcaslab/slash) ·
[vortexgpgpu/vortex](https://github.com/vortexgpgpu/vortex)

## License

Apache License v2.0 with LLVM Exceptions (see [LICENSE](LICENSE)), as FoldSim
and CIRCT.
