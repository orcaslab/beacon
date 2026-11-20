# Copyright © 2019-2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# xilinx_path_export.tcl <dcp | -> <out dir>: Beacon's path database export of one
# checkpoint (synthesized, placed, routed or routed with errors). With "-" it
# exports the design already open (a flow's post-route hook).
#
#   vivado -mode batch -source xilinx_path_export.tcl -tclargs <dcp> <out dir>
#
# Writes into <out dir>:
#   paths.tsv     one row per end point (its worst setup path): a sample of
#                 every clock's end points by slack band (every k-th of the
#                 band's worst PATH_EXPORT_BAND_QUERY 4000, at most
#                 PATH_EXPORT_BAND_KEEP 300; `weight` = k), then the worst
#                 PATH_EXPORT_FAIL (10000) failing end points not yet written
#                 (weight 1). Columns: see `cols`. The walk over a path's cells
#                 (slr_changes, length, bbox) is done for the band sample and the
#                 2000 worst (PATH_EXPORT_WALK caps it per clock); else empty.
#   assign.tsv    hierarchical cells with a USER_SLR_ASSIGNMENT (the floorplan
#                 the placer was given), and pblock members with their ranges
#   util_hier.rpt report_utilization -hierarchical (depth PATH_EXPORT_HIER_DEPTH):
#                 the size of every subtree, for the in-block span term
#   util_slr.rpt, timing_summary.rpt, route_status.rpt, meta.tsv
# Never modifies the design. Cells without placement have empty site columns.
set dcp [lindex $argv 0]
set out [lindex $argv 1]
proc envd {k d} { if {[info exists ::env($k)] && $::env($k) ne ""} { return $::env($k) }; return $d }
set NFAIL [envd PATH_EXPORT_FAIL 10000]
set BQ [envd PATH_EXPORT_BAND_QUERY 4000]
set BK [envd PATH_EXPORT_BAND_KEEP 300]
set DEPTH [envd PATH_EXPORT_HIER_DEPTH 12]
set NWALK [envd PATH_EXPORT_WALK 5000]
file mkdir $out
proc st {msg} {
  set fh [open [file join $::out export.status] a]
  puts $fh "[clock format [clock seconds] -format {%Y-%m-%d %H:%M:%S}] $msg"; close $fh
  puts "PATH_EXPORT: $msg"
}
set_param general.maxThreads [envd PATH_EXPORT_THREADS 8]
set t0 [clock seconds]
if {$dcp ne "-"} { open_checkpoint $dcp; st "opened $dcp ([expr {[clock seconds]-$t0}] s)" }

# Placement of a cell: site, tile column/row, clock region, SLR (cached by site).
array set SITE {}
proc place_of {c} {
  if {$c eq ""} { return [list "" "" "" "" ""] }
  set s [get_property -quiet LOC $c]
  if {$s eq ""} { return [list "" "" "" "" ""] }
  if {[info exists ::SITE($s)]} { return $::SITE($s) }
  set so [get_sites -quiet $s]
  set t [get_tiles -quiet -of_objects $so]
  set slr [get_slrs -quiet -of_objects $so]
  set r [list $s [get_property -quiet COLUMN $t] [get_property -quiet ROW $t] \
           [get_property -quiet CLOCK_REGION $so] [string map {SLR ""} $slr]]
  set ::SITE($s) $r
  return $r
}

set cols {design clock req slack dp logic net skew unc slrcomp levels maxfo maxfo_net nfo1k slr_changes length bbox
  s_site s_col s_row s_cr s_slr e_site e_col e_row e_cr e_slr s_ref e_ref s_cell e_cell s_pin e_pin sample weight}
set fh [open [file join $out paths.tsv] w]
puts $fh [join $cols "\t"]
set ID [envd PATH_EXPORT_ID [file rootname [file tail $dcp]]]
array set SEEN {}
set nrow 0

