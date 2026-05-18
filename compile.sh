#!/bin/bash

make clean
make -j6

mv h264dec.exe h264dec_fast.exe

make clean

make USE_ASM=No -j6

mv h264enc.exe h264enc_stego.exe