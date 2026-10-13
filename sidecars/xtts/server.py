"""XTTS-v2 HTTP sidecar inference server.

Listens on 127.0.0.1:5055 and provides voice generation / cloning using Coqui XTTS-v2.
"""

from __future__ import annotations

import argparse
import gc
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import soundfile as sf
import torch
import torchaudio
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel


# Patch torchaudio 2.11+ to use soundfile instead of requiring missing TorchCodec
def _sf_load(uri, **kwargs):
    data, sr = sf.read(uri, dtype="float32")
    if data.ndim == 1:
        tensor = torch.from_numpy(data).unsqueeze(0)
    else:
        tensor = torch.from_numpy(data.T)
    return tensor, sr

def _sf_save(uri, src, sample_rate, **kwargs):
    if isinstance(src, torch.Tensor):
        src = src.detach().cpu().numpy()
    if src.ndim == 2:
        src = src.T
    sf.write(uri, src, sample_rate)

torchaudio.load = _sf_load
torchaudio.save = _sf_save

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("xtts_sidecar")

# Global TTS model instance
tts_model: Any = None
is_model_ready: bool = False
DEFAULT_SPEAKER_WAV: str = "models/xtts/speaker.wav"


class SynthesizeRequest(BaseModel):
    text: str
    speaker_wav: str = ""
    language: str = "ru"
    speed: float = 1.0


def init_xtts(model_name: str = "tts_models/multilingual/multi-dataset/xtts_v2") -> None:
    """Preload XTTS-v2 model onto GPU or CPU."""
    global tts_model, is_model_ready
    os.environ["COQUI_TOS_AGREED"] = "1"
    use_gpu = torch.cuda.is_available()
    device_str = "cuda" if use_gpu else "cpu"
    logger.info("Initializing XTTS-v2 model '%s' on %s...", model_name, device_str)

    # PyTorch 2.6+ changed default weights_only to True, which breaks Coqui model unpickling
    _orig_load = torch.load
    torch.load = lambda *a, **kw: _orig_load(*a, **dict(kw, weights_only=False))

    try:
        from TTS.api import TTS

        tts_model = TTS(model_name, gpu=use_gpu)
        is_model_ready = True
        logger.info("XTTS-v2 model preloaded successfully and ready for inference")
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to load XTTS-v2 model: %s", exc)
        is_model_ready = False
    finally:
        torch.load = _orig_load


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: preload model
    init_xtts()
    yield
    # Shutdown
    logger.info("Shutting down XTTS sidecar...")


app = FastAPI(title="XTTS Sidecar Server", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health check endpoint."""
    return {
        "status": "ready" if is_model_ready else "loading",
        "cuda_available": torch.cuda.is_available(),
        "model": "xtts_v2",
    }


@app.post("/synthesize")
async def synthesize(req: SynthesizeRequest) -> Response:
    """Generate cloned speech from text and reference speaker WAV."""
    if not is_model_ready or tts_model is None:
        raise HTTPException(status_code=503, detail="XTTS model is not ready or failed to load")

    clean_text = req.text.strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="Text cannot be empty")

    speaker_path = req.speaker_wav or DEFAULT_SPEAKER_WAV
    # Fallback to local default if specified speaker_wav does not exist
    if not os.path.exists(speaker_path):
        candidates = [
            "models/xtts/speaker.wav",
            "test_live.mp3",
            "test_silero.mp3",
        ]
        for c in candidates:
            if os.path.exists(c):
                speaker_path = c
                break

    if not os.path.exists(speaker_path):
        raise HTTPException(
            status_code=400,
            detail=f"Speaker reference audio '{speaker_path}' not found. Please provide a 6-second WAV clip.",
        )

    logger.info(
        "Synthesizing '%s' (lang=%s, speed=%.2f, speaker=%s)",
        clean_text[:40],
        req.language,
        req.speed,
        speaker_path,
    )

    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            tmp_path = tmp_wav.name

        tts_model.tts_to_file(
            text=clean_text,
            speaker_wav=speaker_path,
            language=req.language,
            speed=req.speed,
            file_path=tmp_path,
        )

        wav_bytes = Path(tmp_path).read_bytes()
        try:
            os.remove(tmp_path)
        except OSError:
            pass

        return Response(content=wav_bytes, media_type="audio/wav")

    except Exception as exc:  # noqa: BLE001
        logger.error("Synthesis error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()


def main() -> None:
    parser = argparse.ArgumentParser(description="XTTS Sidecar Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface (127.0.0.1 only)")
    parser.add_argument("--port", type=int, default=5055, help="Port to listen on (default 5055)")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
