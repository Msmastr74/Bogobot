def count_characters(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2

def chunk_text(text: str, max_len: int, *, max_chunks: int | None = None) -> list[str]:
    """Splits text into chunks of max_len, respecting line boundaries."""
    if max_len <= 0:
        raise ValueError("max_len must be greater than 0")
        
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_length = 0

    lines = text.splitlines(keepends=True)

    for line in lines:
        line_length = count_characters(line)
        # Case 1: The line itself is longer than max_len
        if line_length > max_len:
            if current_chunk:
                chunks.append("".join(current_chunk))
                if max_chunks is not None and len(chunks) >= max_chunks:
                    break
                current_chunk = []
                current_length = 0

            split_pieces = split_text_to_character_limit(line, max_len)
            chunks.extend(split_pieces[:-1])
            if split_pieces:
                current_chunk = [split_pieces[-1]]
                current_length = count_characters(split_pieces[-1])
                
        # Case 2: Line fits into the current chunk perfectly
        elif current_length + line_length <= max_len:
            current_chunk.append(line)
            current_length += line_length
            
        # Case 3: Line exceeds current chunk capacity (but is <= max_len)
        else:
            chunks.append("".join(current_chunk))
            if max_chunks is not None and len(chunks) >= max_chunks:
                break
            current_chunk = [line]
            current_length = line_length

    # Clean up any leftover text at the very end
    if current_chunk and (not max_chunks or len(chunks) < max_chunks):
        chunks.append("".join(current_chunk))

    return chunks

def split_text_to_character_limit(text: str, max_len: int, *, max_pieces: int | None = None) -> list[str]:
    pieces: list[str] = []
    current: list[str] = []
    current_length = 0

    for character in text:
        character_length = count_characters(character)
        if current and current_length + character_length > max_len:
            pieces.append("".join(current))
            if max_pieces is not None and len(pieces) >= max_pieces:
                break
            current = []
            current_length = 0
        current.append(character)
        current_length += character_length

    if current and (not max_pieces or len(pieces) < max_pieces):
        pieces.append("".join(current))
    return pieces
