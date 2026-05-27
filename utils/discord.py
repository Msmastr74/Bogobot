def count_characters(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2
