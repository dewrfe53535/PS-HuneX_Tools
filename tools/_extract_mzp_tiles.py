#!/usr/bin/env python
#
# MZP Tiles Extraction Library version 1.4
# comes with ABSOLUTELY NO WARRANTY.
#
# Copyright (C) 2016 Hintay <hintay@me.com>
# Portions Copyright (C) caoyang131
# And probably portions Copyright (C) Waku_Waku
#
# The tiles from MZP files extraction library
# For more information, see Specifications/mzp_format.md
#
# Changelog (recent first):
# 2016-05-30 Hintay: Fixed 4bpp conversion and add 4bpp Alpha-channel to 8bpp for 4bpp MZPs.
# 2016-05-20 Hintay: Fixed 4bpp conversion.
# 2016-04-18 Hintay: Add 24/32bpp True-color support. (bmp_type = 0x08 or 0x0B)
#                    Add pixel crop feature support.
#                    Thanks to caoyang131 for 16bpp conversion.
# 2016-04-11 Hintay: Add 4bpp Alpha-channel to 8bpp conversion. (bmp_type = 0x11 or 0x91)
# 2016-04-10 caoyang131: Add RGBATim2 palette type support.
# 2016-04-09 Hintay: Encapsulated as a library.

import sys
import zlib
import logging
from pathlib import Path
from struct import unpack, pack
from subprocess import call
from mzx.decomp_mzx0 import mzx0_decompress

logger = logging.getLogger(__name__)


# http://blog.flip-edesign.com/?p=23
class Byte(object):
    def __init__(self, number):
        self.number = number

    @property
    def high(self):
        return self.number >> 4

    @property
    def low(self):
        return self.number & 0x0F


def write_pngsig(f):
    f.write(b'\x89\x50\x4E\x47\x0D\x0A\x1A\x0A')


def write_pngchunk_withcrc(f, type, data):
    f.write(pack(">I", len(data)))
    f.write(type)
    f.write(data)
    f.write(pack(">I", zlib.crc32(type + data, 0) & 0xffffffff))


"""
    color = 1 (palette used), 2 (color used), and 4 (alpha channel used). Valid values are 0, 2, 3, 4, and 6.

    Color    Allowed    Interpretation
    Type    Bit Depths

    0       1,2,4,8,16  Each pixel is a grayscale sample.

    2       8,16        Each pixel is an R,G,B triple.

    3       1,2,4,8     Each pixel is a palette index;
                       a PLTE chunk must appear.

    4       8,16        Each pixel is a grayscale sample,
                       followed by an alpha sample.

    6       8,16        Each pixel is an R,G,B triple,
                       followed by an alpha sample.
"""


def write_ihdr(f, width, height, depth, color):
    chunk = pack(">IIBB", width, height, depth, color) + b'\0\0\0'
    write_pngchunk_withcrc(f, b"IHDR", chunk)


def write_plte(f, palettebin):
    write_pngchunk_withcrc(f, b"PLTE", palettebin)


def write_trns(f, transparencydata):
    write_pngchunk_withcrc(f, b"tRNS", transparencydata)


def write_idat(f, pixels):
    write_pngchunk_withcrc(f, b"IDAT", zlib.compress(pixels))


def write_iend(f):
    write_pngchunk_withcrc(f, b"IEND", b"")


def chunks(l, n):
    """ Yield successive n-sized chunks from l.
    """
    for i in range(0, len(l), n):
        yield l[i:i + n]


###############################################
# struct TGAHeader
# {
#   uint8   idLength,           // Length of optional identification sequence.
#           paletteType,        // Is a palette present? (1=yes)
#           imageType;          // Image data type (0=none, 1=indexed, 2=rgb,
#                               // 3=grey, +8=rle packed).
#   uint16  firstPaletteEntry,  // First palette index, if present.
#           numPaletteEntries;  // Number of palette entries, if present.
#   uint8   paletteBits;        // Number of bits per palette entry.
#   uint16  x,                  // Horiz. pixel coord. of lower left of image.
#           y,                  // Vert. pixel coord. of lower left of image.
#           width,              // Image width in pixels.
#           height;             // Image height in pixels.
#   uint8   depth,              // Image color depth (bits per pixel).
#           descriptor;         // Image attribute flags.
# };

def is_indexed_bitmap(bmp_info):
    return bmp_info == 0x01


