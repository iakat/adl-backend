#!/usr/bin/env python3
"""
ADSB.lol Aircraft Data Link ADL Backend
"""

import asyncio
import logging
import os
import signal
import socket
import sys
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Dict, Optional, Set

# Third party
import aiohttp
import orjson
import zstandard as zstd

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

try:
    with open("/app/.venv/.git_commit", "r") as f:
        commit_hash = f.read().strip()
except FileNotFoundError:
    commit_hash = "unknown"


class GitHubUploader:
    """Handles uploading files to GitHub releases"""

    def __init__(self, token: str, repo: str, dry_run: bool = False):
        self.token = token
        self.repo = repo
        self.dry_run = dry_run
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"token {self.token}"},
            timeout=aiohttp.ClientTimeout(total=300),  # 5 minute timeout
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _ensure_release_exists(self, release_tag: str) -> Optional[int]:
        """Ensure the release exists, create if it doesn't. Returns release_id"""
        if self.dry_run:
            logger.info(f"[DRY RUN] Would ensure release {release_tag} exists")
            return None

        url = f"https://api.github.com/repos/{self.repo}/releases/tags/{release_tag}"

        try:
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    release_id = data["id"]
                    logger.info(
                        f"Found existing release: {release_tag} (ID: {release_id})"
                    )
                    return release_id
                elif resp.status != 404:
                    logger.error(f"Error checking release: {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"Error checking release: {e}")
            return None

        # Create new release
        cc0_license = """## License

This data is released under CC0 1.0 Universal (CC0 1.0) Public Domain Dedication.

To the extent possible under law, the person who associated CC0 with this work has waived all copyright and related or neighboring rights to this work.

You can copy, modify, distribute and perform the work, even for commercial purposes, all without asking permission.

See: https://creativecommons.org/publicdomain/zero/1.0/"""

        create_url = f"https://api.github.com/repos/{self.repo}/releases"
        release_data = {
            "tag_name": release_tag,
            "name": release_tag,
            "body": f"Automated aircraft data link collection uploads\n\nCreated: {datetime.now(UTC).isoformat()}\n\n{cc0_license}",
            "draft": False,
            "prerelease": False,
        }

        try:
            async with self.session.post(create_url, json=release_data) as resp:
                if resp.status == 201:
                    data = await resp.json()
                    release_id = data["id"]
                    logger.info(
                        f"Created new release: {release_tag} (ID: {release_id})"
                    )
                    return release_id
                else:
                    error_text = await resp.text()
                    logger.error(
                        f"Failed to create release: {resp.status} - {error_text}"
                    )
                    return None
        except Exception as e:
            logger.error(f"Error creating release: {e}")
            return None

    async def upload_file(self, file_path: Path, file_start_time: datetime) -> bool:
        """Upload a file to the GitHub release using a tag based on the file's start time"""
        # Generate release tag based on file start time
        date_str = file_start_time.date().isoformat()
        release_tag = f"adsblol-adl-{date_str}"

        # Ensure the release exists for this tag
        release_id = await self._ensure_release_exists(release_tag)
        if not release_id:
            logger.error(f"No release ID available for upload to {release_tag}")
            return False

        if self.dry_run:
            logger.info(
                f"[DRY RUN] Would upload {file_path.name} ({file_path.stat().st_size} bytes) to {release_tag}"
            )
            return True

        try:
            # Check if asset already exists
            asset_name = file_path.name
            if await self._asset_exists(release_id, asset_name):
                logger.info(
                    f"Asset {asset_name} already exists in {release_tag}, skipping upload"
                )
                return True

            # Upload the file
            upload_url = f"https://uploads.github.com/repos/{self.repo}/releases/{release_id}/assets"

            with open(file_path, "rb") as f:
                data = f.read()

            params = {"name": asset_name}
            headers = {"Content-Type": "application/octet-stream"}

            async with self.session.post(
                upload_url, params=params, headers=headers, data=data
            ) as resp:
                if resp.status == 201:
                    logger.info(
                        f"Successfully uploaded {asset_name} ({len(data)} bytes) to {release_tag}"
                    )
                    return True
                else:
                    error_text = await resp.text()
                    logger.error(f"Upload failed: {resp.status} - {error_text}")
                    return False

        except Exception as e:
            logger.error(f"Error uploading {file_path.name}: {e}")
            return False

    async def _asset_exists(self, release_id: int, asset_name: str) -> bool:
        """Check if an asset already exists in the release"""
        url = f"https://api.github.com/repos/{self.repo}/releases/{release_id}/assets"

        try:
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    assets = await resp.json()
                    return any(asset["name"] == asset_name for asset in assets)
        except Exception as e:
            logger.error(f"Error checking assets: {e}")

        return False


class FileManager:
    """Manages file rotation, compression, and upload"""

    def __init__(
        self,
        data_dir: Path,
        uploader: GitHubUploader,
        max_size: int = 512 * 1024 * 1024,
        max_age: int = 3600,
    ):
        self.data_dir = data_dir
        self.uploader = uploader
        self.max_size = max_size
        self.max_age = max_age

        # File tracking
        self.files: Dict[str, Optional[object]] = {}
        self.file_sizes: Dict[str, int] = defaultdict(int)
        self.file_start_times: Dict[str, datetime] = {}

        # Background tasks
        self.upload_queue = asyncio.Queue()
        self.upload_task: Optional[asyncio.Task] = None

        # Store file metadata for uploads (file_path -> start_time)
        self.pending_uploads: Dict[Path, datetime] = {}

        # Emergency shutdown flag
        self.emergency_shutdown = False

        # Ensure data directory exists
        self.data_dir.mkdir(parents=True, exist_ok=True)

    async def start(self):
        """Start background tasks"""
        self.upload_task = asyncio.create_task(self._upload_worker())
        logger.info("FileManager started")

    async def stop(self):
        """Stop background tasks and close files"""
        if self.upload_task:
            self.upload_task.cancel()
            try:
                await self.upload_task
            except asyncio.CancelledError:
                pass

        # Only rotate and upload if not already done in emergency shutdown
        if not self.emergency_shutdown:
            for data_type in list(self.files.keys()):
                await self._rotate_and_upload(data_type)

        logger.info("FileManager stopped")

    async def _upload_worker(self):
        """Background worker for uploading files"""
        while True:
            try:
                file_path = await self.upload_queue.get()
                await self._process_upload(file_path)
                self.upload_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Upload worker error: {e}")

    async def _process_upload(self, file_path: Path):
        """Compress and upload a file"""
        try:
            # Get the file start time from metadata
            file_start_time = self.pending_uploads.get(file_path, datetime.now(UTC))

            if self.emergency_shutdown:
                # Skip compression during emergency shutdown for faster upload
                logger.info(f"Emergency upload (no compression): {file_path.name}")
                success = await self.uploader.upload_file(file_path, file_start_time)

                if success:
                    try:
                        file_path.unlink()
                        logger.info(f"Cleaned up local file: {file_path.name}")
                    except Exception as e:
                        logger.error(f"Error cleaning up {file_path.name}: {e}")
                else:
                    logger.error(f"Emergency upload failed for {file_path.name}")

                # Clean up metadata
                self.pending_uploads.pop(file_path, None)
                return

            # Normal compression and upload
            compressed_path = await self._compress_file(file_path)
            if not compressed_path:
                self.pending_uploads.pop(file_path, None)
                return

            # Upload to GitHub with the original file's start time
            success = await self.uploader.upload_file(compressed_path, file_start_time)

            if success:
                # Clean up local files
                try:
                    compressed_path.unlink()
                    logger.info(f"Cleaned up local file: {compressed_path.name}")
                except Exception as e:
                    logger.error(f"Error cleaning up {compressed_path.name}: {e}")
            else:
                logger.error(f"Upload failed for {compressed_path.name}")

            # Clean up metadata
            self.pending_uploads.pop(file_path, None)

        except Exception as e:
            logger.error(f"Error processing upload for {file_path.name}: {e}")
            # Clean up metadata on error
            self.pending_uploads.pop(file_path, None)

    async def _compress_file(self, file_path: Path) -> Optional[Path]:
        """Compress file with zstd"""
        try:
            compressed_path = file_path.with_suffix(file_path.suffix + ".zst")

            def compress():
                with open(file_path, "rb") as f_in:
                    with open(compressed_path, "wb") as f_out:
                        cctx = zstd.ZstdCompressor(level=3, threads=2)
                        cctx.copy_stream(f_in, f_out)
                file_path.unlink()  # Remove original

            await asyncio.to_thread(compress)
            logger.info(f"Compressed: {file_path.name} -> {compressed_path.name}")
            return compressed_path

        except Exception as e:
            logger.error(f"Compression failed for {file_path.name}: {e}")
            return None

    async def emergency_upload_all(self):
        """Emergency upload all current files without compression"""
        self.emergency_shutdown = True
        logger.info("Starting emergency upload of all current files")

        # Rotate and queue all current files for upload
        for data_type in list(self.files.keys()):
            if data_type in self.files and self.files[data_type]:
                await self._rotate_and_upload(data_type)

        # Wait for all uploads to complete
        await self.upload_queue.join()
        logger.info("Emergency upload completed")

    def _should_rotate(self, data_type: str) -> bool:
        """Check if file needs rotation"""
        if data_type not in self.file_start_times:
            return True

        age = (datetime.now(UTC) - self.file_start_times[data_type]).total_seconds()
        return age >= self.max_age or self.file_sizes[data_type] >= self.max_size

    async def _rotate_and_upload(self, data_type: str):
        """Rotate file and queue for upload"""
        now = datetime.now(UTC)

        # Close current file
        if data_type in self.files and self.files[data_type]:
            self.files[data_type].close()
            self.files[data_type] = None

        # Move to timestamped name and queue for upload
        current_path = self.data_dir / f"adsblol-adl-{data_type}_current.jsonl"
        if current_path.exists() and current_path.stat().st_size > 0:
            since = self.file_start_times.get(data_type, now)
            timestamp_start = since.strftime("%Y%m%d-%H%M%S")
            timestamp_end = now.strftime("%Y%m%d-%H%M%S")
            archived_name = (
                f"adsblol-adl-{data_type}_{timestamp_start}_{timestamp_end}.jsonl"
            )
            archived_path = self.data_dir / archived_name

            current_path.rename(archived_path)

            # Store the file start time for the upload
            self.pending_uploads[archived_path] = since

            await self.upload_queue.put(archived_path)
            logger.info(
                f"Queued for upload: {archived_name} (start time: {since.isoformat()})"
            )

        # Start new file
        new_path = self.data_dir / f"adsblol-adl-{data_type}_current.jsonl"
        self.files[data_type] = open(new_path, "w", buffering=8192)
        self.file_sizes[data_type] = 0
        self.file_start_times[data_type] = now

    async def write_message(self, data_type: str, message: dict):
        """Write message with automatic rotation"""
        try:
            # Check for rotation
            if self._should_rotate(data_type):
                await self._rotate_and_upload(data_type)

            # Add metadata
            message["_adsblol"] = {
                "received_at": datetime.now(UTC).isoformat(),
                "protocol": data_type,
                "made_by": f"github.com/iakat/adl-backend@{commit_hash}",
            }
            # Write to file
            line = orjson.dumps(message).decode("utf-8") + "\n"
            self.files[data_type].write(line)
            self.files[data_type].flush()
            self.file_sizes[data_type] += len(line.encode("utf-8"))

        except Exception as e:
            logger.error(f"Write failed for {data_type}: {e}")


class ClientHandler:
    """Handles individual TCP connections"""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        data_type: str,
        file_manager: FileManager,
    ):
        self.reader = reader
        self.writer = writer
        self.data_type = data_type
        self.file_manager = file_manager
        self.client_ip = (
            writer.get_extra_info("peername")[0]
            if writer.get_extra_info("peername")
            else "unknown"
        )
        # Buffer size limits to prevent memory exhaustion
        self.max_buffer_size = 1024 * 1024  # 1MB total buffer limit
        self.max_message_size = 512 * 1024  # 512KB per message limit

    async def handle(self):
        """Handle client connection"""

        # if client_ip is local-ish, don't log...
        if not self.client_ip.startswith("10."):
            logger.info(f"Client connected: {self.client_ip} -> {self.data_type}")

        try:
            buffer = ""
            while True:
                data = await self.reader.read(8192)
                if not data:
                    break

                try:
                    buffer += data.decode("utf-8")
                except UnicodeDecodeError as e:
                    logger.error(f"Unicode decode error from {self.client_ip}: {e} - disconnecting client")
                    break

                # Check if buffer is getting too large to prevent memory exhaustion
                if len(buffer) > self.max_buffer_size:
                    logger.error(f"Buffer size exceeded ({len(buffer)} bytes) from {self.client_ip} - disconnecting client")
                    break

                # Process complete lines
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        # Check message size before processing
                        message_size = len(line.encode('utf-8'))
                        if message_size > self.max_message_size:
                            logger.error(f"Message size exceeded ({message_size} bytes) from {self.client_ip} - disconnecting client")
                            return

                        try:
                            await self._process_message(line)
                        except ValueError as e:
                            # JSON parsing error or other malformed data
                            logger.error(f"Malformed message from {self.client_ip}: {e} - disconnecting client")
                            return

        except asyncio.CancelledError:
            logger.info(f"Client connection cancelled: {self.client_ip}")
        except Exception as e:
            logger.error(f"Client error {self.client_ip}: {e}")
        finally:
            self.writer.close()
            await self.writer.wait_closed()
            if not self.client_ip.startswith("10."):
                logger.info(f"Client disconnected: {self.client_ip}")

    async def _process_message(self, line: str):
        """Process a JSON message"""
        try:
            message = orjson.loads(line)
            await self.file_manager.write_message(self.data_type, message)
        except orjson.JSONDecodeError as e:
            # Raise ValueError to trigger client disconnection
            raise ValueError(f"Invalid JSON: {e}")
        except Exception as e:
            logger.error(f"Message processing error from {self.client_ip}: {e}")
            # Don't disconnect for other processing errors


