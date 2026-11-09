import os
import time
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm.auto import tqdm

# ============================================================
# 1. CẤU HÌNH
# ============================================================
M3U8_PATH = "/content/hh3d.m3u8"  # Đường dẫn file m3u8 của bạn
OUTPUT_MP4 = None                 # Tên file output (None để tự động đặt theo tên m3u8)
WORKERS = 8                       # Số lượng thread tải song song
RETRIES = 3                       # Số lần thử lại khi tải lỗi
KEEP_PNG = False                  # Có giữ lại file .png sau khi cắt header không
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"

M3U8_PATH = Path(M3U8_PATH).resolve()
if not M3U8_PATH.is_file():
    raise FileNotFoundError(f"Không tìm thấy file M3U8: {M3U8_PATH}")

VIDEO_NAME = M3U8_PATH.stem
SEGMENT_DIR = M3U8_PATH.parent / f"{VIDEO_NAME}_segments"
SEGMENT_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_FILE = (
    M3U8_PATH.parent / f"{VIDEO_NAME}.mp4"
    if not OUTPUT_MP4
    else Path(OUTPUT_MP4).resolve()
)

if not shutil.which("ffmpeg"):
    raise RuntimeError("Không tìm thấy FFmpeg trong hệ thống!")

# ============================================================
# 2. CÁC HÀM XỬ LÝ
# ============================================================

