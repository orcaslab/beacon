//===- Passes.td - Beacon passes ----------------------------*- tablegen -*-===//
//
// Part of the Beacon project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// Beacon maps an elaborated CIRCT netlist (HW, Comb, Seq) to a target's cells
// (beacon-map) and counts the logic levels of every end point
// (beacon-estimate-timing) and its area (beacon-estimate-area); the other
// passes prepare a netlist for the mapping.
//
//===----------------------------------------------------------------------===//

#ifndef BEACON_PASSES
#define BEACON_PASSES

include "mlir/Pass/PassBase.td"

def LowerEquality : Pass<"beacon-lower-equality", "::circt::hw::HWModuleOp"> {
  let summary = "Lower equality comparisons to logic";
  let description = [{
    Rewrites `comb.icmp` of equality and inequality as the OR of the bits
    of the operands' XOR, so that LUT mapping packs a comparison with the
    logic around it, as Vivado does. Order comparisons stay: they are carry
    chains, not LUT logic.
  }];
  let dependentDialects = ["circt::comb::CombDialect"];
}

def KeepModules : Pass<"beacon-keep-modules", "::mlir::ModuleOp"> {
  let summary = "Keep the instances of named modules through inlining";
  let description = [{
    Makes the modules named in `modules` public, so that
    `hw-flatten-modules`, which inlines only private modules, keeps their
    instances. A name also matches its specializations (`VX_elastic_buffer`
    matches `VX_elastic_buffer_12`). Run before `beacon-map` to read
    the depth at those instances' ports (`beacon-estimate-timing`'s
    `ports`); LUTs then no longer pack across their boundaries.
  }];
  let options = [
    ListOption<"modules", "modules", "std::string",
               "Modules whose instances to keep">,
  ];
}

def BoundaryArrivals : Pass<"beacon-boundary-arrivals", "::mlir::ModuleOp"> {
  let summary = "Give each module its hierarchy's arrival times at its ports";
  let description = [{
    Reads `file`, the `arrivals` of `beacon-estimate-timing` over a mapping
    of the same design, and sets `synth.input_arrival_times` on each module
    it lists (its input ports) and `synth.output_arrival_times` on each of
    their instances (their outputs), which `synth-generic-lut-mapper` with a
    `carry-block-size` takes as the arrival times of those values. A module
    mapped apart from the rest of its hierarchy then times a late input, or
    an instance's late output, as a mapping of the flattened design would,
    and keeps it out of deep cones. Ports missing from the file, or of
    another width, arrive at 0.
  }];
  let options = [
    Option<"file", "file", "std::string", "",
           "The arrival times (JSON) of beacon-estimate-timing's `arrivals`">,
  ];
}

def FoldZeroWidth : Pass<"beacon-fold-zero-width", "::circt::hw::HWModuleOp"> {
  let summary = "Replace zero-width combinational values with constants";
  let description = [{
    A zero-width value carries no information, so every combinational op
    whose results are all zero-width integers is replaced by the constant 0
    of that width. Elaborated designs keep such ops (a `comb.mux` of `i0`
    from a parameter that sizes a field to nothing), and lowerings that
    expect a bit to exist do not handle them.
  }];
  let dependentDialects = ["circt::hw::HWDialect"];
}

def LowerConstantDivision
    : Pass<"beacon-lower-constant-division", "::circt::hw::HWModuleOp"> {
  let summary = "Lower unsigned division by a constant as synthesis does";
  let description = [{
    Rewrites `comb.divu` and `comb.modu` by a constant the way synthesis
    propagates it:

    - by a power of two 2^k: the bits above k and the low k bits,
      zero-extended;
    - of a dividend with at most 10 significant bits (the rest zero, as a
      zero-extended index): a table of the results, selected by the dividend
      with a tree of `comb.mux`, which LUT mapping folds into logic.

    Any other division stays.
  }];
  let dependentDialects = ["circt::comb::CombDialect", "circt::hw::HWDialect"];
}

