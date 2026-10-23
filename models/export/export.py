"""Write one prepared model to /out/<name>.onnx.

    mobilesam_encoder   MobileSAM image encoder, pinned to a 1024x576 input
    mobilesam_decoder   MobileSAM mask decoder, pinned to one box prompt
"""

import os
import sys
import urllib.request

import onnx
from onnxruntime.tools.onnx_model_utils import make_dim_param_fixed

name, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)

# MIT-licensed ONNX export of MobileSAM (Apache-2.0)
MOBILESAM = "https://huggingface.co/Acly/MobileSAM/resolve/main/"
PINNED = {
    "mobilesam_encoder": ("mobile_sam_image_encoder.onnx",
                          {"image_height": 576, "image_width": 1024}),
    "mobilesam_decoder": ("sam_mask_decoder_single.onnx", {"num_points": 2}),
}

if name in PINNED:
    source, dims = PINNED[name]
    raw = f"/tmp/{source}"
    urllib.request.urlretrieve(MOBILESAM + source, raw)
    model = onnx.load(raw)
    for param, value in dims.items():
        make_dim_param_fixed(model.graph, param, value)
    onnx.save(model, f"{out}/{name}.onnx")
else:
    sys.exit(f"unknown model {name}")
print("exported", name, os.path.getsize(f"{out}/{name}.onnx"))
