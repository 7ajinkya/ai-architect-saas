import re
from pathlib import Path
from typing import Iterator, Optional

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter


class MarkdownHeading:
    def __init__(self, level: int, text: str, start_pos: int):
        self.level = level
        self.text = text.strip()
        self.start_pos = start_pos
        self.end_pos: Optional[int] = None

    def __repr__(self):
        return f"H{self.level}({self.text}, {self.start_pos}-{self.end_pos})"


class CodeFence:
    def __init__(self, language: str, content: str, start_pos: int, end_pos: int):
        self.language = language
        self.content = content
        self.start_pos = start_pos
        self.end_pos = end_pos

    def __repr__(self):
        return f"CodeFence({self.language}, {self.start_pos}-{self.end_pos})"


def parse_markdown_headings(content: str) -> list[MarkdownHeading]:
    """Parse all markdown headings from content."""
    headings = []
    # Match headings: # Heading, ## Heading, etc.
    pattern = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
    for match in pattern.finditer(content):
        level = len(match.group(1))
        text = match.group(2).strip()
        start_pos = match.start()
        headings.append(MarkdownHeading(level, text, start_pos))

    # Set end positions
    for i, heading in enumerate(headings):
        if i + 1 < len(headings):
            heading.end_pos = headings[i + 1].start_pos
        else:
            heading.end_pos = len(content)

    return headings


def get_section_path(headings: list[MarkdownHeading], position: int) -> str:
    """Get the section path (heading hierarchy) for a given position in the document."""
    # Find all headings that start before or at this position
    active_headings = [h for h in headings if h.start_pos <= position]
    if not active_headings:
        return ""

    # Build the path from root to current
    path_parts = []
    current_level = 0
    for h in active_headings:
        if h.level > current_level:
            path_parts.append(h.text)
            current_level = h.level
        elif h.level == current_level and path_parts:
            path_parts[-1] = h.text
        elif h.level < current_level:
            # Pop back to the right level
            while len(path_parts) >= h.level:
                path_parts.pop()
            if path_parts:
                path_parts[-1] = h.text
            else:
                path_parts.append(h.text)
            current_level = h.level

    return " > ".join(path_parts)


def extract_code_fences(content: str) -> tuple[str, list[CodeFence]]:
    """
    Extract code fences from markdown content.
    Returns (content_without_code_fences, list_of_code_fences).
    """
    code_fences = []
    # Match ```language\ncontent\n``` or ~~~language\ncontent\n~~~
    pattern = re.compile(r'(```|~~~)(\w*)\n(.*?)\n\1', re.DOTALL)

    result_parts = []
    last_end = 0

    for match in pattern.finditer(content):
        fence_start = match.start()
        fence_end = match.end()
        language = match.group(2).strip() if match.group(2) else ""
        code_content = match.group(3).strip()

        # Add text before the code fence
        if fence_start > last_end:
            result_parts.append(content[last_end:fence_start])

        # Record the code fence
        code_fence = CodeFence(language, code_content, fence_start, fence_end)
        code_fences.append(code_fence)

        # Replace code fence with a placeholder
        placeholder = f"\n[CODE_FENCE_{len(code_fences)-1}]\n"
        result_parts.append(placeholder)

        last_end = fence_end

    # Add remaining text
    if last_end < len(content):
        result_parts.append(content[last_end:])

    return "".join(result_parts), code_fences


def restore_code_fences(content: str, code_fences: list[CodeFence]) -> str:
    """Restore code fences from placeholders."""
    for i, fence in enumerate(code_fences):
        placeholder = f"[CODE_FENCE_{i}]"
        fence_text = f"```{fence.language}\n{fence.content}\n```"
        content = content.replace(placeholder, fence_text)
    return content


