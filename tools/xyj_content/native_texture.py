"""Bounded native RGB555 texture encoding; previews use the exact encoded game pixels."""

import math
import struct
import zlib

from .binary import ContentError,compress,decompress
from .models import Reader,image_png


def rgba(blob):
    """Decode the already-supported native run stream to exact opaque/transparent RGBA pixels."""
    if len(blob)<8:raise ContentError('贴图封装截断。')
    size,packed=struct.unpack_from('<II',blob)
    if packed!=len(blob)-8:raise ContentError('贴图压缩长度不符。')
    png=image_png(decompress(blob[8:],size))
    width,height=struct.unpack_from('>II',png,16)
    # image_png emits one bounded IDAT and filter-zero RGBA rows.
    length=struct.unpack_from('>I',png,33)[0]
    rows=zlib.decompress(png[41:41+length])
    return width,height,b''.join(rows[i*(width*4+1)+1:(i+1)*(width*4+1)] for i in range(height))


def validate_pixels(width,height,pixels):
    """Reject image size/byte mismatches before encoding or allocating native rows."""
    if type(width) is not int or type(height) is not int or not 1<=width<=2048 or not 1<=height<=2048 or len(pixels)!=width*height*4:
        raise ContentError('贴图须为不超过 2048×2048 的完整 RGBA 图像。')


def encode(width,height,pixels,tint=(1,1,1)):
    """Quantize RGB555 with binary alpha; transparent runs and row lengths follow the native reader."""
    validate_pixels(width,height,pixels)
    if len(tint)!=3 or any(type(x) not in (float,int) or not math.isfinite(x) or not 0<=x<=4 for x in tint):raise ContentError('贴图颜色比例须为 0 至 4。')
    out=bytearray(struct.pack('<4H',65535,width,height,16))
    for y in range(height):
        row=bytearray();x=0
        while x<width:
            opaque=pixels[(y*width+x)*4+3]>=128;end=x+1
            while end<width and (pixels[(y*width+end)*4+3]>=128)==opaque:end+=1
            row.extend(struct.pack('<H',(end-x) if opaque else 0xc000|(end-x)))
            if opaque:
                for index in range(x,end):
                    rgb=[min(31,max(0,round(pixels[(y*width+index)*4+c]*tint[c]*31/255))) for c in range(3)]
                    row.extend(struct.pack('<H',(rgb[0]<<10)|(rgb[1]<<5)|rgb[2]))
            x=end
        row.extend(b'\0\0');out.extend(struct.pack('<H',len(row)+2));out.extend(row)
    packed=compress(bytes(out))
    blob=struct.pack('<II',len(out),len(packed))+packed
    rgba(blob)
    return blob


def png(blob):
    """Render the final native texture, including color loss and thresholded transparency."""
    if len(blob)<8:raise ContentError('贴图封装截断。')
    size,packed=struct.unpack_from('<II',blob)
    if packed!=len(blob)-8:raise ContentError('贴图压缩长度不符。')
    return image_png(decompress(blob[8:],size))
