"""Connection diagnostics for Gaggimate device.

Provides tools to troubleshoot connectivity issues by:
- Testing device reachability (ping)
- Checking HTTP port accessibility
- Detecting HTTPS misconfiguration
- Providing troubleshooting guidance
"""

import asyncio
import socket
from typing import Optional
import aiohttp

from gaggimate_mcp.config import GaggimateConfig
from gaggimate_mcp.logging_config import get_logger

logger = get_logger(__name__)


async def ping_host(host: str, timeout: float = 2.0) -> dict:
    """Check if host is reachable via ICMP ping.

    Args:
        host: Hostname or IP address
        timeout: Timeout in seconds

    Returns:
        Dictionary with:
        - reachable (bool): Whether host responded
        - avg_time_ms (float|None): Average ping time in milliseconds
        - error (str|None): Error message if failed
    """
    try:
        # Use asyncio subprocess for ping
        if host.endswith('.local'):
            # For .local addresses, first try to resolve
            try:
                # This will use mDNS/Bonjour if available
                await asyncio.wait_for(
                    asyncio.to_thread(socket.gethostbyname, host),
                    timeout=timeout
                )
            except (socket.gaierror, asyncio.TimeoutError) as e:
                logger.warning("mdns_resolution_failed", host=host, error=str(e))
                return {
                    "reachable": False,
                    "avg_time_ms": None,
                    "error": f"mDNS resolution failed: {str(e)}. Device might be on different network or mDNS/Bonjour not available."
                }

        # Run ping command
        cmd = ['ping', '-c', '3', '-W', str(int(timeout * 1000)), host]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout + 2.0
        )

        if proc.returncode == 0:
            # Parse average ping time from output
            output = stdout.decode()
            avg_time = None

            # Look for "round-trip min/avg/max/stddev = ..." line
            for line in output.split('\n'):
                if 'round-trip' in line or 'rtt' in line:
                    parts = line.split('=')
                    if len(parts) > 1:
                        times = parts[1].strip().split('/')
                        if len(times) >= 2:
                            try:
                                avg_time = float(times[1])  # avg is second value
                            except (ValueError, IndexError):
                                pass

            return {
                "reachable": True,
                "avg_time_ms": avg_time,
                "error": None
            }
        else:
            error_msg = stderr.decode().strip() or "Ping failed"
            return {
                "reachable": False,
                "avg_time_ms": None,
                "error": error_msg
            }

    except asyncio.TimeoutError:
        return {
            "reachable": False,
            "avg_time_ms": None,
            "error": f"Ping timeout after {timeout}s"
        }
    except Exception as e:
        logger.error("ping_error", host=host, error=str(e))
        return {
            "reachable": False,
            "avg_time_ms": None,
            "error": str(e)
        }


async def check_port(host: str, port: int, timeout: float = 2.0) -> dict:
    """Check if a TCP port is open and accepting connections.

    Args:
        host: Hostname or IP address
        port: Port number
        timeout: Timeout in seconds

    Returns:
        Dictionary with:
        - open (bool): Whether port is accessible
        - error (str|None): Error message if failed
    """
    try:
        # Resolve hostname first
        try:
            _, _, _, _, addr = await asyncio.wait_for(
                asyncio.to_thread(socket.getaddrinfo, host, port, socket.AF_UNSPEC, socket.SOCK_STREAM),
                timeout=timeout
            )
            ip = addr[0][0]
        except (socket.gaierror, asyncio.TimeoutError) as e:
            return {
                "open": False,
                "error": f"DNS resolution failed: {str(e)}"
            }

        # Try to connect
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port),
                timeout=timeout
            )
            writer.close()
            await writer.wait_closed()

            return {
                "open": True,
                "error": None
            }
        except (ConnectionRefusedError, OSError) as e:
            return {
                "open": False,
                "error": f"Connection refused: {str(e)}"
            }

    except asyncio.TimeoutError:
        return {
            "open": False,
            "error": f"Connection timeout after {timeout}s"
        }
    except Exception as e:
        logger.error("port_check_error", host=host, port=port, error=str(e))
        return {
            "open": False,
            "error": str(e)
        }


