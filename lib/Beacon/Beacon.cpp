//===- Beacon.cpp - The targets Beacon maps to and times on ---------------===//
//
// Part of the Beacon project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include "beacon/Beacon.h"
#include "beacon/Passes.h"
#include "circt/Dialect/HW/HWInstanceGraph.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/Pass/PassManager.h"
#include "mlir/Support/FileUtilities.h"
#include "llvm/Support/ToolOutputFile.h"
#include "mlir/Pass/PassRegistry.h"
#include "llvm/ADT/PostOrderIterator.h"
#include <string>

namespace beacon {
#define GEN_PASS_DEF_BEACONMAP
#define GEN_PASS_DEF_INLINESHARED
#include "beacon/Passes.h.inc"
} // namespace beacon

using namespace mlir;
using beacon::BeaconTarget;

namespace {

/// From a registered product, Vivado counts M_DATA, ALUMUX, ALUADD, OUTPUT
/// and the cells before the multiplier it does not absorb on a Versal DSP58;
/// a cascaded DSP adds ALUMUX, ALUADD and OUTPUT. An UltraScale+ DSP48E2 has
/// one ALU cell and an 18-bit B port.
#define VERSAL_DSP 27, 24, 1, 6, 3
#define ULTRASCALE_PLUS_DSP 27, 18, 1, 5, 2
/// The LUT6 families share a fabric and the area mapping; the factor is the
/// geometric mean of Vivado's logic LUTs over the area mapping's on routed
/// U55C builds of Vortex units.
#define LUT6_AREA false, 0, 0, 0, 0, 0.337

const BeaconTarget kTargets[] = {
    // V80, VCK5000.
    {"versal", 6, VERSAL_DSP, LUT6_AREA},
    // U200, U250.
    {"virtexuplus", 6, ULTRASCALE_PLUS_DSP, LUT6_AREA},
    // U50, U55C, U280.
    {"virtexuplusHBM", 6, ULTRASCALE_PLUS_DSP, LUT6_AREA},
    {"zynquplus", 6, ULTRASCALE_PLUS_DSP, LUT6_AREA},
    // ASAP7 7.5-track RVT at the TT corner, as hw/syn/yosys builds with it:
    // 3-input cuts, each one cell (NAND, AOI, MAJ, XOR), which against
    // OpenSTA on five DUTs convert at the library's own cell delay where raw
    // and-inverter gates and 4-input cuts do not; a cell at fanout 3 and its
    // own slew, a DFFHQNx1's clock to output and setup, and 167 ps per path,
    // the median over five asic_gate DUTs. Products are multipliers of cells.
    {"asap7", 3, 0, 0, 0, 0, 0, true, 24, 58, 6, 167},
};

#undef VERSAL_DSP
#undef ULTRASCALE_PLUS_DSP
#undef LUT6_AREA

/// The netlist Beacon counts: an and-inverter graph with adders, compares,
/// multipliers and dividers kept word-level, mapped to the target's cells.
/// For timing, small modules inlined, balanced for depth and mapped for it, as
/// Vivado restructures a path that misses its period; for area, its common
/// subexpressions shared and mapped to the fewest cells, as Vivado maps the
/// rest of the design, module by module.
} // namespace

