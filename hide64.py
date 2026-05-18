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
TargetBitrate           5000
EnableRC                1
MaxQp                   51
MinQp                   0
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

    extension = os.path.splitext(secret_file)[1]
    extension = extension.encode("utf-8")

    # Build Header (HD64 + Size + extension)
    header = struct.pack("<4s I 5s", b"HD64", payload_size, extension)

    # Save to binary for OpenH264 C++ to read
    with open("payload.bin", "wb") as f:
        f.write(header + payload)
    print(f"[+] Payload armed: {payload_size + 13} bytes.")


def hide_data(in_video, secret_file, password, output_mp4):
    print(f"[*] Starting Steganography Process...")

    prep_payload(secret_file, password)

    temp_yuv = "temp_raw.yuv"
    temp_264 = "temp_stealth.264"

    # Use FFprobe to get exact dimensions and framerate
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate", "-of", "json", in_video]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    video_info = json.loads(result.stdout)["streams"][0]
    width = str(video_info["width"])
    height = str(video_info["height"])
    num, den = map(int, video_info["r_frame_rate"].split("/"))
    fps = str(int(num / den))

    print(f"[*] Target Video Profile: {width}x{height} @ {fps} FPS")

    # Use FFmpeg to Decode to Raw YUV
    print("[*] Decoding MP4 to Raw YUV...")
    subprocess.run(["ffmpeg", "-y", "-i", in_video, "-pix_fmt", "yuv420p", temp_yuv], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    generate_dynamic_config()

    print("[*] Spinning up OpenH264 to inject data...")

    encode_cmd = ["./hide64_enc.exe", "welsenc.cfg", "-org", temp_yuv, "-bf", temp_264, "-sw", width, "-sh", height, "-frin", fps, "-numtl", "1", "-numl", "1", "-dw", "0", width, "-dh", "0", height, "-frout", "0", fps]

    encode_process = subprocess.run(encode_cmd)

    if encode_process.returncode != 0:
        print("[-] An error occurred during OpenH264 encoding.")
        return

    print("[*] Encoding complete. Muxing back into MP4 and restoring audio...")

    # Use FFmpeg to Mux the video and original audio back together
    ffmpeg_mux_cmd = ["ffmpeg", "-y", "-i", temp_264, "-i", in_video, "-c:v", "copy", "-c:a", "copy", "-map", "0:v:0", "-map", "1:a:0?", output_mp4]

    subprocess.run(ffmpeg_mux_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[+] Success! Data hidden inside {output_mp4}")

    # Cleanup
    for f in [temp_yuv, temp_264, "payload.bin", "welsenc.cfg"]:
        if os.path.exists(f):
            os.remove(f)


def unhide_data(stego_video, password):
    print(f"[*] Starting extraction on {stego_video}...")

    temp_264 = "temp_extract.264"
    dummy_yuv = "dummy_out.yuv"

    # Demux the MP4 into a raw H264 bitstream
    print("[*] Ripping raw H.264 bitstream from MP4...")
    ffmpeg_cmd = ["ffmpeg", "-y", "-i", stego_video, "-c:v", "copy", "-bsf:v", "h264_mp4toannexb", temp_264]
    subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Run the modified unhide64 Decoder
    print("[*] Running OpenH264 decoder to extract binary payload...")
    subprocess.run(["./hide64_dec.exe", temp_264, dummy_yuv])

    extracted_files = glob.glob("extracted_payload*")

    if not extracted_files:
        print("[-] Error: No payload was extracted. Did the C++ decoder find the 'HD64' signature?")
        return

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
    for f in [temp_264, dummy_yuv]:
        if os.path.exists(f):
            os.remove(f)


if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_video = os.path.join(script_dir, "smallest_fractal.mp4")
    secret = os.path.join(script_dir, "pass.txt")
    output_video = os.path.join(script_dir, "final_stego_video.mp4")

    pwd = "1234"
    # hide_data(input_video, secret, pwd, output_video)
    print("---------------")
    unhide_data(output_video, pwd)
