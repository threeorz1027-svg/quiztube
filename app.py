from __future__ import annotations

import html
import base64
import hashlib
import json
import math
import mimetypes
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from openai import OpenAI
from pydantic import BaseModel, Field, HttpUrl


TZ_UTC8 = timezone(timedelta(hours=8))
ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "notes.json"
SUMMARY_CACHE_PATH = DATA_DIR / "summary_cache.json"
TRANSCRIPT_CACHE_PATH = DATA_DIR / "transcript_cache.json"
QUIZ_STORE_PATH = DATA_DIR / "quiz_store.json"
AI_CONFIG_PATH = DATA_DIR / "ai_config.json"
HTML_PATH = ROOT_DIR / "videonote-preview.html"
SUBTITLE_LANG_PRIORITY = ["zh-CN", "zh-Hans", "zh", "en"]
DEFAULT_CHAT_MODEL = "mimo-v2-flash"
DEFAULT_TRANSCRIBE_MODEL = "mimo-v2-omni"
DEFAULT_OPENAI_COMPATIBLE_BASE_URL = "https://api.xiaomimimo.com/v1"
DEFAULT_TRANSCRIBE_BASE_URL = "https://api.xiaomimimo.com/v1"
DEFAULT_TRANSCRIBE_LANGUAGE = "zh"
DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def now_iso() -> str:
    return datetime.now(TZ_UTC8).isoformat()