class HeadingAwareMarkdownSplitter:
    """
    Splits markdown documents while preserving heading context.
    """

    def __init__(
        self,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        min_chunk_size: int = 100,
        preserve_code_fences: bool = True,
        strip_frontmatter: bool = False,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_chunk_size = min_chunk_size
        self.preserve_code_fences = preserve_code_fences
        self.strip_frontmatter = strip_frontmatter

        # Fallback splitter for when heading-aware splitting produces chunks that are too large
        self.fallback_splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", " ", ""],
        )

    def split_text(self, text: str, metadata: dict | None = None) -> list[Document]:
        """Split markdown text into documents with heading context."""
        if metadata is None:
            metadata = {}

        # Strip frontmatter if requested
        frontmatter = ""
        if self.strip_frontmatter:
            text, frontmatter = self._strip_frontmatter(text)

        # Extract code fences if preserving them
        code_fences = []
        if self.preserve_code_fences:
            text, code_fences = extract_code_fences(text)

        # Parse headings
        headings = parse_markdown_headings(text)

        if not headings:
            # No headings found, use fallback splitter
            chunks = self.fallback_splitter.split_text(text)
            docs = []
            for i, chunk in enumerate(chunks):
                if len(chunk.strip()) >= self.min_chunk_size:
                    doc_metadata = metadata.copy()
                    doc_metadata.update({
                        "section_path": "",
                        "has_code_fences": False,
                    })
                    if self.preserve_code_fences and code_fences:
                        chunk = restore_code_fences(chunk, code_fences)
                    docs.append(Document(page_content=chunk, metadata=doc_metadata))
            return docs

        # Split by heading sections
        sections = self._split_by_headings(text, headings)

        # Process each section, combining small ones, splitting large ones
        chunks = []
        current_chunk = ""
        current_section_path = ""

        for section_text, section_path in sections:
            # If adding this section would exceed chunk_size, finalize current chunk
            if current_chunk and len(current_chunk) + len(section_text) > self.chunk_size:
                if len(current_chunk.strip()) >= self.min_chunk_size:
                    chunks.append((current_chunk.strip(), current_section_path))
                current_chunk = section_text
                current_section_path = section_path
            else:
                if current_chunk:
                    current_chunk += "\n\n" + section_text
                else:
                    current_chunk = section_text
                    current_section_path = section_path

        # Don't forget the last chunk
        if current_chunk and len(current_chunk.strip()) >= self.min_chunk_size:
            chunks.append((current_chunk.strip(), current_section_path))

        # Handle oversized chunks with fallback splitter
        final_chunks = []
        for chunk_text, section_path in chunks:
            if len(chunk_text) > self.chunk_size * 1.5:
                # Too large, split with fallback
                sub_chunks = self.fallback_splitter.split_text(chunk_text)
                for sub_chunk in sub_chunks:
                    if len(sub_chunk.strip()) >= self.min_chunk_size:
                        final_chunks.append((sub_chunk.strip(), section_path))
            else:
                final_chunks.append((chunk_text, section_path))

        # Create documents
        docs = []
        for i, (chunk_text, section_path) in enumerate(final_chunks):
            doc_metadata = metadata.copy()
            doc_metadata.update({
                "section_path": section_path,
                "has_code_fences": len(code_fences) > 0,
            })
            if self.preserve_code_fences and code_fences:
                chunk_text = restore_code_fences(chunk_text, code_fences)
            if frontmatter:
                chunk_text = frontmatter + "\n" + chunk_text
            docs.append(Document(page_content=chunk_text, metadata=doc_metadata))

        return docs

    def _strip_frontmatter(self, text: str) -> tuple[str, str]:
        """Strip YAML frontmatter from markdown content."""
        if text.startswith("---"):
            end_idx = text.find("\n---", 3)
            if end_idx != -1:
                frontmatter = text[:end_idx + 4]
                content = text[end_idx + 4:].lstrip()
                return content, frontmatter
        return text, ""

    def _split_by_headings(self, text: str, headings: list[MarkdownHeading]) -> list[tuple[str, str]]:
        """Split text into sections based on headings."""
        sections = []

        for i, heading in enumerate(headings):
            start = heading.start_pos
            end = heading.end_pos if heading.end_pos else len(text)
            section_text = text[start:end].strip()

            if section_text:
                # Get section path up to this heading
                section_path = get_section_path(headings[:i+1], heading.start_pos)
                sections.append((section_text, section_path))

        return sections


def create_markdown_splitter(
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
    heading_aware: bool = True,
    preserve_code_fences: bool = False,
    strip_frontmatter: bool = False,
) -> HeadingAwareMarkdownSplitter | RecursiveCharacterTextSplitter:
    """Factory function to create the appropriate splitter."""
    if heading_aware:
        return HeadingAwareMarkdownSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            preserve_code_fences=preserve_code_fences,
            strip_frontmatter=strip_frontmatter,
        )
    else:
        return RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", " ", ""],
        )