# One row per path of the list P (all of one clock and sample kind). Path properties are read for the whole list
# at once; per path only its two end cells (placement, cached by site), its nets' fanout and, for at most
# PATH_EXPORT_WALK rows of a clock, the walk over its cells (SLR changes, Manhattan length, bounding box).
proc cell_of_pin {pin} { return [string range $pin 0 [expr {[string last / $pin] - 1}]] }
proc emit {P clk sample weight} {
  if {![llength $P]} return
  foreach k {SLACK REQUIREMENT DATAPATH_DELAY DATAPATH_LOGIC_DELAY DATAPATH_NET_DELAY SKEW UNCERTAINTY LOGIC_LEVELS
             STARTPOINT_PIN ENDPOINT_PIN} { set V($k) [get_property $k $P] }
  set V(SLRC) [get_property -quiet INTER_SLR_COMPENSATION $P]
  if {[llength $V(SLRC)] != [llength $P]} { set V(SLRC) [lrepeat [llength $P] ""] }
  set i -1
  foreach p $P {
    incr i
    set ep [lindex $V(ENDPOINT_PIN) $i]
    if {[info exists ::SEEN($ep)]} continue
    set ::SEEN($ep) 1
    set spin [lindex $V(STARTPOINT_PIN) $i]
    set sc [get_cells -quiet [cell_of_pin $spin]]
    set ec [get_cells -quiet [cell_of_pin $ep]]
    set nch ""; set len ""; set bbox ""
    if {$::walked < $::NWALK} {
      incr ::walked
      set prevs ""; set nch 0; set len 0; set prev ""; set xs {}; set ys {}
      foreach c [get_cells -quiet -of_objects $p] {
        lassign [place_of $c] site x y cr slr
        if {$site eq ""} continue
        if {$prevs ne "" && $slr ne $prevs} { incr nch }
        set prevs $slr
        if {$prev ne ""} { set len [expr {$len + abs($x - [lindex $prev 0]) + abs($y - [lindex $prev 1])}] }
        set prev [list $x $y]; lappend xs $x; lappend ys $y
      }
      if {[llength $xs]} {
        set xs [lsort -integer $xs]; set ys [lsort -integer $ys]
        set bbox [expr {[lindex $xs end] - [lindex $xs 0] + [lindex $ys end] - [lindex $ys 0]}]
      } else { set len ""; set nch "" }
    }
    set mf 0; set mfn ""; set n1k 0
    set nets [get_nets -quiet -of_objects $p]
    if {[llength $nets]} {
      foreach net $nets f [get_property FLAT_PIN_COUNT $nets] {
        incr f -1
        if {$f >= 1000} { incr n1k }
        if {$f > $mf} { set mf $f; set mfn $net }
      }
    }
    set S [place_of $sc]; set E [place_of $ec]
    set row [list $::ID $clk [lindex $V(REQUIREMENT) $i] [lindex $V(SLACK) $i] [lindex $V(DATAPATH_DELAY) $i] \
      [lindex $V(DATAPATH_LOGIC_DELAY) $i] [lindex $V(DATAPATH_NET_DELAY) $i] [lindex $V(SKEW) $i] \
      [lindex $V(UNCERTAINTY) $i] [lindex $V(SLRC) $i] [lindex $V(LOGIC_LEVELS) $i] \
      $mf $mfn $n1k $nch $len $bbox {*}$S {*}$E [get_property -quiet REF_NAME $sc] [get_property -quiet REF_NAME $ec] \
      $sc $ec $spin $ep $sample $weight]
    puts $::fh [join $row "\t"]
    incr ::nrow
  }
}

# Clocks with setup end points (generated clocks included; virtual clocks have no end points).
set clocks {}
foreach clk [get_clocks -quiet] {
  set p [get_timing_paths -quiet -setup -max_paths 1 -nworst 1 -to $clk]
  if {[llength $p]} { lappend clocks [list $clk [get_property SLACK $p]] }
}
st "[llength $clocks] clocks with end points"
set bands {-100 -3 -3 -2 -2 -1 -1 -0.5 -0.5 0 0 0.25 0.25 0.5 0.5 1 1 1.5 1.5 2 2 3 3 4 4 6 6 10 10 1000}
foreach cs $clocks {
  lassign $cs clk wns
  set t [clock seconds]
  set walked 0
  # The band sample first (walked), then the worst failing end points (the first ones walked, the rest not).
  foreach {lo hi} $bands {
    set B [get_timing_paths -quiet -setup -max_paths $BQ -nworst 1 -to $clk -slack_greater_than $lo -slack_lesser_than $hi]
    if {![llength $B]} continue
    set k [expr {max(1, [llength $B] / $BK)}]
    set S {}
    for {set j 0} {$j < [llength $B]} {incr j $k} { lappend S [lindex $B $j] }
    emit $S $clk "band:$lo:$hi:[llength $B]" $k
  }
  set P [get_timing_paths -quiet -setup -max_paths $NFAIL -nworst 1 -to $clk -slack_lesser_than 0]
  set walked [expr {$NWALK - 2000}]
  emit $P $clk worst 1
  set nf [llength $P]
  st "clock $clk WNS $wns: $nf failing exported, $nrow rows so far ([expr {[clock seconds]-$t}] s)"
}
close $fh

# The floorplan the placer was given: SLR hints on hierarchical cells, and pblocks.
set fa [open [file join $out assign.tsv] w]
puts $fa "kind\tcell\tvalue"
foreach c [get_cells -quiet -hierarchical -filter {IS_PRIMITIVE == 0 && USER_SLR_ASSIGNMENT != ""}] {
  puts $fa "slr\t$c\t[get_property USER_SLR_ASSIGNMENT $c]"
}
foreach pb [get_pblocks -quiet] {
  set r [get_property -quiet GRID_RANGES $pb]
  set m [get_cells -quiet -of_objects $pb]
  set nm [llength $m]
  if {$nm > 2000} { set m [lrange $m 0 1999]; st "pblock $pb: first 2000 of $nm member cells written" }
  foreach c $m { puts $fa "pblock\t$c\t$pb [join $r ,]" }
}
close $fa
st "floorplan written"

set rpt {}
if {[catch { report_utilization -hierarchical -hierarchical_depth $DEPTH -file [file join $out util_hier.rpt] } e]} { lappend rpt "util_hier: $e" }
catch { report_utilization -slr -file [file join $out util_slr.rpt] }
catch { report_timing_summary -no_detailed_paths -file [file join $out timing_summary.rpt] }
catch { report_route_status -file [file join $out route_status.rpt] }
set fm [open [file join $out meta.tsv] w]
puts $fm "design\t$ID"
puts $fm "dcp\t$dcp"
puts $fm "part\t[get_property -quiet PART [current_design]]"
puts $fm "rows\t$nrow"
foreach cs $clocks { puts $fm "clock\t[lindex $cs 0]\t[get_property -quiet PERIOD [get_clocks [lindex $cs 0]]]\t[lindex $cs 1]" }
puts $fm "seconds\t[expr {[clock seconds]-$t0}]"
foreach r $rpt { puts $fm "warning\t$r" }
close $fm
st "DONE: $nrow end points ([expr {[clock seconds]-$t0}] s)"
