"""Recover prompts from the metadata embedded in generated images.

PromptManager has no hook into the program that generates images, so the only
reliable source for "which prompt made this image" is what the generator wrote
into the file itself: ComfyUI's ``prompt``/``workflow`` PNG text chunks, or an
A1111-style ``parameters`` block.

This module is the single implementation of that lookup. Both the filesystem
monitor (new images as they appear) and the bulk scan endpoint (existing
libraries) go through :func:`ingest_image`, so an image is linked to the same
prompt regardless of how it was discovered.

Typical usage:
    from promptmanager.ingest.prompt_extraction import ingest_image

    result = ingest_image(db, "/path/to/image.png")
    if result.prompt_id:
        print(f"linked to prompt {result.prompt_id} (new={result.created})")
"""

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from PIL import Image

from ..utils.hashing import generate_prompt_hash
from ..utils.logging_config import get_logger

logger = get_logger("prompt_manager.ingest")

# Node types whose first widget value / ``text`` input holds prompt text.
# "PromptManager" and "PromptManagerText" are kept so that images generated
# with the original ComfyUI custom node are still recognized.
TEXT_ENCODER_TYPES = (
    "CLIPTextEncode",
    "CLIPTextEncodeSDXL",
    "CLIPTextEncodeSDXLRefiner",
    "CLIPTextEncodeFlux",
    "PromptManager",
    "PromptManagerText",
    "BNK_CLIPTextEncoder",
    "Text Encoder",
    "CLIP Text Encode",
)

# Media the gallery serves but whose metadata cannot be read with Pillow.
# They are skipped quietly rather than logged as unreadable images.
VIDEO_EXTENSIONS = (".mp4", ".webm", ".avi", ".mov", ".mkv", ".m4v", ".wmv")

# Category and tag applied to prompts created from image metadata.
SCANNED_CATEGORY = "scanned"
SCANNED_TAGS = ("auto-scanned",)


@dataclass
class IngestResult:
    """Outcome of ingesting one image.

    Attributes:
        has_metadata: The file carried recognizable generation metadata.
        prompt_id: ID of the prompt the image was linked to, or None if no
            prompt text could be recovered.
        created: True if a new prompt row was created for this image.
        image_id: ID of the generated_images row, or None if linking failed.
    """

    has_metadata: bool = False
    prompt_id: Optional[int] = None
    created: bool = False
    image_id: Optional[int] = None


def read_embedded_metadata(image_path: str) -> Dict[str, str]:
    """Read the raw text chunks embedded in an image.

    Args:
        image_path: Path to the image file.

    Returns:
        Mapping of chunk name to raw string value; empty if the file has no
        text chunks, is a video, or cannot be read.
    """
    if str(image_path).lower().endswith(VIDEO_EXTENSIONS):
        return {}

    try:
        with Image.open(image_path) as img:
            if hasattr(img, "text"):
                return dict(img.text.items())
            return {}
    except Exception as e:
        logger.error(f"Error reading {image_path}: {e}")
        return {}


