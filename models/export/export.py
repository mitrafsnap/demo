"""Export one torchvision model to /out/<name>.onnx.

    maskrcnn_v2   Mask R-CNN ResNet-50 FPN v2, COCO instance segmentation,
                  fixed 960x540 input (the demo's frame size)
"""

import os
import sys

import torch
import torchvision

name, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)

if name == "maskrcnn_v2":
    weights = torchvision.models.detection.MaskRCNN_ResNet50_FPN_V2_Weights.COCO_V1
    model = torchvision.models.detection.maskrcnn_resnet50_fpn_v2(weights=weights,
                                                                   box_score_thresh=0.5)
    # Keep the frame's own resolution instead of upscaling to 800 px.
    model.transform.min_size = (540,)
    model.transform.max_size = 960
    model.eval()
    image = torch.rand(3, 540, 960)
    torch.onnx.export(model, ([image],), f"{out}/{name}.onnx", opset_version=17,
                      input_names=["image"], output_names=["boxes", "labels", "scores", "masks"],
                      dynamo=False)
else:
    sys.exit(f"unknown model {name}")
print("exported", name, os.path.getsize(f"{out}/{name}.onnx"))
