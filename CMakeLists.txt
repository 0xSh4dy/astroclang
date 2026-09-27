cmake_minimum_required(VERSION 3.20)

# The extractor is a Clang LibTooling tool. LLVM's own CMake package runs
# check_c_source_compiles(), so the C language must be enabled alongside CXX
# even though we only write C++ ourselves.
project(cpp-code-graph
        VERSION 0.1.0
        DESCRIPTION "Semantic code graph for C and C++"
        LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# ---------------------------------------------------------------------------
# Locate a complete LLVM + Clang development installation.
#
# A usable installation needs all four of:
#   LLVM headers, LLVM libraries, Clang headers, Clang libraries
# Debian/Ubuntu frequently ship these split across packages
# (llvm-N-dev vs libclang-N-dev) so it is common to find a version that has
# only half of them.  We therefore probe candidates and accept the first one
# that carries both CMake config packages, and let find_package report a clear
# error if none do.
# ---------------------------------------------------------------------------
set(CCG_LLVM_SEARCH_ROOTS
    /usr/lib/llvm-22 /usr/lib/llvm-21 /usr/lib/llvm-20 /usr/lib/llvm-19
    /usr/lib/llvm-18 /usr/lib/llvm-17 /usr/lib/llvm-16 /usr/lib/llvm-15
    /usr/local/opt/llvm /opt/homebrew/opt/llvm /usr/local
    /usr)

if(NOT DEFINED LLVM_DIR OR LLVM_DIR STREQUAL "")
  foreach(_root IN LISTS CCG_LLVM_SEARCH_ROOTS)
    if(EXISTS "${_root}/lib/cmake/llvm/LLVMConfig.cmake"
       AND EXISTS "${_root}/lib/cmake/clang/ClangConfig.cmake"
       AND EXISTS "${_root}/include/clang/Tooling/Tooling.h"
       AND EXISTS "${_root}/include/llvm/ADT/StringRef.h")
      set(LLVM_DIR "${_root}/lib/cmake/llvm" CACHE PATH "LLVM CMake config dir" FORCE)
      set(Clang_DIR "${_root}/lib/cmake/clang" CACHE PATH "Clang CMake config dir" FORCE)
      message(STATUS "cpp-code-graph: using LLVM/Clang from ${_root}")
      break()
    endif()
  endforeach()
endif()

find_package(LLVM REQUIRED CONFIG)
find_package(Clang REQUIRED CONFIG)

message(STATUS "cpp-code-graph: LLVM ${LLVM_PACKAGE_VERSION} (${LLVM_DIR})")

if(LLVM_PACKAGE_VERSION VERSION_LESS 16.0)
  message(FATAL_ERROR
    "cpp-code-graph requires LLVM/Clang 16 or newer; found ${LLVM_PACKAGE_VERSION}.\n"
    "Install a matching development package, e.g. on Debian/Ubuntu:\n"
    "  sudo apt install llvm-19-dev libclang-19-dev")
endif()

# ---------------------------------------------------------------------------
# Warnings: the extractor must compile cleanly against Clang's headers, which
# are not warning-clean themselves, so they are pulled in as SYSTEM includes.
# ---------------------------------------------------------------------------
add_library(ccg_warnings INTERFACE)
target_compile_options(ccg_warnings INTERFACE
  $<$<CXX_COMPILER_ID:GNU,Clang,AppleClang>:-Wall;-Wextra;-Wpedantic;-Wno-unused-parameter>)

add_subdirectory(cpp)
