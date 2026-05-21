import subprocess
import json
import sys
import glob
import os
import struct
import base64
import argparse
import re
import shutil
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


def check_dependencies(mode):
    for tool in ["ffmpeg", "ffprobe"]:
        if shutil.which(tool) is None:
            print(f"[-] FATAL ERROR: '{tool}' is not installed or not in the system PATH.")
            sys.exit(1)
            
    exe_name = "hide64_enc.exe" if mode == "hide" else "hide64_dec.exe"
    if not os.path.isfile(exe_name):
        print(f"[-] FATAL ERROR: '{exe_name}' not found in the current directory")
        sys.exit(1)


def is_valid_video(filepath):
    # Uses ffprobe to verify the file is a valid media container with a video stream
    try:
        cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type", "-of", "csv=p=0", filepath]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return "video" in result.stdout.strip()
    except Exception:
        return False


def generate_key(password: str):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b"HD64_salt", iterations=480_000)
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
    print("[*] Encrypting and packaging payload")
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

    # Build Header (HD64 + 4 byte size + 4 byte extension)
    header = struct.pack("<4s I 4s", b"HD64", payload_size, extension)

    with open("payload.bin", "wb") as f:
        f.write(header + payload)
        
    total_bytes = payload_size + 12
    print(f"[+] Payload ready: {f"{total_bytes / 1024:.2f} KB" if total_bytes > 1024 else f"{total_bytes} bytes"}")
    return total_bytes


def hide_data(in_video, secret_file, output_mp4, password=None):
    print(f"[*] Starting Steganography Process")

    try:
        payload_size = prep_payload(secret_file, password)

        # Get info about input mp4
        cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration", "-of", "json", in_video]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
        video_info = json.loads(result.stdout)["streams"][0]
        width, height = str(video_info["width"]), str(video_info["height"])
        num, den = map(int, video_info["r_frame_rate"].split("/"))
        fps_float = str(num / den)
        fps_fraction = f"{num}/{den}"

        # Calculate Total Frames
        if "nb_frames" in video_info:
            total_frames = int(video_info["nb_frames"])
        elif "duration" in video_info:
            total_frames = int(float(video_info["duration"]) * float(fps_float))
        else:
            total_frames = 1000 # Fallback

        print(f"[*] Target Video Profile: {width}x{height} @ {fps_float} FPS ({total_frames} frames)")

        # capacity estimation
        total_4x4_blocks_per_frame = (int(width) / 4) * (int(height) / 4) * 1.5
        theoretical_max_bytes = int((total_4x4_blocks_per_frame * total_frames) / 8)
        
        # conservative estimate: Assume only 5% of AC blocks will actually have high-frequency detail
        safe_capacity_bytes = int(theoretical_max_bytes * 0.018) 
        
        print(f"[*] Safe Storage Capacity: ~{safe_capacity_bytes / 1024:.2f} KB")

        if payload_size > theoretical_max_bytes:
            raise Exception(f"[-] FATAL: Payload ({payload_size} bytes) exceeds absolute mathematical capacity ({theoretical_max_bytes} bytes). Use a longer video.")
        elif payload_size > safe_capacity_bytes:
            print(f"[!] WARNING: Payload ~({payload_size / 1024:.2f} KB) exceeds the safe detailed-block capacity. It may be truncated. Proceeding anyway")

        generate_dynamic_config()
        temp_mp4 = "temp_clean_video.mp4"

        # Demux MP4 to Raw YUV (Dump to stdout via "-")
        ffmpeg_decode_cmd = [
            "ffmpeg", "-v", "error", "-y", "-i", in_video, 
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"
        ]
        
        # hide64 Encoder (Read stdin via "-org -", Write stdout via "-bf -")
        encode_cmd = [
            "./hide64_enc.exe", "welsenc.cfg", 
            "-org", "-", "-bf", "-", 
            "-sw", width, "-sh", height, "-frin", fps_float, 
            "-numtl", "1", "-numl", "1", 
            "-dw", "0", width, "-dh", "0", height, "-frout", "0", fps_float, 
            "-dprofile", "0", "77", "-cabac", "1", "-frms", "-1"
        ]

        # Mux Raw H.264 to temp MP4 without audio
        ffmpeg_mux_cmd = [
            "ffmpeg", "-y",
            "-r", fps_float,
            "-i", "-", 
            "-c:v", "copy",
            "-fflags", "+genpts",            
            "-fps_mode", "cfr",              
            "-video_track_timescale", "90k", 
            temp_mp4
        ]

        # Link the pipes
        p_decode = subprocess.Popen(ffmpeg_decode_cmd, stdout=subprocess.PIPE)
        p_encode = subprocess.Popen(encode_cmd, stdin=p_decode.stdout, stdout=subprocess.PIPE)
        
        p_mux = subprocess.Popen(ffmpeg_mux_cmd, stdin=p_encode.stdout, stderr=subprocess.PIPE, text=True, bufsize=1)

        p_decode.stdout.close() 
        p_encode.stdout.close()
        
        # progress bar
        frame_re = re.compile(r"frame=\s*(\d+)")
        
        for line in p_mux.stderr:
            match = frame_re.search(line)
            if match:
                current_frame = int(match.group(1))
                percent = min(100, int((current_frame / total_frames) * 100))
                bar = "#" * (percent // 2) + "-" * (50 - (percent // 2))
                sys.stdout.write(f"\r[*] Encoding: [{bar}] {percent}% ({current_frame}/{total_frames} frames)")
                sys.stdout.flush()
                
        p_mux.communicate() 
        print()
        
        if os.path.exists("payload.bin"):
            raise Exception("[-] FATAL: The video was too short. The payload was not fully injected.")

        print("[*] Stitching audio track")
        
        # Stitch audio and video
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", 
            "-i", temp_mp4, 
            "-i", in_video, 
            "-c:v", "copy", "-c:a", "copy", 
            "-map", "0:v:0", "-map", "1:a:0?", 
            output_mp4
        ])
        
        print(f"[*] Data hidden inside {output_mp4}")
    except Exception as e:
        print(e.args[0])
    finally:
        # Clean up temp files
        for f in ["payload.bin", "welsenc.cfg", "temp_clean_video.mp4"]:
            if os.path.exists(f): os.remove(f)


