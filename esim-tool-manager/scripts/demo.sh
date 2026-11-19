#!/usr/bin/env bash
# Walks through every feature. Safe: real installs are replaced by --dry-run.
# Record it with:  asciinema rec demo.cast -c "bash scripts/demo.sh"
set -u
tm="python3 -m esim_tool_manager"
run() { echo; echo "\$ $*"; "$@"; }
run $tm info
run $tm doctor
run $tm list
run $tm verify ngspice kicad
run $tm --dry-run -y install ghdl
run $tm install ngspice --version 99 -y
run $tm check-updates ngspice kicad
run $tm config pin ngspice 42
run $tm update ngspice -y
run $tm config unpin ngspice
run $tm config apply
run $tm log -n 8
run $tm log --errors
# Graphical interface (needs a display):  python3 -m esim_tool_manager gui
