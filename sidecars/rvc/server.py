"""RVC v2 HTTP sidecar inference server.

Listens on 127.0.0.1:5050 and transforms TTS audio into target character voice.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import anyio
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

# Fix PyTorch 2.6+ weights_only issue with legacy fairseq checkpoints
_orig_torch_load = torch.load


def _patched_load(*args: object, **kwargs: object) -> object:
    kw = dict(kwargs)
    kw["weights_only"] = False
    return _orig_torch_load(*args, **kw)


torch.load = _patched_load

from rvc_python.infer import RVCInference

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rvc_sidecar")

DEFAULT_MODEL = "models/rvc/hutao/Hutao_v2.pth"
DEFAULT_INDEX = "models/rvc/hutao/added_IVF459_Flat_nprobe_1_Hutao_v2_v2.index"

# Global inference engine
engine: RVCInference | None = None
current_loaded_model: str = ""
current_loaded_index: str = ""


def init_engine(
    model_path: str = DEFAULT_MODEL,
    index_path: str = DEFAULT_INDEX,
    device: str = "cuda:0",
) -> None:
    """Initialize RVC engine and preload model weights into VRAM."""
    global engine, current_loaded_model, current_loaded_index
    if not torch.cuda.is_available():
        device = "cpu:0"
    logger.info("Initializing RVCInference on %s...", device)
    engine = RVCInference(device=device)

    if os.path.isfile(model_path):
        idx = index_path if os.path.isfile(index_path) else ""
        logger.info("Preloading model: %s (index: %s)", model_path, idx)
        engine.load_model(model_path, version="v2", index_path=idx)
        current_loaded_model = model_path
        current_loaded_index = idx
    else:
        logger.warning("Default model %s not found on disk, engine starts unloaded", model_path)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_engine()
    yield


app = FastAPI(title="RVC v2 Sidecar Service", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "engine_ready": engine is not None,
        "model": current_loaded_model,
        "index": current_loaded_index,
    }


def _run_inference(in_path: str, out_path: str) -> None:
    if engine is not None:
        engine.infer_file(in_path, out_path)


@app.post("/infer")
async def infer(
    audio: Annotated[UploadFile, File(...)],
    f0_up_key: Annotated[int, Form()] = 0,
    model: Annotated[str, Form()] = "",
    index: Annotated[str, Form()] = "",
    f0_method: Annotated[str, Form()] = "rmvpe",
    index_rate: Annotated[float, Form()] = 0.7,
) -> Response:
    """Convert input audio into target character voice timbre."""
    if engine is None:
        raise HTTPException(status_code=503, detail="RVC engine is not initialized")

    global current_loaded_model, current_loaded_index

    # Check if a different model was requested
    target_model = model or DEFAULT_MODEL
    target_index = index or DEFAULT_INDEX
    if target_model and target_model != current_loaded_model and os.path.isfile(target_model):
        logger.info("Switching model to: %s", target_model)
        await asyncio.to_thread(
            engine.load_model, target_model, version="v2", index_path=target_index
        )
        current_loaded_model = target_model
        current_loaded_index = target_index

    engine.set_params(
        f0method=f0_method,
        f0up_key=f0_up_key,
        index_rate=index_rate,
    )

    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio payload")

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as in_f:
        in_path = in_f.name
        in_f.write(audio_bytes)

    out_path = in_path + "_converted.wav"
    mp3_path = in_path + "_converted.mp3"
    try:
        await asyncio.to_thread(_run_inference, in_path, out_path)
        if not await anyio.Path(out_path).is_file():
            raise HTTPException(status_code=500, detail="Inference output not created")

        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-i",
            out_path,
            "-codec:a",
            "libmp3lame",
            "-qscale:a",
            "2",
            mp3_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        if await anyio.Path(mp3_path).is_file():
            converted_bytes = await anyio.Path(mp3_path).read_bytes()
            media_type = "audio/mpeg"
        else:
            converted_bytes = await anyio.Path(out_path).read_bytes()
            media_type = "audio/wav"

        return Response(content=converted_bytes, media_type=media_type)
    except Exception as exc:
        logger.exception("Inference failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        for p in (in_path, out_path, mp3_path):
            pth = Path(p)
            if pth.is_file():
                try:
                    pth.unlink()
                except OSError:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Run RVC sidecar server")
    parser.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5050, help="Port (default: 5050)")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
