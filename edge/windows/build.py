"""
Builds dist/Cam2Drone-Setup.exe (one file, no console window) on Windows.

  python -m pip install pyinstaller onvif-zeep
  python edge/windows/build.py

Downloads the two bundled programs into edge/windows/bin/ the first time (not kept in git):
  go2rtc.exe  - github.com/AlexxIT/go2rtc releases (MIT)
  ffmpeg.exe  - github.com/BtbN/FFmpeg-Builds releases, LGPL build
"""
import io
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
EDGE = os.path.dirname(HERE)
BIN = os.path.join(HERE, "bin")
GO2RTC = "https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_win64.zip"
FFMPEG = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-win64-lgpl-8.1.zip"


def fetch_zip_member(url, suffix, dest):
    if os.path.exists(dest):
        return
    print("downloading", url)
    data = urllib.request.urlopen(url, timeout=300).read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(suffix))
        with z.open(name) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


def main():
    os.makedirs(BIN, exist_ok=True)
    fetch_zip_member(GO2RTC, "go2rtc.exe", os.path.join(BIN, "go2rtc.exe"))
    fetch_zip_member(FFMPEG, "bin/ffmpeg.exe", os.path.join(BIN, "ffmpeg.exe"))
    sep = os.pathsep
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--noconsole",
           "--name", "Cam2Drone-Setup",
           "--paths", EDGE,
           "--hidden-import", "agent",
           "--collect-all", "onvif", "--collect-all", "zeep",
           "--add-binary", f"{os.path.join(BIN, 'go2rtc.exe')}{sep}bin",
           "--add-binary", f"{os.path.join(BIN, 'ffmpeg.exe')}{sep}bin",
           "--add-data", f"{os.path.join(HERE, 'THIRD_PARTY.txt')}{sep}.",
           "--distpath", os.path.join(HERE, "dist"), "--workpath", os.path.join(HERE, "build"), "--specpath", os.path.join(HERE, "build"),
           os.path.join(HERE, "cam2drone_edge.py")]
    subprocess.run(cmd, check=True)
    print("ready:", os.path.join(HERE, "dist", "Cam2Drone-Setup.exe"))


if __name__ == "__main__":
    main()
