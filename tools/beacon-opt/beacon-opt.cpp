//===- beacon-opt.cpp - Beacon's modular optimizer ------------------------===//
//
// Part of the Beacon project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// Runs Beacon's passes with the CIRCT HW, Comb, Seq, SV and Synth dialects and
// the CIRCT passes `beacon-map` runs to map a design to a Beacon target's cells.
//
//===----------------------------------------------------------------------===//

#include "beacon/Passes.h"
#include "beacon/Version.inc"
#include "circt/Conversion/Passes.h"
#include "circt/Dialect/Comb/CombPasses.h"
#include "circt/Dialect/HW/HWPasses.h"
#include "circt/Dialect/SV/SVDialect.h"
#include "circt/Dialect/SV/SVPasses.h"
#include "circt/Dialect/Seq/SeqPasses.h"
#include "circt/Dialect/Synth/SynthDialect.h"
#include "circt/Dialect/Synth/Transforms/SynthPasses.h"
#include "circt/Support/Version.h"
#include "mlir/Pass/PassRegistry.h"
#include "mlir/Tools/mlir-opt/MlirOptMain.h"
#include "mlir/Transforms/Passes.h"
#include "llvm/Support/raw_ostream.h"

int main(int argc, char **argv) {
  mlir::DialectRegistry registry;
  registry.insert<circt::comb::CombDialect, circt::hw::HWDialect,
                  circt::seq::SeqDialect, circt::sv::SVDialect,
                  circt::synth::SynthDialect>();

  beacon::registerBeaconPasses();
  circt::comb::registerPasses();
  circt::hw::registerPasses();
  circt::seq::registerPasses();
  circt::sv::registerPasses();
  circt::synth::registerPasses();
  circt::registerConvertCombToSynth();
  mlir::registerCanonicalizerPass();
  mlir::registerCSEPass();
  mlir::registerSymbolDCEPass();
  mlir::registerTopologicalSortPass();
  llvm::cl::AddExtraVersionPrinter([](llvm::raw_ostream &os) {
    os << circt::getCirctVersion() << '\n';
    os << "beacon " << BEACON_REVISION << '\n';
  });

  return mlir::failed(
      mlir::MlirOptMain(argc, argv, "Beacon modular optimizer", registry));
}