class ADSBLOLADL:
    """Main application class"""

    PORTS = {5550: "acars", 5552: "vdl2", 5551: "hfdl"}

    def __init__(
        self,
        data_dir: str,
        host: str = None,  # None enables dual-stack binding
        github_token: Optional[str] = None,
        github_repo: Optional[str] = None,
        max_size: int = None,
        max_age: int = None,
        dry_run: bool = False,
    ):
        self.data_dir = Path(data_dir)
        self.host = host
        self.github_token = github_token
        self.github_repo = github_repo
        self.max_size = max_size
        self.max_age = max_age
        self.dry_run = dry_run
        self.running = True

        # Components
        self.servers = []
        self.uploader: Optional[GitHubUploader] = None
        self.file_manager: Optional[FileManager] = None

    async def start(self):
        """Start the collector"""
        logger.info("Starting ADSB.lol ADL Backend")

        # Initialize GitHub uploader
        if self.github_token and self.github_repo:
            self.uploader = GitHubUploader(
                token=self.github_token,
                repo=self.github_repo,
                dry_run=self.dry_run,
            )
            await self.uploader.__aenter__()
        else:
            logger.warning(
                "GitHub configuration missing - files will only be stored locally"
            )

        # Initialize file manager
        self.file_manager = FileManager(
            data_dir=self.data_dir,
            uploader=self.uploader,
            max_size=self.max_size,
            max_age=self.max_age,
        )
        await self.file_manager.start()

        # Start TCP servers with dual-stack support (IPv4 and IPv6)
        for port, data_type in self.PORTS.items():
            server = await asyncio.start_server(
                lambda r, w, dt=data_type: self._handle_client(r, w, dt),
                self.host,
                port,
                family=socket.AF_UNSPEC,  # Enable dual-stack IPv4/IPv6
            )
            self.servers.append(server)
            logger.info(
                f"Listening on {self.host or 'all interfaces'}:{port} ({data_type}) - dual-stack IPv4/IPv6"
            )

        logger.info("ADSB.lol ADL Backend started successfully")

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, data_type: str
    ):
        """Handle new client connection"""
        handler = ClientHandler(reader, writer, data_type, self.file_manager)
        await handler.handle()

    async def stop(self):
        """Stop the collector"""
        logger.info("Stopping ADSB.lol ADL Backend")
        self.running = False

        # Stop servers
        for server in self.servers:
            server.close()
            await server.wait_closed()

        # Stop file manager
        if self.file_manager:
            await self.file_manager.stop()

        # Close uploader
        if self.uploader:
            await self.uploader.__aexit__(None, None, None)

        logger.info("ADSB.lol ADL Backend stopped")

    async def run_forever(self):
        """Main run loop"""
        try:
            while self.running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("Received interrupt signal - starting emergency upload")
            # Trigger emergency upload on KeyboardInterrupt as well
            if self.file_manager:
                await self.file_manager.emergency_upload_all()
        finally:
            await self.stop()


