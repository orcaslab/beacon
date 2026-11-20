add_llvm_executable(beacon-opt beacon-opt.cpp)
llvm_update_compile_flags(beacon-opt)
add_dependencies(beacon-opt beacon-version)
target_link_libraries(beacon-opt PRIVATE
  BeaconTransforms
  CIRCTComb
  CIRCTCombToSynth
  CIRCTCombTransforms
  CIRCTDatapath
  CIRCTHW
  CIRCTHWTransforms
  CIRCTSeq
  CIRCTSeqTransforms
  CIRCTSV
  CIRCTSVTransforms
  CIRCTSupport
  CIRCTSynth
  CIRCTSynthTransforms
  MLIROptLib
  MLIRTransforms
)
install(TARGETS beacon-opt RUNTIME DESTINATION bin)
