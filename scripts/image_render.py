"""One bounded image, offline; container exits to release all model memory."""

import hashlib
import json
import time
from pathlib import Path

import torch
from diffusers import ZImagePipeline

started = time.monotonic()
request = json.loads(Path("/job/image-request.json").read_text())
if set(request) != {"prompt", "seed"} or not 100 <= len(request["prompt"]) <= 2200:
    raise ValueError("Invalid image request")
torch.set_num_threads(6)
pipe = ZImagePipeline.from_pretrained(
    "/models/z-image-turbo", torch_dtype=torch.bfloat16, local_files_only=True, low_cpu_mem_usage=True
)
pipe.to("cuda")
pipe.vae.enable_tiling()
loaded = time.monotonic()
Path("/job/image-stage.json").write_text(json.dumps({"stage": "generating"}))
image = pipe(
    prompt=request["prompt"],
    height=1024,
    width=1024,
    num_inference_steps=9,
    guidance_scale=0.0,
    generator=torch.Generator("cuda").manual_seed(request["seed"]),
).images[0]
image.save("/job/visual.tmp.png")
Path("/job/visual.tmp.png").replace("/job/visual.png")
manifest = json.loads(Path("/models/manifest.json").read_text())
Path("/job/image-metrics.tmp").write_text(
    json.dumps(
        {
            **manifest,
            "prompt_sha256": hashlib.sha256(request["prompt"].encode()).hexdigest(),
            "sha256": hashlib.sha256(Path("/job/visual.png").read_bytes()).hexdigest(),
            "load_seconds": round(loaded - started, 2),
            "generation_seconds": round(time.monotonic() - loaded, 2),
            "peak_cuda_bytes": torch.cuda.max_memory_allocated(),
            "width": 1024,
            "height": 1024,
        }
    )
)
Path("/job/image-metrics.tmp").replace("/job/image-metrics.json")
