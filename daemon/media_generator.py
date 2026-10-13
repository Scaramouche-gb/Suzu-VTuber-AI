"""Media processing pipelines for Telegram Voice Notes (.ogg) and Video Notes / Round Circles (.mp4)."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".cache" / "vtuber" / "telegram"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class MediaGenerator:
    """Encodes voice notes (OGG Opus) and circular video notes (H.264 MP4 1:1) for Telegram."""

    @staticmethod
    def encode_voice_note(audio_bytes: bytes, output_path: Path | None = None) -> Path:
        """Encode audio bytes into Telegram-compliant Voice Note (OGG Opus 48kHz mono)."""
        out = output_path or (CACHE_DIR / f"voice_{tempfile.mktemp(dir='')}.ogg")
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            "pipe:0",
            "-c:a",
            "libopus",
            "-b:a",
            "48k",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-application",
            "voip",
            str(out),
        ]
        try:
            subprocess.run(cmd, input=audio_bytes, capture_output=True, check=True)
            logger.info("Encoded Telegram voice note: %s (%d bytes)", out, out.stat().st_size)
            return out
        except subprocess.CalledProcessError as exc:
            logger.error("FFmpeg voice note encoding failed: %s", exc.stderr.decode("utf-8", "ignore"))
            raise RuntimeError(f"Voice note encoding failed: {exc}") from exc

    @staticmethod
    def encode_round_video(
        video_bytes: bytes,
        audio_bytes: bytes | None = None,
        output_path: Path | None = None,
        dimension: int = 384,
    ) -> Path:
        """Encode avatar canvas video stream and voice into a Telegram Round Video Note (MP4 1:1 H.264+AAC)."""
        out = output_path or (CACHE_DIR / f"round_{tempfile.mktemp(dir='')}.mp4")

        # Save temporary video input
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp_vid:
            tmp_vid.write(video_bytes)
            tmp_vid_path = tmp_vid.name

        tmp_aud_path: str | None = None
        if audio_bytes:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_aud:
                tmp_aud.write(audio_bytes)
                tmp_aud_path = tmp_aud.name

        try:
            cmd = ["ffmpeg", "-y", "-i", tmp_vid_path]
            if tmp_aud_path:
                cmd.extend(["-i", tmp_aud_path])

            filter_complex = (
                f"scale={dimension}:{dimension}:force_original_aspect_ratio=increase,"
                f"crop={dimension}:{dimension}"
            )
            cmd.extend([
                "-vf",
                filter_complex,
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-profile:v",
                "baseline",
                "-level",
                "3.0",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "30",
            ])

            if tmp_aud_path:
                cmd.extend([
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "64k",
                    "-ar",
                    "44100",
                    "-shortest",
                ])
            else:
                cmd.extend(["-c:a", "aac", "-b:a", "64k"])

            cmd.extend(["-movflags", "+faststart", str(out)])

            subprocess.run(cmd, capture_output=True, check=True)
            logger.info("Encoded Telegram round video note: %s (%d bytes)", out, out.stat().st_size)
            return out
        except subprocess.CalledProcessError as exc:
            logger.error("FFmpeg round video encoding failed: %s", exc.stderr.decode("utf-8", "ignore"))
            raise RuntimeError(f"Round video encoding failed: {exc}") from exc
        finally:
            try:
                Path(tmp_vid_path).unlink(missing_ok=True)
                if tmp_aud_path:
                    Path(tmp_aud_path).unlink(missing_ok=True)
            except OSError:
                pass
