"""
Tests for promptmanager.ingest.prompt_extraction.

The application has no hook into the image generator, so recovering the prompt
from a file's embedded metadata is the only way images get linked to prompts.
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from promptmanager.database.operations import PromptDatabase
from promptmanager.ingest.prompt_extraction import (
    extract_positive_prompt,
    ingest_image,
    parse_embedded_metadata,
    prompt_text_from_image,
)
from promptmanager.utils.hashing import generate_prompt_hash


def api_prompt(positive="a red fox", negative="blurry", encoder="CLIPTextEncode"):
    """Build a minimal ComfyUI API-format prompt graph."""
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {"positive": ["6", 0], "negative": ["7", 0], "seed": 1},
        },
        "6": {"class_type": encoder, "inputs": {"text": positive, "clip": ["4", 1]}},
        "7": {"class_type": encoder, "inputs": {"text": negative, "clip": ["4", 1]}},
    }


def write_png(path, chunks=None):
    """Write a small PNG with the given text chunks."""
    info = PngInfo()
    for key, value in (chunks or {}).items():
        info.add_text(key, value if isinstance(value, str) else json.dumps(value))
    Image.new("RGB", (8, 8), "white").save(path, pnginfo=info)


class TestExtractPositivePrompt(unittest.TestCase):
    """Test locating the positive prompt in a node graph."""

    def test_follows_positive_connection(self):
        """The sampler's positive link wins over graph order."""
        self.assertEqual(extract_positive_prompt(api_prompt()), "a red fox")

    def test_plain_clip_text_encode_needs_no_custom_node(self):
        """Stock CLIPTextEncode workflows are understood."""
        text = extract_positive_prompt(api_prompt(encoder="CLIPTextEncode"))
        self.assertEqual(text, "a red fox")

    def test_legacy_promptmanager_nodes_still_recognized(self):
        """Images made with the original custom node keep working."""
        text = extract_positive_prompt(api_prompt(encoder="PromptManager"))
        self.assertEqual(text, "a red fox")

    def test_workflow_format_uses_widget_values(self):
        """UI-format workflows store text in widgets_values."""
        workflow = {
            "nodes": [
                {"id": 1, "type": "CLIPTextEncode", "widgets_values": ["a castle"]},
            ]
        }
        self.assertEqual(extract_positive_prompt(workflow), "a castle")

    def test_negative_titled_node_is_not_preferred(self):
        """Without connections, nodes titled as negative are ranked last."""
        workflow = {
            "nodes": [
                {
                    "id": 1,
                    "type": "CLIPTextEncode",
                    "title": "Negative",
                    "widgets_values": ["ugly"],
                },
                {"id": 2, "type": "CLIPTextEncode", "widgets_values": ["a castle"]},
            ]
        }
        self.assertEqual(extract_positive_prompt(workflow), "a castle")

    def test_non_dict_and_empty_graphs_return_none(self):
        self.assertIsNone(extract_positive_prompt(None))
        self.assertIsNone(extract_positive_prompt({}))
        self.assertIsNone(extract_positive_prompt({"3": {"class_type": "KSampler"}}))


class TestParseEmbeddedMetadata(unittest.TestCase):
    """Test parsing of raw text chunks."""

    def test_a1111_parameters_block(self):
        parsed = parse_embedded_metadata(
            {"parameters": "a knight\nNegative prompt: lowres\nSteps: 20"}
        )
        self.assertEqual(parsed["positive_prompt"], "a knight")
        self.assertEqual(parsed["negative_prompt"], "lowres")

    def test_comfyui_prompt_chunk_is_decoded(self):
        parsed = parse_embedded_metadata({"prompt": json.dumps(api_prompt())})
        self.assertIsInstance(parsed["prompt"], dict)
        self.assertIsNone(parsed["positive_prompt"])

    def test_invalid_json_is_kept_as_string(self):
        parsed = parse_embedded_metadata({"prompt": "not json"})
        self.assertEqual(parsed["prompt"], "not json")


