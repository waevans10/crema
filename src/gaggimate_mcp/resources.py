"""MCP resource endpoints for Gaggimate knowledge, coffees, and user data.

Provides read-only access to local markdown files via MCP resources:
- gaggimate://knowledge                — list all knowledge files
- gaggimate://knowledge/{name}         — read a top-level knowledge file
- gaggimate://knowledge/{subdir}/{name} — read a knowledge file from a subdirectory
- gaggimate://coffees          — list all coffee tracking files
- gaggimate://coffees/{name}   — read a specific coffee file
- gaggimate://user/setup       — read user setup/preferences
- gaggimate://user/grind-map   — read grind map
- gaggimate://user/brewing-insights — read cross-coffee brewing insights
"""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from gaggimate_mcp.config import GaggimateConfig
from gaggimate_mcp.logging_config import get_logger

logger = get_logger(__name__)


def _list_markdown_files(directory: Path) -> list[dict[str, str]]:
    """List markdown files in a directory, including subdirectories.

    Args:
        directory: Path to scan for .md files

    Returns:
        List of dicts with 'name', 'filename', and optional 'subdirectory' keys,
        sorted by subdirectory then name.
    """
    if not directory.is_dir():
        return []

    files = []
    # Top-level files
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith("."):
            continue
        files.append({
            "name": path.stem,
            "filename": path.name,
        })

    # Subdirectory files (one level deep)
    for subdir in sorted(directory.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith("."):
            continue
        for path in sorted(subdir.glob("*.md")):
            if path.name.startswith("."):
                continue
            relative = f"{subdir.name}/{path.name}"
            files.append({
                "name": path.stem,
                "filename": relative,
                "subdirectory": subdir.name,
            })
    return files


def _read_markdown_file(directory: Path, name: str) -> str:
    """Read a markdown file from a directory.

    Supports both top-level files ("BASICS.md") and subdirectory files
    ("profiles/EXAMPLES.md"). Path traversal is prevented by checking
    that the resolved path stays within the base directory.

    Args:
        directory: Base directory
        name: Filename or relative path (with or without .md extension).
              May contain a single forward slash for subdirectory access.

    Returns:
        File contents as string

    Raises:
        FileNotFoundError: If the file doesn't exist
        ValueError: If the name attempts path traversal
    """
    # Sanitize: reject path traversal patterns
    if ".." in name or "\\" in name:
        raise ValueError(f"Invalid file name: {name}")

    # Allow at most one forward slash (subdirectory/filename)
    parts = name.split("/")
    if len(parts) > 2:
        raise ValueError(f"Invalid file name: {name}")

    # Add .md extension if not present
    filename = name if name.endswith(".md") else f"{name}.md"
    file_path = directory / filename

    # Verify the resolved path is still inside the directory
    resolved = file_path.resolve()
    base_dir = directory.resolve()
    try:
        resolved.relative_to(base_dir)
    except ValueError:
        raise ValueError(f"Path traversal detected: {name}")

    if not file_path.is_file():
        raise FileNotFoundError(
            f"File not found: {filename}. "
            f"Available files: {', '.join(f['filename'] for f in _list_markdown_files(directory))}"
        )

    return file_path.read_text(encoding="utf-8")


def register_resources(mcp: FastMCP, config: GaggimateConfig) -> None:
    """Register all MCP resource endpoints.

    Args:
        mcp: FastMCP server instance
        config: Gaggimate configuration
    """
    knowledge_dir = Path(config.knowledge_dir)
    coffees_dir = Path(config.coffees_dir)
    user_dir = Path(config.user_dir)

    # ── Knowledge resources ──────────────────────────────────────────

    @mcp.resource(
        "gaggimate://knowledge",
        name="knowledge_index",
        description=(
            "List all available espresso knowledge files. "
            "Returns file names that can be used with gaggimate://knowledge/{name}."
        ),
        mime_type="text/plain",
    )
    def list_knowledge() -> str:
        """List available knowledge files."""
        files = _list_markdown_files(knowledge_dir)
        if not files:
            return "No knowledge files found."

        lines = ["# Available Knowledge Files", ""]
        current_subdir = None
        for f in files:
            subdir = f.get("subdirectory")
            if subdir != current_subdir:
                if subdir is not None:
                    lines.append(f"\n## {subdir}/")
                current_subdir = subdir
            lines.append(f"- **{f['name']}** → `gaggimate://knowledge/{f['filename']}`")

        return "\n".join(lines)

    @mcp.resource(
        "gaggimate://knowledge/{filename}",
        name="knowledge_file",
        description=(
            "Read a specific espresso knowledge file by name. "
            "Use gaggimate://knowledge to see available files."
        ),
        mime_type="text/markdown",
    )
    def read_knowledge(filename: str) -> str:
        """Read a specific knowledge file."""
        return _read_markdown_file(knowledge_dir, filename)

    @mcp.resource(
        "gaggimate://knowledge/{subdir}/{filename}",
        name="knowledge_subdir_file",
        description=(
            "Read a knowledge file from a subdirectory. "
            "Use gaggimate://knowledge to see available files."
        ),
        mime_type="text/markdown",
    )
    def read_knowledge_subdir(subdir: str, filename: str) -> str:
        """Read a knowledge file from a subdirectory."""
        return _read_markdown_file(knowledge_dir, f"{subdir}/{filename}")

    # ── Coffee resources ─────────────────────────────────────────────

    @mcp.resource(
        "gaggimate://coffees",
        name="coffees_index",
        description=(
            "List all coffee tracking files. "
            "Returns file names that can be used with gaggimate://coffees/{name}."
        ),
        mime_type="text/plain",
    )
    def list_coffees() -> str:
        """List available coffee tracking files."""
        files = _list_markdown_files(coffees_dir)
        if not files:
            return "No coffee files found. Use the manage_coffee tool to create one."

        lines = ["# Coffee Files", ""]
        for f in files:
            lines.append(f"- **{f['name']}** → `gaggimate://coffees/{f['filename']}`")

        return "\n".join(lines)

    @mcp.resource(
        "gaggimate://coffees/{filename}",
        name="coffee_file",
        description=(
            "Read a specific coffee tracking file by name. "
            "Use gaggimate://coffees to see available files."
        ),
        mime_type="text/markdown",
    )
    def read_coffee(filename: str) -> str:
        """Read a specific coffee file."""
        return _read_markdown_file(coffees_dir, filename)

    # ── User resources ───────────────────────────────────────────────

    @mcp.resource(
        "gaggimate://user/setup",
        name="user_setup",
        description=(
            "Read the user's equipment, preferences, and workflow setup. "
            "Returns the user-setup.md file contents, or guidance to create one."
        ),
        mime_type="text/markdown",
    )
    def read_user_setup() -> str:
        """Read user setup file."""
        setup_path = user_dir / "user-setup.md"
        if not setup_path.is_file():
            example_path = user_dir / "user-setup.example.md"
            if example_path.is_file():
                return (
                    "# User setup not configured\n\n"
                    "No `user/user-setup.md` found. Use the `manage_user_setup` tool "
                    "to create one, or copy `user/user-setup.example.md` manually.\n\n"
                    "---\n\n"
                    "## Example Template\n\n"
                    + example_path.read_text(encoding="utf-8")
                )
            return (
                "# User setup not configured\n\n"
                "No user setup file found. Use the `manage_user_setup` tool to create one."
            )

        return setup_path.read_text(encoding="utf-8")

    @mcp.resource(
        "gaggimate://user/grind-map",
        name="user_grind_map",
        description=(
            "Read the user's grind map — a record of successful grind settings. "
            "Returns the grind-map.md file contents, or guidance to create one."
        ),
        mime_type="text/markdown",
    )
    def read_grind_map() -> str:
        """Read grind map file."""
        grind_path = user_dir / "grind-map.md"
        if not grind_path.is_file():
            example_path = user_dir / "grind-map.example.md"
            if example_path.is_file():
                return (
                    "# Grind map not configured\n\n"
                    "No `user/grind-map.md` found. Use the `manage_grind_map` tool "
                    "to create one, or copy `user/grind-map.example.md` manually.\n\n"
                    "---\n\n"
                    "## Example Template\n\n"
                    + example_path.read_text(encoding="utf-8")
                )
            return (
                "# Grind map not configured\n\n"
                "No grind map file found. Use the `manage_grind_map` tool to create one."
            )

        return grind_path.read_text(encoding="utf-8")

    @mcp.resource(
        "gaggimate://user/brewing-insights",
        name="user_brewing_insights",
        description=(
            "Read the user's brewing insights — cross-coffee patterns and learnings. "
            "Returns the brewing-insights.md file contents, or guidance to create one."
        ),
        mime_type="text/markdown",
    )
    def read_brewing_insights() -> str:
        """Read brewing insights file."""
        insights_path = user_dir / "brewing-insights.md"
        if not insights_path.is_file():
            return (
                "# Brewing insights not yet started\n\n"
                "No `user/brewing-insights.md` found. Use the `manage_brewing_insights` "
                "tool with action='init' to create one.\n\n"
                "This file will capture cross-coffee patterns and learnings — "
                "e.g. which origin/process/roast combinations respond to which profiles."
            )

        return insights_path.read_text(encoding="utf-8")

    try:
        logger.info(
            "resources_registered",
            knowledge_dir=str(knowledge_dir),
            coffees_dir=str(coffees_dir),
            user_dir=str(user_dir),
        )
    except ValueError:
        # Structlog may fail if stdout is closed (e.g. during testing)
        pass
