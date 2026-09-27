# ---------------------------------------------------------------------------
# cg-index : the Clang LibTooling semantic extractor.
#
# It parses one translation unit and streams a language-independent
# "fact stream" (JSON Lines) describing declarations, references, call edges,
# inheritance and inclusions.  All storage and querying lives in the Python
# layer, which keeps this binary a pure analyzer: it has no opinion about how
# facts are persisted, and its output can be diffed and asserted on directly
# in tests.
# ---------------------------------------------------------------------------

set(CCG_SOURCES
  src/main.cpp
  src/FactWriter.cpp
  src/FileTable.cpp
  src/SymbolEmitter.cpp
  src/AnalysisVisitor.cpp
  src/AnalysisAction.cpp
  src/CompileCommandRunner.cpp
  src/CompilationDatabaseFactory.cpp
  src/Options.cpp
)

add_executable(cg-index ${CCG_SOURCES})

target_include_directories(cg-index
  PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
  SYSTEM PRIVATE
    ${LLVM_INCLUDE_DIRS}
    ${CLANG_INCLUDE_DIRS})

target_compile_definitions(cg-index PRIVATE ${LLVM_DEFINITIONS})

target_link_libraries(cg-index
  PRIVATE
    ccg_warnings
    clangTooling
    clangFrontend
    clangDriver
    clangSerialization
    clangParse
    clangSema
    clangAnalysis
    clangAST
    clangASTMatchers
    clangBasic
    clangEdit
    clangLex
    clangIndex
    clangSupport)

install(TARGETS cg-index RUNTIME DESTINATION libexec/cpp-code-graph)
