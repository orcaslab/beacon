// RUN: beacon-opt %s --pass-pipeline='builtin.module(hw.module(beacon-inject-in-place))' | FileCheck %s
// RUN: beacon-opt %s --pass-pipeline='builtin.module(beacon-map{target=asap7},beacon-estimate-timing{output=- report= top=Top target=asap7})' -o /dev/null | FileCheck %s --check-prefix=ASAP7
// RUN: beacon-opt %s --pass-pipeline='builtin.module(beacon-map{target=virtexuplusHBM},beacon-estimate-timing{output=- report= top=Top target=virtexuplusHBM})' -o /dev/null | FileCheck %s --check-prefix=U55C

// `m[i] <= m[i] | bit` over 128 elements, as circt-verilog writes
// `m[i][w] <= 1`: element i read back, updated, written at i. In place,
// element s is mux(i == s, m[s] | bit, m[s]); the read at i is left unused.
// CHECK-LABEL: hw.module @Top
// CHECK-NOT: hw.array_inject
// CHECK: %[[S:.+]] = hw.constant -1 : i7
// CHECK-NEXT: %[[E:.+]] = hw.array_get %m[%[[S]]]
// CHECK-NEXT: %[[U:.+]] = comb.or %[[E]], %bit : i4
// CHECK-NEXT: %[[H:.+]] = comb.icmp eq %i, %[[S]] : i7
// CHECK-NEXT: %[[M:.+]] = comb.mux %[[H]], %[[U]], %[[E]] : i4
// CHECK: hw.array_create %[[M]],
// CHECK-NOT: hw.array_inject
// Synthesis writes each element under its own enable and never reads the
// element back through the index: the deepest path is the index decode and
// the element's update (3 stages, 2 LUTs), not a 128:1 read multiplexer
// under them (8 stages, 5 LUTs before this pass).
// ASAP7: 3,3,d,reg,{{[0-9]+}},0,0,i[{{[0-9]}}],0,m.d[{{[0-9]+}}],
// ASAP7-NOT: {{^[4-9],}}
// U55C: 2,2,d,reg,{{[0-9]+}},0,0,i[{{[0-9]}}],0,m.d[{{[0-9]+}}],
// U55C-NOT: {{^[3-9],}}
hw.module @Top(in %clk : !seq.clock, in %ia : i7, in %wa : i4, out o : !hw.array<128xi4>) {
  %i = seq.firreg %ia clock %clk : i7
  %bit = seq.firreg %wa clock %clk : i4
  %row = hw.array_get %m[%i] : !hw.array<128xi4>, i7
  %new = comb.or %row, %bit : i4
  %upd = hw.array_inject %m[%i], %new : !hw.array<128xi4>, i7
  %m = seq.firreg %upd clock %clk : !hw.array<128xi4>
  hw.output %m : !hw.array<128xi4>
}