def setup_signal_handlers(app):
    """Setup graceful shutdown"""

    def signal_handler():
        logger.info("Received shutdown signal - starting emergency upload")
        app.running = False
        # Trigger emergency upload without compression
        if app.file_manager:
            asyncio.create_task(app.file_manager.emergency_upload_all())

    for sig in [signal.SIGTERM, signal.SIGINT]:
        try:
            asyncio.get_event_loop().add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass  # Windows doesn't support this


async def main():
    """Main entry point"""
    # Configure logging level from environment
    if os.getenv("DEBUG", "").lower() in ("true", "1", "yes"):
        logging.getLogger().setLevel(logging.DEBUG)

    # Configuration from environment variables
    data_dir = os.getenv("DATA_DIR", "/data")
    default_repo = f"adsblol/aircraft-data-links-{datetime.now(UTC).year}"
    host = os.getenv("HOST", "::")
    dry_run = os.getenv("DRY_RUN", "").lower() in ("true", "1", "yes")
    gh_token = os.getenv("GITHUB_TOKEN")
    gh_repo = os.getenv("GITHUB_REPO", default_repo)
    max_size = int(os.getenv("MAX_FILE_SIZE", 512 * 1024 * 1024))  # 512 MB
    max_age = int(os.getenv("MAX_FILE_AGE", 3600))  # 1 hour
    # GitHub configuration from environment
    current_year = datetime.now(UTC).year

    if not gh_token:
        if not dry_run:
            logger.error("GITHUB_TOKEN environment variable is required")
            logger.error("Set it or use DRY_RUN=true for testing")
            sys.exit(1)
        logger.warning("Running in dry-run mode - no GitHub uploads will occur")

    logger.info(f"Repository: {gh_repo}")
    logger.info("Tags will be generated per-file based on file start time")

    # Create and run app
    app = ADSBLOLADL(
        data_dir=data_dir,
        host=host,
        github_token=gh_token,
        github_repo=gh_repo,
        max_size=max_size,
        max_age=max_age,
        dry_run=dry_run,
    )

    setup_signal_handlers(app)

    try:
        await app.start()
        await app.run_forever()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


def cli_main():
    """Synchronous entry point for CLI script"""
    asyncio.run(main())
