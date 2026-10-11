from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "image_flux2_klein_image_edit_4b_distilled (1).json"
UI_TARGET = ROOT / "image_flux2_klein_multireference_4b_9x16.json"
API_TARGET = ROOT / "workflows" / "flux_klein_multireference_4b_api.json"


def api_node(class_type: str, inputs: dict[str, Any], title: str) -> dict[str, Any]:
    return {"inputs": inputs, "class_type": class_type, "_meta": {"title": title}}


def build_api(reference_count: int = 4) -> dict[str, Any]:
    graph: dict[str, Any] = {
        "1": api_node("UNETLoader", {
            "unet_name": "flux-2-klein-4b-fp8.safetensors", "weight_dtype": "default"
        }, "FLUX.2 Klein 4B FP8"),
        "2": api_node("CLIPLoader", {
            "clip_name": "qwen_3_4b.safetensors", "type": "flux2", "device": "default"
        }, "Qwen text encoder"),
        "3": api_node("VAELoader", {"vae_name": "flux2-vae.safetensors"}, "FLUX.2 VAE"),
        "4": api_node("CLIPTextEncode", {
            "text": (
                "Create a new vertical cinematic shot. Use image 1 for the primary character, "
                "image 2 for the second character, image 3 for the environment, and image 4 for "
                "the important prop or style. Preserve the referenced identities and environment, "
                "but compose one coherent scene rather than a reference sheet or collage."
            ),
            "clip": ["2", 0],
        }, "SCENE PROMPT — refer to images 1–4 explicitly"),
        "5": api_node("ConditioningZeroOut", {"conditioning": ["4", 0]}, "Zero negative conditioning"),
    }

    if not 1 <= reference_count <= 4:
        raise ValueError("reference_count must be between 1 and 4")
    labels = ["PRIMARY CHARACTER", "SECOND CHARACTER", "ENVIRONMENT", "PROP OR STYLE"]
    for index, label in enumerate(labels[:reference_count], start=1):
        load_id = str(9 + index)
        scale_id = str(19 + index)
        encode_id = str(29 + index)
        graph[load_id] = api_node("LoadImage", {"image": "comfy_logo_blue.png"}, f"IMAGE {index} — {label}")
        graph[scale_id] = api_node("ImageScaleToTotalPixels", {
            "upscale_method": "nearest-exact", "megapixels": 0.35,
            "resolution_steps": 1, "image": [load_id, 0],
        }, f"Scale reference {index}")
        graph[encode_id] = api_node("VAEEncode", {
            "pixels": [scale_id, 0], "vae": ["3", 0]
        }, f"Encode reference {index}")

    positive = ["4", 0]
    negative = ["5", 0]
    for index in range(1, reference_count + 1):
        positive_id = str(39 + index)
        negative_id = str(49 + index)
        latent = [str(29 + index), 0]
        graph[positive_id] = api_node("ReferenceLatent", {
            "conditioning": positive, "latent": latent
        }, f"Positive reference {index}")
        graph[negative_id] = api_node("ReferenceLatent", {
            "conditioning": negative, "latent": latent
        }, f"Negative reference {index}")
        positive = [positive_id, 0]
        negative = [negative_id, 0]

    graph.update({
        "60": api_node("EmptyFlux2LatentImage", {
            "width": 720, "height": 1280, "batch_size": 1
        }, "Fixed 9:16 output latent"),
        "61": api_node("RandomNoise", {"noise_seed": 1}, "Candidate seed"),
        "62": api_node("KSamplerSelect", {"sampler_name": "euler"}, "Euler sampler"),
        "63": api_node("Flux2Scheduler", {"steps": 4, "width": 720, "height": 1280}, "FLUX.2 4-step schedule"),
        "64": api_node("CFGGuider", {
            "cfg": 1, "model": ["1", 0], "positive": positive, "negative": negative
        }, "FLUX.2 guider"),
        "65": api_node("SamplerCustomAdvanced", {
            "noise": ["61", 0], "guider": ["64", 0], "sampler": ["62", 0],
            "sigmas": ["63", 0], "latent_image": ["60", 0],
        }, "Generate independent scene anchor"),
        "66": api_node("VAEDecode", {"samples": ["65", 0], "vae": ["3", 0]}, "Decode"),
        "67": api_node("SaveImage", {
            "filename_prefix": "Flux2_Klein_MultiRef", "images": ["66", 0]
        }, "Save scene anchor"),
    })
    return graph


