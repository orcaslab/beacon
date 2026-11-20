add_mlir_library(BeaconTransforms
  Beacon.cpp
  EstimateArea.cpp
  EstimateTiming.cpp

  DEPENDS
  BeaconPassesIncGen

  LINK_LIBS PUBLIC
  CIRCTComb
  CIRCTCombToSynth
  CIRCTCombTransforms
  CIRCTDatapath
  CIRCTHW
  CIRCTHWTransforms
  CIRCTSeq
  CIRCTSV
  CIRCTSupport
  CIRCTSynth
  CIRCTSynthTransforms
  MLIRIR
  MLIRPass
  MLIRTransforms
)
