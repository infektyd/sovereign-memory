"""
Sovereign Memory V3.1 — Markdown-Aware Chunker.

V3 split text by word count with fixed overlap — no awareness of document
structure. A heading and its content could be split across chunks, losing
the semantic relationship between them.

V3.1 chunks by markdown structure:
1. Split on headings (##, ###, etc.) — each section is a natural unit
2. Keep heading context attached to every chunk from that section
3. Preserve code blocks and lists as atomic units (never split mid-block)
4. Fall back to sliding window only for sections that exceed chunk_size
5. Chunks carry their heading breadcrumb so retrieval knows WHERE in the
   document the chunk came from
"""

import re
from typing import List, Tuple
from dataclasses import dataclass

from sovereign_memory.core.config import SovereignConfig, DEFAULT_CONFIG


@dataclass
class Chunk:
    """A chunk of text with its heading context."""
    text: str
    heading: str           # The nearest heading above this chunk
    heading_path: str      # Full breadcrumb: "# Title > ## Section > ### Subsection"
    chunk_index: int       # Position within the document


class MarkdownChunker:
    """
    Markdown-aware document chunker.

    Strategy:
    1. Parse document into sections by headings
    2. Each section becomes one or more chunks
    3. Code blocks and list blocks are kept atomic
    4. Heading breadcrumbs are attached to every chunk
    """

    def __init__(self, config: SovereignConfig = DEFAULT_CONFIG):
        self.chunk_size = config.chunk_size
        self.chunk_overlap = config.chunk_overlap
        self.strategy = config.chunk_strategy

    def chunk_document(self, text: str) -> List[Chunk]:
        """
        Split a markdown document into chunks.

        Returns list of Chunk objects with heading context.
        """
        if self.strategy == "sliding":
            return self._sliding_window(text)

        # Markdown-aware chunking
        sections = self._split_sections(text)
        chunks = []
        idx = 0

        for heading_path, heading, body in sections:
            if not body.strip():
                continue

            # Try to keep each section as one chunk
            words = body.split()
            if len(words) <= self.chunk_size:
                chunks.append(Chunk(
                    text=body.strip(),
                    heading=heading,
                    heading_path=heading_path,
                    chunk_index=idx,
                ))
                idx += 1
            else:
                # Section too large — split by atomic blocks, then sliding window
                blocks = self._split_atomic_blocks(body)
                for block in blocks:
                    block_words = block.split()
                    if len(block_words) <= self.chunk_size:
                        chunks.append(Chunk(
                            text=block.strip(),
                            heading=heading,
                            heading_path=heading_path,
                            chunk_index=idx,
                        ))
                        idx += 1
                    else:
                        # Atomic block still too big — sliding window
                        sub_chunks = self._sliding_window_raw(
                            block_words, heading, heading_path, idx
                        )
                        chunks.extend(sub_chunks)
                        idx += len(sub_chunks)

        # Fallback: if no sections found (no headings), use sliding window
        if not chunks:
            return self._sliding_window(text)

        return chunks

    def _split_sections(self, text: str) -> List[Tuple[str, str, str]]:
        """
        Split markdown into sections by headings.

        Returns: [(heading_path, heading_text, body_text), ...]
        """
        # Match markdown headings: # Title, ## Section, etc.
        heading_pattern = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)

        sections = []
        heading_stack = []  # [(level, text), ...]
        last_end = 0
        last_heading = ""
        last_path = ""

        for match in heading_pattern.finditer(text):
            # Save body before this heading
            body = text[last_end:match.start()]
            if last_end > 0 or body.strip():  # Skip empty preamble
                sections.append((last_path, last_heading, body))

            level = len(match.group(1))
            heading_text = match.group(2).strip()

            # Update heading stack (pop deeper/equal levels)
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, heading_text))

            # Build breadcrumb path
            last_path = " > ".join(h[1] for h in heading_stack)
            last_heading = heading_text
            last_end = match.end()

        # Final section after last heading
        body = text[last_end:]
        if body.strip():
            sections.append((last_path, last_heading, body))

        # If no headings found, return entire text as one section
        if not sections:
            sections = [("", "", text)]

        return sections

    def _split_atomic_blocks(self, text: str) -> List[str]:
        """
        Split text into atomic blocks that should not be broken:
        - Code blocks (``` ... ```)
        - Ordered/unordered list blocks (contiguous list items)
        - Regular paragraphs (split on double newline)

        Returns list of block strings.
        """
        blocks = []
        lines = text.split('\n')
        current_block = []
        in_code_block = False

        for line in lines:
            # Toggle code block
            if line.strip().startswith('```'):
                if in_code_block:
                    # End of code block
                    current_block.append(line)
                    blocks.append('\n'.join(current_block))
                    current_block = []
                    in_code_block = False
                else:
                    # Start of code block — flush current
                    if current_block:
                        blocks.append('\n'.join(current_block))
                        current_block = []
                    current_block.append(line)
                    in_code_block = True
                continue

            if in_code_block:
                current_block.append(line)
                continue

            # Blank line = paragraph break (if not in a list)
            if not line.strip():
                is_list = any(
                    l.strip().startswith(('-', '*', '+'))
                    or re.match(r'^\s*\d+\.', l)
                    for l in current_block
                )
                if current_block and not is_list:
                    blocks.append('\n'.join(current_block))
                    current_block = []
                elif current_block:
                    current_block.append(line)  # Keep blank line in list
                continue

            current_block.append(line)

        if current_block:
            blocks.append('\n'.join(current_block))

        return [b for b in blocks if b.strip()]

    def _sliding_window_raw(
        self,
        words: List[str],
        heading: str,
        heading_path: str,
        start_idx: int,
    ) -> List[Chunk]:
        """Sliding window over a word list, returning Chunk objects."""
        step = max(self.chunk_size - self.chunk_overlap, 1)
        chunks = []
        idx = start_idx

        for start in range(0, len(words), step):
            text = " ".join(words[start:start + self.chunk_size])
            if text.strip():
                chunks.append(Chunk(
                    text=text,
                    heading=heading,
                    heading_path=heading_path,
                    chunk_index=idx,
                ))
                idx += 1
            if start + self.chunk_size >= len(words):
                break

        return chunks

    def _sliding_window(self, text: str) -> List[Chunk]:
        """Fallback: V3-style sliding window with no heading context."""
        words = text.split()
        step = max(self.chunk_size - self.chunk_overlap, 1)
        chunks = []
        idx = 0

        for start in range(0, len(words), step):
            chunk_text = " ".join(words[start:start + self.chunk_size])
            if chunk_text.strip():
                chunks.append(Chunk(
                    text=chunk_text,
                    heading="",
                    heading_path="",
                    chunk_index=idx,
                ))
                idx += 1
            if start + self.chunk_size >= len(words):
                break

        return chunks if chunks else [Chunk(
            text=text[:2000], heading="", heading_path="", chunk_index=0
        )]
