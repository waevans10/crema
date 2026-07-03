"""Markdown file storage helpers for user data.

Provides read/write operations for markdown files managed by MCP tools:
- User setup (user/user-setup.md)
- Coffee tracking files (coffees/*.md)
- Grind map (user/grind-map.md)
- Brewing insights (user/brewing-insights.md)
"""

from pathlib import Path
from datetime import datetime

from gaggimate_mcp.logging_config import get_logger

logger = get_logger(__name__)


def _sanitize_filename(name: str) -> str:
    """Convert a display name to a safe filename.

    Args:
        name: Human-readable name (e.g. "Jack LeFleur — Azul")

    Returns:
        Safe filename without extension (e.g. "jack-lefleur-azul")
    """
    # Lowercase and replace common separators
    safe = name.lower()
    # Replace em-dash, en-dash, and other separators with hyphen
    for char in ["—", "–", ":", "|"]:
        safe = safe.replace(char, "-")
    # Replace spaces and underscores with hyphens
    safe = safe.replace(" ", "-").replace("_", "-")
    # Keep only alphanumeric and hyphens
    safe = "".join(c for c in safe if c.isalnum() or c == "-")
    # Collapse multiple hyphens and strip leading/trailing
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")


class MarkdownStorage:
    """Read/write markdown files in a directory."""

    def __init__(self, directory: Path):
        """Initialize storage for a directory.

        Args:
            directory: Path to the directory
        """
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, filename: str) -> Path:
        """Resolve a filename to a safe path within the storage directory.

        Args:
            filename: Filename with or without .md extension

        Returns:
            Resolved path within the storage directory

        Raises:
            ValueError: If the filename would escape the storage directory
        """
        if not filename.endswith(".md"):
            filename = f"{filename}.md"

        if ".." in filename or filename.startswith("/") or "\\" in filename:
            raise ValueError(f"Invalid filename: {filename}")

        path = (self.directory / filename).resolve()
        base = self.directory.resolve()
        try:
            path.relative_to(base)
        except ValueError:
            raise ValueError(f"Path traversal detected: {filename}")

        return path

    def read(self, filename: str) -> str | None:
        """Read a markdown file.

        Args:
            filename: Filename with or without .md extension

        Returns:
            File contents, or None if file doesn't exist
        """
        path = self._resolve_path(filename)
        if not path.is_file():
            return None

        return path.read_text(encoding="utf-8")

    def write(self, filename: str, content: str) -> Path:
        """Write content to a markdown file (create or overwrite).

        Args:
            filename: Filename with or without .md extension
            content: Full markdown content to write

        Returns:
            Path to the written file
        """
        path = self._resolve_path(filename)
        path.write_text(content, encoding="utf-8")
        return path

    def exists(self, filename: str) -> bool:
        """Check if a markdown file exists.

        Args:
            filename: Filename with or without .md extension

        Returns:
            True if file exists
        """
        try:
            return self._resolve_path(filename).is_file()
        except ValueError:
            return False

    def delete(self, filename: str) -> bool:
        """Delete a markdown file.

        Args:
            filename: Filename with or without .md extension

        Returns:
            True if file was deleted, False if it didn't exist
        """
        path = self._resolve_path(filename)
        if path.is_file():
            path.unlink()
            return True
        return False

    def list_files(self) -> list[str]:
        """List all markdown files in the directory.

        Returns:
            List of filenames (without .md extension)
        """
        if not self.directory.is_dir():
            return []

        return sorted(
            p.stem for p in self.directory.glob("*.md")
            if not p.name.startswith(".")
        )


def build_coffee_markdown(
    *,
    coffee_name: str,
    roaster: str,
    origin: str = "",
    process: str = "",
    variety: str = "",
    roast_level: str = "",
    roast_date: str = "",
    bag_size: str = "",
    roaster_notes: str = "",
    freshness_note: str = "",
    approach: str = "",
) -> str:
    """Build a coffee tracking markdown file.

    The coffee file captures bean identity, the brewing approach (narrative),
    a brewing journal (dated analysis entries), and key insights. Raw shot
    numbers live on the device — this file stores *thinking* and *learnings*.

    Args:
        coffee_name: Full coffee name
        roaster: Roaster name
        origin: Country/region of origin
        process: Processing method
        variety: Coffee variety
        roast_level: Roast level
        roast_date: Roast date string
        bag_size: Bag size
        roaster_notes: Tasting notes from roaster
        freshness_note: Freshness/peak window note
        approach: Narrative description of the brewing approach — profile
            choice, pressure logic, starting parameters, and reasoning.
            Written by the agent after researching the coffee.

    Returns:
        Complete markdown string
    """
    lines = [f"# {roaster} — {coffee_name}", ""]

    # Bean Profile table
    lines.append("## Bean Profile")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    lines.append(f"| **Roaster** | {roaster} |")
    lines.append(f"| **Coffee name** | {coffee_name} |")
    if origin:
        lines.append(f"| **Origin** | {origin} |")
    if variety:
        lines.append(f"| **Variety** | {variety} |")
    if process:
        lines.append(f"| **Process** | {process} |")
    if roast_level:
        lines.append(f"| **Roast level** | {roast_level} |")
    if roast_date:
        lines.append(f"| **Roast date** | {roast_date} |")
    if bag_size:
        lines.append(f"| **Bag size** | {bag_size} |")
    if roaster_notes:
        lines.append(f"| **Roaster notes** | {roaster_notes} |")

    if freshness_note:
        lines.append("")
        lines.append(f"**Freshness note:** {freshness_note}")

    # Approach (narrative)
    if approach:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## Approach")
        lines.append("")
        lines.append(approach)

    # Brewing Journal (empty starter)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Brewing Journal")
    lines.append("")
    lines.append("*No entries yet. Use the feedback skill after pulling a shot.*")

    # Key Insights (empty starter)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Key Insights")
    lines.append("")
    lines.append("*No insights yet. Patterns will emerge as you dial in.*")

    lines.append("")
    return "\n".join(lines)


