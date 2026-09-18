"""Filesystem monitoring for newly generated images.

Watches the configured output directories and, for every new image, recovers
the prompt embedded in the file and links the image to it (creating the prompt
if it has not been seen before). PromptManager has no hook into the program
that generates the images, so the file's own metadata is the single source of
truth; see :mod:`promptmanager.ingest.prompt_extraction`.

The main components are:
- ImageGenerationHandler: Handles filesystem events for new image creation
- ImageMonitor: Manages directory watching and its lifecycle

Typical usage:
    from promptmanager.ingest.image_monitor import ImageMonitor

    monitor = ImageMonitor(db)
    monitor.start_monitoring(['/path/to/output'])
    ...
    monitor.stop_monitoring()
"""

import os
import threading
from typing import Any, Callable, Dict, Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from ..utils.logging_config import get_logger
from .metadata_extractor import ComfyUIMetadataExtractor
from .prompt_extraction import ingest_image


class ImageGenerationHandler(FileSystemEventHandler):
    """Filesystem event handler for detecting new image generation.

    When a new image appears in a monitored directory the handler waits a
    short delay (so the writer can finish), then ingests the file: the prompt
    embedded in its metadata is looked up or created, and the image is linked
    to it. Images that carry no recoverable prompt are left unlinked; they
    still show up in the gallery, which lists files straight from disk.
    """

    def __init__(self, db_manager, on_ingest: Optional[Callable[[], None]] = None):
        """
        Initialize the image generation handler.

        Args:
            db_manager: Database manager instance for storing image-prompt relationships
            on_ingest: Optional callback invoked after a new image is detected
                (used to invalidate the gallery listing cache)
        """
        self.db_manager = db_manager
        self.on_ingest = on_ingest
        self.metadata_extractor = ComfyUIMetadataExtractor()
        self.logger = get_logger("prompt_manager.image_monitor")

        from ..config import GalleryConfig

        self.processing_delay = GalleryConfig.PROCESSING_DELAY
        self.supported_extensions = tuple(GalleryConfig.SUPPORTED_EXTENSIONS)

    def on_created(self, event):
        """Handle filesystem creation events.

        Filters for image files and schedules them for processing after a small
        delay to ensure the file is fully written.

        Args:
            event: FileSystemEvent object containing event details
        """
        if not event.is_directory and self.is_image_file(event.src_path):
            self.logger.info(f"New image detected: {event.src_path}")
            timer = threading.Timer(
                self.processing_delay, self.process_new_image, args=[event.src_path]
            )
            timer.daemon = True
            timer.start()

    def on_moved(self, event):
        """Handle renames, which is how some writers finalize output files.

        Args:
            event: FileSystemMovedEvent with the destination in ``dest_path``
        """
        if not event.is_directory and self.is_image_file(event.dest_path):
            self.logger.info(f"New image detected (moved): {event.dest_path}")
            timer = threading.Timer(
                self.processing_delay, self.process_new_image, args=[event.dest_path]
            )
            timer.daemon = True
            timer.start()

    def is_image_file(self, filepath: str) -> bool:
        """Check if file is a supported image format and not a thumbnail.

        Args:
            filepath: Path to the file to check

        Returns:
            True if the file has a supported image extension and is not
            inside a thumbnails directory, False otherwise
        """
        # Skip files in thumbnails directory - those are derivatives, not generated images
        if "/thumbnails/" in filepath or "\\thumbnails\\" in filepath:
            return False
        return filepath.lower().endswith(self.supported_extensions)

    def process_new_image(self, image_path: str):
        """Ingest a newly created image file.

        Args:
            image_path: Full path to the newly created image file
        """
        try:
            self.logger.info(f"Processing image: {image_path}")

            if not os.path.exists(image_path):
                self.logger.warning(f"Image file no longer exists: {image_path}")
                return

            # Structured metadata (workflow, parameters, file info) is stored on
            # the image record for the gallery's metadata panel.
            image_metadata = None
            try:
                image_metadata = self.metadata_extractor.extract_metadata(image_path)
            except Exception as meta_error:
                self.logger.warning(f"Metadata extraction failed: {meta_error}")

            result = ingest_image(
                self.db_manager, image_path, image_metadata=image_metadata
            )

            if result.prompt_id:
                verb = "new" if result.created else "existing"
                self.logger.info(
                    f"Linked {os.path.basename(image_path)} to {verb} "
                    f"prompt {result.prompt_id}"
                )
            else:
                self.logger.info(
                    f"No prompt found in {os.path.basename(image_path)}; left unlinked"
                )

            if self.on_ingest:
                self.on_ingest()

        except Exception as e:
            self.logger.error(f"Error processing image {image_path}: {e}")
            import traceback

            self.logger.error(traceback.format_exc())