std::string beacon::beaconMapPipeline(const BeaconTarget &target, bool area,
                                       unsigned carryBlockSize, llvm::StringRef arrivals,
                                       unsigned inlineShared) {
  // Cells pack across small modules on a critical path, and across a body
  // instances share, whose every instance maps in its own context; area
  // keeps every module, so each instance's resources stay its own.
  std::string pipeline =
      area ? "hw.module(hw-bypass-inner-symbols,canonicalize,cse,"
           : "beacon-inline-shared{growth=" + std::to_string(inlineShared) +
                 "},hw-flatten-modules{hw-inline-all=false hw-inline-small "
                 "hw-inline-with-state hw-inline-marked "
                 "hw-small-threshold=300},";
  if (!area && !arrivals.empty())
    pipeline += "beacon-boundary-arrivals{file=" + arrivals.str() + "},";
  if (!area)
    pipeline += "hw.module(hw-bypass-inner-symbols,canonicalize,cse,"
                "comb-balance-mux,";
  // For depth, a subtraction from all ones is a complement, and an array
  // element updated from its own value is updated in place, as synthesis
  // maps both.
  if (!area)
    pipeline += "beacon-lower-complement,beacon-inject-in-place,";
  pipeline += "beacon-lower-equality,beacon-lower-constant-division,"
              "hw-aggregate-to-comb,";
  // For depth, the equalities hw-aggregate-to-comb makes of an array written
  // at a dynamic index (its element selects) are LUT logic too, as Vivado
  // decodes an index: beacon-lower-equality runs again and finds only those.
  if (!area)
    pipeline += "beacon-lower-equality,";
  pipeline += "cse,beacon-fold-zero-width,"
              "convert-comb-to-synth{additional-legal-ops=comb.add,comb.sub,"
              "comb.icmp,comb.mul,comb.divu,comb.divs,comb.modu,comb.mods},"
              "cse,canonicalize,synth-lower-word-to-bits,cse,";
  if (area)
    pipeline += "synth-lower-variadic{timing-aware=false reuse-subsets},"
                "synth-structural-hash,canonicalize,";
  else
    pipeline += "synth-lower-variadic{timing-aware},synth-structural-hash,"
                "canonicalize,synth-maximum-and-cover,"
                "synth-lower-variadic{timing-aware},synth-structural-hash,"
                "synth-sop-balancing{strategy=timing},synth-structural-hash,";
  pipeline += "synth-generic-lut-mapper{max-lut-size=" +
              std::to_string(target.cutSize) + " strategy=" +
              (area ? "area" : "timing");
  if (!area && carryBlockSize)
    pipeline += " carry-block-size=" + std::to_string(carryBlockSize);
  return pipeline + "},cse)";
}

namespace {

std::string mapPipeline(const BeaconTarget &target, bool area,
                        unsigned carryBlockSize, llvm::StringRef arrivals,
                        unsigned inlineShared) {
  return beacon::beaconMapPipeline(target, area, carryBlockSize, arrivals,
                                   inlineShared);
}

struct BeaconMapPass : public beacon::impl::BeaconMapBase<BeaconMapPass> {
  using BeaconMapBase::BeaconMapBase;

  void getDependentDialects(DialectRegistry &registry) const override {
    OpPassManager pm(ModuleOp::getOperationName());
    if (const BeaconTarget *t = beacon::lookupBeaconTarget(target))
      if (succeeded(parsePassPipeline(mapPipeline(*t, area, carryBlockSize, arrivals, inlineShared), pm)))
        pm.getDependentDialects(registry);
  }

