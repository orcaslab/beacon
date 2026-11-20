# -*- Python -*-

import os

import lit.formats
from lit.llvm import llvm_config

config.name = "beacon"
config.test_format = lit.formats.ShTest(execute_external=False)
config.suffixes = [".mlir", ".test"]
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.beacon_obj_root, "test")
config.excludes = ["CMakeLists.txt", "lit.cfg.py", "lit.site.cfg.py", "Inputs"]

llvm_config.use_default_substitutions()
llvm_config.with_environment("PATH", config.llvm_tools_dir, append_path=True)
if config.circt_tools_dir and config.circt_tools_dir != config.llvm_tools_dir:
    llvm_config.with_environment("PATH", config.circt_tools_dir, append_path=True)
# The driver's Python tools beside beacon-opt (beacon, beacon_terms.py, ...).
config.substitutions.append(("%beacon_tools", config.beacon_tools_dir))
llvm_config.add_tool_substitutions(["beacon-opt"], [config.beacon_tools_dir])