def collect_templates(source: dict[str, Any]) -> dict[str, dict[str, Any]]:
    templates: dict[str, dict[str, Any]] = {}
    for subgraph in source.get("definitions", {}).get("subgraphs", []):
        for node in subgraph.get("nodes", []):
            templates.setdefault(node["type"], node)
    for node in source.get("nodes", []):
        templates.setdefault(node["type"], node)
    return templates


def build_ui(api: dict[str, Any]) -> dict[str, Any]:
    source = json.loads(SOURCE.read_text())
    templates = collect_templates(source)
    positions = {
        "1": (-900, -500), "2": (-900, -350), "3": (-900, -200), "4": (-520, -500),
        "5": (-180, -340), "10": (-900, 40), "11": (-900, 520), "12": (-900, 1000), "13": (-900, 1480),
        "20": (-540, 40), "21": (-540, 520), "22": (-540, 1000), "23": (-540, 1480),
        "30": (-230, 40), "31": (-230, 520), "32": (-230, 1000), "33": (-230, 1480),
        "40": (80, -120), "41": (350, -120), "42": (620, -120), "43": (890, -120),
        "50": (80, 260), "51": (350, 260), "52": (620, 260), "53": (890, 260),
        "60": (900, 720), "61": (900, 900), "62": (900, 1040), "63": (900, 1160),
        "64": (1220, 80), "65": (1500, 300), "66": (1780, 300), "67": (2040, 300),
    }
    widget_order = {
        "UNETLoader": ["unet_name", "weight_dtype"],
        "CLIPLoader": ["clip_name", "type", "device"],
        "VAELoader": ["vae_name"], "CLIPTextEncode": ["text"],
        "LoadImage": ["image", "upload"],
        "ImageScaleToTotalPixels": ["upscale_method", "megapixels", "resolution_steps"],
        "EmptyFlux2LatentImage": ["width", "height", "batch_size"],
        "RandomNoise": ["noise_seed", "control_after_generate"],
        "KSamplerSelect": ["sampler_name"], "Flux2Scheduler": ["steps", "width", "height"],
        "CFGGuider": ["cfg"], "SaveImage": ["filename_prefix"],
    }
    input_types = {
        "clip": "CLIP", "conditioning": "CONDITIONING", "latent": "LATENT", "image": "IMAGE",
        "pixels": "IMAGE", "vae": "VAE", "model": "MODEL", "positive": "CONDITIONING",
        "negative": "CONDITIONING", "noise": "NOISE", "guider": "GUIDER", "sampler": "SAMPLER",
        "sigmas": "SIGMAS", "latent_image": "LATENT", "samples": "LATENT", "images": "IMAGE",
    }
    outputs_by_type = {
        "UNETLoader": [("MODEL", "MODEL")], "CLIPLoader": [("CLIP", "CLIP")],
        "VAELoader": [("VAE", "VAE")], "CLIPTextEncode": [("CONDITIONING", "CONDITIONING")],
        "ConditioningZeroOut": [("CONDITIONING", "CONDITIONING")],
        "LoadImage": [("IMAGE", "IMAGE"), ("MASK", "MASK")],
        "ImageScaleToTotalPixels": [("IMAGE", "IMAGE")], "VAEEncode": [("LATENT", "LATENT")],
        "ReferenceLatent": [("CONDITIONING", "CONDITIONING")],
        "EmptyFlux2LatentImage": [("LATENT", "LATENT")], "RandomNoise": [("NOISE", "NOISE")],
        "KSamplerSelect": [("SAMPLER", "SAMPLER")], "Flux2Scheduler": [("SIGMAS", "SIGMAS")],
        "CFGGuider": [("GUIDER", "GUIDER")],
        "SamplerCustomAdvanced": [("output", "LATENT"), ("denoised_output", "LATENT")],
        "VAEDecode": [("IMAGE", "IMAGE")], "SaveImage": [],
    }
    nodes: list[dict[str, Any]] = []
    links: list[list[Any]] = []
    output_links: dict[tuple[str, int], list[int]] = {}
    link_id = 0

    for node_id, api_value in api.items():
        class_type = api_value["class_type"]
        template = templates.get(class_type, {})
        node = {
            "id": int(node_id), "type": class_type, "pos": list(positions[node_id]),
            "size": copy.deepcopy(template.get("size", [280, 100])), "flags": {},
            "order": len(nodes), "mode": 0, "inputs": [], "outputs": [],
            "properties": copy.deepcopy(template.get("properties", {"Node name for S&R": class_type})),
            "widgets_values": [], "title": api_value.get("_meta", {}).get("title", class_type),
        }
        for output_index, (name, value_type) in enumerate(outputs_by_type[class_type]):
            node["outputs"].append({"name": name, "type": value_type, "links": []})
            output_links[(node_id, output_index)] = node["outputs"][-1]["links"]
        widget_names = widget_order.get(class_type, [])
        for name in widget_names:
            value = api_value["inputs"].get(name)
            if name == "control_after_generate":
                value = "randomize"
            if name == "upload":
                value = "image"
            node["widgets_values"].append(value)
        for name, value in api_value["inputs"].items():
            if not (isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)):
                continue
            link_id += 1
            origin_id, origin_slot = value
            slot = len(node["inputs"])
            value_type = input_types[name]
            node["inputs"].append({"name": name, "type": value_type, "link": link_id})
            links.append([link_id, int(origin_id), origin_slot, int(node_id), slot, value_type])
            output_links[(origin_id, origin_slot)].append(link_id)
        nodes.append(node)

    return {
        "id": "flux2-klein-multireference-4b-9x16", "revision": 0,
        "last_node_id": max(int(node_id) for node_id in api), "last_link_id": link_id,
        "nodes": nodes, "links": links,
        "groups": [
            {"id": 1, "title": "MODEL AND PROMPT", "bounding": [-940, -560, 1060, 500], "color": "#3f789e", "font_size": 24, "flags": {}},
            {"id": 2, "title": "REFERENCE IMAGES — replace every placeholder", "bounding": [-940, -20, 1250, 1960], "color": "#8e6b3f", "font_size": 24, "flags": {}},
            {"id": 3, "title": "NATIVE MULTI-REFERENCE CONDITIONING", "bounding": [40, -180, 1150, 620], "color": "#3f789e", "font_size": 24, "flags": {}},
            {"id": 4, "title": "FIXED 9:16 GENERATION", "bounding": [860, 680, 1510, 600], "color": "#3f789e", "font_size": 24, "flags": {}},
        ],
        "config": {}, "extra": {
            "ds": {"scale": 0.6, "offset": [1050, 480]},
            "frontendVersion": source.get("extra", {}).get("frontendVersion", "1.38.6"),
            "workflowRendererVersion": "LG",
        },
        "version": 0.4,
    }


def main() -> None:
    api = build_api(4)
    API_TARGET.write_text(json.dumps(api, indent=2) + "\n")
    UI_TARGET.write_text(json.dumps(build_ui(api), indent=2) + "\n")
    for count in range(1, 5):
        target = ROOT / "workflows" / f"flux_klein_multiref_{count}_api.json"
        target.write_text(json.dumps(build_api(count), indent=2) + "\n")
        print(target)
    print(UI_TARGET)
    print(API_TARGET)


if __name__ == "__main__":
    main()
