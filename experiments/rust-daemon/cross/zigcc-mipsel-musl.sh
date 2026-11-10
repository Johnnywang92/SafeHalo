#!/bin/sh
# Filter rustc's --fix-cortex-a53-843419 (unsupported by zig ld); keep order.
n=$#
i=0
while [ $i -lt $n ]; do
  a=$1; shift
  case "$a" in *fix-cortex-a53-843419*|-lgcc_s|-pie) ;; *) set -- "$@" "$a" ;; esac
  i=$((i+1))
done
exec zig cc -target mipsel-linux-musleabi "$@"
