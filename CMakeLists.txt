cmake_minimum_required(VERSION 3.20)
project(beacon LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 17 CACHE STRING "C++ standard")
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Beacon builds on an installed (or built) CIRCT, as FoldSim does:
#   cmake -G Ninja -B build -DCIRCT_DIR=<circt>/lib/cmake/circt \
#     -DMLIR_DIR=<llvm>/lib/cmake/mlir -DLLVM_DIR=<llvm>/lib/cmake/llvm
# It also builds as a subproject (add_subdirectory) of a project that has
# found MLIR and CIRCT already: FoldSim links its passes.
if(CMAKE_SOURCE_DIR STREQUAL PROJECT_SOURCE_DIR)
  set(BEACON_STANDALONE ON)
  find_package(MLIR REQUIRED CONFIG)
  find_package(CIRCT REQUIRED CONFIG)
  message(STATUS "Using MLIRConfig.cmake in: ${MLIR_DIR}")
  message(STATUS "Using CIRCTConfig.cmake in: ${CIRCT_DIR}")

  set(LLVM_RUNTIME_OUTPUT_INTDIR ${CMAKE_BINARY_DIR}/bin)
  set(LLVM_LIBRARY_OUTPUT_INTDIR ${CMAKE_BINARY_DIR}/lib)
  set(MLIR_BINARY_DIR ${CMAKE_BINARY_DIR})

  list(APPEND CMAKE_MODULE_PATH "${MLIR_CMAKE_DIR}" "${LLVM_CMAKE_DIR}")
  include(TableGen)
  include(AddLLVM)
  include(AddMLIR)
  include(HandleLLVMOptions)

  include_directories(${LLVM_INCLUDE_DIRS} ${MLIR_INCLUDE_DIRS} ${CIRCT_INCLUDE_DIRS})
  link_directories(${LLVM_BUILD_LIBRARY_DIR})
  add_definitions(${LLVM_DEFINITIONS})
else()
  set(BEACON_STANDALONE OFF)
endif()

include_directories(${PROJECT_SOURCE_DIR}/include ${PROJECT_BINARY_DIR}/include)
# The directory beacon-opt and the driver land in.
if(LLVM_RUNTIME_OUTPUT_INTDIR)
  set(BEACON_TOOLS_DIR ${LLVM_RUNTIME_OUTPUT_INTDIR})
else()
  set(BEACON_TOOLS_DIR ${CMAKE_BINARY_DIR}/bin)
endif()

# The revision beacon-opt --version prints, refreshed on every build.
set(BEACON_VERSION_INC ${PROJECT_BINARY_DIR}/include/beacon/Version.inc)
add_custom_target(beacon-version
  COMMAND ${CMAKE_COMMAND} -DSRC=${PROJECT_SOURCE_DIR}
          -DOUT=${BEACON_VERSION_INC}
          -DTXT=${BEACON_TOOLS_DIR}/beacon_version.txt
          -P ${PROJECT_SOURCE_DIR}/cmake/GenerateVersion.cmake
  BYPRODUCTS ${BEACON_VERSION_INC}
  COMMENT "Checking the Beacon revision")

add_subdirectory(include/beacon)
add_subdirectory(lib)
add_subdirectory(tools)
add_subdirectory(test)
