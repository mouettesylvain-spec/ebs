# Sourced by `ebs toolchain register` inside the clean capture shell (ebs.toolchain.env):
#   source module_load.bash <module command> <module>
# The module command (Environment Modules `modulecmd.tcl` or Lmod `lmod`) prints shell code that
# changes the environment; evaluating that code is how every `module` shell function works. The
# module name is only ever passed as an argument, never spliced into code.
_ebs_modcmd=$1
shift
_ebs_code=$("$_ebs_modcmd" bash purge) || return
eval "$_ebs_code" || return
_ebs_code=$("$_ebs_modcmd" bash load "$@") || return
eval "$_ebs_code" || return
unset _ebs_modcmd _ebs_code
