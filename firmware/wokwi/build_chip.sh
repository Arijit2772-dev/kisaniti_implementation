#!/bin/sh
# Compile the SHT3x Wokwi custom chip to WebAssembly.
# Uses the same toolchain as wokwi/wokwi-chip-clang-action (clang + wasi-libc wasi-sdk-27).
# One-time: docker build -t wokwi-chip-builder:local https://github.com/wokwi/wokwi-chip-clang-action.git
set -e
cd "$(dirname "$0")/chips"
docker run --rm -v "$PWD":/src -w /src --entrypoint clang wokwi-chip-builder:local \
  --target=wasm32-unknown-wasi --sysroot /opt/wasi-libc -nostartfiles \
  -Wl,--import-memory -Wl,--export-table -Wl,--no-entry -Werror \
  -o sht3x.chip.wasm sht3x.chip.c
echo "built $(pwd)/sht3x.chip.wasm"
