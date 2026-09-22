"""Minimal ELF symbol-table reader (32/64-bit, either endianness).

Used to read layout probes from *target* objects without running target code:
each probe is an array whose size encodes a value (``sizeof``, ``offsetof``,
alignment), so the symbol size in the object's symbol table carries the
answer.  Non-ELF objects return ``None`` (the probe is then unavailable, not
passed).
"""

from __future__ import annotations

import struct
from pathlib import Path


def symbol_sizes(path: str | Path, prefix: str = "") -> dict[str, int] | None:
    data = Path(path).read_bytes()
    if len(data) < 52 or data[:4] != b"\x7fELF":
        return None
    cls, enc = data[4], data[5]
    if cls not in (1, 2) or enc not in (1, 2):
        return None
    e = "<" if enc == 1 else ">"
    if cls == 1:
        (e_shoff,) = struct.unpack_from(e + "I", data, 0x20)
        e_shentsize, e_shnum, _ = struct.unpack_from(e + "HHH", data, 0x2E)
    else:
        (e_shoff,) = struct.unpack_from(e + "Q", data, 0x28)
        e_shentsize, e_shnum, _ = struct.unpack_from(e + "HHH", data, 0x3A)
    if e_shoff == 0 or e_shnum == 0:
        return None

    def section(i: int) -> tuple[int, int, int, int, int]:
        off = e_shoff + i * e_shentsize
        if cls == 1:
            _name, sh_type, _flags, _addr, sh_offset, sh_size, sh_link, _info, _align, sh_entsize = struct.unpack_from(
                e + "10I", data, off
            )
        else:
            _name, sh_type, _flags, _addr, sh_offset, sh_size, sh_link, _info, _align, sh_entsize = struct.unpack_from(
                e + "IIQQQQIIQQ", data, off
            )
        return sh_type, sh_offset, sh_size, sh_link, sh_entsize

    out: dict[str, int] = {}
    for i in range(e_shnum):
        sh_type, off, size, link, entsize = section(i)
        if sh_type != 2:  # SHT_SYMTAB
            continue
        _, str_off, str_size, _, _ = section(link)
        strtab = data[str_off : str_off + str_size]
        ent = entsize or (16 if cls == 1 else 24)
        for j in range(size // ent):
            so = off + j * ent
            if cls == 1:
                st_name, _value, st_size, _info, _other, _shndx = struct.unpack_from(e + "IIIBBH", data, so)
            else:
                st_name, _info, _other, _shndx, _value, st_size = struct.unpack_from(e + "IBBHQQ", data, so)
            end = strtab.find(b"\0", st_name)
            name = strtab[st_name:end].decode(errors="replace")
            if name.startswith(prefix):
                out[name] = st_size
    return out