class IngestTestCase(unittest.TestCase):
    """Base class providing a temp database and image directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = PromptDatabase(os.path.join(self.tmp.name, "test.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def png(self, name, chunks=None):
        path = os.path.join(self.tmp.name, name)
        write_png(path, chunks)
        return path


class TestPromptTextFromImage(IngestTestCase):
    def test_reads_comfyui_png(self):
        path = self.png("a.png", {"prompt": api_prompt(positive="  a red fox  ")})
        self.assertEqual(prompt_text_from_image(path), "a red fox")

    def test_image_without_metadata(self):
        self.assertIsNone(prompt_text_from_image(self.png("plain.png")))

    def test_video_is_skipped_without_logging_an_error(self):
        """Videos are served by the gallery but carry no readable prompt."""
        path = os.path.join(self.tmp.name, "clip.MP4")
        with open(path, "wb") as handle:
            handle.write(b"\x00\x00\x00\x18ftypmp42")

        with patch("promptmanager.ingest.prompt_extraction.logger") as log:
            self.assertIsNone(prompt_text_from_image(path))
            self.assertFalse(ingest_image(self.db, path).has_metadata)

        log.error.assert_not_called()

    def test_unreadable_file(self):
        path = os.path.join(self.tmp.name, "broken.png")
        with open(path, "wb") as handle:
            handle.write(b"not an image")
        self.assertIsNone(prompt_text_from_image(path))


class TestIngestImage(IngestTestCase):
    """Test creating/looking up prompts and linking images."""

    def test_new_prompt_is_created_and_linked(self):
        path = self.png("a.png", {"prompt": api_prompt()})

        result = ingest_image(self.db, path)

        self.assertTrue(result.has_metadata)
        self.assertTrue(result.created)
        prompt = self.db.get_prompt_by_hash(generate_prompt_hash("a red fox"))
        self.assertEqual(prompt["id"], result.prompt_id)
        self.assertEqual(prompt["category"], "scanned")
        self.assertIn("auto-scanned", prompt["tags"])
        images = self.db.get_prompt_images(result.prompt_id)
        self.assertEqual([img["image_path"] for img in images], [path])

    def test_same_prompt_is_reused_across_images(self):
        first = ingest_image(self.db, self.png("a.png", {"prompt": api_prompt()}))
        second = ingest_image(self.db, self.png("b.png", {"prompt": api_prompt()}))

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.prompt_id, second.prompt_id)
        self.assertEqual(len(self.db.get_prompt_images(first.prompt_id)), 2)

    def test_existing_user_prompt_is_matched_not_duplicated(self):
        """A prompt saved by hand is linked to, keeping its category and tags."""
        text = "a red fox"
        prompt_id = self.db.save_prompt(
            text, "animals", ["fox"], 5, None, generate_prompt_hash(text)
        )

        result = ingest_image(self.db, self.png("a.png", {"prompt": api_prompt()}))

        self.assertFalse(result.created)
        self.assertEqual(result.prompt_id, prompt_id)
        self.assertEqual(self.db.get_prompt_by_id(prompt_id)["category"], "animals")

    def test_image_without_metadata_is_left_alone(self):
        """No guessing: images with no embedded prompt are never linked."""
        self.db.save_prompt("unrelated", None, None, None, None, "hash")

        result = ingest_image(self.db, self.png("plain.png"))

        self.assertFalse(result.has_metadata)
        self.assertIsNone(result.prompt_id)
        self.assertIsNone(result.image_id)

    def test_metadata_without_prompt_text(self):
        graph = {"3": {"class_type": "KSampler", "inputs": {"seed": 1}}}
        result = ingest_image(self.db, self.png("a.png", {"prompt": graph}))

        self.assertTrue(result.has_metadata)
        self.assertIsNone(result.prompt_id)

    def test_a1111_image(self):
        path = self.png("a.png", {"parameters": "a knight\nNegative prompt: lowres"})

        result = ingest_image(self.db, path)

        self.assertTrue(result.created)
        self.assertEqual(self.db.get_prompt_by_id(result.prompt_id)["text"], "a knight")

    def test_preread_metadata_is_used(self):
        """Callers that already read the chunks can pass them in."""
        path = self.png("plain.png")
        raw = {"prompt": json.dumps(api_prompt(positive="from caller"))}

        result = ingest_image(self.db, path, raw_metadata=raw)

        self.assertEqual(
            self.db.get_prompt_by_id(result.prompt_id)["text"], "from caller"
        )

    def test_structured_metadata_is_stored_on_image(self):
        path = self.png("a.png", {"prompt": api_prompt()})
        meta = {"file_info": {"size": 123, "dimensions": [8, 8], "format": "PNG"}}

        result = ingest_image(self.db, path, image_metadata=meta)

        image = self.db.get_prompt_images(result.prompt_id)[0]
        self.assertEqual(image["width"], 8)
        self.assertEqual(image["file_size"], 123)


if __name__ == "__main__":
    unittest.main()
