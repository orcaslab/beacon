//===- Passes.h - Beacon passes ---------------------------------*- C++ -*-===//
//
// Part of the Beacon project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#ifndef BEACON_PASSES_H
#define BEACON_PASSES_H

#include "circt/Dialect/Comb/CombDialect.h"
#include "circt/Dialect/HW/HWDialect.h"
#include "circt/Dialect/Seq/SeqDialect.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/Pass/Pass.h"

namespace beacon {

#define GEN_PASS_DECL
#define GEN_PASS_REGISTRATION
#include "beacon/Passes.h.inc"

} // namespace beacon

#endif // BEACON_PASSES_H