def BeaconMap : Pass<"beacon-map", "::mlir::ModuleOp"> {
  let summary = "Map a design to a Beacon target's cells";
  let description = [{
    Maps a design to the cells of `target` (beacon/Beacon.h) for
    `beacon-estimate-timing` with the same `target`: 6-input LUTs on an FPGA
    family, 3-input cuts, each one library cell, on an ASIC PDK. Modules of
    fewer than 300 ops are inlined so cells pack across their boundaries, as
    Vivado maps a flattened design; the mapping is depth-oriented, as Vivado
    restructures chains of associative logic and priority muxes into trees;
    equality comparisons become logic so they pack with their neighbours;
    adders, order comparisons, multipliers and dividers stay word-level, as
    carry chains and DSPs.

    With `area`, the netlist is mapped for `beacon-estimate-area` instead:
    every module kept, so each instance's resources stay its own (inlining
    changes the count by a few percent); no balancing, common subexpressions
    shared, each cone covered by the fewest LUTs, as Vivado maps the logic
    off its critical paths. A depth-oriented mapping duplicates logic to save
    levels, so its cells are no measure of area.

    For depth, `carry-block-size` makes the mapper time the bits of adders,
    comparators and distributed-RAM reads as the carry blocks of that many
    bits deliver them, so a late carry is not buried under a deep cone as
    Vivado would not bury it (0: every input bit arrives at 0). Each module
    left after inlining (a fold's models) is mapped apart, its input ports and
    its instances' outputs arriving at 0 unless `arrivals` gives them:
    `beacon-estimate-timing`'s `arrivals` over a first mapping, so a second
    one times them as the whole hierarchy delivers them (as Vivado maps the
    flattened design; `beacon-boundary-arrivals`).
  }];
  let options = [
    Option<"target", "target", "std::string", "\"versal\"",
           "The Beacon target to map to">,
    Option<"area", "area", "bool", "false",
           "Map for area (beacon-estimate-area) instead of depth">,
    Option<"carryBlockSize", "carry-block-size", "unsigned", "0",
           "Bits per carry block the depth mapping times adders and "
           "comparators with (0: their input bits arrive at 0)">,
    Option<"arrivals", "arrivals", "std::string", "\"\"",
           "Arrival times at the modules' ports, from beacon-estimate-timing's "
           "`arrivals` over a first mapping (JSON); none: they arrive at 0">,
  ];
}