class ImageMonitor:
    """Main image monitoring system for the gallery.

    This class manages the overall image monitoring system, including:
    - Setting up filesystem watchers for output directories
    - Managing the lifecycle of monitoring operations
    - Providing status information

    The monitor uses watchdog to efficiently watch filesystem changes and can
    monitor multiple directories simultaneously with recursive subdirectory support.
    """

    def __init__(self, db_manager, on_ingest: Optional[Callable[[], None]] = None):
        """
        Initialize the image monitor.

        Args:
            db_manager: Database manager instance for storing image relationships
            on_ingest: Optional callback invoked after each new image is processed
        """
        self.db_manager = db_manager
        self.on_ingest = on_ingest
        self.observer = None
        self.handler = None
        self.monitored_directories = []
        self.logger = get_logger("prompt_manager.image_monitor")

    def set_database(self, db_manager):
        """Point the monitor at a new database object (after a restore).

        Args:
            db_manager: Replacement database manager instance
        """
        self.db_manager = db_manager
        if self.handler is not None:
            self.handler.db_manager = db_manager

    def start_monitoring(self, output_directories: Optional[list] = None):
        """
        Start monitoring output directories for new images.

        Begins filesystem watching on the specified directories, or on
        GalleryConfig.MONITORING_DIRECTORIES when none are given. All
        monitoring is recursive to catch images in subdirectories.

        Args:
            output_directories: List of directory paths to monitor. If None, uses config.
        """
        if self.observer:
            self.logger.warning("Image monitoring already running")
            return

        from ..config import GalleryConfig

        if not GalleryConfig.MONITORING_ENABLED:
            self.logger.info("Image monitoring disabled in config")
            return

        if not output_directories:
            output_directories = list(GalleryConfig.MONITORING_DIRECTORIES)

        if not output_directories:
            self.logger.warning(
                "No output directories configured; nothing to monitor. "
                "Pass --output-dir or add one in Settings."
            )
            return

        # Create event handler
        self.handler = ImageGenerationHandler(self.db_manager, self.on_ingest)

        # Start observer
        self.observer = Observer()

        for output_dir in output_directories:
            if os.path.exists(output_dir):
                self.observer.schedule(self.handler, output_dir, recursive=True)
                self.monitored_directories.append(output_dir)
                self.logger.info(f"Monitoring directory (recursive): {output_dir}")
            else:
                self.logger.warning(f"Directory does not exist: {output_dir}")

        if self.monitored_directories:
            self.observer.start()
            self.logger.info(
                f"Image monitoring started for {len(self.monitored_directories)} directories"
            )
        else:
            self.logger.warning("No valid directories to monitor")
            self.observer = None
            self.handler = None

    def restart_monitoring(self):
        """Stop and start monitoring so configuration changes take effect."""
        self.stop_monitoring()
        self.start_monitoring()

    def stop_monitoring(self):
        """Stop the image monitoring system.

        Cleanly shuts down the filesystem watcher and clears all monitoring state.
        This method should be called before program exit to ensure proper cleanup.
        """
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
            self.handler = None
            self.monitored_directories = []
            self.logger.debug("Image monitoring stopped")

    def get_status(self) -> Dict[str, Any]:
        """Get monitoring status information.

        Returns:
            Dictionary containing:
            - running: Boolean indicating if monitoring is active
            - monitored_directories: List of currently monitored directory paths
            - handler_active: Boolean indicating if the event handler is active
            - observer_alive: Boolean indicating if observer thread is alive
        """
        observer_alive = False
        if self.observer is not None:
            try:
                observer_alive = self.observer.is_alive()
            except Exception:
                pass
        return {
            "running": self.observer is not None,
            "monitored_directories": self.monitored_directories,
            "handler_active": self.handler is not None,
            "observer_alive": observer_alive,
        }
