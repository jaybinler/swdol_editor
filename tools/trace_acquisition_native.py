"""Reproduce acquisition instruction evidence without executing the game or compiled plot scripts."""

from pathlib import Path
from argparse import ArgumentParser

from xyj_content.binary import ContentError, read_bytes
from xyj_content.files import output_path, sha256, write_json
from xyj_content.plot_maps import NATIVE_SHA256


def main():
    """Pin the researched server version and disassemble only the acquisition-related native handlers."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('output/editor/verification-0.11/acquisition-native.json'))
    args = parser.parse_args()
    import capstone
    import pefile
    root = Path(__file__).resolve().parents[1]
    raw = read_bytes(root/'XYJ_SEVVER/qingfeng/NewLocalServer.exe')
    if sha256(raw) != NATIVE_SHA256:
        raise ContentError('服务端版本变化，不能复用原反汇编地址。')
    pe = pefile.PE(data=raw)
    decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    ranges = {'inventory_quantity_one':(0x45783b,0x4578dd), 'shop_open_555':(0x43ba50,0x43bb30),
              'shop_list_556':(0x43bb30,0x43bbf8), 'reward_900':(0x447300,0x44751c),
              'branch_108_to_113':(0x4371f0,0x437490), 'switch_11_12':(0x435f60,0x436060),
              'menu_402':(0x43a450,0x43a520), 'give_object_503':(0x43b700,0x43b880),
              'mission_items_806':(0x441600,0x441810), 'check_items_909':(0x4482d0,0x448430),
              'check_items_917':(0x448ea0,0x448fe3)}
    result = {'sha256':NATIVE_SHA256,'ranges':{}}
    for name, (start,end) in ranges.items():
        offset = pe.get_offset_from_rva(start-pe.OPTIONAL_HEADER.ImageBase)
        result['ranges'][name] = [f'{ins.address:x} {ins.mnemonic} {ins.op_str}'
                                 for ins in decoder.disasm(raw[offset:offset+end-start],start)]
    write_json(output_path(root,args.out),result)
    print('Native acquisition evidence saved; no game code executed.')


if __name__ == '__main__':
    main()