def ensure_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        return
    initial = {"notes": [], "meta": {"streak_days": 7}}
    DB_PATH.write_text(json.dumps(initial, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_summary_cache() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SUMMARY_CACHE_PATH.exists():
        return
    SUMMARY_CACHE_PATH.write_text(json.dumps({"items": {}}, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_transcript_cache() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if TRANSCRIPT_CACHE_PATH.exists():
        return
    TRANSCRIPT_CACHE_PATH.write_text(json.dumps({"items": {}}, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_quiz_store() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if QUIZ_STORE_PATH.exists():
        return
    QUIZ_STORE_PATH.write_text(
        json.dumps({"questions": [], "sessions": []}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def default_ai_config() -> dict[str, Any]:
    return {
        "chat": {"base_url": "", "model": "", "api_key": ""},
        "asr": {"base_url": "", "model": "", "language": "", "api_key": ""},
    }


def ensure_ai_config() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if AI_CONFIG_PATH.exists():
        return
    AI_CONFIG_PATH.write_text(json.dumps(default_ai_config(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_db() -> dict[str, Any]:
    ensure_db()
    return json.loads(DB_PATH.read_text(encoding="utf-8"))


def save_db(payload: dict[str, Any]) -> None:
    ensure_db()
    temp_path = DB_PATH.with_name(f"{DB_PATH.stem}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(DB_PATH)


def load_summary_cache() -> dict[str, Any]:
    ensure_summary_cache()
    return json.loads(SUMMARY_CACHE_PATH.read_text(encoding="utf-8"))


def save_summary_cache(payload: dict[str, Any]) -> None:
    ensure_summary_cache()
    temp_path = SUMMARY_CACHE_PATH.with_name(f"{SUMMARY_CACHE_PATH.stem}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(SUMMARY_CACHE_PATH)


def load_transcript_cache() -> dict[str, Any]:
    ensure_transcript_cache()
    return json.loads(TRANSCRIPT_CACHE_PATH.read_text(encoding="utf-8"))


def save_transcript_cache(payload: dict[str, Any]) -> None:
    ensure_transcript_cache()
    temp_path = TRANSCRIPT_CACHE_PATH.with_name(f"{TRANSCRIPT_CACHE_PATH.stem}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(TRANSCRIPT_CACHE_PATH)


def load_quiz_store() -> dict[str, Any]:
    ensure_quiz_store()
    payload = json.loads(QUIZ_STORE_PATH.read_text(encoding="utf-8"))
    payload.setdefault("questions", [])
    payload.setdefault("sessions", [])
    return payload


def save_quiz_store(payload: dict[str, Any]) -> None:
    ensure_quiz_store()
    temp_path = QUIZ_STORE_PATH.with_name(f"{QUIZ_STORE_PATH.stem}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(QUIZ_STORE_PATH)


def load_ai_config() -> dict[str, Any]:
    ensure_ai_config()
    payload = json.loads(AI_CONFIG_PATH.read_text(encoding="utf-8"))
    defaults = default_ai_config()
    for section, values in defaults.items():
        current = payload.get(section)
        if not isinstance(current, dict):
            payload[section] = values.copy()
            continue
        for key, default_val in values.items():
            if not isinstance(current.get(key), str):
                current[key] = default_val
    return payload


def save_ai_config(payload: dict[str, Any]) -> None:
    ensure_ai_config()
    temp_path = AI_CONFIG_PATH.with_name(f"{AI_CONFIG_PATH.stem}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(AI_CONFIG_PATH)


def validate_bilibili_url(url: str) -> bool:
    pattern = r"^https?://(?:www\.)?bilibili\.com/video/BV[0-9A-Za-z]{10,}"
    return bool(re.match(pattern, url.strip()))


def validate_bilibili_playlist_url(url: str) -> bool:
    return validate_bilibili_url(url)


def parse_bilibili_p_from_url(url: str) -> int | None:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    raw = (query.get("p") or [None])[0]
    if not raw:
        return None
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return None


def build_bilibili_part_url(base_url: str, p_index: int) -> str:
    parsed = urllib.parse.urlparse(base_url)
    query = urllib.parse.parse_qs(parsed.query)
    query["p"] = [str(max(1, p_index))]
    new_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


def derive_note_status(note: dict[str, Any]) -> str:
    next_review_at = (note.get("next_review_at") or "").strip()
    if not next_review_at:
        return "due"
    try:
        due_date = datetime.fromisoformat(next_review_at).date()
    except ValueError:
        return "due"
    today = datetime.now(TZ_UTC8).date()
    if due_date < today:
        return "overdue"
    if due_date == today:
        return "due"
    return "upcoming"


def compute_next_interval_days(prev_interval_days: int, was_correct: bool) -> int:
    prev = max(1, int(prev_interval_days or 1))
    if not was_correct:
        return 1
    if prev <= 1:
        return 3
    return min(30, int(math.ceil(prev * 1.9)))


def normalize_quiz_generation_status(note: dict[str, Any]) -> str:
    status = str(note.get("quiz_generation_status") or "idle")
    if status in {"queued", "generating"}:
        total = int(note.get("quiz_total_count") or 0)
        pending = int(note.get("pending_quiz_count") or 0)
        if total > 0 or pending > 0:
            return "ready"
    return status

def sort_notes(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(notes, key=lambda n: n.get("last_edited_at", ""), reverse=True)


def extract_bvid(url: str) -> str:
    match = re.search(r"/video/([A-Za-z0-9]+)", url)
    return match.group(1) if match else "BV-UNKNOWN"

def normalize_bilibili_subtitle_url(raw_url: str) -> str:
    if raw_url.startswith("//"):
        return f"https:{raw_url}"
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url
    return f"https://{raw_url.lstrip('/')}"


def format_mmss(seconds: int) -> str:
    minute = max(0, seconds) // 60
    second = max(0, seconds) % 60
    return f"{minute:02d}:{second:02d}"


def timestamp_to_mmss(raw: str) -> str:
    match = re.match(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", raw.strip())
    if not match:
        return "00:00"
    if match.group(3):
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        return format_mmss(hours * 3600 + minutes * 60 + seconds)
    return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"


def ensure_yt_dlp_metadata(url: str) -> dict[str, Any]:
    command = [
        "yt-dlp",
        "-J",
        "--no-warnings",
        "--skip-download",
        *build_yt_dlp_network_args(),
        url,
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="服务端未安装 yt-dlp，无法抓取字幕") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="字幕抓取超时，请稍后重试") from exc
    except subprocess.CalledProcessError as exc:
        message = normalize_yt_dlp_error_message(exc.stderr.strip() or "无法解析该视频信息")
        if "412" in message or "Precondition Failed" in message:
            message = (
                "字幕抓取失败：B站返回 412（风控拦截）。请在启动前设置 "
                "YTDLP_COOKIES_FROM_BROWSER=chrome（或 edge/safari）"
                "，或设置 YTDLP_COOKIES_FILE 指向 cookies.txt 后重试。"
            )
        raise HTTPException(status_code=400, detail=message) from exc

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="字幕元数据解析失败") from exc


def build_bilibili_cookie_header() -> str | None:
    raw_cookie = (os.getenv("BILIBILI_COOKIE") or "").strip()
    if raw_cookie:
        return raw_cookie
    sessdata = (os.getenv("BILIBILI_SESSDATA") or "").strip()
    if sessdata:
        return f"SESSDATA={sessdata}"
    return None


def is_bilibili_host(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return "bilibili.com" in host or "bilivideo.com" in host


def build_request_headers(url: str, extra_headers: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"User-Agent": DEFAULT_BROWSER_UA}
    if is_bilibili_host(url):
        cookie_header = build_bilibili_cookie_header()
        if cookie_header:
            headers["Cookie"] = cookie_header
        headers["Referer"] = "https://www.bilibili.com/"
        headers["Origin"] = "https://www.bilibili.com"
    if extra_headers:
        headers.update(extra_headers)
    return headers


def should_retry_fetch(exc: urllib.error.URLError) -> bool:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, TimeoutError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        code = int(exc.code or 0)
        return code == 429 or code >= 500
    return False


def fetch_text_from_url(
    url: str,
    *,
    retries: int = 0,
    retry_delay: float = 0.35,
    extra_headers: dict[str, str] | None = None,
) -> str:
    request = urllib.request.Request(url, headers=build_request_headers(url, extra_headers=extra_headers))
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="ignore")
        except urllib.error.URLError as exc:
            if attempt >= retries or not should_retry_fetch(exc):
                raise HTTPException(status_code=502, detail="字幕文件拉取失败") from exc
            attempt += 1
            time.sleep(retry_delay * attempt)


def fetch_json_from_url(
    url: str,
    *,
    retries: int = 0,
    retry_delay: float = 0.35,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    text = fetch_text_from_url(url, retries=retries, retry_delay=retry_delay, extra_headers=extra_headers)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="B站接口返回数据解析失败") from exc


def fetch_bilibili_view_info(bvid: str) -> dict[str, Any]:
    api = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    payload = fetch_json_from_url(api, retries=2)
    if payload.get("code") != 0:
        raise HTTPException(status_code=400, detail=f"B站视频信息获取失败：{payload.get('message', 'unknown')}")
    return payload.get("data") or {}


def fetch_bilibili_subtitle_rows(bvid: str, source_url: str | None = None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    view = fetch_bilibili_view_info(bvid)
    pages = view.get("pages") or []
    selected_page: dict[str, Any] | None = None
    page_index = parse_bilibili_p_from_url(source_url or "") or 1
    if isinstance(pages, list) and pages:
        if 1 <= page_index <= len(pages) and isinstance(pages[page_index - 1], dict):
            selected_page = pages[page_index - 1]
        elif isinstance(pages[0], dict):
            selected_page = pages[0]
    cid = (selected_page or {}).get("cid") or view.get("cid")
    if not cid:
        raise HTTPException(status_code=400, detail="B站视频缺少 CID，无法获取字幕")

    subtitle_api = f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}"
    subtitle_payload = fetch_json_from_url(subtitle_api, retries=2)
    if subtitle_payload.get("code") != 0:
        raise HTTPException(status_code=400, detail=f"B站字幕接口失败：{subtitle_payload.get('message', 'unknown')}")

    subtitle_info = ((subtitle_payload.get("data") or {}).get("subtitle") or {})
    subtitle_list = subtitle_info.get("subtitles") or []
    if not subtitle_list:
        return view, []

    preferred: dict[str, Any] | None = None
    for lang in SUBTITLE_LANG_PRIORITY:
        preferred = next((s for s in subtitle_list if s.get("lan") == lang), None)
        if preferred:
            break
    if not preferred:
        preferred = subtitle_list[0]

    subtitle_url = preferred.get("subtitle_url") or preferred.get("url")
    if not subtitle_url:
        return view, []
    subtitle_url = normalize_bilibili_subtitle_url(subtitle_url)

    subtitle_body = fetch_json_from_url(subtitle_url, retries=2)
    rows: list[dict[str, str]] = []
    for item in subtitle_body.get("body", []):
        content = (item.get("content") or "").strip()
        if not content:
            continue
        start = int(float(item.get("from", 0)))
        rows.append({"timestamp": format_mmss(start), "text": content})
    return view, rows


def fetch_bilibili_playlist_parts(url: str) -> tuple[str, list[dict[str, Any]]]:
    metadata = ensure_yt_dlp_metadata(url)
    playlist_title = (metadata.get("title") or metadata.get("playlist_title") or "合集").strip()
    entries = metadata.get("entries") or []
    parts: list[dict[str, Any]] = []
    if isinstance(entries, list) and entries:
        for idx, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                continue
            p_index = int(entry.get("playlist_index") or idx)
            part_url = entry.get("webpage_url") or build_bilibili_part_url(url, p_index)
            parts.append(
                {
                    "part_id": f"p{p_index}",
                    "p_index": p_index,
                    "title": (entry.get("title") or f"第 {p_index} 集").strip(),
                    "duration": int(entry.get("duration") or 0),
                    "url": part_url,
                }
            )
    else:
        current_p = parse_bilibili_p_from_url(url) or 1
        parts = [
            {
                "part_id": f"p{current_p}",
                "p_index": current_p,
                "title": (metadata.get("title") or "视频").strip(),
                "duration": int(metadata.get("duration") or 0),
                "url": build_bilibili_part_url(url, current_p),
            }
        ]

    dedup: dict[str, dict[str, Any]] = {}
    for part in parts:
        dedup[part["part_id"]] = part
    parts = sorted(dedup.values(), key=lambda p: p.get("p_index", 0))
    return playlist_title, parts


def select_playlist_parts(parts: list[dict[str, Any]], selected_part_ids: list[str]) -> list[dict[str, Any]]:
    if not selected_part_ids:
        return parts
    selected = set(selected_part_ids)
    return [part for part in parts if part.get("part_id") in selected]


def merge_playlist_transcript_rows(
    selected_parts: list[dict[str, Any]],
    strict_mode: bool,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[dict[str, Any]], int, str | None]:
    merged_rows: list[dict[str, str]] = []
    actual_source_parts: list[dict[str, Any]] = []
    skipped_parts: list[dict[str, Any]] = []
    total_duration = 0
    chosen_thumbnail: str | None = None
    for part in selected_parts:
        part_url = str(part.get("url") or "")
        part_title = str(part.get("title") or "")
        duration = int(part.get("duration") or 0)
        try:
            metadata, rows = load_transcript_from_bilibili(part_url)
        except HTTPException as exc:
            skipped = {"part_id": part.get("part_id"), "title": part_title, "reason": str(exc.detail)}
            if strict_mode:
                raise HTTPException(status_code=400, detail=f"分P转写失败：{part_title}，{exc.detail}") from exc
            skipped_parts.append(skipped)
            continue
        shifted = shift_rows_with_offset(rows, total_duration)
        merged_rows.extend(shifted)
        actual_source_parts.append(
            {
                "part_id": part.get("part_id"),
                "title": part_title,
                "url": part_url,
                "duration": duration,
                "offset_seconds": total_duration,
            }
        )
        total_duration += max(0, duration)
        if not chosen_thumbnail:
            chosen_thumbnail = metadata.get("thumbnail") or metadata.get("pic")
    return merged_rows, actual_source_parts, skipped_parts, total_duration, chosen_thumbnail


def build_playlist_metadata(
    playlist_title: str,
    parts: list[dict[str, Any]],
    selected_part_ids: list[str],
    total_duration_seconds: int,
    skipped_parts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "title": playlist_title,
        "total_parts": len(parts),
        "selected_parts": len(selected_part_ids) if selected_part_ids else len(parts),
        "total_duration_seconds": total_duration_seconds,
        "selected_part_ids": selected_part_ids or [p.get("part_id") for p in parts],
        "skipped_parts_count": len(skipped_parts),
    }


def build_playlist_summary_cache_key(url: str, selected_part_ids: list[str], model: str, transcript_text: str) -> str:
    digest = hashlib.sha256(transcript_text.encode("utf-8")).hexdigest()[:24]
    return f"playlist:{extract_bvid(url)}:{','.join(selected_part_ids)}:{model}:{digest}"


def pick_subtitle_track(metadata: dict[str, Any]) -> tuple[str, str] | None:
    candidates: list[tuple[str, str, str]] = []
    for source in ("subtitles", "automatic_captions"):
        lang_map = metadata.get(source, {}) or {}
        for lang, tracks in lang_map.items():
            for track in tracks or []:
                track_url = track.get("url")
                ext = (track.get("ext") or "").lower()
                if not track_url or ext not in {"vtt", "json3"}:
                    continue
                candidates.append((lang, ext, track_url))

    for lang in SUBTITLE_LANG_PRIORITY:
        for cand_lang, cand_ext, cand_url in candidates:
            if cand_lang == lang:
                return cand_ext, cand_url
    return (candidates[0][1], candidates[0][2]) if candidates else None


def parse_vtt(vtt_text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current_ts = "00:00"
    current_parts: list[str] = []

    def flush() -> None:
        if not current_parts:
            return
        text = " ".join(part.strip() for part in current_parts if part.strip())
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            rows.append({"timestamp": current_ts, "text": text})

    for line in vtt_text.splitlines():
        raw = line.strip()
        if not raw:
            flush()
            current_parts = []
            continue
        if raw.startswith("WEBVTT") or raw.startswith("NOTE"):
            continue
        if "-->" in raw:
            flush()
            current_parts = []
            start = raw.split("-->", maxsplit=1)[0].strip()
            current_ts = timestamp_to_mmss(start.split(".", maxsplit=1)[0])
            continue
        if raw.isdigit():
            continue
        current_parts.append(raw)

    flush()
    dedup: list[dict[str, str]] = []
    previous = ""
    for row in rows:
        if row["text"] == previous:
            continue
        dedup.append(row)
        previous = row["text"]
    return dedup


def parse_json3(json3_text: str) -> list[dict[str, str]]:
    try:
        payload = json.loads(json3_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="json3 字幕解析失败") from exc

    rows: list[dict[str, str]] = []
    for event in payload.get("events", []):
        segs = event.get("segs") or []
        text = "".join(seg.get("utf8", "") for seg in segs).strip()
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        start_ms = int(event.get("tStartMs", 0))
        rows.append({"timestamp": format_mmss(start_ms // 1000), "text": text})
    return rows


def download_audio_for_asr(url: str) -> Path:
    with tempfile.TemporaryDirectory(prefix="quiztube_asr_") as temp_dir:
        output_template = str(Path(temp_dir) / "audio.%(ext)s")
        command = [
            "yt-dlp",
            "--no-warnings",
            "--no-playlist",
            "-f",
            "ba",
            "--extract-audio",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "9",
            *build_yt_dlp_network_args(),
            "-o",
            output_template,
            url,
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=504, detail="ASR 音频下载超时，请稍后重试") from exc
        except subprocess.CalledProcessError as exc:
            message = normalize_yt_dlp_error_message(exc.stderr.strip() or "音频下载失败")
            if "412" in message or "Precondition Failed" in message:
                message = (
                    "ASR 预处理失败：B站返回 412（风控拦截）。请在启动前设置 "
                    "YTDLP_COOKIES_FROM_BROWSER=chrome（或 edge/safari）"
                    "，或设置 YTDLP_COOKIES_FILE 指向 cookies.txt。"
                )
            raise HTTPException(status_code=502, detail=message) from exc

        files = sorted(Path(temp_dir).glob("audio.*"))
        if not files:
            raise HTTPException(status_code=500, detail="ASR 音频文件生成失败")
        audio_file = files[0]
        cached_path = DATA_DIR / f"asr_{uuid.uuid4().hex}{audio_file.suffix}"
        cached_path.write_bytes(audio_file.read_bytes())
        return cached_path


def split_audio_for_asr(audio_path: Path, chunk_seconds: int = 240) -> list[Path]:
    chunking_enabled = os.getenv("QUIZTUBE_ASR_CHUNK_SECONDS", str(chunk_seconds))
    try:
        chunk_seconds = max(60, int(chunking_enabled))
    except ValueError:
        chunk_seconds = 240

    chunk_dir = DATA_DIR / f"asr_chunks_{uuid.uuid4().hex}"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    out_pattern = str(chunk_dir / "chunk_%03d.mp3")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-f",
        "segment",
        "-segment_time",
        str(chunk_seconds),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "48k",
        out_pattern,
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)
    except Exception:
        # ffmpeg 不可用或分片失败时，退化为整段音频。
        return [audio_path]

    chunks = sorted(chunk_dir.glob("chunk_*.mp3"))
    if not chunks:
        return [audio_path]
    return chunks


def transcribe_single_chunk(
    client: OpenAI,
    chunk_path: Path,
    base_url: str,
    transcribe_model: str,
    transcribe_language: str,
) -> list[dict[str, str]]:
    if "xiaomimimo.com" in base_url:
        audio_bytes = chunk_path.read_bytes()
        approx_base64_size = (len(audio_bytes) * 4) // 3
        if approx_base64_size > 50 * 1024 * 1024:
            raise HTTPException(
                status_code=413,
                detail="ASR 音频分片仍超过 50MB，请缩短音频后重试。",
            )
        mime_type = mimetypes.guess_type(chunk_path.name)[0] or "audio/mpeg"
        audio_data = base64.b64encode(audio_bytes).decode("utf-8")
        resp = client.chat.completions.create(
            model=transcribe_model,
            temperature=0,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": f"data:{mime_type};base64,{audio_data}"},
                        },
                        {
                            "type": "text",
                            "text": (
                                "请将音频逐段转写为文本，并输出 JSON。"
                                "格式：{\"segments\":[{\"timestamp\":\"MM:SS\",\"text\":\"...\"}]}"
                                "。timestamp 使用近似时间即可；若无法判断时间，使用 00:00。"
                            ),
                        },
                    ],
                }
            ],
        )
        return parse_transcribe_response_rows(resp, from_mimo_chat=True)

    with chunk_path.open("rb") as audio_file:
        request_kwargs: dict[str, Any] = {
            "model": transcribe_model,
            "file": audio_file,
            "response_format": "verbose_json",
            "timestamp_granularities": ["segment"],
        }
        if transcribe_language:
            request_kwargs["language"] = transcribe_language
        resp = client.audio.transcriptions.create(**request_kwargs)
    return parse_transcribe_response_rows(resp, from_mimo_chat=False)


def transcribe_audio_with_openai(audio_path: Path) -> list[dict[str, str]]:
    base_url, transcribe_model, transcribe_language = get_asr_config()
    api_key = get_asr_api_key()
    if not api_key:
        # Local NIM deployments can run without auth; OpenAI client still needs a placeholder.
        if "127.0.0.1" in base_url or "localhost" in base_url:
            api_key = "nim-local-no-key"
        else:
            raise HTTPException(
                status_code=500,
                detail="缺少 MIMO_TRANSCRIBE_API_KEY / MIMO_API_KEY（或 ARK/OPENAI 同名变量），无法执行 ASR 转写",
            )
    client = OpenAI(api_key=api_key, base_url=base_url)
    chunk_seconds = max(60, int(os.getenv("QUIZTUBE_ASR_CHUNK_SECONDS", "240")))
    chunk_paths = split_audio_for_asr(audio_path, chunk_seconds=chunk_seconds)

    try:
        all_rows: list[dict[str, str]] = []
        for idx, chunk_path in enumerate(chunk_paths):
            rows = transcribe_single_chunk(
                client=client,
                chunk_path=chunk_path,
                base_url=base_url,
                transcribe_model=transcribe_model,
                transcribe_language=transcribe_language,
            )
            offset = idx * chunk_seconds
            all_rows.extend(shift_rows_with_offset(rows, offset))
        rows = all_rows
    except Exception as exc:
        detail = f"ASR 转写失败：{exc}"
        lowered = str(exc).lower()
        if "404 page not found" in lowered and "integrate.api.nvidia.com" in base_url:
            detail = (
                "ASR 转写失败：当前 NVIDIA integrate 托管网关不支持 /v1/audio/transcriptions。"
                "若坚持全 NVIDIA，请改用已开通的托管 ASR 端点；否则只能使用自建 Speech NIM 或其他 ASR 服务。"
            )
        if "404 page not found" in lowered and "xiaomimimo.com" in base_url:
            detail = (
                "ASR 转写失败：小米 MiMo 返回 404。请检查 base_url 是否为 OpenAI 兼容地址（以 /v1 结尾），"
                "并确认当前账号已开通语音识别模型及正确的 MIMO_TRANSCRIBE_MODEL。"
            )
        if ("connection error" in lowered or "failed to connect" in lowered) and (
            "127.0.0.1" in base_url or "localhost" in base_url
        ):
            detail = (
                "ASR 转写失败：无法连接本地 NVIDIA ASR 服务。"
                "请先启动可访问的 NVIDIA Speech NIM（或把 OPENAI_TRANSCRIBE_BASE_URL "
                "改为远端 NIM 地址）。"
            )
        raise HTTPException(status_code=502, detail=detail) from exc
    finally:
        if audio_path.exists():
            audio_path.unlink(missing_ok=True)
        for chunk_path in chunk_paths:
            if chunk_path != audio_path and chunk_path.exists():
                chunk_path.unlink(missing_ok=True)
        # 删除分片目录
        for chunk_path in chunk_paths:
            if chunk_path != audio_path:
                parent = chunk_path.parent
                if parent.exists() and parent.name.startswith("asr_chunks_"):
                    try:
                        parent.rmdir()
                    except OSError:
                        pass
    return rows


def parse_transcribe_response_rows(resp: Any, from_mimo_chat: bool) -> list[dict[str, str]]:
    if not from_mimo_chat:
        rows: list[dict[str, str]] = []
        segments = getattr(resp, "segments", None) or []
        for seg in segments:
            text = (getattr(seg, "text", "") or "").strip()
            if not text:
                continue
            start = int(getattr(seg, "start", 0) or 0)
            rows.append({"timestamp": format_mmss(start), "text": text})
        return rows

    content = ""
    choices = getattr(resp, "choices", None) or []
    if choices and getattr(choices[0], "message", None):
        content = getattr(choices[0].message, "content", "") or ""
    content = content.strip()
    if not content:
        return []

    try:
        payload = extract_json_object(content)
        segments = payload.get("segments") if isinstance(payload, dict) else None
        if isinstance(segments, list):
            parsed_rows: list[dict[str, str]] = []
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                text = str(seg.get("text") or "").strip()
                if not text:
                    continue
                raw_ts = str(seg.get("timestamp") or "00:00")
                parsed_rows.append({"timestamp": timestamp_to_mmss(raw_ts), "text": text})
            if parsed_rows:
                return parsed_rows
    except Exception:
        pass

    # Fallback: treat each non-empty line as one segment.
    rows: list[dict[str, str]] = []
    for line in content.splitlines():
        text = line.strip()
        if not text:
            continue
        text = re.sub(r"^\[\d{1,2}:\d{2}\]\s*", "", text)
        rows.append({"timestamp": "00:00", "text": text})
    return rows


def load_transcript_from_bilibili(url: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    started_total = time.perf_counter()
    bvid = extract_bvid(url)
    fallback_meta: dict[str, Any] = {
        "title": f"视频笔记 - {bvid}",
        "uploader": "B站UP主",
        "thumbnail": None,
        "duration": 0,
        "duration_string": "00:00",
    }
    _, asr_model, asr_language = get_asr_config()
    part_index = parse_bilibili_p_from_url(url) or 1
    part_token = f"p{part_index}"
    transcript_cache_key = build_transcript_cache_key(bvid, asr_model, asr_language, part_token=part_token)
    legacy_cache_key = build_transcript_cache_key(bvid, asr_model, asr_language)
    if os.getenv("QUIZTUBE_ENABLE_TRANSCRIPT_CACHE", "1") == "1":
        cached_meta, cached_rows = get_cached_transcript_entry(transcript_cache_key)
        if not cached_rows:
            cached_meta, cached_rows = get_cached_transcript_entry(legacy_cache_key)
        if cached_rows:
            print(f"[CACHE] transcript hit: {transcript_cache_key}")
            log_stage_timing("transcript_total (cache)", started_total)
            return (cached_meta or fallback_meta), cached_rows

    # 1) 参考 BiliNote：优先走 B 站官方字幕接口
    official_subtitle_state = "error"
    try:
        view_info, rows = fetch_bilibili_subtitle_rows(bvid, source_url=url)
        fallback_meta = {
            "title": view_info.get("title") or fallback_meta["title"],
            "uploader": ((view_info.get("owner") or {}).get("name")) or fallback_meta["uploader"],
            "thumbnail": view_info.get("pic"),
            "duration": view_info.get("duration") or 0,
            "duration_string": format_mmss(int(view_info.get("duration") or 0)),
        }
        if rows:
            official_subtitle_state = "found"
            if os.getenv("QUIZTUBE_ENABLE_TRANSCRIPT_CACHE", "1") == "1":
                set_cached_transcript_rows(transcript_cache_key, rows, fallback_meta)
            log_stage_timing("transcript_total (official_subtitle)", started_total)
            return fallback_meta, rows
        official_subtitle_state = "empty"
    except HTTPException:
        # 官方接口失败时继续兜底，不直接中断
        pass

    asr_enabled = os.getenv("QUIZTUBE_ENABLE_ASR_FALLBACK", "1") == "1"
    # 2) 兜底：yt-dlp 拉字幕（官方明确无字幕时可直走 ASR）
    skip_ytdlp_on_official_empty = (
        asr_enabled
        and official_subtitle_state == "empty"
        and os.getenv("QUIZTUBE_FAST_ASR_ON_OFFICIAL_EMPTY", "1") == "1"
    )
    metadata: dict[str, Any] = fallback_meta
    ytdlp_error: str | None = None
    if not skip_ytdlp_on_official_empty:
        try:
            metadata = ensure_yt_dlp_metadata(url)
            track = pick_subtitle_track(metadata)
            if track:
                ext, subtitle_url = track
                text = fetch_text_from_url(subtitle_url)
                rows = parse_vtt(text) if ext == "vtt" else parse_json3(text)
                if rows:
                    if os.getenv("QUIZTUBE_ENABLE_TRANSCRIPT_CACHE", "1") == "1":
                        set_cached_transcript_rows(transcript_cache_key, rows, metadata)
                    log_stage_timing("transcript_total (ytdlp_subtitle)", started_total)
                    return metadata, rows
        except HTTPException as exc:
            ytdlp_error = exc.detail if isinstance(exc.detail, str) else "yt-dlp 抓取失败"

    if asr_enabled:
        download_started = time.perf_counter()
        audio_path = download_audio_for_asr(url)
        log_stage_timing("asr_download", download_started)

        transcribe_started = time.perf_counter()
        rows = transcribe_audio_with_openai(audio_path)
        log_stage_timing("asr_transcribe", transcribe_started)
        if rows:
            if os.getenv("QUIZTUBE_ENABLE_TRANSCRIPT_CACHE", "1") == "1":
                set_cached_transcript_rows(transcript_cache_key, rows, metadata)
            log_stage_timing("transcript_total (asr)", started_total)
            return metadata, rows

    if ytdlp_error:
        raise HTTPException(status_code=400, detail=ytdlp_error)

    raise HTTPException(
        status_code=400,
        detail="该视频无可用字幕，且 ASR 转写未启用或失败，暂时无法生成笔记",
    )


def build_yt_dlp_network_args() -> list[str]:
    args = [
        "--add-header",
        "Referer:https://www.bilibili.com",
        "--add-header",
        "Origin:https://www.bilibili.com",
        "--add-header",
        f"User-Agent:{DEFAULT_BROWSER_UA}",
    ]
    cookies_from_browser = (os.getenv("YTDLP_COOKIES_FROM_BROWSER") or "").strip()
    cookies_file = (os.getenv("YTDLP_COOKIES_FILE") or "").strip()
    if cookies_from_browser:
        args.extend(["--cookies-from-browser", cookies_from_browser])
    elif cookies_file:
        args.extend(["--cookies", cookies_file])
    return args


def normalize_yt_dlp_error_message(message: str) -> str:
    cleaned = []
    for line in message.splitlines():
        lower = line.lower()
        if "deprecated feature" in lower and "python version 3.9" in lower:
            continue
        cleaned.append(line.strip())
    return "\n".join(line for line in cleaned if line).strip() or "字幕抓取失败"


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _shrink_text_to_byte_limit(text: str, byte_limit: int) -> str:
    if _utf8_len(text) <= byte_limit:
        return text
    if byte_limit <= 0:
        return ""
    ratio = byte_limit / max(_utf8_len(text), 1)
    candidate = text[: max(1, int(len(text) * ratio))]
    while _utf8_len(candidate) > byte_limit and candidate:
        candidate = candidate[:-1]
    return candidate


def transcript_rows_to_prompt_text(
    rows: list[dict[str, str]],
    max_rows: int = 320,
    byte_limit: int = 6200,
) -> str:
    # Borrowing BibiGPT's idea: enforce prompt byte budget.
    clipped = rows[:max_rows]
    lines: list[str] = []
    for row in clipped:
        line = f"[{row['timestamp']}] {row['text']}".strip()
        if not line:
            continue
        candidate = "\n".join(lines + [line])
        if _utf8_len(candidate) <= byte_limit:
            lines.append(line)
            continue
        if not lines:
            lines.append(_shrink_text_to_byte_limit(line, byte_limit))
        break
    return "\n".join(lines)


def parse_mmss_to_seconds(raw: str) -> int:
    parts = [int(x) for x in re.findall(r"\d+", raw)]
    if len(parts) >= 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return 0


def shift_rows_with_offset(rows: list[dict[str, str]], offset_seconds: int) -> list[dict[str, str]]:
    if offset_seconds <= 0:
        return rows
    shifted: list[dict[str, str]] = []
    for row in rows:
        raw_ts = row.get("timestamp", "00:00")
        shifted.append(
            {
                "timestamp": format_mmss(parse_mmss_to_seconds(raw_ts) + offset_seconds),
                "text": row.get("text", ""),
            }
        )
    return shifted


def log_stage_timing(stage: str, started_at: float) -> None:
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    print(f"[TIMING] {stage}: {elapsed_ms:.1f} ms")


def extract_json_object(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end < 0 or end <= start:
        raise ValueError("LLM 未返回可解析 JSON")
    return json.loads(candidate[start : end + 1])


def get_chat_runtime_config() -> tuple[str, str, str | None]:
    config = load_ai_config()
    chat_cfg = config.get("chat") or {}
    model = (
        str(chat_cfg.get("model") or "").strip()
        or os.getenv("MIMO_CHAT_MODEL")
        or os.getenv("ARK_CHAT_MODEL")
        or os.getenv("OPENAI_CHAT_MODEL")
        or DEFAULT_CHAT_MODEL
    )
    base_url = (
        str(chat_cfg.get("base_url") or "").strip()
        or os.getenv("MIMO_BASE_URL")
        or os.getenv("ARK_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or DEFAULT_OPENAI_COMPATIBLE_BASE_URL
    ).rstrip("/")
    api_key = (
        str(chat_cfg.get("api_key") or "").strip()
        or os.getenv("MIMO_API_KEY")
        or os.getenv("ARK_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    return base_url, model, api_key or None


def get_llm_model_id() -> str:
    _, model, _ = get_chat_runtime_config()
    return model


def get_asr_config() -> tuple[str, str, str]:
    config = load_ai_config()
    asr_cfg = config.get("asr") or {}
    base_url = (
        str(asr_cfg.get("base_url") or "").strip()
        or os.getenv("MIMO_TRANSCRIBE_BASE_URL")
        or os.getenv("MIMO_BASE_URL")
        or os.getenv("ARK_TRANSCRIBE_BASE_URL")
        or os.getenv("ARK_BASE_URL")
        or os.getenv("OPENAI_TRANSCRIBE_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or DEFAULT_TRANSCRIBE_BASE_URL
    ).rstrip("/")
    model = (
        str(asr_cfg.get("model") or "").strip()
        or os.getenv("MIMO_TRANSCRIBE_MODEL")
        or os.getenv("ARK_TRANSCRIBE_MODEL")
        or os.getenv("OPENAI_TRANSCRIBE_MODEL")
        or DEFAULT_TRANSCRIBE_MODEL
    )
    language = (
        str(asr_cfg.get("language") or "").strip()
        or os.getenv("MIMO_TRANSCRIBE_LANGUAGE")
        or os.getenv("ARK_TRANSCRIBE_LANGUAGE")
        or os.getenv("OPENAI_TRANSCRIBE_LANGUAGE")
        or DEFAULT_TRANSCRIBE_LANGUAGE
    ).strip()
    return base_url, model, language


def get_asr_api_key() -> str | None:
    config = load_ai_config()
    asr_cfg = config.get("asr") or {}
    chat_cfg = config.get("chat") or {}
    api_key = (
        str(asr_cfg.get("api_key") or "").strip()
        or str(chat_cfg.get("api_key") or "").strip()
        or os.getenv("MIMO_TRANSCRIBE_API_KEY")
        or os.getenv("MIMO_API_KEY")
        or os.getenv("ARK_TRANSCRIBE_API_KEY")
        or os.getenv("OPENAI_TRANSCRIBE_API_KEY")
        or os.getenv("ARK_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    return api_key or None


def build_transcript_cache_key(video_id: str, asr_model: str, language: str, part_token: str | None = None) -> str:
    if part_token:
        return f"{video_id}:{part_token}:{asr_model}:{language or 'auto'}"
    return f"{video_id}:{asr_model}:{language or 'auto'}"


def get_cached_transcript_entry(cache_key: str) -> tuple[dict[str, Any] | None, list[dict[str, str]] | None]:
    payload = load_transcript_cache()
    item = (payload.get("items") or {}).get(cache_key)
    if not isinstance(item, dict):
        return None, None
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else None
    rows = item.get("rows")
    if not isinstance(rows, list):
        return metadata, None
    normalized: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        timestamp = timestamp_to_mmss(str(row.get("timestamp") or "00:00"))
        normalized.append({"timestamp": timestamp, "text": text})
    return metadata, normalized or None


def set_cached_transcript_rows(cache_key: str, rows: list[dict[str, str]], metadata: dict[str, Any] | None = None) -> None:
    payload = load_transcript_cache()
    items = payload.setdefault("items", {})
    items[cache_key] = {"created_at": now_iso(), "rows": rows, "metadata": metadata or {}}
    # Keep transcript cache bounded: newest 120 items.
    if len(items) > 120:
        keys = list(items.keys())
        for old_key in keys[:-120]:
            items.pop(old_key, None)
    save_transcript_cache(payload)


def build_summary_cache_key(video_id: str, model: str, transcript_text: str) -> str:
    digest = hashlib.sha256(transcript_text.encode("utf-8")).hexdigest()[:24]
    return f"{video_id}:{model}:{digest}"


def expected_note_range(duration_seconds: int) -> tuple[int, int]:
    if duration_seconds <= 30 * 60:
        return 4, 6
    if duration_seconds <= 60 * 60:
        return 6, 9
    return 9, 12


def get_cached_llm_summary(cache_key: str) -> dict[str, Any] | None:
    payload = load_summary_cache()
    item = (payload.get("items") or {}).get(cache_key)
    if not isinstance(item, dict):
        return None
    data = item.get("data")
    return data if isinstance(data, dict) else None


def set_cached_llm_summary(cache_key: str, llm_note: dict[str, Any]) -> None:
    payload = load_summary_cache()
    items = payload.setdefault("items", {})
    items[cache_key] = {"created_at": now_iso(), "data": llm_note}
    # Keep cache bounded: retain newest 200 items.
    if len(items) > 200:
        keys = list(items.keys())
        for old_key in keys[:-200]:
            items.pop(old_key, None)
    save_summary_cache(payload)


def call_llm_generate_cornell(
    video_title: str,
    transcript_text: str,
    model: str,
    duration_seconds: int = 0,
) -> dict[str, Any]:
    if os.getenv("QUIZTUBE_USE_FAKE_LLM") == "1":
        return {
            "title": video_title,
            "summary": "视频讲解了关键问题与解决路径，重点在于从原理到落地方法的串联。",
            "notes": [
                {
                    "cue": "这个视频最先定义了什么核心问题？",
                    "content": transcript_text.splitlines()[0].split("] ", maxsplit=1)[-1][:120],
                    "timestamp": transcript_text.splitlines()[0][1:6],
                    "keywords": ["核心问题", "定义"],
                },
                {
                    "cue": "关键解决策略是如何被提出与验证的？",
                    "content": transcript_text.splitlines()[1].split("] ", maxsplit=1)[-1][:120] if len(transcript_text.splitlines()) > 1 else "字幕较短，请补充视频内容后再生成。",
                    "timestamp": transcript_text.splitlines()[1][1:6] if len(transcript_text.splitlines()) > 1 else "00:00",
                    "keywords": ["策略", "验证"],
                },
            ],
        }

    base_url, _, api_key = get_chat_runtime_config()
    if not api_key:
        raise HTTPException(status_code=500, detail="缺少 MIMO_API_KEY（或 ARK/OPENAI_API_KEY），无法调用 LLM 生成笔记")
    client = OpenAI(api_key=api_key, base_url=base_url)

    min_notes, max_notes = expected_note_range(duration_seconds)
    system_prompt = (
        "你是一个专业的学习笔记助手，负责将视频字幕转化为结构化的康奈尔笔记。\n\n"
        "输入\n"
        "你会收到：\n"
        "- 视频标题\n"
        "- 带时间戳的字幕文本，格式为 [MM:SS] 内容\n\n"
        "输出格式\n"
        "严格输出以下 JSON 结构，不要输出任何额外内容：\n\n"
        "{\n"
        "  \"title\": \"视频标题\",\n"
        "  \"summary\": \"一句话核心提炼，不超过 80 字，说明这个视频最重要的一个结论或方法\",\n"
        "  \"notes\": [\n"
        "    {\n"
        "      \"cue\": \"这部分内容用一个问题来表达，以「什么是」「为什么」「如何」开头，让用户能通过回答这个问题来检验自己是否理解\",\n"
        "      \"content\": \"对应这个问题的详细笔记内容，用自己的语言重新表达，不要照抄字幕，保留关键术语\",\n"
        "      \"timestamp\": \"MM:SS\",\n"
        "      \"keywords\": [\"关键词1\", \"关键词2\"]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "笔记生成规则\n\n"
        "关于分组\n"
        "- 按「知识点」分组，不按时间分组\n"
        "- 每组对应一个完整的概念或论点\n"
        "- 视频时长 30 分钟以内生成 4～6 组，30～60 分钟生成 6～9 组，60 分钟以上生成 9～12 组\n"
        "- 每组之间要有明显的概念边界，不要强行切分连续的论述\n\n"
        "关于 Cue（左栏）\n"
        "- 必须是疑问句，不能是陈述句\n"
        "- 好的例子：「为什么模型输出的 JSON 字符串不能直接当字典用？」\n"
        "- 坏的例子：「JSON 字符串的问题」「关于数据类型」\n"
        "- 问题要能独立成立，即不看 content 也能理解这个问题在问什么\n\n"
        "关于 Content（右栏）\n"
        "- 用你自己的语言重新组织，不要逐字复述字幕\n"
        "- 可以适当补充逻辑连接，但不要引入字幕中没有的信息\n"
        "- 如果视频里有代码或公式，保留核心示例，去掉冗余演示\n\n"
        "关于 Timestamp\n"
        "- 取这组知识点在字幕中第一次出现的时间\n"
        "- 格式统一为 MM:SS（如 04:30，不要写成 4:30）\n\n"
        "关于 Summary\n"
        "- 只说最重要的一件事，不要罗列多个要点\n"
        "- 好的例子：「LangChain 中模型输出本质是字符串，必须用 StructuredOutputParser 解析后才能作为字典操作」\n"
        "- 坏的例子：「本视频介绍了 JSON 字符串的问题、解析器的使用方法以及如何构建自动化流程」\n\n"
        "注意事项\n"
        "- 如果字幕质量差（大量乱码、断句错误），在 JSON 外额外输出一行警告：WARNING: 字幕质量较低，笔记准确性可能受影响\n"
        "- 如果视频内容与学习无关（纯娱乐、音乐等），输出：{\"error\": \"该视频内容不适合生成学习笔记\"}\n"
    )
    user_prompt = (
        f"视频标题：{video_title}\n"
        f"视频时长（秒）：{max(duration_seconds, 0)}\n"
        f"本次必须输出 {min_notes}~{max_notes} 组 notes（若内容确实极少，至少输出 3 组并保持质量）。\n"
        "带时间戳字幕如下：\n"
        f"{transcript_text}\n\n"
        "请按约定 JSON 输出。"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    content = ""
    stream_error: Exception | None = None
    try:
        stream = client.chat.completions.create(
            model=model,
            temperature=0.2,
            messages=messages,
            stream=True,
        )
        chunks: list[str] = []
        for chunk in stream:
            piece = (chunk.choices[0].delta.content or "") if chunk.choices else ""
            if piece:
                chunks.append(piece)
        content = "".join(chunks).strip()
        if not content:
            raise ValueError("stream returned empty content")
    except Exception as exc:
        stream_error = exc
        try:
            # Degrade gracefully like BibiGPT: non-stream fallback.
            resp = client.chat.completions.create(
                model=model,
                temperature=0.2,
                messages=messages,
                stream=False,
            )
            content = (resp.choices[0].message.content or "").strip()
        except Exception as fallback_exc:
            detail = f"LLM 调用失败（stream+fallback）：{fallback_exc}"
            if stream_error:
                detail += f"；stream 错误：{stream_error}"
            raise HTTPException(status_code=502, detail=detail[:700]) from fallback_exc

    try:
        parsed = extract_json_object(content)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="LLM 返回内容无法解析为 JSON") from exc
    return parsed


def build_cornell_json_from_note(note: dict[str, Any]) -> dict[str, Any]:
    cues = note.get("cues") or []
    items = note.get("note_items") or []
    max_len = max(len(cues), len(items), 1)
    notes: list[dict[str, Any]] = []
    for idx in range(max_len):
        cue = str(cues[idx] if idx < len(cues) else f"知识点 {idx + 1}").strip()
        item = items[idx] if idx < len(items) and isinstance(items[idx], dict) else {}
        content = re.sub(r"<[^>]+>", "", str(item.get("content_html") or "")).strip()
        timestamp = timestamp_to_mmss(str(item.get("timestamp") or "00:00"))
        notes.append(
            {
                "cue": cue,
                "content": content,
                "timestamp": timestamp,
                "keywords": [],
            }
        )
    return {
        "title": str(note.get("video_title") or "视频笔记"),
        "summary": str(note.get("summary") or "").strip(),
        "notes": notes,
    }


def normalize_llm_quiz_questions(raw_questions: list[dict[str, Any]], note_id: str) -> list[dict[str, Any]]:
    today = datetime.now(TZ_UTC8).date().isoformat()
    normalized: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_questions):
        if not isinstance(raw, dict):
            continue
        options_raw = raw.get("options") or {}
        option_keys = ["A", "B", "C", "D"]
        options: list[dict[str, Any]] = []
        answer = str(raw.get("answer") or "").strip().upper()
        for key in option_keys:
            text = str(options_raw.get(key) or "").strip() if isinstance(options_raw, dict) else ""
            if not text:
                continue
            options.append({"id": key, "text": text, "is_correct": key == answer})
        if len(options) < 2 or answer not in {opt["id"] for opt in options}:
            continue
        normalized.append(
            {
                "id": f"q_{uuid.uuid4().hex}",
                "note_id": note_id,
                "type": "single_choice",
                "quiz_kind": str(raw.get("type") or "application").strip().lower(),
                "stem": str(raw.get("stem") or f"第 {idx + 1} 题").strip(),
                "options": options,
                "explanation": str(raw.get("explanation") or "").strip() or "请回看对应笔记片段再复习。",
                "timestamp": timestamp_to_mmss(str(raw.get("timestamp") or "00:00")),
                "hint": str(raw.get("hint") or "回看对应线索").strip()[:30],
                "difficulty": str(raw.get("difficulty") or "medium").strip().lower(),
                "status": "pending",
                "interval_days": 1,
                "due_at": today,
                "last_answered_at": None,
            }
        )
    return normalized


def call_llm_generate_quiz_from_note(note: dict[str, Any], model: str) -> list[dict[str, Any]]:
    base_url, _, api_key = get_chat_runtime_config()
    if not api_key:
        raise HTTPException(status_code=500, detail="缺少 MIMO_API_KEY（或 ARK/OPENAI_API_KEY），无法调用 LLM 生成 Quiz")
    client = OpenAI(api_key=api_key, base_url=base_url)
    cornell_payload = build_cornell_json_from_note(note)
    system_prompt = (
        "你是一个出题专家，负责根据康奈尔笔记生成用于间隔重复复习的单选题。\n\n"
        "输入\n"
        "你会收到一份康奈尔笔记 JSON，包含 notes 数组（每条有 cue、content、timestamp、keywords）和 summary。\n\n"
        "输出格式\n"
        "严格输出以下 JSON 结构，不要输出任何额外内容：\n\n"
        "{\n"
        "  \"questions\": [\n"
        "    {\n"
        "      \"id\": \"q1\",\n"
        "      \"type\": \"concept | application | inference\",\n"
        "      \"difficulty\": \"easy | medium | hard\",\n"
        "      \"stem\": \"题目问题\",\n"
        "      \"options\": {\n"
        "        \"A\": \"选项内容\",\n"
        "        \"B\": \"选项内容\",\n"
        "        \"C\": \"选项内容\",\n"
        "        \"D\": \"选项内容\"\n"
        "      },\n"
        "      \"answer\": \"A\",\n"
        "      \"explanation\": \"2～3 句解析，说明为什么对、为什么其他选项错\",\n"
        "      \"timestamp\": \"MM:SS\",\n"
        "      \"hint\": \"不直接给答案，给一个能帮用户想起来的提示，15 字以内\"\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "题目数量与难度分布\n\n"
        "根据笔记条数决定出题数量：\n"
        "- 4～5 条笔记：出 4 题\n"
        "- 6～8 条笔记：出 5 题\n"
        "- 9 条及以上：出 6 题\n\n"
        "每组题目的难度必须包含：\n"
        "- 1 道 concept（概念理解）：考察「是什么」「定义」\n"
        "- 2～3 道 application（知识应用）：考察「怎么用」「在什么场景下」\n"
        "- 1 道 inference（综合推断）：需要结合多个知识点才能作答\n\n"
        "出题规则\n\n"
        "关于题干\n"
        "- 每道题只考一个知识点，不出「以下哪些说法正确」这类多点判断题\n"
        "- 题干要有具体场景或条件，不要出纯定义背诵题\n"
        "- 好的例子：「小明用 LangChain 调用模型后，想直接用 response[\"key\"] 取值，结果报错。最可能的原因是？」\n"
        "- 坏的例子：「什么是 StructuredOutputParser？」\n\n"
        "关于选项\n"
        "- 4 个选项，只有 1 个正确答案\n"
        "- 干扰项必须和正确答案在同一领域，不能是明显无关的内容\n"
        "- 干扰项要有迷惑性：可以是「部分正确」「概念混淆」「因果倒置」\n"
        "- 禁止出现「以上都是」「以上都不是」「A 和 B 都对」\n"
        "- 选项长度尽量均匀，避免正确答案因为最长或最短而被猜到\n\n"
        "关于解析\n"
        "- 先说正确答案对在哪里（1 句）\n"
        "- 再点出 1～2 个干扰项的错误原因\n"
        "- 不超过 3 句，不要啰嗦\n\n"
        "关于 hint\n"
        "- 这是用户点「我想不起来了」时看到的内容\n"
        "- 给方向，不给答案\n"
        "- 好的例子：「想想模型返回的数据类型是什么」\n"
        "- 坏的例子：「答案和 StructuredOutputParser 有关」\n\n"
        "关于 timestamp\n"
        "- 取这道题考查的知识点在笔记中对应的 timestamp\n"
        "- 用户答错后可点击跳回视频原片\n\n"
        "注意事项\n"
        "- inference 题必须真的需要跨知识点推理，不能只是把两个知识点拼在一起问\n"
        "- 不要出「视频中提到了几个步骤」这类纯记忆数量的题\n"
        "- 如果笔记内容太少不足以出题，输出：{\"error\": \"笔记内容不足，无法生成有效题目\"}\n"
    )
    user_prompt = f"康奈尔笔记 JSON：\n{json.dumps(cornell_payload, ensure_ascii=False, indent=2)}\n\n请按约定 JSON 输出。"
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            stream=False,
        )
        content = (resp.choices[0].message.content or "").strip()
        parsed = extract_json_object(content)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Quiz 题目生成失败：{exc}") from exc
    if isinstance(parsed, dict) and parsed.get("error"):
        raise HTTPException(status_code=400, detail=str(parsed.get("error")))
    questions = parsed.get("questions") if isinstance(parsed, dict) else None
    if not isinstance(questions, list):
        raise HTTPException(status_code=500, detail="Quiz 题目返回格式异常")
    return normalize_llm_quiz_questions(questions, str(note.get("id") or ""))


def markdown_to_html(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", escaped)
    return escaped.replace("\n", "<br>")


def calc_pending_quiz_count(points: int) -> int:
    if points <= 5:
        return 4
    if points <= 8:
        return 5
    return 6


def rebuild_note_quiz_counters(note: dict[str, Any], quiz_store: dict[str, Any]) -> dict[str, int]:
    questions = [
        q for q in (quiz_store.get("questions") or []) if isinstance(q, dict) and q.get("note_id") == note.get("id")
    ]
    total = len(questions)
    pending = len([q for q in questions if q.get("status") in {"pending", "learning"}])
    return {"quiz_total_count": total, "pending_quiz_count": pending}


def refresh_note_quiz_fields(note: dict[str, Any], quiz_store: dict[str, Any] | None = None) -> None:
    store = quiz_store or load_quiz_store()
    counters = rebuild_note_quiz_counters(note, store)
    note["quiz_total_count"] = counters["quiz_total_count"]
    note["pending_quiz_count"] = counters["pending_quiz_count"]
    note["quiz_generation_status"] = normalize_quiz_generation_status(note)
    note["quiz_needs_regen"] = bool(note.get("quiz_generation_status") in {"failed", "stale"})
    note["status"] = derive_note_status(note)


def update_all_note_statuses(db: dict[str, Any]) -> None:
    quiz_store = load_quiz_store()
    for note in db.get("notes", []):
        refresh_note_quiz_fields(note, quiz_store)


def build_generated_note(
    url: str,
    metadata: dict[str, Any],
    llm_note: dict[str, Any],
    *,
    is_playlist: bool = False,
    playlist_meta: dict[str, Any] | None = None,
    source_parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    bvid = extract_bvid(url)
    created_at = now_iso()
    note_id = str(uuid.uuid4())
    notes = llm_note.get("notes", [])
    if len(notes) < 1:
        raise HTTPException(status_code=500, detail="LLM 生成的笔记条目不足，至少需要 1 条")
    note_cap = max(1, min(12, int(os.getenv("QUIZTUBE_NOTE_ITEMS_CAP", "8"))))
    selected_notes = notes[:note_cap]

    cues = []
    note_items = []
    for row in selected_notes:
        cue = (row.get("cue") or "").strip() or "这个知识点的核心问题是什么？"
        content = (row.get("content") or "").strip() or "内容解析缺失，请重新生成。"
        timestamp = timestamp_to_mmss((row.get("timestamp") or "00:00").split(".", maxsplit=1)[0])
        cues.append(cue)
        note_items.append({"timestamp": timestamp, "content_html": markdown_to_html(content)})

    duration_seconds = int(metadata.get("duration") or 0)
    duration_text = metadata.get("duration_string") or format_mmss(duration_seconds)
    summary = (llm_note.get("summary") or "该视频解析了关键问题并给出可执行方法。").strip()

    return {
        "id": note_id,
        "source_url": url,
        "video_id": bvid,
        "video_title": metadata.get("title") or f"视频笔记 - {bvid}",
        "author": metadata.get("uploader") or "B站UP主",
        "description": f"自动生成：{summary[:120]}",
        "cover_url": metadata.get("thumbnail") or "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=800",
        "duration_text": duration_text,
        "summary": summary,
        "cues": cues,
        "note_items": note_items,
        "knowledge_points_count": len(selected_notes),
        "pending_quiz_count": calc_pending_quiz_count(len(selected_notes)),
        "quiz_total_count": 0,
        "review_generation": 0,
        "quiz_needs_regen": False,
        "quiz_generation_status": "queued",
        "is_playlist": is_playlist,
        "playlist_meta": playlist_meta if is_playlist else None,
        "source_parts": source_parts if is_playlist else [],
        "status": "due",
        "next_review_at": (datetime.now(TZ_UTC8) + timedelta(days=1)).date().isoformat(),
        "created_at": created_at,
        "last_edited_at": created_at,
    }


def pending_badge_count(notes: list[dict[str, Any]]) -> int:
    return len([n for n in notes if n.get("status") in {"due", "overdue"}])


def build_fallback_quiz_questions_for_note(note: dict[str, Any]) -> list[dict[str, Any]]:
    cues = note.get("cues") or []
    note_items = note.get("note_items") or []
    today = datetime.now(TZ_UTC8).date().isoformat()
    generated: list[dict[str, Any]] = []
    target_count = min(4, max(1, len(cues), len(note_items)))
    for idx in range(target_count):
        cue = str(cues[idx % len(cues)] if cues else f"知识点 {idx + 1}").strip()
        item = note_items[idx % len(note_items)] if note_items else {}
        content = str(item.get("content_html") or "").strip()
        stem = cue if cue.endswith("？") else f"{cue}？"
        correct = re.sub(r"<[^>]+>", "", content).strip()[:42] or "依据该知识点，选择最贴近原文结论的说法"
        generated.append(
            {
                "id": f"q_{uuid.uuid4().hex}",
                "note_id": note.get("id"),
                "type": "single_choice",
                "quiz_kind": "application",
                "stem": stem,
                "options": [
                    {"id": "A", "text": f"{correct}（正确）", "is_correct": True},
                    {"id": "B", "text": "忽略该知识点，按经验处理", "is_correct": False},
                    {"id": "C", "text": "只看结论，不核对前提", "is_correct": False},
                    {"id": "D", "text": "依赖模糊印象快速作答", "is_correct": False},
                ],
                "explanation": re.sub(r"<[^>]+>", "", content).strip() or "请回看对应笔记片段再复习。",
                "timestamp": str(item.get("timestamp") or "00:00"),
                "hint": "回看对应线索",
                "difficulty": "application",
                "status": "pending",
                "interval_days": 1,
                "due_at": today,
                "last_answered_at": None,
            }
        )
    return generated


def build_default_quiz_questions_for_note(note: dict[str, Any], force: bool = False) -> list[dict[str, Any]]:
    quiz_store = load_quiz_store()
    note_id = note.get("id")
    existing = [q for q in (quiz_store.get("questions") or []) if q.get("note_id") == note_id]
    if existing and not force:
        return existing

    quiz_store["questions"] = [q for q in (quiz_store.get("questions") or []) if q.get("note_id") != note_id]
    generated: list[dict[str, Any]] = []
    if os.getenv("QUIZTUBE_USE_FAKE_LLM") != "1":
        try:
            generated = call_llm_generate_quiz_from_note(note, get_llm_model_id())
        except HTTPException:
            generated = []
    if not generated:
        generated = build_fallback_quiz_questions_for_note(note)

    quiz_store["questions"].extend(generated)
    save_quiz_store(quiz_store)
    return generated


def persist_generated_quizzes_for_note(note_id: str, force: bool = False) -> int:
    db = load_db()
    note = next((n for n in db.get("notes", []) if n.get("id") == note_id), None)
    if not note:
        return 0
    note["quiz_generation_status"] = "generating"
    save_db(db)
    try:
        generated = build_default_quiz_questions_for_note(note, force=force)
        db2 = load_db()
        note2 = next((n for n in db2.get("notes", []) if n.get("id") == note_id), None)
        if note2:
            note2["quiz_generation_status"] = "ready"
            refresh_note_quiz_fields(note2)
            note2["last_edited_at"] = now_iso()
            save_db(db2)
        return len(generated)
    except Exception:
        db3 = load_db()
        note3 = next((n for n in db3.get("notes", []) if n.get("id") == note_id), None)
        if note3:
            note3["quiz_generation_status"] = "failed"
            note3["quiz_needs_regen"] = True
            note3["last_edited_at"] = now_iso()
            save_db(db3)
        raise


def normalize_note_for_client(note: dict[str, Any]) -> dict[str, Any]:
    cues = [str(x) for x in (note.get("cues") or []) if str(x).strip()]
    if not cues:
        cues = [""]
    items = [x for x in (note.get("note_items") or []) if isinstance(x, dict)]
    if not items:
        items = [{"timestamp": "00:00", "content_html": ""}]

    return {
        **note,
        "cues": cues,
        "note_items": items,
        "quiz_generation_status": normalize_quiz_generation_status(note),
        "is_playlist": bool(note.get("is_playlist")),
        "playlist_meta": note.get("playlist_meta"),
        "source_parts": note.get("source_parts") or [],
    }


class GenerateRequest(BaseModel):
    url: HttpUrl


class PlaylistInspectRequest(BaseModel):
    url: HttpUrl


class PlaylistGenerateRequest(BaseModel):
    url: HttpUrl
    selected_part_ids: list[str] = Field(default_factory=list)
    strict_mode: bool = False


class SaveNoteRequest(BaseModel):
    video_title: str = Field(min_length=1, max_length=120)
    author: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=500)
    cues: list[str] = Field(min_length=1, max_length=12)
    note_items: list[dict[str, str]] = Field(min_length=1, max_length=12)


class GenerateQuizRequest(BaseModel):
    force: bool = False


class StartQuizSessionRequest(BaseModel):
    note_id: str
    limit: int = Field(default=4, ge=1, le=12)


class SubmitQuizAnswerRequest(BaseModel):
    question_id: str
    selected_option_id: str


class UpdateAiSettingsRequest(BaseModel):
    chat_base_url: Optional[str] = None
    chat_model: Optional[str] = None
    chat_api_key: Optional[str] = None
    asr_base_url: Optional[str] = None
    asr_model: Optional[str] = None
    asr_language: Optional[str] = None
    asr_api_key: Optional[str] = None


app = FastAPI(title="QuizTube MVP Backend", version="0.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def index() -> FileResponse:
    if not HTML_PATH.exists():
        raise HTTPException(status_code=404, detail="videonote-preview.html not found")
    return FileResponse(HTML_PATH)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 6:
        return "*" * len(secret)
    return f"{secret[:3]}***{secret[-3:]}"


@app.get("/api/settings/ai")
def get_ai_settings() -> dict[str, Any]:
    config = load_ai_config()
    chat_cfg = config.get("chat") or {}
    asr_cfg = config.get("asr") or {}
    return {
        "chat": {
            "base_url": str(chat_cfg.get("base_url") or ""),
            "model": str(chat_cfg.get("model") or ""),
            "has_api_key": bool(str(chat_cfg.get("api_key") or "").strip()),
            "api_key_masked": mask_secret(str(chat_cfg.get("api_key") or "").strip()),
        },
        "asr": {
            "base_url": str(asr_cfg.get("base_url") or ""),
            "model": str(asr_cfg.get("model") or ""),
            "language": str(asr_cfg.get("language") or ""),
            "has_api_key": bool(str(asr_cfg.get("api_key") or "").strip()),
            "api_key_masked": mask_secret(str(asr_cfg.get("api_key") or "").strip()),
        },
    }


@app.put("/api/settings/ai")
def update_ai_settings(payload: UpdateAiSettingsRequest) -> dict[str, Any]:
    config = load_ai_config()
    chat_cfg = config.setdefault("chat", {})
    asr_cfg = config.setdefault("asr", {})
    if payload.chat_base_url is not None:
        chat_cfg["base_url"] = payload.chat_base_url.strip()
    if payload.chat_model is not None:
        chat_cfg["model"] = payload.chat_model.strip()
    if payload.chat_api_key is not None:
        chat_cfg["api_key"] = payload.chat_api_key.strip()

    if payload.asr_base_url is not None:
        asr_cfg["base_url"] = payload.asr_base_url.strip()
    if payload.asr_model is not None:
        asr_cfg["model"] = payload.asr_model.strip()
    if payload.asr_language is not None:
        asr_cfg["language"] = payload.asr_language.strip()
    if payload.asr_api_key is not None:
        asr_cfg["api_key"] = payload.asr_api_key.strip()

    save_ai_config(config)
    return {"message": "AI 配置已保存", "settings": get_ai_settings()}


@app.get("/api/workbench/recent")
def workbench_recent(limit: int = Query(default=3, ge=1, le=10)) -> dict[str, Any]:
    db = load_db()
    update_all_note_statuses(db)
    save_db(db)
    notes = sort_notes(db["notes"])[:limit]
    return {"items": [normalize_note_for_client(n) for n in notes]}


@app.post("/api/workbench/generate")
def workbench_generate(payload: GenerateRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    request_started = time.perf_counter()
    url = str(payload.url)
    if not validate_bilibili_url(url):
        raise HTTPException(status_code=400, detail="仅支持 B 站视频链接")

    transcript_started = time.perf_counter()
    metadata, rows = load_transcript_from_bilibili(url)
    log_stage_timing("transcript_pipeline", transcript_started)
    transcript_text = transcript_rows_to_prompt_text(rows)
    model = get_llm_model_id()
    cache_key = build_summary_cache_key(extract_bvid(url), model, transcript_text)

    llm_started = time.perf_counter()
    llm_note = get_cached_llm_summary(cache_key)
    if not llm_note:
        llm_note = call_llm_generate_cornell(
            metadata.get("title") or extract_bvid(url),
            transcript_text,
            model,
            int(metadata.get("duration") or 0),
        )
        set_cached_llm_summary(cache_key, llm_note)
        log_stage_timing("llm_summary (fresh)", llm_started)
    else:
        log_stage_timing("llm_summary (cache)", llm_started)

    if isinstance(llm_note, dict) and llm_note.get("error"):
        raise HTTPException(status_code=400, detail=str(llm_note.get("error")))

    new_note = build_generated_note(url, metadata, llm_note)

    db = load_db()
    db["notes"].insert(0, new_note)
    update_all_note_statuses(db)
    db["notes"] = sort_notes(db["notes"])
    save_db(db)
    run_sync = os.getenv("QUIZTUBE_SYNC_QUIZ_GENERATION", "0") == "1"
    if run_sync:
        persist_generated_quizzes_for_note(new_note["id"], force=False)
    else:
        background_tasks.add_task(persist_generated_quizzes_for_note, new_note["id"], False)
    log_stage_timing("workbench_generate_total", request_started)
    return {"note": normalize_note_for_client(new_note), "message": "笔记生成成功"}


@app.get("/api/notes/latest")
def latest_note() -> dict[str, Any]:
    db = load_db()
    update_all_note_statuses(db)
    save_db(db)
    if not db["notes"]:
        raise HTTPException(status_code=404, detail="暂无笔记")
    note = sort_notes(db["notes"])[0]
    return {"note": normalize_note_for_client(note)}


@app.get("/api/notes")
def list_notes(
    search: str = Query(default=""),
    status: str = Query(default="all"),
) -> dict[str, Any]:
    db = load_db()
    update_all_note_statuses(db)
    save_db(db)
    notes = sort_notes(db["notes"])

    if search:
        needle = search.strip().lower()
        notes = [
            n
            for n in notes
            if needle in n.get("video_title", "").lower()
            or needle in n.get("summary", "").lower()
        ]

    if status != "all":
        notes = [n for n in notes if n.get("status") == status]

    return {
        "items": [normalize_note_for_client(n) for n in notes],
        "meta": {
            "pending_badge_count": pending_badge_count(db["notes"]),
            "streak_days": db.get("meta", {}).get("streak_days", 7),
        },
    }


@app.get("/api/notes/{note_id}")
def get_note(note_id: str) -> dict[str, Any]:
    db = load_db()
    update_all_note_statuses(db)
    save_db(db)
    note = next((n for n in db["notes"] if n["id"] == note_id), None)
    if not note:
        raise HTTPException(status_code=404, detail="笔记不存在")
    return {"note": normalize_note_for_client(note)}


@app.put("/api/notes/{note_id}")
def save_note(note_id: str, payload: SaveNoteRequest) -> dict[str, Any]:
    db = load_db()
    note = next((n for n in db["notes"] if n["id"] == note_id), None)
    if not note:
        raise HTTPException(status_code=404, detail="笔记不存在")

    note["video_title"] = payload.video_title.strip()
    note["author"] = payload.author.strip()
    note["description"] = payload.description.strip()
    note["summary"] = payload.summary.strip()
    note["cues"] = [x.strip() for x in payload.cues]
    note["note_items"] = payload.note_items
    note["last_edited_at"] = now_iso()
    refresh_note_quiz_fields(note)

    save_db(db)
    return {"note": normalize_note_for_client(note), "message": "保存成功"}


@app.post("/api/workbench/playlist/inspect")
def workbench_playlist_inspect(payload: PlaylistInspectRequest) -> dict[str, Any]:
    url = str(payload.url)
    if not validate_bilibili_playlist_url(url):
        raise HTTPException(status_code=400, detail="仅支持 B 站视频链接")
    playlist_title, parts = fetch_bilibili_playlist_parts(url)
    is_playlist = len(parts) > 1
    return {
        "url": url,
        "is_playlist": is_playlist,
        "playlist_title": playlist_title,
        "parts": parts,
    }


@app.post("/api/workbench/generate-playlist")
def workbench_generate_playlist(payload: PlaylistGenerateRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    request_started = time.perf_counter()
    url = str(payload.url)
    if not validate_bilibili_playlist_url(url):
        raise HTTPException(status_code=400, detail="仅支持 B 站视频链接")
    playlist_title, parts = fetch_bilibili_playlist_parts(url)
    selected_parts = select_playlist_parts(parts, payload.selected_part_ids)
    if not selected_parts:
        raise HTTPException(status_code=400, detail="未选择任何分P")

    merged_rows, source_parts, skipped_parts, total_duration, thumbnail = merge_playlist_transcript_rows(
        selected_parts,
        payload.strict_mode,
    )
    if not merged_rows:
        raise HTTPException(status_code=400, detail="没有可用的分P字幕/转写结果")

    transcript_text = transcript_rows_to_prompt_text(merged_rows, max_rows=420, byte_limit=7200)
    model = get_llm_model_id()
    selected_ids = [p.get("part_id", "") for p in selected_parts]
    cache_key = build_playlist_summary_cache_key(url, selected_ids, model, transcript_text)
    llm_note = get_cached_llm_summary(cache_key)
    if not llm_note:
        llm_note = call_llm_generate_cornell(playlist_title, transcript_text, model, total_duration)
        set_cached_llm_summary(cache_key, llm_note)

    if isinstance(llm_note, dict) and llm_note.get("error"):
        raise HTTPException(status_code=400, detail=str(llm_note.get("error")))

    metadata = {
        "title": f"{playlist_title}（合集）",
        "uploader": "B站UP主",
        "thumbnail": thumbnail,
        "duration": total_duration,
        "duration_string": format_mmss(total_duration),
    }
    playlist_meta = build_playlist_metadata(playlist_title, parts, selected_ids, total_duration, skipped_parts)
    new_note = build_generated_note(
        url,
        metadata,
        llm_note,
        is_playlist=True,
        playlist_meta=playlist_meta,
        source_parts=source_parts,
    )

    db = load_db()
    db["notes"].insert(0, new_note)
    update_all_note_statuses(db)
    db["notes"] = sort_notes(db["notes"])
    save_db(db)

    run_sync = os.getenv("QUIZTUBE_SYNC_QUIZ_GENERATION", "0") == "1"
    if run_sync:
        persist_generated_quizzes_for_note(new_note["id"], force=False)
    else:
        background_tasks.add_task(persist_generated_quizzes_for_note, new_note["id"], False)
    log_stage_timing("workbench_generate_playlist_total", request_started)
    return {"note": normalize_note_for_client(new_note), "message": "合集笔记生成成功", "skipped_parts": skipped_parts}


@app.post("/api/notes/{note_id}/quizzes/generate")
def generate_quiz_for_note(
    note_id: str,
    background_tasks: BackgroundTasks,
    payload: GenerateQuizRequest = Body(default=GenerateQuizRequest(force=False)),
    run_async: bool = Query(default=True),
) -> dict[str, Any]:
    db = load_db()
    note = next((n for n in db.get("notes", []) if n.get("id") == note_id), None)
    if not note:
        raise HTTPException(status_code=404, detail="笔记不存在")
    note["quiz_generation_status"] = "queued"
    note["quiz_needs_regen"] = False
    note["last_edited_at"] = now_iso()
    save_db(db)

    if run_async:
        background_tasks.add_task(persist_generated_quizzes_for_note, note_id, payload.force)
        return {"status": "queued", "message": "题目正在后台生成"}

    count = persist_generated_quizzes_for_note(note_id, payload.force)
    return {"status": "ready", "generated_count": count}


@app.get("/api/quiz/due")
def quiz_due(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    db = load_db()
    update_all_note_statuses(db)
    save_db(db)
    notes = sort_notes(db.get("notes", []))
    items = []
    for note in notes:
        items.append(
            {
                "note_id": note.get("id"),
                "video_title": note.get("video_title"),
                "status": note.get("status"),
                "next_review_at": note.get("next_review_at"),
                "pending_quiz_count": int(note.get("pending_quiz_count") or 0),
                "quiz_total_count": int(note.get("quiz_total_count") or 0),
                "quiz_generation_status": normalize_quiz_generation_status(note),
            }
        )
    due_items = [item for item in items if item.get("status") in {"due", "overdue", "upcoming"}][:limit]
    total_pending = sum(int(item.get("pending_quiz_count") or 0) for item in due_items)
    total_rounds = int(math.ceil(total_pending / 4)) if total_pending else 0
    return {"items": due_items, "meta": {"total_pending_quizzes": total_pending, "today_rounds": total_rounds}}


def get_question_public_view(question: dict[str, Any]) -> dict[str, Any]:
    options = [
        {"id": opt.get("id"), "text": opt.get("text")}
        for opt in (question.get("options") or [])
        if isinstance(opt, dict)
    ]
    return {
        "id": question.get("id"),
        "stem": question.get("stem"),
        "options": options,
        "timestamp": question.get("timestamp"),
        "hint": question.get("hint"),
    }


@app.post("/api/quiz/sessions")
def create_quiz_session(payload: StartQuizSessionRequest) -> dict[str, Any]:
    quiz_store = load_quiz_store()
    questions = [
        q
        for q in (quiz_store.get("questions") or [])
        if q.get("note_id") == payload.note_id and q.get("status") in {"pending", "learning", "done"}
    ]
    if not questions:
        db = load_db()
        note = next((n for n in db.get("notes", []) if n.get("id") == payload.note_id), None)
        if not note:
            raise HTTPException(status_code=404, detail="笔记不存在")
        questions = build_default_quiz_questions_for_note(note, force=False)
        quiz_store = load_quiz_store()

    questions = sorted(questions, key=lambda q: (str(q.get("due_at") or ""), str(q.get("last_answered_at") or "")))
    selected = questions[: payload.limit]
    if not selected:
        raise HTTPException(status_code=400, detail="暂无可复习题目")

    session = {
        "id": f"s_{uuid.uuid4().hex}",
        "note_id": payload.note_id,
        "question_ids": [q.get("id") for q in selected],
        "current_index": 0,
        "correct_count": 0,
        "started_at": now_iso(),
        "finished_at": None,
        "answers": [],
    }
    quiz_store.setdefault("sessions", []).append(session)
    save_quiz_store(quiz_store)
    first = selected[0]
    return {"session": session, "question": get_question_public_view(first), "remaining": len(selected)}


@app.post("/api/quiz/sessions/{session_id}/answer")
def submit_quiz_answer(session_id: str, payload: SubmitQuizAnswerRequest) -> dict[str, Any]:
    quiz_store = load_quiz_store()
    session = next((s for s in (quiz_store.get("sessions") or []) if s.get("id") == session_id), None)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.get("finished_at"):
        raise HTTPException(status_code=400, detail="会话已结束")

    questions = quiz_store.get("questions") or []
    qid = payload.question_id
    question = next((q for q in questions if q.get("id") == qid), None)
    if not question:
        raise HTTPException(status_code=404, detail="题目不存在")

    is_correct = any(
        opt.get("id") == payload.selected_option_id and bool(opt.get("is_correct"))
        for opt in (question.get("options") or [])
        if isinstance(opt, dict)
    )
    prev_interval = int(question.get("interval_days") or 1)
    next_interval = compute_next_interval_days(prev_interval, is_correct)
    next_due = (datetime.now(TZ_UTC8).date() + timedelta(days=next_interval)).isoformat()

    question["interval_days"] = next_interval
    question["due_at"] = next_due
    question["status"] = "done" if is_correct else "learning"
    question["last_answered_at"] = now_iso()
    session.setdefault("answers", []).append(
        {
            "question_id": qid,
            "selected_option_id": payload.selected_option_id,
            "is_correct": is_correct,
            "answered_at": now_iso(),
        }
    )
    if is_correct:
        session["correct_count"] = int(session.get("correct_count") or 0) + 1
    session["current_index"] = int(session.get("current_index") or 0) + 1
    finished = session["current_index"] >= len(session.get("question_ids") or [])
    next_question = None
    if finished:
        session["finished_at"] = now_iso()
        db = load_db()
        note = next((n for n in db.get("notes", []) if n.get("id") == session.get("note_id")), None)
        if note:
            note["next_review_at"] = (
                datetime.now(TZ_UTC8).date()
                + timedelta(days=compute_next_interval_days(prev_interval_days=1, was_correct=is_correct))
            ).isoformat()
            note["review_generation"] = int(note.get("review_generation") or 0) + 1
            note["last_edited_at"] = now_iso()
            refresh_note_quiz_fields(note, quiz_store)
            save_db(db)
    else:
        next_qid = session.get("question_ids")[session["current_index"]]
        q = next((item for item in questions if item.get("id") == next_qid), None)
        if q:
            next_question = get_question_public_view(q)

    save_quiz_store(quiz_store)
    return {
        "is_correct": is_correct,
        "explanation": question.get("explanation") or "",
        "correct_option_id": next((opt.get("id") for opt in (question.get("options") or []) if opt.get("is_correct")), None),
        "is_session_finished": finished,
        "next_question": next_question,
    }


@app.post("/api/quiz/sessions/{session_id}/complete")
def complete_quiz_session(session_id: str) -> dict[str, Any]:
    quiz_store = load_quiz_store()
    session = next((s for s in (quiz_store.get("sessions") or []) if s.get("id") == session_id), None)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if not session.get("finished_at"):
        session["finished_at"] = now_iso()
        save_quiz_store(quiz_store)

    total = len(session.get("question_ids") or [])
    correct = int(session.get("correct_count") or 0)
    accuracy = round((correct / total) * 100, 2) if total else 0.0
    return {"session_id": session_id, "total": total, "correct": correct, "accuracy": accuracy}