def parse_embedded_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Parse ComfyUI and A1111 prompt data out of raw embedded metadata.

    Args:
        metadata: Raw text chunks as returned by :func:`read_embedded_metadata`.

    Returns:
        Dict with ``prompt``, ``workflow``, ``parameters``, ``positive_prompt``
        and ``negative_prompt`` keys. Unavailable entries are None / empty.
    """
    result = {
        "prompt": None,
        "workflow": None,
        "parameters": {},
        "positive_prompt": None,
        "negative_prompt": None,
    }

    # A1111-style parameters block: first line is the positive prompt.
    if "parameters" in metadata:
        params = metadata["parameters"]
        lines = params.splitlines()
        if lines:
            result["positive_prompt"] = lines[0].strip()
        for line in lines:
            if line.lower().startswith("negative prompt:"):
                result["negative_prompt"] = line.split(":", 1)[1].strip()
                break
        result["parameters"]["parameters"] = params

    # Otherwise fall through to ComfyUI's JSON chunks.
    if result["positive_prompt"] is None:
        if "prompt" in metadata:
            try:
                result["prompt"] = json.loads(metadata["prompt"])
            except json.JSONDecodeError:
                result["prompt"] = metadata["prompt"]

        if "workflow" in metadata:
            try:
                result["workflow"] = json.loads(metadata["workflow"])
            except json.JSONDecodeError:
                result["workflow"] = metadata["workflow"]

        common_fields = [
            "positive",
            "negative",
            "steps",
            "cfg",
            "sampler",
            "scheduler",
            "seed",
        ]
        for field in common_fields:
            if field in metadata:
                try:
                    result["parameters"][field] = json.loads(metadata[field])
                except json.JSONDecodeError:
                    result["parameters"][field] = metadata[field]

    return result


def get_node_inputs(node: Any) -> Dict[str, Any]:
    """Get a node's inputs as a dict, handling both serialization formats.

    API format stores inputs as a dict of values::

        inputs = {"text": "my prompt", "seed": 123}

    Workflow (UI) format stores a list of connection objects::

        inputs = [{"name": "text", "type": "STRING", "link": None}, ...]

    Args:
        node: Node dict from either format.

    Returns:
        Inputs keyed by name; empty if the node has none.
    """
    if not isinstance(node, dict):
        return {}

    inputs = node.get("inputs", {})

    if isinstance(inputs, dict):
        return inputs

    if isinstance(inputs, list):
        return {
            item["name"]: item
            for item in inputs
            if isinstance(item, dict) and "name" in item
        }

    return {}


def find_text_in_node(node: Any) -> Optional[str]:
    """Find prompt text held by a node, if it is a text encoder.

    Args:
        node: Node dict from either the API or workflow format.

    Returns:
        The prompt text, or None if the node holds none.
    """
    if not isinstance(node, dict):
        return None

    inputs = get_node_inputs(node)
    if "text" in inputs and isinstance(inputs["text"], str):
        return inputs["text"]

    class_type = node.get("class_type", node.get("type", ""))
    if any(t.lower() in class_type.lower() for t in TEXT_ENCODER_TYPES):
        widgets_values = node.get("widgets_values", [])
        if widgets_values and len(widgets_values) > 0:
            if isinstance(widgets_values[0], str) and widgets_values[0].strip():
                return widgets_values[0]

    return None


def extract_positive_prompt(data: Any) -> Optional[str]:
    """Extract the positive prompt from ComfyUI prompt or workflow data.

    Follows the ``positive`` connection of a sampler-like node to its text
    encoder when possible, otherwise falls back to the first text encoder
    whose title does not look negative.

    Args:
        data: Parsed ``prompt`` (flat id -> node) or ``workflow`` (``nodes``
            array) structure.

    Returns:
        The positive prompt text, or None if none could be found.
    """
    if not isinstance(data, dict):
        return None

    nodes_by_id = {}
    if "nodes" in data:
        for node in data["nodes"]:
            if isinstance(node, dict):
                nid = node.get("id")
                if nid is not None:
                    nodes_by_id[nid] = node
    else:
        for nid_str, node in data.items():
            try:
                nid = int(nid_str)
            except (ValueError, TypeError):
                nid = nid_str
            if isinstance(node, dict):
                if "id" in node:
                    nid = node["id"]
                nodes_by_id[nid] = node

    if not nodes_by_id:
        return None

    # Prefer the encoder wired into a node that has both positive and negative.
    pos_id = None
    for node in nodes_by_id.values():
        inputs = get_node_inputs(node)
        if "positive" in inputs and "negative" in inputs:
            try:
                pos_input = inputs["positive"]
                if isinstance(pos_input, list) and len(pos_input) > 0:
                    pos_id = int(pos_input[0])
                    break
            except (ValueError, TypeError, IndexError):
                continue

    if pos_id is not None and pos_id in nodes_by_id:
        text_val = find_text_in_node(nodes_by_id[pos_id])
        if text_val:
            return text_val

    # Fallback: any text encoder, non-negative titles first.
    text_nodes = []
    for node in nodes_by_id.values():
        text_val = find_text_in_node(node)
        if text_val:
            node_title = (node.get("title") or "").lower()
            if "neg" not in node_title:
                text_nodes.insert(0, text_val)
            else:
                text_nodes.append(text_val)

    return text_nodes[0] if text_nodes else None


def extract_readable_prompt(parsed_data: Dict[str, Any]) -> Optional[str]:
    """Extract human-readable prompt text from parsed metadata.

    Args:
        parsed_data: Output of :func:`parse_embedded_metadata`.

    Returns:
        The prompt text, or None if none could be recovered.
    """

    def safe_to_string(value):
        if isinstance(value, str):
            return value
        elif isinstance(value, list):
            return " ".join(str(item) for item in value if item)
        elif value is not None:
            return str(value)
        return None

    if parsed_data.get("positive_prompt"):
        return parsed_data["positive_prompt"]

    prompt_data = parsed_data.get("prompt")

    if isinstance(prompt_data, str):
        return prompt_data

    if prompt_data and not isinstance(prompt_data, dict):
        return safe_to_string(prompt_data)

    if isinstance(prompt_data, dict):
        positive_prompt = extract_positive_prompt(prompt_data)
        if positive_prompt:
            return positive_prompt

    workflow_data = parsed_data.get("workflow")
    if isinstance(workflow_data, dict):
        positive_prompt = extract_positive_prompt(workflow_data)
        if positive_prompt:
            return positive_prompt

    if parsed_data.get("parameters", {}).get("positive"):
        return safe_to_string(parsed_data["parameters"]["positive"])

    return None


def prompt_text_from_image(image_path: str) -> Optional[str]:
    """Recover the positive prompt text embedded in an image file.

    Args:
        image_path: Path to the image file.

    Returns:
        Stripped prompt text, or None if the image carries none.
    """
    raw = read_embedded_metadata(image_path)
    if not raw:
        return None
    return _clean_prompt_text(extract_readable_prompt(parse_embedded_metadata(raw)))


def _clean_prompt_text(prompt_text: Any) -> Optional[str]:
    """Normalize extracted prompt text to a non-empty string or None."""
    if prompt_text is None:
        return None
    if not isinstance(prompt_text, str):
        prompt_text = str(prompt_text)
    prompt_text = prompt_text.strip()
    return prompt_text or None


def ingest_image(
    db,
    image_path: str,
    raw_metadata: Optional[Dict[str, str]] = None,
    image_metadata: Optional[Dict[str, Any]] = None,
) -> IngestResult:
    """Link an image to the prompt embedded in it, creating the prompt if new.

    Prompts are deduplicated by hash, so ingesting many images generated from
    the same text links them all to one prompt. Images without recoverable
    prompt text are left alone rather than guessed at.

    Args:
        db: PromptDatabase instance.
        image_path: Path to the image file.
        raw_metadata: Raw text chunks if the caller already read them; read
            from the file when omitted.
        image_metadata: Structured metadata to store on the image record
            (as produced by ComfyUIMetadataExtractor), if available.

    Returns:
        An :class:`IngestResult` describing what happened.
    """
    result = IngestResult()
    image_path = str(image_path)

    if raw_metadata is None:
        raw_metadata = read_embedded_metadata(image_path)
    if not raw_metadata:
        return result

    parsed = parse_embedded_metadata(raw_metadata)
    if not (parsed.get("prompt") or parsed.get("parameters")):
        return result
    result.has_metadata = True

    prompt_text = _clean_prompt_text(extract_readable_prompt(parsed))
    if not prompt_text:
        return result

    prompt_hash = generate_prompt_hash(prompt_text)
    existing = db.get_prompt_by_hash(prompt_hash)

    if existing:
        result.prompt_id = existing["id"]
    else:
        file_name = image_path.replace("\\", "/").rsplit("/", 1)[-1]
        result.prompt_id = db.save_prompt(
            prompt_text,
            SCANNED_CATEGORY,
            list(SCANNED_TAGS),
            None,
            f"Auto-scanned from {file_name}",
            prompt_hash,
        )
        result.created = bool(result.prompt_id)

    if result.prompt_id:
        try:
            result.image_id = db.link_image_to_prompt(
                result.prompt_id, image_path, image_metadata
            )
        except Exception as e:
            logger.error(f"Failed to link {image_path} to prompt: {e}")

    return result
