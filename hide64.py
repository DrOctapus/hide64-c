import subprocess
import json
import sys
import glob
import os
import struct
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


def generate_key(password: str):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b"HD64_salt", iterations=480000)
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def generate_dynamic_config(config_filename="welsenc.cfg"):
    config_text = """
UsageType               0
MultipleThreadIdc       1
SliceMode               0
RCMode                  -1
FixedQp                 26
EntropyCodingModeFlag   1
"""
    with open(config_filename, "w") as f:
        f.write(config_text)


def prep_payload(secret_file, password):
    print("[*] Encrypting and packaging payload...")
    with open(secret_file, "rb") as f:
        raw_data = f.read()

    payload = raw_data

    # Encrypt
    if password:
        fernet = Fernet(generate_key(password))
        payload = fernet.encrypt(raw_data)

    payload_size = len(payload)

    extension = os.path.splitext(secret_file)[1][1:]
    extension = extension.encode("utf-8")

    # Build Header (HD64 + Size + extension)
    header = struct.pack("<4s I 4s", b"HD64", payload_size, extension)

    # Save to binary for OpenH264 C++ to read
    with open("payload.bin", "wb") as f:
        f.write(header + payload)
    print(f"[+] Payload armed: {payload_size + 12} bytes.")


def hide_data(in_video, secret_file, password, output_mp4):
    print(f"[*] Starting Steganography Process...")

    prep_payload(secret_file, password)

    temp_yuv = f"{in_video.split("/")[-1]}.yuv"
    temp_264 = "temp_stealth.264"

    # Use FFprobe to get exact dimensions and framerate
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate", "-of", "json", in_video]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    video_info = json.loads(result.stdout)["streams"][0]
    width = str(video_info["width"])
    height = str(video_info["height"])
    num, den = map(int, video_info["r_frame_rate"].split("/"))
    fps_float = str(num / den)

    print(f"[*] Target Video Profile: {width}x{height} @ {fps_float} FPS")
    
    # Use FFmpeg to Decode to Raw YUV
    print("[*] Decoding MP4 to Raw YUV...")
    if not os.path.exists(temp_yuv):
        subprocess.run(["ffmpeg", "-y", "-i", in_video, "-pix_fmt", "yuv420p", temp_yuv], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    generate_dynamic_config()

    print("[*] Spinning up OpenH264 to inject data...")

    encode_cmd = [
        "./hide64_enc.exe", "welsenc.cfg", 
        "-org", temp_yuv, 
        "-bf", temp_264, 
        "-sw", width, "-sh", height, 
        "-frin", fps_float,
        "-numtl", "1", "-numl", "1", 
        "-dw", "0", width, "-dh", "0", height, 
        "-frout", "0", fps_float,
        "-dprofile", "0", "77", 
        "-cabac", "1",
        "-frms", "-1",
    ]
    
    encode_process = subprocess.run(encode_cmd, stdout=subprocess.DEVNULL)

    if encode_process.returncode != 0:
        print("[-] An error occurred during OpenH264 encoding.")
        return

    if not os.path.exists(temp_264) or os.path.getsize(temp_264) == 0:
        print("[-] FATAL ERROR: OpenH264 failed to generate temp_stealth.264!")
        return

    print("[*] Encoding complete. Muxing back into MP4 and restoring audio...")
    
    temp_mp4 = "temp_clean_video.mp4"

    # THE FIX - STEP 1: Wrap the raw .264 into an MKV container.
    # The MKV muxer perfectly understands raw H.264 and generates flawless timestamps.
    subprocess.run([
        "ffmpeg", "-y", 
        "-r", fps_float,                 # Treat input as exact FPS
        "-i", temp_264, 
        "-c:v", "copy",                  # Still copy the bits (steganography is safe!)
        "-fflags", "+genpts",            # Regenerate timestamps
        "-fps_mode", "cfr",              # Enforce constant frame rate
        "-video_track_timescale", "90k", # Standardize timebase
        temp_mp4
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ffmpeg_mux_cmd = [
        "ffmpeg", "-y", 
        "-i", temp_mp4, 
        "-i", in_video, 
        "-c:v", "copy", "-c:a", "copy", 
        "-map", "0:v:0", "-map", "1:a:0?", 
        output_mp4
    ]
    
    subprocess.run(ffmpeg_mux_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[+] Success! Data hidden inside {output_mp4}")

    # Cleanup
    # temp_yuv
    for f in [temp_264, temp_mp4, "payload.bin", "welsenc.cfg"]:
        if os.path.exists(f):
            os.remove(f)


def unhide_data(stego_video, password):
    print(f"[*] Starting extraction on {stego_video}...")

    temp_264 = "temp_extract.264"
    dummy_yuv = "temp_out.yuv"

    try:
        # Demux the MP4 into a raw H264 bitstream
        print("[*] Ripping raw H.264 bitstream from MP4...")
        ffmpeg_cmd = ["ffmpeg", "-y", "-i", stego_video, "-c:v", "copy", "-bsf:v", "h264_mp4toannexb", temp_264]
        subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Run the modified unhide64 Decoder
        print("[*] Running OpenH264 decoder to extract binary payload...")
        subprocess.run(["./hide64_dec.exe", temp_264, dummy_yuv])

        extracted_files = glob.glob("extracted_payload*")

        if not extracted_files:
            raise Exception("[-] Error: No payload was extracted. Did the C++ decoder find the 'HD64' signature?")

        # Grab the first matched file
        encrypted_file = extracted_files[0]

        # Decrypt
        print(f"[*] Found payload: {encrypted_file}. Decrypting...")
        with open(encrypted_file, "rb") as f:
            payload = f.read()

        try:
            data = payload
            if password:
                fernet = Fernet(generate_key(password))
                data = fernet.decrypt(payload)

            original_ext = os.path.splitext(encrypted_file)[1]
            final_filename = f"decrypted_secret{original_ext}"

            with open(final_filename, "wb") as f:
                f.write(data)

            print(f"[+] Success! Decrypted data saved to: {final_filename}")

            os.remove(encrypted_file)

        except Exception as e:
            print(f"[-] Decryption failed! Wrong password or corrupted payload. (Error: {e})")

        print("[*] Cleaning up temporary files...")
    except Exception as e:
        print(e.args[0])
    finally:
        for f in [temp_264, dummy_yuv]:
            if os.path.exists(f):
                os.remove(f)


if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_video = os.path.join(script_dir, "v_smallest_fractal.mp4")
    secret = os.path.join(script_dir, "pass.txt")
    output_video = os.path.join(script_dir, "stego_video.mp4")

    pwd = None
    hide_data(input_video, secret, pwd, output_video)
    print("---------------")
    if os.path.exists(output_video):
        unhide_data(output_video, pwd)
