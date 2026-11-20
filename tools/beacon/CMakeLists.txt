# The driver (`beacon`), its gate summary and routing terms, the path database
# and the calibrations, beside beacon-opt in the build's bin and installed to
# bin, so `beacon` finds beacon-opt and its calibrations next to itself.
configure_file(beacon.py ${BEACON_TOOLS_DIR}/beacon COPYONLY)
foreach(f beacon_summary.py beacon_terms.py db/beacon_db.py db/xilinx_path_export.tcl)
  get_filename_component(name ${f} NAME)
  configure_file(${f} ${BEACON_TOOLS_DIR}/${name} COPYONLY)
endforeach()
foreach(f u55c.json v80_slr.json v80_timing.json)
  configure_file(calibrations/${f} ${BEACON_TOOLS_DIR}/calibrations/${f} COPYONLY)
endforeach()
install(PROGRAMS beacon.py DESTINATION bin RENAME beacon)
install(PROGRAMS beacon_summary.py beacon_terms.py db/beacon_db.py DESTINATION bin)
install(FILES db/xilinx_path_export.tcl DESTINATION bin)
install(DIRECTORY calibrations DESTINATION bin)
