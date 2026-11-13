BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_INDEX = {char: index for index, char in enumerate(BASE58_ALPHABET)}


def decode_base58(value: str) -> bytes:
    if not value or any(char not in BASE58_INDEX for char in value):
        raise ValueError("Invalid base58 value")
    number = 0
    for char in value:
        number = number * 58 + BASE58_INDEX[char]
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeroes + number.to_bytes((number.bit_length() + 7) // 8, "big")
