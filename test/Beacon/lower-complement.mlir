// RUN: beacon-opt %s --pass-pipeline='builtin.module(hw.module(beacon-lower-complement))' | FileCheck %s
// RUN: beacon-opt %s --pass-pipeline='builtin.module(beacon-map{target=asap7},beacon-estimate-timing{output=- report= top=Top target=asap7})' -o /dev/null | FileCheck %s --check-prefix=ASAP7

// All ones minus x (the index of an array declared [N]) is ~x.
// CHECK-LABEL: hw.module @Top
// CHECK: %[[R:.+]] = comb.xor %i, %c-1_i7 : i7
// Any other constant minus x stays a subtraction.
// CHECK: comb.sub %c0_i7, %i : i7
// CHECK: hw.output %[[R]]
// No carry: the deepest path into `s` starts at the data, eight stages of a
// 128:1 multiplexer; timed as a 7-bit subtraction, the index took ten.
// ASAP7: 8,8,d,reg,1,0,0,x[{{[0-9]+}}],0,s.d[0],1
hw.module @Top(in %clk : !seq.clock, in %a : i7, in %v : i128, out r : i7, out n : i7, out s : i1) {
  %c-1_i7 = hw.constant -1 : i7
  %c0_i7 = hw.constant 0 : i7
  %i = seq.firreg %a clock %clk : i7
  %x = seq.firreg %v clock %clk : i128
  %r = comb.sub %c-1_i7, %i : i7
  %n = comb.sub %c0_i7, %i : i7
  %ext = comb.concat %c0_i121, %r : i121, i7
  %c0_i121 = hw.constant 0 : i121
  %sh = comb.shru %x, %ext : i128
  %b = comb.extract %sh from 0 : (i128) -> i1
  %s = seq.firreg %b clock %clk : i1
  hw.output %r, %n, %s : i7, i7, i1
}
