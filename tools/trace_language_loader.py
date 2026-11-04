"""Reproduce static evidence for the researched client; never execute game code."""

from pathlib import Path
import argparse
import hashlib
import json


def main() -> int:
    """Disassemble known loader ranges into a research artifact, requiring the exact client hash."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, default=Path("output/loader-evidence"))
    args = parser.parse_args()
    from xyj_content.files import output_path, write_json
    from xyj_content.binary import ContentError, read_bytes
    try:
        import capstone
        import pefile
        root = args.root.resolve()
        blob = read_bytes(root / "swdol/Main/SWDOL.exe")
        baseline = json.loads((root / "research/2026-10-28/inventory_sha256.json").read_text(encoding="utf-8"))
        digest = next(x["sha256"] for x in baseline["files"] if x["path"] == "swdol/Main/SWDOL.exe")
        if hashlib.sha256(blob).hexdigest() != digest:
            raise ContentError("客户端版本已变化，不能复用旧虚拟地址。")
        pe = pefile.PE(data=blob)
        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        ranges = {"transform": (0x468B20, 0x468BFA), "loader": (0x468DD0, 0x4690A8)}
        evidence = {"source": "swdol/Main/SWDOL.exe", "sha256": digest, "ranges": {}}
        for name, (start, end) in ranges.items():
            offset = pe.get_offset_from_rva(start - pe.OPTIONAL_HEADER.ImageBase)
            evidence["ranges"][name] = [
                {"va": hex(ins.address), "bytes": ins.bytes.hex(), "instruction": f"{ins.mnemonic} {ins.op_str}"}
                for ins in decoder.disasm(blob[offset:offset + end - start], start)
            ]
        write_json(output_path(root, args.out) / "client_language_loader.json", evidence)
        print("静态反汇编证据已生成；未执行客户端。")
        return 0
    except (ImportError, OSError, ValueError, StopIteration) as exc:
        print(f"无法生成证据：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
