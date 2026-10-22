#!/bin/sh
# Host test of the Wokwi SHT3x chip against the firmware decoder.
set -e
T=$(mktemp -d)
cd "$(dirname "$0")"
cc -std=c11 -Wno-unknown-attributes -c ../sht3x.chip.c -o "$T"/kn_sht3x_chip.o
cc -std=c11 -Wno-unknown-attributes -c mock_wokwi.c -o "$T"/kn_mock_wokwi.o
c++ -std=c++17 -Wno-unknown-attributes test_chip.cpp "$T"/kn_sht3x_chip.o "$T"/kn_mock_wokwi.o -o "$T"/kn_test_chip
"$T"/kn_test_chip