def append_journal_entry(
    content: str,
    *,
    date: str = "",
    headline: str = "",
    body: str = "",
) -> str:
    """Append a brewing journal entry to a coffee markdown file.

    Finds the Brewing Journal section and appends a dated entry.
    Each entry has a headline (date, grind, profile, rating) and a
    freeform body with the agent's analysis and next-step thinking.

    Args:
        content: Existing file content
        date: Entry date (defaults to today)
        headline: Entry headline — e.g. "Grind 10, Lever Decline [AI] — 4/5"
        body: Freeform analysis — what worked, what didn't, what to try next

    Returns:
        Updated file content
    """
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    entry_lines = [f"### {date} — {headline}" if headline else f"### {date}"]
    if body:
        entry_lines.append(body)
    entry_lines.append("")  # Blank line after entry

    lines = content.split("\n")
    insert_idx = None

    for i, line in enumerate(lines):
        if "## Brewing Journal" in line:
            # Remove placeholder text if present
            for k in range(i + 1, len(lines)):
                if lines[k].strip().startswith("*No entries yet"):
                    lines[k] = ""
                    break

            # Find end of Brewing Journal section: stop before next ## or ---
            # that precedes a ## heading (section separator)
            j = i + 1
            while j < len(lines):
                stripped = lines[j].strip()
                # Stop at next top-level section heading
                if stripped.startswith("## ") and stripped != "## Brewing Journal":
                    break
                # Stop at a --- separator if followed (after blanks) by a ## heading
                if stripped == "---":
                    peek = j + 1
                    while peek < len(lines) and lines[peek].strip() == "":
                        peek += 1
                    if peek < len(lines) and lines[peek].strip().startswith("## "):
                        break
                j += 1
            insert_idx = j
            break

    if insert_idx is not None:
        for offset, entry_line in enumerate(entry_lines):
            lines.insert(insert_idx + offset, entry_line)
    else:
        # No Brewing Journal section — append one
        lines.append("")
        lines.append("## Brewing Journal")
        lines.append("")
        lines.extend(entry_lines)

    return "\n".join(lines)


def build_grind_map_markdown() -> str:
    """Build the initial grind map markdown file.

    Returns:
        Empty grind map template
    """
    return """# Grind Map

A personal record of successful grind settings that grows from your shots.

## Successful Settings

| Coffee | Roast | Process | Origin | Days Off Roast | Grind | Profile | Ratio | Temp | Rating | Date |
|--------|-------|---------|--------|----------------|-------|---------|-------|------|--------|------|

*Days Off Roast is optional — use "—" when roast date is unknown.*
*Grind notation: use your grinder's format.*

---

*This file is automatically updated when you rate shots 4–5 stars with grind settings provided.*
"""


def append_grind_entry(
    content: str,
    *,
    coffee: str = "",
    roast: str = "",
    process: str = "",
    origin: str = "",
    days_off_roast: str = "—",
    grind: str = "",
    profile: str = "",
    ratio: str = "",
    temp: str = "",
    rating: str = "",
    date: str = "",
) -> str:
    """Append a row to the grind map's Successful Settings table.

    Args:
        content: Existing grind map content
        coffee: Coffee name
        roast: Roast level
        process: Processing method
        origin: Origin country
        days_off_roast: Days since roast
        grind: Grind setting
        profile: Profile used
        ratio: Brew ratio
        temp: Temperature
        rating: Rating
        date: Date of shot

    Returns:
        Updated grind map content
    """
    if not date:
        date = datetime.now().strftime("%b %d")

    # Escape pipes and normalize newlines in all fields to prevent table corruption
    def _sanitize_cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ").strip()

    row = (
        f"| {_sanitize_cell(coffee)} | {_sanitize_cell(roast)} | {_sanitize_cell(process)} "
        f"| {_sanitize_cell(origin)} | {_sanitize_cell(days_off_roast)} "
        f"| {_sanitize_cell(grind)} | {_sanitize_cell(profile)} | {_sanitize_cell(ratio)} "
        f"| {_sanitize_cell(temp)} | {_sanitize_cell(rating)} | {_sanitize_cell(date)} |"
    )

    lines = content.split("\n")
    insert_idx = None

    for i, line in enumerate(lines):
        if line.startswith("|---") and "Grind" in lines[i - 1] if i > 0 else False:
            insert_idx = i + 1
            # Skip existing rows
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("|"):
                    insert_idx = j + 1
                else:
                    break
            break

    if insert_idx is not None:
        lines.insert(insert_idx, row)
    else:
        # Fallback: append table at end
        lines.append("")
        lines.append("## Successful Settings")
        lines.append("")
        lines.append(
            "| Coffee | Roast | Process | Origin | Days Off Roast "
            "| Grind | Profile | Ratio | Temp | Rating | Date |"
        )
        lines.append(
            "|--------|-------|---------|--------|----------------"
            "|-------|---------|-------|------|--------|------|"
        )
        lines.append(row)

    return "\n".join(lines)

def build_brewing_insights_markdown() -> str:
    """Build the initial brewing insights markdown file.

    Returns:
        Empty brewing insights template
    """
    return """# Brewing Insights

Accumulated patterns and learnings across coffees. Review this when
dialing in a new coffee to leverage past experience.

## By Origin & Process

*No entries yet. Patterns will emerge as you brew more coffees.*

## By Profile Style

*No entries yet.*

## General Patterns

*No entries yet.*

---

*This file captures cross-coffee learnings. Each insight links to the
specific coffee file for full details.*
"""