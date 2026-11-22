#!/bin/bash
# demo.sh: the Beacon quick-start demo (README). Needs circt-verilog and beacon on PATH.
# Screens mac.sv for the Versal family before and after pipelining its multiply-add:
# logic levels of every end point, the deepest path op by op, and the area.
set -euo pipefail
cd "$(dirname "$0")"
OUT=${1:-out}
mkdir -p "$OUT"
for p in 0 1; do
  circt-verilog --ir-hw --mlir-print-debuginfo --top=mac -G PIPE=$p mac.sv -o "$OUT/mac$p.mlir"
  beacon run "$OUT/mac$p.mlir" "$OUT/run$p" top=mac target=versal > "$OUT/run$p.log"
  echo "PIPE=$p: $(sed -n 2p "$OUT/run$p/report.txt")"
  echo "        $(head -1 "$OUT/run$p/area.txt")"
done
echo
echo "Deepest paths of PIPE=0 (12 levels or more is a timing risk at 300 MHz on Versal):"
beacon paths "$OUT/run0" --min-levels 12
