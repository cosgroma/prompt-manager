# Changelog

## 0.1.0 — 2026-09-18

First release of PromptManager as a standalone application, hard-forked from
[ComfyAssets/ComfyUI_PromptManager](https://github.com/ComfyAssets/ComfyUI_PromptManager) v3.2.2.

### Added
- `promptmanager` command (`python -m promptmanager`) serving the gallery, admin,
  tagging and metadata UIs from its own aiohttp server, with optional `--token` auth.
- `create_app()` application factory; importing the package has no side effects.
- Shared metadata ingestion (`promptmanager.ingest.prompt_extraction`): the
  filesystem monitor now creates prompts from image metadata, as the bulk scan does.
- Data directory (`~/.promptmanager`, `PROMPT_MANAGER_HOME`, `--data-dir`) holding the
  database, `config.json`, logs and models.
- `PROMPT_MANAGER_DB`, `PROMPT_MANAGER_OUTPUT_DIRS`, `PROMPT_MANAGER_MONITORING`,
  `PROMPT_MANAGER_MODELS_DIR` and `PROMPT_MANAGER_COMFYUI_DIR` environment variables.
- Changing gallery directories in Settings restarts the monitor without a server restart.
- The monitor also picks up images finalized by rename.
- `video` extra for OpenCV-based video thumbnails; the bulk scan skips videos quietly
  instead of logging an error for each one.

### Changed
- `src/` layout with a single `promptmanager` package; pip-installable, web assets
  shipped as package data.
- Output directories must be configured explicitly; they are no longer auto-detected.
- Images without a recoverable prompt are left unlinked instead of being attached
  to the most recent prompt.

### Removed
- ComfyUI custom node support: the `PromptManager`, `PromptManagerText` and
  `PromptSearchList` nodes, the prompt tracker, the ComfyUI frontend extension,
  and Comfy Registry publishing.