async def check_http_endpoint(url: str, timeout: float = 5.0) -> dict:
    """Check if HTTP endpoint is accessible.

    Args:
        url: Full URL to check
        timeout: Timeout in seconds

    Returns:
        Dictionary with:
        - accessible (bool): Whether endpoint responded
        - status (int|None): HTTP status code
        - error (str|None): Error message if failed
    """
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url) as response:
                return {
                    "accessible": response.status < 500,
                    "status": response.status,
                    "error": None if response.status < 500 else f"Server error: {response.status}"
                }
    except aiohttp.ClientConnectorError as e:
        return {
            "accessible": False,
            "status": None,
            "error": f"Connection failed: {str(e)}"
        }
    except asyncio.TimeoutError:
        return {
            "accessible": False,
            "status": None,
            "error": f"Request timeout after {timeout}s"
        }
    except Exception as e:
        logger.error("http_check_error", url=url, error=str(e))
        return {
            "accessible": False,
            "status": None,
            "error": str(e)
        }


async def diagnose_connection(config: Optional[GaggimateConfig] = None) -> dict:
    """Run comprehensive connection diagnostics.

    Args:
        config: Configuration object (uses default if None)

    Returns:
        Dictionary with diagnostic results and troubleshooting guidance
    """
    cfg = config or GaggimateConfig()
    host = cfg.host

    logger.info("starting_diagnostics", host=host)

    results = {
        "host": host,
        "tests": {},
        "overall_status": "unknown",
        "issues": [],
        "recommendations": []
    }

    # Test 1: Ping host
    ping_result = await ping_host(host)
    results["tests"]["ping"] = ping_result

    if not ping_result["reachable"]:
        results["issues"].append(
            f"Device unreachable: {ping_result.get('error', 'Unknown error')}"
        )
        results["recommendations"].extend([
            "⚠️  VPN CHECK: If you have a VPN active, disconnect it. VPNs route traffic outside your local network, making local devices like GaggiMate unreachable.",
            "Check that your computer and GaggiMate are on the same WiFi network",
            "If using .local domain, ensure mDNS/Bonjour is working on your system",
            "Try accessing via IP address instead of hostname"
        ])
        results["overall_status"] = "unreachable"
        return results

    # Log ping performance
    if ping_result.get("avg_time_ms"):
        if ping_result["avg_time_ms"] > 100:
            results["issues"].append(
                f"High latency detected: {ping_result['avg_time_ms']:.1f}ms (>100ms)"
            )
            results["recommendations"].append(
                "High ping times suggest WiFi interference or weak signal. Consider moving closer to router."
            )

    # Test 2: Check HTTP port (80)
    http_port_result = await check_port(host, 80)
    results["tests"]["http_port"] = http_port_result

    if not http_port_result["open"]:
        results["issues"].append(
            f"HTTP port 80 not accessible: {http_port_result.get('error', 'Unknown error')}"
        )
        results["recommendations"].extend([
            "Device is reachable but web interface is not responding",
            "Check if GaggiMate web server is running",
            "Try restarting the GaggiMate device"
        ])
        results["overall_status"] = "port_closed"
        return results

    # Test 3: Check HTTP endpoint
    http_url = f"http://{host}/api/history/index.bin"
    http_result = await check_http_endpoint(http_url)
    results["tests"]["http_endpoint"] = http_result

    # Test 4: Check if HTTPS is incorrectly configured
    if cfg.use_https:
        https_url = f"https://{host}/api/history/index.bin"
        https_result = await check_http_endpoint(https_url, timeout=3.0)
        results["tests"]["https_endpoint"] = https_result

        if not https_result["accessible"] and http_result["accessible"]:
            results["issues"].append(
                "HTTPS is enabled in config but device only supports HTTP"
            )
            results["recommendations"].append(
                "Set GAGGIMATE_USE_HTTPS=false in your environment or MCP config"
            )

    # Determine overall status
    if http_result["accessible"]:
        if results["issues"]:
            results["overall_status"] = "connected_with_warnings"
        else:
            results["overall_status"] = "healthy"
    else:
        results["overall_status"] = "api_error"
        results["issues"].append(
            f"API endpoint not responding: {http_result.get('error', 'Unknown error')}"
        )

    # Add browser-specific recommendations
    if results["overall_status"] in ["healthy", "connected_with_warnings"]:
        results["recommendations"].extend([
            f"Use http://{host} in your browser (not https://)",
            "If browser still fails, try clearing HSTS cache",
            f"Bookmark http://{host} to avoid HTTPS auto-upgrade"
        ])

    logger.info("diagnostics_complete", status=results["overall_status"])
    return results