def unhide_data(stego_video, password):
    print(f"[*] Extraction on {stego_video}")
    temp_264 = "temp_extract.264"
    
    try:
        print("[*] Ripping H.264 bitstream from MP4")
        ffmpeg_cmd = ["ffmpeg", "-y", "-i", stego_video, "-c:v", "copy", "-bsf:v", "h264_mp4toannexb", temp_264]
        subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        print("[*] hide64 decoder extracts binary payload")
        # output = NUL -> delete it
        subprocess.run(["./hide64_dec.exe", temp_264, "NUL"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        extracted_files = glob.glob("extracted_payload*")
        if not extracted_files:
            raise Exception("[-] Error: No payload was extracted or detected")

        file = extracted_files[0]
        print(f"[*] Found payload: {file}")
        with open(file, "rb") as f: payload = f.read()

        try:
            data = payload
            if password:
                print("[*] Decrypting")
                fernet = Fernet(generate_key(password))
                data = fernet.decrypt(payload)

            original_ext = os.path.splitext(file)[1]
            final_filename = f"{os.path.basename(stego_video)}_secret{original_ext}"

            with open(final_filename, "wb") as f: f.write(data)
            print(f"[*] Success. Data saved to: {final_filename}")
            os.remove(file)
        except Exception as e:
            print(f"[-] Decryption failed! Wrong password or corrupted payload. (Error: {e})")

    except Exception as e:
        print(e.args[0])
    finally:
        # Clean up temp files
        if os.path.exists(temp_264): os.remove(temp_264)


if __name__ == "__main__":
    # Disable default to use '-h' for 'hide'
    parser = argparse.ArgumentParser(description="HIDE64 - Video Steganography Tool", add_help=False)
    
    # Arguments
    parser.add_argument('--help', action='help', help='Show this help message and exit')
    parser.add_argument('-h', '--hide', type=str, metavar='VIDEO', help='Video file to hide information in')
    parser.add_argument('-s', '--secret', type=str, metavar='FILE', help='Secret file to hide into the video')
    parser.add_argument('-u', '--unhide', type=str, metavar='VIDEO', help='Video file to uncover secrets from')
    parser.add_argument('-p', '--password', type=str, metavar='PASS', default=None, help='Password for encryption/decryption (optional)')
    parser.add_argument('-o', '--output', type=str, metavar='OUTPUT', help='Output filepath for the stego video (default: stego_<input_video>)')

    args = parser.parse_args()

    # Show help if  no arguments are provided
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    # HIDE MODE
    if args.hide or args.secret:
        if not (args.hide and args.secret):
            parser.error("Arguments -h/--hide and -s/--secret must be used together.")
        if args.unhide:
            parser.error("Cannot use hide (-h/-s) and unhide (-u) modes at the same time.")

        # --- validation ---
        if not os.path.isfile(args.hide):
            parser.error(f"Input video file not found: '{args.hide}'")
        if not os.path.isfile(args.secret):
            parser.error(f"Secret file not found: '{args.secret}'")
        
        check_dependencies("hide")

        if not is_valid_video(args.hide):
            parser.error(f"Input file is not a valid video container: '{args.hide}'")

        # Handle Default Output Path
        output_video = args.output
        if not output_video:
            dir_name = os.path.dirname(os.path.abspath(args.hide))
            base_name = os.path.basename(args.hide)
            output_video = os.path.join(dir_name, f"stego_{base_name}")

        hide_data(args.hide, args.secret, output_video, args.password)

    # UNHIDE MODE
    elif args.unhide:
        if args.output:
            print("[!] Warning: Output argument (-o) is ignored in unhide mode.")

        # --- validation ---
        if not os.path.isfile(args.unhide):
            parser.error(f"Input stego video not found: '{args.unhide}'")

        check_dependencies("unhide")

        if not is_valid_video(args.unhide):
            parser.error(f"Input file is not a valid video container: '{args.unhide}'")
            
        unhide_data(args.unhide, args.password)