  void runOnOperation() override {
    const BeaconTarget *t = beacon::lookupBeaconTarget(target);
    if (!t) {
      beacon::emitUnknownBeaconTarget(getOperation(), target);
      return signalPassFailure();
    }
    OpPassManager pm(ModuleOp::getOperationName());
    if (failed(parsePassPipeline(mapPipeline(*t, area, carryBlockSize, arrivals, inlineShared), pm)) ||
        failed(runPipeline(pm, getOperation())))
      signalPassFailure();
  }
};

/// Marks the shared bodies hw-flatten-modules{hw-inline-marked} inlines.
struct InlineSharedPass
    : public beacon::impl::InlineSharedBase<InlineSharedPass> {
  using InlineSharedBase::InlineSharedBase;
  using Node = circt::igraph::InstanceGraphNode;

  /// One line per shared body: name, instances, ops, the ops its copies
  /// add, and whether it is inlined; then the totals.
  void writeReport(ArrayRef<Node *> order, const DenseSet<Node *> &inlined,
                   const DenseSet<Node *> &kept,
                   DenseMap<Node *, uint64_t> &ops, uint64_t base,
                   uint64_t budget, uint64_t added) {
    std::string err;
    auto out = mlir::openOutputFile(report, &err);
    if (!out) {
      getOperation().emitError(err);
      return signalPassFailure();
    }
    auto &os = out->os();
    os << "module,instances,ops,added,inlined\n";
    for (auto *node : order) {
      unsigned uses = node->getNumUses();
      if (uses < 2 || !(inlined.count(node) || kept.count(node)))
        continue;
      os << node->getModule().getModuleName() << "," << uses << ","
         << ops[node] << "," << (uses - 1) * (ops[node] - 1) << ","
         << (inlined.count(node) ? 1 : 0) << "\n";
    }
    os << "# base " << base << " budget " << budget << " added " << added
       << " kept " << kept.size() << "\n";
    out->keep();
  }

  void runOnOperation() override {
    auto &graph = getAnalysis<circt::hw::InstanceGraph>();
    // Children before parents.
    SmallVector<circt::igraph::InstanceGraphNode *> order;
    DenseSet<circt::igraph::InstanceGraphNode *> seen;
    for (auto *start : graph)
      for (auto *node : llvm::post_order(start))
        if (seen.insert(node).second)
          order.push_back(node);

    // Each module's own ops, less its instances; what hw-flatten-modules
    // inlines anyway (one instance, or a body of fewer than 300 ops).
    DenseMap<circt::igraph::InstanceGraphNode *, uint64_t> own;
    DenseSet<circt::igraph::InstanceGraphNode *> inlined, candidates;
    for (auto *node : order) {
      auto module =
          dyn_cast_or_null<circt::hw::HWModuleOp>(node->getModule().getOperation());
      uint64_t size = 0;
      if (module)
        module.getBodyBlock()->walk([&](Operation *op) {
          if (!isa<circt::hw::InstanceOp>(op))
            ++size;
        });
      own[node] = size;
      if (!module || !module.isPrivate() || node->noUses())
        continue;
      if (node->getNumUses() == 1)
        inlined.insert(node);
      else
        candidates.insert(node);
    }
    // The ops of each module once the inlining below it is done (an
    // instance it keeps counts one op).
    DenseMap<circt::igraph::InstanceGraphNode *, uint64_t> ops;
    auto measure = [&] {
      for (auto *node : order) {
        uint64_t size = own[node];
        for (auto *record : *node) {
          auto *child = record->getTarget();
          size += inlined.count(child) ? ops[child] : 1;
        }
        ops[node] = size;
      }
    };
    measure();
    // Small shared bodies, which hw-flatten-modules inlines too.
    for (auto *node : order)
      if (candidates.count(node) && ops[node] < 300) {
        inlined.insert(node);
        candidates.erase(node);
        measure();
      }
    uint64_t base = 0;
    for (auto *node : order)
      if (!inlined.count(node))
        base += ops[node];
    uint64_t budget =
        growth ? std::max<uint64_t>(minOps, base * growth / 100) : 0;

    // The cheapest body first, while the budget lasts.
    auto attr = StringAttr::get(&getContext(), "hw.inline");
    uint64_t added = 0;
    while (!candidates.empty()) {
      circt::igraph::InstanceGraphNode *best = nullptr;
      uint64_t bestCost = 0;
      for (auto *node : order) {
        if (!candidates.count(node))
          continue;
        uint64_t cost = (node->getNumUses() - 1) * (ops[node] - 1);
        if (!best || cost < bestCost)
          best = node, bestCost = cost;
      }
      if (added + bestCost > budget)
        break;
      added += bestCost;
      candidates.erase(best);
      inlined.insert(best);
      best->getModule().getOperation()->setAttr(attr,
                                                UnitAttr::get(&getContext()));
      measure();
    }
    if (!report.empty())
      writeReport(order, inlined, candidates, ops, base, budget, added);
    markAllAnalysesPreserved();
  }
};

} // namespace

namespace beacon {

const BeaconTarget *lookupBeaconTarget(llvm::StringRef name) {
  for (const BeaconTarget &target : kTargets)
    if (target.name == name)
      return &target;
  return nullptr;
}

void emitUnknownBeaconTarget(Operation *op, llvm::StringRef name) {
  InFlightDiagnostic diag = op->emitError("no Beacon target '") << name
                                                               << "'; one of";
  for (const BeaconTarget &target : kTargets)
    diag << " " << target.name;
}

} // namespace beacon
