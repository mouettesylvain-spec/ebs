#!/bin/bash
# Create (or repair) the directories of one confidentiality domain (P2-01 R4, decision D7).
#
#   create_domain.sh NAME GROUP [--cas-root /cas] [--debug-root /debug]
#
# Run as root (or as an admin owning the roots). Idempotent: it creates what is missing and sets
# group and modes again on every run. The Unix GROUP must already exist (LDAP/SSSD); members
# of GROUP are the domain's users, and the GC and metadata service accounts must be members too.
#
#   <cas-root>/NAME            group GROUP, 2750  (setgid: new entries inherit the group)
#   <cas-root>/NAME/{blobs,trees,tmp}  2770      (members' runners upload objects here)
#   <debug-root>/NAME          group GROUP, 2770  (runners write debug and live logs)
#
# Then add the domain to ebs.toml ([[domains]] name, unix_group), and check it with
# `ebs domain check`.
set -euo pipefail

usage() {
    echo "usage: $0 NAME GROUP [--cas-root DIR] [--debug-root DIR]" >&2
    exit 2
}

[ $# -ge 2 ] || usage
name=$1
group=$2
shift 2
cas_root=/cas
debug_root=/debug
while [ $# -gt 0 ]; do
    case $1 in
        --cas-root) [ $# -ge 2 ] || usage; cas_root=$2; shift 2 ;;
        --debug-root) [ $# -ge 2 ] || usage; debug_root=$2; shift 2 ;;
        *) usage ;;
    esac
done

if ! [[ $name =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
    echo "create_domain: invalid domain name '$name' (letters, digits, '.', '_', '-')" >&2
    exit 2
fi
if ! getent group "$group" >/dev/null; then
    echo "create_domain: Unix group '$group' does not exist; create it in the directory first" >&2
    exit 2
fi
for root in "$cas_root" "$debug_root"; do
    if [ ! -d "$root" ]; then
        echo "create_domain: root directory '$root' does not exist (mount or create it first)" >&2
        exit 2
    fi
done

make_dir() {  # path mode
    mkdir -p -- "$1"
    chgrp -- "$group" "$1"
    chmod -- "$2" "$1"
    echo "  $2 $group $1"
}

echo "domain $name (group $group):"
make_dir "$cas_root/$name" 2750
for sub in blobs trees tmp; do
    make_dir "$cas_root/$name/$sub" 2770
done
make_dir "$debug_root/$name" 2770
