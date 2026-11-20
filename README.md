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
| `test/` | lit tests (`check-beacon`) |

## Build

Beacon builds against an installed or built CIRCT, as an out-of-tree MLIR
project. **CIRCT version:** [orcaslab/circt](https://github.com/orcaslab/circt)
`main` (`313868c23`), whose LLVM is its `llvm` submodule's commit.
Beacon uses its Synth changes (`LongestPathAnalysis`, the LUT mapper's
arrival times, `LowerWordToBits`) and its `circt-verilog` front end.

```sh
cmake -G Ninja -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DCIRCT_DIR=<circt build>/lib/cmake/circt \
  -DMLIR_DIR=<llvm build>/lib/cmake/mlir -DLLVM_DIR=<llvm build>/lib/cmake/llvm \
  -DLLVM_EXTERNAL_LIT=<llvm build>/bin/llvm-lit
ninja -C build                   # bin/beacon-opt, bin/beacon and its tools
ninja -C build check-beacon      # lit tests
cmake --install build --prefix <prefix>
```

A project that has found MLIR and CIRCT itself can also build Beacon as a
subproject (`add_subdirectory`). FoldSim does that with its `third_party/beacon`
submodule.

## Use

```sh
circt-verilog --ir-hw --top=Top design.sv -o design.mlir   # or any HW/Comb/Seq netlist
beacon run design.mlir runs/top top=Top target=versal      # map, count levels and area
beacon summarize runs/top --min-levels 12                  # deep end points by RTL unit
beacon paths runs/top --unit lsu_unit                      # one unit's paths, op by op
beacon summary runs/top --top Top                          # the gate's tables
```

`beacon` runs the `beacon-opt` installed beside it, or the one named by `$BEACON_OPT`.

## Related repositories

[orcaslab/circt](https://github.com/orcaslab/circt) ·
[orcaslab/foldsim](https://github.com/orcaslab/foldsim) ·
[orcaslab/firesim](https://github.com/orcaslab/firesim) ·
[orcaslab/slash](https://github.com/orcaslab/slash) ·
[vortexgpgpu/vortex](https://github.com/vortexgpgpu/vortex)

## License

Apache License v2.0 with LLVM Exceptions (see [LICENSE](LICENSE)), as FoldSim
and CIRCT.