def read_m3u8(path):
    path = Path(path)
    with open(path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    lines = content.splitlines()
    if not lines or not lines[0].strip().startswith("#EXTM3U"):
        raise ValueError("File M3U8 không hợp lệ (thiếu #EXTM3U)!")

    if any(line.strip().startswith("#EXT-X-STREAM-INF") for line in lines):
        raise ValueError("Đây là Master M3U8! Vui lòng chọn playlist con.")

    segments = []
    base_url = path.parent.as_uri() + "/" if path.is_absolute() else str(path.parent) + "/"

    for line in lines:
        item = line.strip()
        if not item or item.startswith("#"):
            continue

        if item.startswith(("http://", "https://", "file://")):
            segment_url = item
        else:
            segment_url = urllib.parse.urljoin(base_url, item)
        
        segments.append(segment_url)

    if not segments:
        raise ValueError("Không tìm thấy segment nào trong file M3U8!")

    return lines, segments


def download_one(args):
    idx, url = args
    filename = f"{idx:05d}.png"
    filepath = SEGMENT_DIR / filename

    if filepath.exists() and filepath.stat().st_size > 0:
        return idx, True

    temp_path = filepath.with_suffix(".part")

    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as response:
                data = response.read()

            if not data:
                raise ValueError("Segment rỗng!")

            with open(temp_path, "wb") as f:
                f.write(data)
            
            os.replace(temp_path, filepath)
            return idx, True

        except Exception as e:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            
            if attempt == RETRIES - 1:
                return idx, str(e)
            
            time.sleep(1.5) 

    return idx, False


def is_ts_at(data, start, min_packets=3):
    remaining = len(data) - start
    if remaining <= 0:
        return False
    checks = min(min_packets, remaining // 188)
    if checks == 0:
        return remaining > 0 and data[start] == 0x47
    return all(data[start + i * 188] == 0x47 for i in range(checks))


def find_ts_start(data):
    iend = data.find(b"\x00\x00\x00\x00IEND")
    if iend != -1:
        start = iend + 12
        if is_ts_at(data, start):
            return start

    for i in range(max(0, len(data) - 376)):
        if data[i] == 0x47 and is_ts_at(data, i):
            return i

    if is_ts_at(data, 0):
        return 0

    return None


def strip_png_header(data):
    start = find_ts_start(data)
    if start is None:
        raise ValueError("Không tìm thấy MPEG-TS hợp lệ trong segment!")

    ts = data[start:]
    remainder = len(ts) % 188
    if remainder:
        ts = ts[:-remainder]

    if not ts or ts[0] != 0x47 or len(ts) % 188 != 0:
        raise ValueError("Dữ liệu TS sau khi cắt không hợp lệ!")

    return ts


# ============================================================
# 3. KHỐI LỆNH CHÍNH
# ============================================================

if __name__ == "__main__":
    print("=" * 55)
    print("BẮT ĐẦU XỬ LÝ M3U8")
    print("=" * 55)
    print("Playlist :", M3U8_PATH)
    print("Output   :", OUTPUT_FILE)
    print("Thư mục  :", SEGMENT_DIR)

    print("\n[1/4] ĐANG ĐỌC PLAYLIST...")
    orig_lines, segments = read_m3u8(M3U8_PATH)
    print(f"✅ Tìm thấy {len(segments)} segments.")

    print("\n[2/4] ĐANG TẢI SEGMENT...")
    jobs = list(enumerate(segments))
    errors = []

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(download_one, job) for job in jobs]
        with tqdm(total=len(futures), desc="Downloading", unit="segment") as pbar:
            for future in as_completed(futures):
                idx, result = future.result()
                if result is not True:
                    errors.append((idx, result))
                pbar.update(1)

    if errors:
        print("\n⚠️ Có segment tải thất bại:")
        for idx, error in errors[:5]:
            print(f"  Segment {idx}: {error}")
        raise RuntimeError("Tải chưa hoàn tất. Hãy chạy lại để tải tiếp.")
    print("✅ Tải hoàn tất!")

    print("\n[3/4] ĐANG CẮT HEADER PNG - LẤY MPEG-TS...")
    for i in tqdm(range(len(segments)), desc="Processing", unit="segment"):
        png_path = SEGMENT_DIR / f"{i:05d}.png"
        ts_path = SEGMENT_DIR / f"{i:05d}.ts"

        if ts_path.exists() and ts_path.stat().st_size > 0:
            continue

        with open(png_path, "rb") as f:
            raw = f.read()

        ts = strip_png_header(raw)

        with open(ts_path, "wb") as f:
            f.write(ts)

        if not KEEP_PNG and png_path.exists():
            png_path.unlink()
    print("✅ Cắt header hoàn tất!")

    print("\n[4/4] ĐANG TẠO PLAYLIST LOCAL SẠCH...")
    LOCAL_M3U8 = SEGMENT_DIR / f"{VIDEO_NAME}_local.m3u8"
    
    # Tái tạo file M3U8 hoàn toàn mới để tránh lỗi ký tự ẩn / định dạng dòng
    with open(LOCAL_M3U8, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write("#EXT-X-VERSION:3\n")
        f.write("#EXT-X-MEDIA-SEQUENCE:0\n")
        
        seg_idx = 0
        for line in orig_lines:
            s = line.strip()
            # Giữ lại các tag quan trọng như EXTINF
            if s.startswith("#EXTINF:"):
                f.write(s + "\n")
            # Thay thế dòng URL bằng tên file local
            elif not s.startswith("#") and s:
                f.write(f"{seg_idx:05d}.ts\n")
                seg_idx += 1
        
        f.write("#EXT-X-ENDLIST\n") # Thẻ kết thúc bắt buộc để FFmpeg hoạt động ổn định

    # Kiểm tra debug: In ra 5 dòng đầu để đảm bảo file đúng
    print("✅ Đã tạo playlist local. Kiểm tra nhanh nội dung:")
    with open(LOCAL_M3U8, "r", encoding="utf-8") as f:
        sample = f.readlines()[:5]
        for ln in sample:
            print("  ", ln.strip())

    print("\n[5/5] ĐANG GHÉP VIDEO BẰNG FFMPEG...")
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-allowed_extensions", "ALL",
        "-i", str(LOCAL_M3U8),
        "-map", "0",
        "-c", "copy",
        "-movflags", "+faststart",
        str(OUTPUT_FILE)
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("\n❌ FFmpeg gặp lỗi:")
        print(result.stderr)
        
        # Thêm kiểm tra phụ nếu vẫn lỗi
        if LOCAL_M3U8.stat().st_size == 0:
            print("⚠️ CẢNH BÁO: File M3U8 local bị rỗng (0 bytes).")
        raise RuntimeError("Ghép video thất bại!")

    print("\n" + "=" * 55)
    print("🎉 HOÀN THÀNH! 🎉")
    print("=" * 55)
    print("Video :", OUTPUT_FILE)
    print("Size  :", round(OUTPUT_FILE.stat().st_size / 1024 / 1024, 2), "MB")
    print("Folder:", SEGMENT_DIR)

    try:
        from IPython.display import display, FileLink
        display(FileLink(str(OUTPUT_FILE)))
    except ImportError:
        pass
