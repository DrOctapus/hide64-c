#!/bin/bash

make clean
make -j6

mv h264dec.exe ../hide64_dec.exe

#make clean

#make USE_ASM=No -j6

mv h264enc.exe ../hide64_enc.exe
make clean