def EstimateTiming : Pass<"beacon-estimate-timing", "::mlir::ModuleOp"> {
  let summary = "Rank every register-to-register path by its logic levels";
  let description = [{
    Estimates, for every end point of the design elaborated from `top`, the
    logic levels of its longest path the way Vivado counts them on the
    `target` device: LUTs, CARRY8s, DSP cells and distributed-RAM reads. The input is a design mapped by
    `beacon-map` for the same `target`, in which
    `comb.truth_table` ops are LUTs and adders, subtracters, comparators,
    multipliers and dividers are still word-level ops:

    - `comb.truth_table`: one level.
    - `comb.add`/`comb.sub`: a LUT and the CARRY8s from an input bit's
      8-bit block to the output bit's.
    - `comb.icmp` of order: a LUT and the CARRY8s from the input bit to the
      most significant one. Of equality: a LUT6 tree over the bits.
    - `comb.mul`: a table in LUTs if its operands have at most ten variable
      bits; otherwise a cascade of DSPs over their significant bits (27 x 24
      each on the V80, 27 x 18 on the U55C): the cells up to the first
      product, then each further DSP. A register the product feeds, with
      its enable logic, is the DSP's own; any other load adds the DSP's
      output stage.
    - `comb.divu`/`divs`/`modu`/`mods`: a table over the bits the operands
      depend on if there are at most ten of them; otherwise a ripple of one
      subtracter per quotient bit, from every input bit to every output bit.
    - `seq.firmem` read of latency 0 (distributed RAM): one level, plus one
      per doubling of the depth past 64 words, from the address.

    `target` is a Beacon target (beacon/Beacon.h): an FPGA family as
    hw/syn/xilinx/platforms names it (`versal`, `virtexuplus`,
    `virtexuplusHBM`, `zynquplus`, which differ in their DSPs), or the PDK
    of hw/syn/yosys.

    `target=asap7` counts cell stages of an ASAP7 netlist (3-input cuts)
    instead: a cell
    (`comb.truth_table`, or an and-inverter gate) is a stage; an adder bit from bit j to
    bit i is a prefix adder, 2 + ceil(log2(i - j + 1)); a compare,
    1 + ceil(log2(span)); a product, its partial products, a Dadda tree of
    full adders and a prefix adder; a memory of flops, a decoder and a
    multiplexer tree. The report converts the deepest path to a period with
    the library's cell, clock-to-output and setup delays and a per-path
    overhead fitted on OpenSTA.

    End points are register inputs (data, reset, clock enable), the address,
    data, enable and mask of memory writes, the address and enable of
    registered memory reads, the inputs of external modules (vendor IPs) and
    the outputs of `top`. Start points are registers, registered memory
    reads, outputs of external modules and inputs of `top`.

    Paths are computed per bit over the elaborated instances, keeping only
    the worst input of each bit, so time and memory grow linearly with the
    design. Writes one row per end point bit of at least `min-levels` levels
    to `output` (levels, retimed levels, end kind, maximum fanout and module
    crossings on the path, then start and end as an instance id and a name,
    and the largest fanout of a bit into the end point's last cell),
    the instances to `instances` (id, parent, instance name, module), and
    the `top-paths` deepest paths, op by op, to `report`. Leaves the design
    as it is.

    `paths` lists every deep path once per pair of start and end registers,
    as JSON lines, deepest first: the pair's deepest bit, if it has at least
    `path-levels` levels, with its levels, retimed levels, end kind, start
    and end (instance id and name), the number of end point bits the pair
    has, and `ops`, the path from the end point back to its start as
    [levels, kind, fanout, instance id, name, source]: the source is the
    first file:line of the op's location, which names mapped logic.

    With `retime-levels`, the `retimed` column estimates the depth after
    Vivado's global retiming to a period of that many levels: an end point
    deeper than the period borrows the room of its neighbour stages, its
    register moving backward and its start register forward, neither
    through a distributed RAM or a DSP. Top ports and the pins of
    `black-boxes` count as registers with empty neighbour stages, as a gate
    harness registers them. Without it, `retimed` is the depth.

    `arrivals` writes, for every module instantiated below `top`, the levels
    at which each bit of its ports arrives, the worst over its instances: an
    input's in the instantiating module, an output's behind it. It is JSON,
    `{"<module>": {"in": {"<port>": [levels per bit]}, "out": {...}}}`, a
    single element when every bit has it, and no entry for a port whose bits
    all arrive at 0. It is what
    `beacon-boundary-arrivals` reads to map each module again with its
    hierarchy's arrival times at its boundary.
  }];
  let options = [
    Option<"topName", "top", "std::string", "",
           "The top module (the one no other instantiates if empty)">,
    Option<"output", "output", "std::string", "",
           "End point table (CSV)">,
    Option<"instances", "instances", "std::string", "",
           "Instance table (CSV)">,
    Option<"report", "report", "std::string", "\"-\"",
           "Summary and deepest paths (text)">,
    Option<"minLevels", "min-levels", "unsigned", "0",
           "Only write end points of at least this many levels">,
    Option<"topPaths", "top-paths", "unsigned", "50",
           "Deepest paths to print op by op">,
    ListOption<"trace", "trace", "std::string",
               "Also print the deepest path of each end point whose "
               "instance path and name contain one of these">,
    ListOption<"blackBoxes", "black-boxes", "std::string",
               "Modules to treat as external, as a harness stubs them">,
    Option<"target", "target", "std::string", "\"versal\"",
           "The Beacon target whose logic levels to count">,
    Option<"ports", "ports", "std::string", "",
           "Port depth table (CSV) of the instances of `port-modules`">,
    ListOption<"portModules", "port-modules", "std::string",
               "Modules (and their specializations) whose instances' ports "
               "to report: the levels at each input and behind each output">,
    Option<"crossings", "crossings", "std::string", "",
           "Crossing table (CSV) between partitions: each instance of "
           "`port-modules` (an SLR group's module, say) and the rest of the "
           "design. One row per cell bit read across (wires through a port "
           "are no crossing): the levels from its start to it (driver side) "
           "and from it to the end points it reaches in the reading "
           "partition (load side), with its start and that end point">,
    Option<"paths", "paths", "std::string", "",
           "Deep paths, op by op, one per pair of start and end registers "
           "(JSON lines)">,
    Option<"pathLevels", "path-levels", "unsigned", "12",
           "Only write the paths of pairs whose deepest bit has at least "
           "this many levels">,
    Option<"retimeLevels", "retime-levels", "unsigned", "0",
           "Also estimate each end point's depth after Vivado's retiming "
           "to a period of this many levels (0: off)">,
    Option<"arrivals", "arrivals", "std::string", "",
           "Per-bit arrival times at every instantiated module's ports, "
           "the worst over its instances (JSON)">,
  ];
  let dependentDialects = [
    "circt::hw::HWDialect",
    "circt::comb::CombDialect",
    "circt::seq::SeqDialect"
  ];
}

def EstimateArea : Pass<"beacon-estimate-area", "::mlir::ModuleOp"> {
  let summary = "Count the cells, memories and DSPs a design maps to";
  let description = [{
    Estimates the resources of the design elaborated from `top` on the
    `target` device, per instance: the input is a design mapped by
    `beacon-map` for the same `target`, as for
    `beacon-estimate-timing`, so both read one netlist.

    On an FPGA family:

    - `comb.truth_table`: a LUT. An and-inverter gate the mapper left: a
      LUT per five more inputs.
    - `comb.add`/`comb.sub`: a LUT per bit into the carry chain; an order
      `comb.icmp`, a LUT per two bits.
    - `comb.mul`: times a constant, shifts and adders, a LUT per bit; a
      table in LUTs, one per output bit up to the inputs' count, if its
      operands have at most ten variable bits; otherwise DSPs, the fewer of
      the two orientations over the significant bits of the operands
      (27 x 24 on Versal, 27 x 18 on UltraScale+).
    - dividers: a table over at most ten variable bits, else a subtracter
      per quotient bit; as many output bits as the dividend has.
    - registers: a flip-flop per bit.
    - `seq.firmem`: placed as Vivado infers it. A `ram_style` the RTL asks
      for (`block`, `distributed`, `ultra`, `registers`) wins; otherwise a
      read of latency 0 is distributed RAM, and a registered read of at
      least 32 words and 2 Kb is block RAM. Distributed RAM takes 0.57 LUTs
      a bit up to 32 words (RAM32M16) and 1.14 per 64 words beyond (RAM64M8)
      per read port, with a multiplexer tree over the 64-word LUTs. Block
      RAM takes the fewest 36 Kb blocks (or 18 Kb halves) of any shape, a
      copy per two ports (a read-write port is one); UltraRAM, 4096 x 72.
    - Vivado floating-point cores (`xil_*`, external modules): their
      measured LUTs, flip-flops and DSPs.

    On an ASIC library (`asap7`), `lut` counts cells: a 3-input cut is a
    cell, an adder a prefix adder, a product partial products and a Dadda
    tree, a memory without a macro flip-flops and multiplexers; an external
    module (an SRAM macro) counts as a macro.

    The mapping's LUTs are a cut mapper's cover: every output scales them by
    the target's `lutScale` (`lut-scale` overrides it), fitted on routed
    Vivado builds, and adds the memories' read multiplexers and vendor IP's
    measured LUTs, which no mapper covers.

    Writes the instances depth first to `output` (CSV: the instance's own
    resources and its subtree's), the memories and their placement to
    `memories`, each module's totals to `modules` (JSON), and a summary to
    `report`. With `state-threads`, `modules` also gives, per module and
    thread count, the distributed and block RAM of its registers (one memory
    of a word per thread) and of its memories that many times deeper, as a
    folded copy stores its instances' state. Leaves the design as it is.
  }];
  let options = [
    Option<"topName", "top", "std::string", "",
           "The top module (the one no other instantiates if empty)">,
    Option<"target", "target", "std::string", "\"versal\"",
           "The Beacon target whose cells to count">,
    Option<"output", "output", "std::string", "",
           "Instance table (CSV)">,
    Option<"memoryTable", "memories", "std::string", "",
           "Memory table (CSV)">,
    Option<"moduleTable", "modules", "std::string", "",
           "Per-module totals (JSON)">,
    Option<"report", "report", "std::string", "\"-\"",
           "Summary (text)">,
    ListOption<"blackBoxes", "black-boxes", "std::string",
               "Modules to leave out, as a harness stubs them">,
    ListOption<"stateThreads", "state-threads", "unsigned",
               "Thread counts at which `modules` prices folded state">,
    Option<"lutScale", "lut-scale", "double", "-1",
           "Device LUTs per mapped LUT in `output` and `report` (default: "
           "the target's lutScale)">,
  ];
  let dependentDialects = [
    "circt::hw::HWDialect",
    "circt::comb::CombDialect",
    "circt::seq::SeqDialect"
  ];
}

#endif // BEACON_PASSES
