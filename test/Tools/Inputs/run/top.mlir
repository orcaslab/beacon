hw.module @Top(in %clk : !seq.clock, in %i_in : i2, in %v_in : i16, out o : !hw.array<4xi16>) {
  %i = seq.firreg %i_in clock %clk : i2
  %v = seq.firreg %v_in clock %clk : i16
  %0 = hw.array_inject %r[%i], %v : !hw.array<4xi16>, i2
  %r = seq.firreg %0 clock %clk : !hw.array<4xi16>
  hw.output %r : !hw.array<4xi16>
}
