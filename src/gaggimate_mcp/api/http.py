"""HTTP client for Gaggimate API.

Handles shot history operations via HTTP:
- Fetch index.bin (shot history index)
- Fetch specific shot .slog files
"""

import asyncio
from typing import Optional
import aiohttp

from gaggimate_mcp.config import GaggimateConfig
from gaggimate_mcp.errors import GaggimateError, ErrorCode
from gaggimate_mcp.logging_config import get_logger
from gaggimate_mcp.parsers.index import parse_binary_index, index_to_shot_list
from gaggimate_mcp.parsers.shot import parse_binary_shot, ShotData, is_html_response

logger = get_logger(__name__)

# Maximum retries for transient failures (timeouts, HTML responses)
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0  # seconds


class GaggimateHTTPClient:
    """HTTP client for Gaggimate API."""

    def __init__(self, config: Optional[GaggimateConfig] = None):
        """Initialize HTTP client.

        Args:
            config: Configuration object (uses default if None)
        """
        self.config = config or GaggimateConfig()
        self.timeout = aiohttp.ClientTimeout(total=self.config.request_timeout)

    @property
    def base_url(self) -> str:
        """Get base HTTP URL from config."""
        protocol = "https" if self.config.use_https else "http"
        return f"{protocol}://{self.config.host}/api/history"

    async def fetch_shot_index(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None
    ) -> list[dict]:
        """Fetch shot history index.

        Args:
            limit: Maximum number of shots to return
            offset: Number of shots to skip

        Returns:
            List of shot metadata dictionaries (sorted newest first)

        Raises:
            GaggimateError: If request fails
        """
        url = f"{self.base_url}/index.bin"
        logger.info("fetching_shot_index", url=url)

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(
                    url,
                    headers={"Accept": "application/octet-stream"}
                ) as response:
                    if response.status == 404:
                        # Index doesn't exist - empty history
                        logger.info("shot_index_not_found", url=url)
                        return []

                    if response.status != 200:
                        logger.error("http_error", status=response.status, url=url)
                        raise GaggimateError(
                            code=ErrorCode.API_ERROR,
                            message=f"HTTP {response.status}: {response.reason}"
                        )

                    # Parse binary index
                    binary_data = await response.read()

                    if is_html_response(binary_data):
                        logger.warning("html_response_for_index", url=url)
                        raise GaggimateError(
                            code=ErrorCode.PARSE_ERROR,
                            message=(
                                "Device returned HTML instead of binary data for index.bin. "
                                "This typically happens when the device is overloaded or "
                                "the firmware changed its HTTP response behavior."
                            ),
                            retryable=True,
                        )

                    index_data = parse_binary_index(binary_data)
                    shot_list = index_to_shot_list(index_data)

                    # Apply offset and limit
                    if offset and offset > 0:
                        shot_list = shot_list[offset:]
                    if limit and limit > 0:
                        shot_list = shot_list[:limit]

                    logger.info("shot_index_fetched", count=len(shot_list))
                    return shot_list

        except GaggimateError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error("http_connection_error", error=str(e), url=url)
            raise GaggimateError(
                code=ErrorCode.TIMEOUT if isinstance(e, asyncio.TimeoutError) else ErrorCode.DEVICE_UNREACHABLE,
                message=f"Request timed out after {self.config.request_timeout}s fetching shot index"
                if isinstance(e, asyncio.TimeoutError)
                else f"HTTP connection error: {str(e)}",
                retryable=True,
            ) from e
        except ValueError as e:
            logger.error("parse_error", error=str(e), url=url)
            raise GaggimateError(
                code=ErrorCode.PARSE_ERROR,
                message=f"Failed to parse index.bin: {str(e)}"
            ) from e

    async def fetch_shot(self, shot_id: str) -> Optional[ShotData]:
        """Fetch a specific shot by ID with automatic retry.

        Retries on transient failures (timeouts, HTML responses from overloaded
        device) with exponential backoff.

        Args:
            shot_id: Shot identifier (will be zero-padded to 6 digits)

        Returns:
            Parsed shot data, or None if not found

        Raises:
            GaggimateError: If request fails after all retries (excluding 404)
        """
        # Normalize ID to 6 digits for both filename and storage
        padded_id = shot_id.zfill(6)
        url = f"{self.base_url}/{padded_id}.slog"
        logger.info("fetching_shot", shot_id=shot_id, padded_id=padded_id, url=url)

        for attempt in range(MAX_RETRIES):
            try:
                return await self._fetch_shot_once(padded_id, url)
            except GaggimateError as e:
                if not e.retryable or attempt == MAX_RETRIES - 1:
                    raise
                wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "shot_fetch_retry",
                    shot_id=shot_id,
                    attempt=attempt + 1,
                    max_retries=MAX_RETRIES,
                    wait_seconds=wait,
                    error=str(e),
                )
                await asyncio.sleep(wait)

        raise RuntimeError("unreachable: retry loop exited without return or raise")

    async def _fetch_shot_once(self, padded_id: str, url: str) -> Optional[ShotData]:
        """Fetch a shot file in a single attempt.

        Args:
            padded_id: Zero-padded shot ID
            url: Full URL to the .slog file

        Returns:
            Parsed shot data, or None if not found

        Raises:
            GaggimateError: If request fails
        """
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(
                    url,
                    headers={"Accept": "application/octet-stream"}
                ) as response:
                    if response.status == 404:
                        logger.warning("shot_not_found", shot_id=padded_id, url=url)
                        return None

                    if response.status != 200:
                        logger.error("http_error", status=response.status, url=url)
                        raise GaggimateError(
                            code=ErrorCode.API_ERROR,
                            message=f"HTTP {response.status}: {response.reason}"
                        )

                    binary_data = await response.read()

                    # Detect HTML responses (device returning error page instead of binary)
                    if is_html_response(binary_data):
                        logger.warning(
                            "html_response_for_shot",
                            shot_id=padded_id,
                            url=url,
                            response_size=len(binary_data),
                        )
                        raise GaggimateError(
                            code=ErrorCode.PARSE_ERROR,
                            message=(
                                f"Device returned HTML instead of binary shot data for shot {padded_id}. "
                                "This typically happens when the device is overloaded or "
                                "the firmware's web server is serving its UI instead of the API response. "
                                "The request will be retried automatically."
                            ),
                            retryable=True,
                        )

                    shot_data = parse_binary_shot(binary_data, padded_id)

                    logger.info("shot_fetched", shot_id=padded_id, samples=shot_data.sample_count)
                    return shot_data

        except GaggimateError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error("http_connection_error", error=str(e), url=url)
            raise GaggimateError(
                code=ErrorCode.TIMEOUT if isinstance(e, asyncio.TimeoutError) else ErrorCode.DEVICE_UNREACHABLE,
                message=f"Request timed out after {self.config.request_timeout}s fetching shot {padded_id}"
                if isinstance(e, asyncio.TimeoutError)
                else f"HTTP connection error: {str(e)}",
                retryable=True,
            ) from e
        except ValueError as e:
            logger.error("parse_error", error=str(e), url=url)
            raise GaggimateError(
                code=ErrorCode.PARSE_ERROR,
                message=f"Failed to parse shot file: {str(e)}"
            ) from e

    async def list_recent_shots(self, limit: int = 10) -> list[dict]:
        """List recent shots (convenience method).

        Args:
            limit: Maximum number of shots to return (default 10)

        Returns:
            List of recent shot metadata dictionaries

        Raises:
            GaggimateError: If request fails
        """
        return await self.fetch_shot_index(limit=limit)