class MzpFile:
    def __init__(self, file: Path, data, entries_descriptors):
        self.file = file
        self.data = data
        self.entries_descriptors = entries_descriptors
        self.paletteblob = b''
        self.palettepng = b''
        self.transpng = b''
        self.extract_desc()
        self.bytesprepx = self.bitmap_bpp // 8
        if self.bytesprepx == 0:
            self.bytesprepx = 1
        self.debug_format()
        self.rows = [b''] * (self.height - self.tile_y_count * self.tile_crop)
        self.loop_data()
        self.output_png()

    def extract_desc(self):
        self.data.seek(self.entries_descriptors[0].real_offset)
        self.width, self.height, self.tile_width, self.tile_height, self.tile_x_count, self.tile_y_count, \
            self.bmp_type, self.bmp_depth, self.tile_crop = unpack('<HHHHHHHBB', self.data.read(0x10))
        self.tile_size = self.tile_width * self.tile_height
        if self.bmp_type not in [0x01, 0x03, 0x08, 0x0B, 0x0C]:
            logger.error("Unknown type 0x{:02X}".format(self.bmp_type))
            call(["cmd", "/c", "pause"])
            sys.exit(1)

        # 有索引
        if is_indexed_bitmap(self.bmp_type):
            if self.bmp_depth == 0x01:
                self.bitmap_bpp = 8
                self.palette_count = 0x100
            elif self.bmp_depth == 0x00 or self.bmp_depth == 0x10:
                self.bitmap_bpp = 4
                self.palette_count = 0x10
            elif self.bmp_depth == 0x11 or self.bmp_depth == 0x91:
                self.bitmap_bpp = 8
                self.palette_count = 0x100
            else:
                logger.error("Unknown depth 0x{:02X}".format(self.bmp_depth))
                call(["cmd", "/c", "pause"])
                sys.exit(1)

            if self.bmp_depth in [0x00, 0x10]:
                for i in range(self.palette_count):
                    r = self.data.read(1)
                    g = self.data.read(1)
                    b = self.data.read(1)

                    # a = self.data.read(1)
                    # Experimental
                    # 4bpp Alpha-channel to 8bpp
                    # Author: Hintay <hintay@me.com>
                    temp_a, = unpack('B', self.data.read(1))
                    a = (temp_a << 1) + (temp_a >> 6) if (temp_a < 0x80) else 255
                    a = pack('B', a)

                    self.paletteblob += (b + g + r + a)
                    self.palettepng += (r + g + b)
                    self.transpng += a

            # :PalType:RGBATim2:
            # Author: caoyang131
            elif self.bmp_depth in [0x11, 0x91, 0x01]:
                pal_start = self.data.tell()
                for h in range(0, self.palette_count * 4 // 0x80, 1):
                    for i in range(2):
                        for j in range(2):
                            self.data.seek(h * 0x80 + (i + j * 2) * 0x20 + pal_start)
                            for k in range(8):
                                r = self.data.read(1)
                                g = self.data.read(1)
                                b = self.data.read(1)

                                # Experimental
                                # 4bpp Alpha-channel to 8bpp
                                # Author: Hintay <hintay@me.com>
                                temp_a, = unpack('B', self.data.read(1))
                                a = (temp_a << 1) + (temp_a >> 6) if (temp_a < 0x80) else 255
                                a = pack('B', a)

                                self.paletteblob += (b + g + r + a)
                                self.palettepng += (r + g + b)
                                self.transpng += a
            else:
                logger.error("Unsupported palette type 0x{:02X}".format(self.bmp_depth))
                call(["cmd", "/c", "pause"])
                sys.exit(1)

            # 补全索引
            for i in range(self.palette_count, 0x100):
                self.paletteblob += b'\x00\x00\x00\xFF'
                self.palettepng += b'\x00\x00\x00'
                self.transpng += b'\xFF'
        elif self.bmp_type == 0x08:
            if self.bmp_depth == 0x14:
                self.bitmap_bpp = 24
            else:
                logger.error("Unknown depth 0x{:02X}".format(self.bmp_depth))
                call(["cmd", "/c", "pause"])
                sys.exit(1)
        elif self.bmp_type == 0x0B:
            if self.bmp_depth == 0x14:
                self.bitmap_bpp = 32
            else:
                logger.error("Unknown depth 0x{:02X}".format(self.bmp_depth))
                call(["cmd", "/c", "pause"])
                sys.exit(1)
        elif self.bmp_type == 0x0C:
            if self.bmp_depth == 0x11:
                self.bitmap_bpp = 24
        elif self.bmp_type == 0x03:  # 'PEH' 8bpp + palette
            logger.error("Unsupported type 0x{:02X} (PEH)".format(self.bmp_type))
            call(["cmd", "/c", "pause"])
            sys.exit(1)

        del self.entries_descriptors[0]

    def debug_format(self):
        logger.debug(
            'MZP Format: Width = %s, Height = %s, Bitmap type = %s, Bitmap depth = %s, Bits per pixel = %s, '
            'Bytes Pre pixel = %s' % (
                self.width, self.height, self.bmp_type, self.bmp_depth, self.bitmap_bpp, self.bytesprepx))
        logger.debug('Tile Format: Width = %s, Height = %s, X count = %s, Y count = %s, Tile crop = %s' % (
            self.tile_width, self.tile_height, self.tile_x_count, self.tile_y_count, self.tile_crop))
        if self.tile_crop:
            width = self.width - self.tile_x_count * self.tile_crop * 2
            height = self.height - self.tile_y_count * self.tile_crop * 2
            logger.debug('MZP Cropped Size: Width = %s, Height = %s' % (width, height))

    def extract_tile(self, index):
        entry = self.entries_descriptors[index]
        self.data.seek(entry.real_offset)
        sig, size = unpack('<LL', self.data.read(0x8))
        status, dec_buf = mzx0_decompress(self.data, entry.real_size - 8, size)
        dec_buf = dec_buf.read()
        if self.bitmap_bpp == 4:
            tile_data = b''
            for octet in dec_buf:
                the_byte = Byte(octet)
                tile_data += pack('BB', the_byte.low, the_byte.high)
            dec_buf = tile_data

        # RGB/RGBA true color for 0x08 and 0x0B bmp type
        elif self.bitmap_bpp in [24, 32] and self.bmp_type in [0x08, 0x0B]:
            # 16bpp
            tile_data = b''
            for index in range(self.tile_size):
                P = dec_buf[index * 2]
                Q = dec_buf[(index * 2) + 1]
                b = (P & 0x1f) << 3
                g = (Q & 0x07) << 5 | (P & 0xe0) >> 3
                r = (Q & 0xf8)

                # Offset for 16bpp to 24bpp
                offset_byte = dec_buf[self.tile_size * 2 + index]
                r_offset = offset_byte >> 5
                g_offset = (offset_byte & 0x1f) >> 3
                b_offset = offset_byte & 0x7

                # Alpha
                if self.bitmap_bpp == 32:
                    a = dec_buf[self.tile_size * 3 + index]
                    tile_data += pack('BBBB', r + r_offset, g + g_offset, b + b_offset, a)
                else:
                    tile_data += pack('BBB', r + r_offset, g + g_offset, b + b_offset)
            dec_buf = tile_data
        # HEP
        elif self.bmp_type in [0x0C]:
            tile_data = b''
            dec_buf_size = len(dec_buf)
            image_data = dec_buf[32:dec_buf_size - 1024]
            image_palette = dec_buf[-1024:]
            for i in image_data:
                r, g, b, a = image_palette[int(i) * 4:int(i) * 4 + 4]
                tile_data += pack('BBB', r, g, b)
            dec_buf = tile_data
        return dec_buf

    def loop_data(self):
        for y in range(self.tile_y_count):
            start_row = y * (self.tile_height - self.tile_crop * 2)  # 上下切边
            rowcount = min(self.height, start_row + self.tile_height) - start_row - self.tile_crop * 2  # 共几行
            self.loop_x(y, start_row, rowcount)

    def loop_x(self, y, start_row, rowcount):
        # Tile 块处理
        for x in range(self.tile_x_count):
            dec_buf = self.extract_tile(self.tile_x_count * y + x)

            for i, tile_row_px in enumerate(chunks(dec_buf, self.tile_width * self.bytesprepx)):
                if i < self.tile_crop:
                    continue
                if (i - self.tile_crop) >= rowcount:
                    break
                cur_width = len(self.rows[start_row + i - self.tile_crop])
                px_count = min(self.width, cur_width + self.tile_width) * self.bytesprepx - cur_width
                try:
                    temp_row = tile_row_px[:px_count]
                    self.rows[start_row + i - self.tile_crop] += temp_row[self.tile_crop * self.bytesprepx: len(
                        temp_row) - self.tile_crop * self.bytesprepx]
                except IndexError:
                    logger.error(start_row + i - self.tile_crop)

    # 输出PNG
    def output_png(self):
        png_path = self.file.with_suffix('.png')
        with png_path.open('wb') as png:
            write_pngsig(png)
            width = self.width - self.tile_x_count * self.tile_crop * 2
            height = self.height - self.tile_y_count * self.tile_crop * 2
            if is_indexed_bitmap(self.bmp_type):
                write_ihdr(png, width, height, 8, 3)  # 8bpp (PLTE)
                write_plte(png, self.palettepng)
                write_trns(png, self.transpng)

            elif self.bitmap_bpp == 24:  # RGB True-color
                write_ihdr(png, width, height, 8, 2)  # 24bpp

            elif self.bitmap_bpp == 32:  # RGBA True-color
                write_ihdr(png, width, height, 8, 6)  # 32bpp

            # split into rows and add png filtering info (mandatory even with no filter)
            row_data = b''
            for row in self.rows:
                row_data += b'\x00' + row

            write_idat(png, row_data)
            write_iend(png)
    # call(["cmd", "/c", "start", pngoutpath])
