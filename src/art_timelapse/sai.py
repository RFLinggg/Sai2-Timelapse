# ---------------------------------------------------------------------------
# Modified from art-timelapse -- https://github.com/cromachina/art-timelapse
# Copyright (C) 2024-2026 cromachina. Licensed under GPL-3.0 (see LICENSE).
#
# 本文件在上游基础上做过修改：新增 SAI2 版本表（24 个构建）、外部版本表热加载、动态版本类工厂；修复 new 布局没有 id 字段时的画布存活判定（改比 _self_ptr）
# 修改自 2026-11-19 起，逐次记录见 适配说明.md。
# ---------------------------------------------------------------------------
import ctypes
import hashlib
import json
import logging
import re
import sys
from ctypes import wintypes
from pathlib import Path

import psutil
from PyMemoryEditor import OpenProcess
from PyMemoryEditor.process import AbstractProcess
import numpy as np

def trim_null(data):
    if '\0' in data:
        data = data[0:data.index('\0')]
    return data

def from_wide_str(data):
    return trim_null(bytes(data).decode('utf-16le'))

def from_narrow_str(data):
    return trim_null(bytes(data).decode('utf-8'))

class RemotePointerBase(ctypes.Structure):
    def get(self, proc:AbstractProcess, index=0, count=1, buffer=None):
        dtype = self._type_
        dsize = ctypes.sizeof(dtype)
        offset = dsize * index
        address = self.value + offset
        if buffer is None:
            byte_size = dsize * count
            try:
                data = proc.read_bytes(address, byte_size)
                if count == 1:
                    result = self._type_.from_buffer_copy(data)
                else:
                    result = (self._type_ * count).from_buffer_copy(data)
            except:
                if count == 1:
                    result = self._type_()
                else:
                    result = (self._type_ * count)()
            if isinstance(result, ctypes._SimpleCData):
                return result.value
            return result
        else:
            try:
                proc.read_process_memory_into(address, buffer)
            except:
                pass
            return buffer

    def __bool__(self):
        return bool(self.value)

    def __eq__(self, other):
        return self.value == other.value

class RemotePointer32(RemotePointerBase):
    prefix = 'RP32'
    _fields_ = [('value', ctypes.c_uint32)]

class RemotePointer64(RemotePointerBase):
    prefix = 'RP'
    _fields_ = [('value', ctypes.c_uint64)]

_remote_pointer_cache = {}
def _RPOINTER(dtype, rpointer_class):
    name = f'{rpointer_class.prefix}_{dtype.__qualname__}'
    if name in _remote_pointer_cache:
        return _remote_pointer_cache[name]
    ptype = type(name, (rpointer_class,), { '_type_': dtype })
    _remote_pointer_cache[name] = ptype
    return ptype

def RPOINTER32(dtype):
    return _RPOINTER(dtype, RemotePointer32)

def RPOINTER(dtype):
    return _RPOINTER(dtype, RemotePointer64)

def get_base_address_windows(proc:AbstractProcess) -> int | None:
    try:
        return next(proc.get_modules()).base_address
    except:
        return None

def get_region_data_by_name(proc:AbstractProcess, name:str) -> tuple[str, int] | tuple[None, None]:
    for region in proc.get_memory_regions():
        if name in region.path:
            return region.path, region.address
    return None, None

def get_base_address(proc:AbstractProcess) -> int | None:
    if sys.platform == 'win32':
        return get_base_address_windows(proc)
    else:
        return get_region_data_by_name(proc, psutil.Process(proc.pid).name())[1]

sai_api_lookup = {}
def register_sai_api(api):
    sai_api_lookup[api.exe_hash] = api
    return api

def get_sai_api(exe_hash):
    return sai_api_lookup.get(exe_hash)

def get_sai_api_list():
    return sorted(sai_api_lookup.values(), key=lambda x: x.__name__)

pad_index = 0
def pad(size):
    global pad_index
    field = (f'__pad{pad_index}', ctypes.c_char * size)
    pad_index += 1
    return field

def offset_fields(fields):
    fields = sorted(fields, key=lambda x: x[0])
    result = []
    current_offset = 0
    for offset, name, type in fields:
        if offset > current_offset:
            result.append(pad(offset - current_offset))
            current_offset = offset
        elif offset < current_offset:
            raise ValueError(f'field \'{name}\' offset {hex(offset)} overlaps the previous field at {hex(current_offset)}')
        result.append((name, type))
        current_offset += ctypes.sizeof(type)
    return result

class StructureBase(ctypes.Structure):
    _pack_ = 1
    _layout_ = 'ms'

class SAI_API_Base:
    def __init__(self, proc:AbstractProcess):
        self.proc = proc
        self.base_address = get_base_address(proc)

    # 画布链表的遍历上限与环保护。正常用户同时开几十个画布已经很夸张了；
    # offset 指错时 next_canvas 可能是一长串垃圾、甚至指回自己，没有保护就会
    # 死循环（2026-11-18 收到的那份嗅探报告里读出 166 个画布，全是 0×0 的空壳，
    # 只是恰好走到了 NULL 而已）。
    MAX_CANVASES = 256

    def collect_canvases(self, canvas_ptr):
        result = []
        seen = set()
        while canvas_ptr:
            address = int(canvas_ptr.value)
            if address in seen:
                logging.warning('Canvas list loops back to %#x after %d nodes; stopping',
                                address, len(result))
                break
            if len(result) >= self.MAX_CANVASES:
                logging.warning('Canvas list is longer than %d nodes; stopping',
                                self.MAX_CANVASES)
                break
            seen.add(address)
            canvas = canvas_ptr.get(self.proc)
            canvas._self_ptr = canvas_ptr
            result.append(canvas)
            canvas_ptr = canvas.next_canvas
        return result

    def update_canvas(self, canvas) -> None:
        canvas._self_ptr.get(self.proc, buffer=canvas)

    def get_map_level_for_size(self, canvas, size:int) -> int:
        return 0

    def check_if_canvas_exists(self, canvas) -> bool:
        return any((c._self_ptr == canvas._self_ptr for c in self.get_canvas_list()))

class SAIv1CanvasBase(StructureBase):
    def get_name(self):
        return from_narrow_str(self.name)

    def get_short_path(self):
        return from_narrow_str(self.short_path)

class SAIv1_API_Base(SAI_API_Base):
    process_name = 'sai.exe'
    map_count = 1

    class SAIPixelHeap(StructureBase):
        _fields_ = offset_fields([
            (0x4, 'stride_x', ctypes.c_int32),
            (0x8, 'stride_y', ctypes.c_int32),
            (0xc, 'data', RPOINTER32(ctypes.c_uint8)),
        ])

    class SAICanvas(SAIv1CanvasBase):
        pass

    SAICanvas._fields_ = offset_fields([
            (0x004, 'next_canvas', RPOINTER32(SAICanvas)),
            (0x030, 'pixel_heap_level_1', RPOINTER32(SAIPixelHeap)),
            (0x114, 'lower_pad_x', ctypes.c_int32),
            (0x118, 'lower_pad_y', ctypes.c_int32),
            (0x124, 'width', ctypes.c_int32),
            (0x128, 'height', ctypes.c_int32),
            (0x12c, 'padded_width', ctypes.c_int32),
            (0x130, 'padded_height', ctypes.c_int32),
            (0x4d0, 'short_path', ctypes.c_char * 0x108),
            (0x5d8, 'name', ctypes.c_char * 0x108),
        ])

    class SAISession(StructureBase):
        pass

    SAISession._fields_ = offset_fields([
        (0x4c, 'canvas_list', RPOINTER32(SAICanvas)),
    ])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.image = np.empty((0,), dtype=np.uint8)

    def get_canvas_list(self) -> list[SAICanvas]:
        canvas_ptr = RPOINTER32(RPOINTER32(self.SAISession))(self.base_address + self.session_offset).get(self.proc).get(self.proc).canvas_list
        return self.collect_canvases(canvas_ptr)

    def check_if_canvas_exists(self, canvas:SAICanvas) -> bool:
        return any((c._self_ptr == canvas._self_ptr for c in self.get_canvas_list()))

    def get_canvas_image(self, canvas:SAICanvas, map_level=0) -> np.ndarray:
        self.update_canvas(canvas)
        lower_pad_x = canvas.lower_pad_x
        lower_pad_y = canvas.lower_pad_y
        pixel_heap = canvas.pixel_heap_level_1.get(self.proc)
        image_shape = (canvas.padded_height, canvas.padded_width, 4)
        if self.image.shape != image_shape:
            self.image = np.empty(shape=image_shape, dtype=np.uint8)
        pixel_heap.data.get(self.proc, buffer=self.image)
        return self.image[lower_pad_y : canvas.height + lower_pad_y, lower_pad_x : canvas.width + lower_pad_x, :3]

@register_sai_api
class SAIv1_API_1_2_5(SAIv1_API_Base):
    version_name = 'SAI Ver.1.2.5 (32bit)'
    exe_hash = 'f4e7e00aa4c222d6253aa1f0a5f302c2'
    session_offset = 0x491a8c

@register_sai_api
class SAIv1_API_1_2_6_Beta_3(SAIv1_API_Base):
    version_name = 'SAI Ver.1.2.6-Beta.3 (32bit)'
    exe_hash = 'b693f2c4516c09d008e30827208be1e6'
    session_offset = 0x494bcc

class SAIv2CanvasBase(StructureBase):
    def get_name(self):
        return from_wide_str(self.name)

    def get_short_path(self):
        return from_wide_str(self.short_path)

class SAIv2_API_Base(SAI_API_Base):
    process_name = 'sai2.exe'
    map_count = 0xb

    class SAICanvasTileMap(StructureBase):
        _fields_ = offset_fields([
            # (0x00, 'allocator', RPOINTER(WINFUNCTYPE(None, int64_t, int512_t, int512_t)))
            (0x08, 'tree', RPOINTER(RPOINTER(RPOINTER(ctypes.c_uint8)))),
            (0x10, 'width', ctypes.c_int32),
            (0x14, 'height', ctypes.c_int32),
            (0x18, 'count_x', ctypes.c_int32),
            (0x1c, 'count_y', ctypes.c_int32),
        ])

    class SAICanvas(SAIv2CanvasBase):
        pass

    SAICanvas._fields_ = offset_fields([
            (0x000, 'next_canvas', RPOINTER(SAICanvas)),
            (0x020, 'id', ctypes.c_int32),
            (0x028, 'tile_maps', RPOINTER(RPOINTER(SAICanvasTileMap)) * map_count),
            (0x250, 'width', ctypes.c_int32),
            (0x254, 'height', ctypes.c_int32),
            (0x2ac, 'name', ctypes.c_uint16 * 0x100),
            (0x4ac, 'short_path', ctypes.c_uint16 * 0x100)
        ])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.image = np.empty((0,), dtype=np.uint8)
        tile_shape = (256, 256, 4)
        self.tile = np.empty(shape=tile_shape, dtype=np.uint8)

    def get_canvas_list(self) -> list[SAICanvas]:
        canvas_ptr = RPOINTER(RPOINTER(self.SAICanvas))(self.base_address + self.session_offset).get(self.proc)
        return self.collect_canvases(canvas_ptr)[1:]

    def check_if_canvas_exists(self, canvas:SAICanvas) -> bool:
        return any((c.id == canvas.id for c in self.get_canvas_list()))

    def get_map_level_for_size(self, canvas:SAICanvas, size:int) -> int:
        self.update_canvas(canvas)
        map_level = 0
        for i in range(self.map_count):
            tile_map = canvas.tile_maps[i].get(self.proc).get(self.proc)
            if tile_map.width < size and tile_map.height < size:
                break
            map_level = i
        return map_level

    def get_canvas_image(self, canvas:SAICanvas, map_level=0) -> np.ndarray:
        self.update_canvas(canvas)
        tile_map = canvas.tile_maps[map_level].get(self.proc).get(self.proc)
        tiles_y = tile_map.count_y
        tiles_x = tile_map.count_x
        image_shape = (tiles_y * 256, tiles_x * 256, 4)
        if self.image.shape != image_shape:
            self.image = np.empty(shape=image_shape, dtype=np.uint8)
        for y in range(tiles_y):
            for x in range(tiles_x):
                tile_map.tree.get(self.proc, y).get(self.proc, x).get(self.proc, buffer=self.tile)
                self.image[y * 256 : (y + 1) * 256, x * 256 : (x + 1) * 256] = self.tile
        return self.image[0 : tile_map.height, 0 : tile_map.width, :3]

@register_sai_api
class SAIv2_API_2019_build(SAIv2_API_Base):
    # Earliest Ver.2 build we can drive — exe dated 2019-08-07, version resource
    # only says "PaintTool SAI Ver.2" with no preview date. Base layout.
    #
    # This is the same machine that sent the very first sniffer report, back
    # when the tool accepted a garbage offset (0x2f3838, 166 canvases of 0x0).
    # With the validation fixed it now picks 0x2cf6d8 instead, and a
    # 2480x3508 document reads back 620x877 (exactly 1:4) with the artwork
    # intact. Same user, same exe, opposite verdict — the offset was there all
    # along, the old code just never got to try it.
    version_name = 'SAI Ver.2 (2019 build)'
    exe_hash = '1a65a72f1436d268964bf3808fa54d26'
    session_offset = 0x2cf6d8

@register_sai_api
class SAIv2_API_2023_07_11(SAIv2_API_Base):
    version_name= 'SAI Ver.2 (64bit) Preview.2023.07.11'
    exe_hash = 'f8df4067657d811e6e48c37c5b0f8fc5'
    session_offset = 0x31af20

@register_sai_api
class SAIv2_API_2024_08_14(SAIv2_API_Base):
    # Preview.2024.08.14 (D:\软件\sai2\2024.8\[Saier]SAI Ver.2\sai2.exe).
    # Same offset 0x320720 as Preview.2025.01.05 — that offset covers at least
    # half a year of builds.
    #
    # This one also settles the open question on 2025.01.05: its canvas had no
    # content to check against, but here a 2126x2126 document read back
    # 1063x1063 (exactly 1:2) and rendered a complete two-character
    # illustration. So 0x320720 is now confirmed by content, not just ratio.
    version_name = 'SAI Ver.2 (64bit) Preview.2024.08.14'
    exe_hash = '849d03a62e91401ae2f6da430fddee0e'
    session_offset = 0x320720

@register_sai_api
class SAIv2_API_2024_08_14_b(SAIv2_API_Base):
    # Second [Saier] repack of Preview.2024.08.14 (C:\Users\10933\Documents\
    # [Saier]SAI Ver.2\sai2.exe, 5698560 bytes). Different MD5 from 849d03a6
    # although the version resource and the offset (0x320720) are identical —
    # the repacker's bundle differs, the binary structure doesn't.
    # Verified with content: a 3445x3445 document read back 862x862 (exact 1:4)
    # and rendered a clean, centred sword lineart. Std is only 4.34 because the
    # lineart itself is pale — the strokes are crisp, so the read is correct.
    version_name = 'SAI Ver.2 (64bit) Preview.2024.08.14'
    exe_hash = '3b5aa4c7705afd2729488126323629aa'
    session_offset = 0x320720

@register_sai_api
class SAIv2_API_2024_10_02(SAIv2_API_Base):
    # 2024.10.02 (E:\SAI2\sai2\sai2.exe, 5696000 bytes). Shares offset 0x320220
    # with 2025.02.10 and 2026.04.05 — same structure, three different builds.
    # Verified by ratio only: a 3000x2100 canvas read back 750x525 (exactly
    # 4:3) but blank white. Both other users of this offset had real artwork,
    # which is why we're comfortable accepting this one on structure alone.
    version_name = 'SAI Ver.2 (64bit) 2024.10.02'
    exe_hash = '2bbec6a5b621b8547a00bf81b9be2bfe'
    session_offset = 0x320220

@register_sai_api
class SAIv2_API_2024_11_23(SAIv2_API_Base):
    version_name = 'SAI Ver.2 (64bit) Preview.2024.11.23'
    exe_hash = 'bd60d6750ef57668f9bc44eb98d992c4'
    session_offset = 0x322620

@register_sai_api
class SAIv2_API_2024_11_23_b(SAIv2_API_Base):
    # Second build labelled Preview.2024.11.23 (after bd60d675) — the PivInk
    # repack. Same offset 0x322620, so the structure didn't move.
    #
    # This is the one that showed up as "canvas size 0x0" while v1.15 was
    # force-fitting 2025.02.10 params: same generation, wrong offset, so width
    # and height landed on garbage. Now it has its own entry.
    # Verified with content: a 4961x6614 document read back 621x827 (exact 1:8)
    # and rendered a complete illustration.
    version_name = 'SAI Ver.2 (64bit) Preview.2024.11.23'
    exe_hash = 'a5eea6c0edd820193f516c96dabcb90d'
    session_offset = 0x322620

@register_sai_api
class SAIv2_API_2026_04_05(SAIv2_API_Base):
    # 2026.04.05 (D:\SAI2中文版, 5696000 bytes). Shares offset 0x320220 with
    # 2025.02.10 — the structure didn't move between them.
    # Verified: a 1024x1024 canvas came back at full 1024x1024 (no downscale),
    # a few blue strokes on white with clean edges and no artifacts.
    version_name = 'SAI Ver.2 (64bit) 2026.04.05'
    exe_hash = '8c2200728c9cea2ad9d4577b8afc7a9a'
    session_offset = 0x320220

@register_sai_api
class SAIv2_API_2025_01_05(SAIv2_API_Base):
    # Preview.2025.01.05 (E:\画画，笔和键盘\SAI2绘画工具\sai2.exe, 5698560
    # bytes). The vendor renamed the version resource to "SAI2绘画工具  SAI
    # Ver.2 ..." — a repackaged bundle, but the binary structure is untouched.
    # Offset 0x320720 is new, sitting between 2024.02.22 (0x320420) and
    # 2024.05.22 (0x321620), which fits the timeline.
    #
    # Evidence is ratio-only: a 5905x5905 canvas read back 739x739 (exactly 1:8,
    # square stays square) with a correct placeholder head, but the canvas was
    # blank. Tell the user to record a short clip before trusting it.
    version_name = 'SAI Ver.2 (64bit) Preview.2025.01.05'
    exe_hash = 'ab74be8df042759f006fc7b51e815697'
    session_offset = 0x320720

@register_sai_api
class SAIv2_API_2025_02_10(SAIv2_API_Base):
    # Stock (non-localized) 2025.02.10 build, base layout.
    # Strongest evidence of the batch: all three open canvases read back at
    # exactly 1:2 — 1700x1700 -> 850x850 twice, 2200x2200 -> 1100x1100 — and the
    # one with content (PSD.psd, mean 231 / std 67) came out clean.
    # Three correct aspect ratios from one offset can't be a coincidence.
    version_name = 'SAI Ver.2 (64bit) 2025.02.10'
    exe_hash = 'e9f4f56c9212fd8e0d9d6b992cec2b0c'
    session_offset = 0x320220

@register_sai_api
class SAIv2_API_2024_01_06(SAIv2_API_Base):
    # [Saier] 2024.01.06 (E:\[Saier]SAI Ver.2\SAI2绘画工具\sai2.exe, 5696000
    # bytes), base layout. Older than 2024.02.22 but the offset sits lower
    # (0x320220 vs 0x320420) — the offsets aren't strictly monotonic with the
    # release date, so this had to be measured, not extrapolated.
    # Verified with content: 5000x5550 read back 625x694 (1:8 on both axes) and
    # rendered a complete chibi illustration; the checkered grey in the preview
    # is the canvas alpha, not corruption.
    version_name = 'SAI Ver.2 (64bit) 2024.01.06'
    exe_hash = 'f2936f9d1f573de302c7dfd21322fcd3'
    session_offset = 0x320220

@register_sai_api
class SAIv2_API_2024_02_22(SAIv2_API_Base):
    # [Saier] build of the non-preview 2024.02.22 release, base layout.
    # Verified end to end: a 2500x2500 canvas (1001.sai2) produced a 625x625
    # thumbnail with a fully rendered illustration (mean 205, std 53) — nothing
    # garbled, so offset and field layout both line up.
    version_name = 'SAI Ver.2 (64bit) 2024.02.22'
    exe_hash = 'bc572fd712e2c11db6e7bc5067a3de02'
    session_offset = 0x320420

@register_sai_api
class SAIv2_API_2024_02_22_b(SAIv2_API_Base):
    # Second build labelled 2024.02.22 (after bc572fd7) — a colour-picker tool
    # shipped this exe as a reference copy. Same offset 0x320420 as bc572fd7.
    # Best evidence of the batch: FOUR canvases of different shapes all read
    # back at their exact aspect ratios (1080x2340 -> 540x1170, 1440x1920 ->
    # 720x960, 1220x2712 -> 305x678, 2000x2000 -> 1000x1000), and the one we
    # rendered is a complete illustration.
    version_name = 'SAI Ver.2 (64bit) 2024.02.22'
    exe_hash = '24875bdd844a187b853008258067b845'
    session_offset = 0x320420

@register_sai_api
class SAIv2_API_2024_05_22(SAIv2_API_Base):
    # Local adaptation: [Saier] localized build, canvas layout identical to
    # 2023.07.11 / 2024.11.23. session_offset located by scanning the image
    # for the canvas pointer and walking the linked list.
    version_name = 'SAI Ver.2 (64bit) Preview.2024.05.22'
    exe_hash = '918afa18b2e67f29a800b52154b8f14d'
    session_offset = 0x321620

@register_sai_api
class SAIv2_API_2021_02_28(SAIv2_API_Base):
    # [Saier] localized build of the 2021.02.28 preview. Base layout, same as
    # 2023.07.11. Offset came from Sai2-Sniffer and was confirmed by a readable
    # canvas dump: a 2500x2500 document produced a 625x625 thumbnail with real
    # content (mean 220, std 72), which is what "the offset is right" looks like.
    version_name = 'SAI Ver.2 (64bit) Preview.2021.02.28'
    exe_hash = 'dcfd64edbc8a035f0530fe04c34c81ef'
    session_offset = 0x302e20

@register_sai_api
class SAIv2_API_2020_08_28(SAIv2_API_Base):
    # [Saier] build of the 2020.08.28 preview, base layout as well.
    # Verified on a blank 4724x8268 canvas: the thumbnail came out 591x1034,
    # exactly 1:8, so the offset and both dimension fields line up. The picture
    # is white because the document was empty, not because the read failed.
    version_name = 'SAI Ver.2 (64bit) Preview.2020.08.28'
    exe_hash = '40646f90da87323451057d375dd96f2e'
    session_offset = 0x301e20

@register_sai_api
class SAIv2_API_2026_07_02b(SAIv2_API_Base):
    version_name = 'SAI Ver.2 (64bit) Preview.2026.07.02b'
    exe_hash = '6f3f351303cf3896f4c2977925c994c2'
    session_offset = 0x32eb20

@register_sai_api
class SAIv2_API_2026_07_02b_ja(SAIv2_API_Base):
    # The Japanese build of Preview.2026.07.02b (D:\sai2-20260702b-64bit-ja).
    # Same offset 0x32eb20 as the other one, so the structure is identical.
    # Placeholder canvas is named 便签本1 here rather than 便签薄1 — cosmetic.
    # Verified with content: 1946x2253 -> 973x1127 (exact 1:2), a finished
    # illustration with full colour.
    version_name = 'SAI Ver.2 (64bit) Preview.2026.07.02b'
    exe_hash = '57bffe89c133820cda1bbcc000e830c4'
    session_offset = 0x32eb20

@register_sai_api
class SAIv2_API_2026_09_25_alpha(SAIv2_API_Base):
    version_name = 'SAI Ver.2 (64bit) Alpha.2026.09.25'
    exe_hash = 'b7460c024f48c97abb465a72b2ab36c7'
    session_offset = 0x478280

    class SAICanvas(SAIv2CanvasBase):
        pass

    SAICanvas._fields_ = offset_fields([
            (0x000, 'next_canvas', RPOINTER(SAICanvas)),
            (0x008, 'prev_canvas', RPOINTER(SAICanvas)),
            (0x010, 'prev_user_canvas', RPOINTER(SAICanvas)),
            (0x018, 'next_user_canvas', RPOINTER(SAICanvas)),
            # A canvas ID was here, but seems to be removed now.
            (0x028, 'width', ctypes.c_int32),
            (0x02c, 'height', ctypes.c_int32),
            (0x048, 'tile_maps', RPOINTER(RPOINTER(SAIv2_API_Base.SAICanvasTileMap)) * SAIv2_API_Base.map_count),
            (0x180, 'tile_count_x', ctypes.c_uint32),
            (0x184, 'tile_count_y', ctypes.c_uint32),
            (0x8f8, 'name', ctypes.c_uint16 * 0x104),
            (0xb00, 'short_path', ctypes.c_uint16 * 0x104)
        ])

    def check_if_canvas_exists(self, canvas:SAICanvas) -> bool:
        return SAI_API_Base.check_if_canvas_exists(self, canvas)

@register_sai_api
class SAIv2_API_2026_09_25_b(SAIv2_API_Base):
    # Chinese-localized build of Alpha.2026.09.25 (5200384 bytes vs b7460c02's
    # size). Same offset and the same new-layout field table — the localizer
    # only touched resources, so the structure didn't move.
    # Verified with content: a 4093x2894 canvas read back 1024x724 (1:4 on both
    # axes), mean 254.38 / std 9.91 — pale lineart on white, strokes clean.
    version_name = 'SAI Ver.2 (64bit) Alpha.2026.09.25'
    exe_hash = 'f309b47e54f116acc84f74c18e1a6c6d'
    session_offset = 0x478280

    class SAICanvas(SAIv2CanvasBase):
        pass

    SAICanvas._fields_ = offset_fields([
            (0x000, 'next_canvas', RPOINTER(SAICanvas)),
            (0x008, 'prev_canvas', RPOINTER(SAICanvas)),
            (0x010, 'prev_user_canvas', RPOINTER(SAICanvas)),
            (0x018, 'next_user_canvas', RPOINTER(SAICanvas)),
            # No id here either — same as b7460c02, hence the override below.
            (0x028, 'width', ctypes.c_int32),
            (0x02c, 'height', ctypes.c_int32),
            (0x048, 'tile_maps', RPOINTER(RPOINTER(SAIv2_API_Base.SAICanvasTileMap)) * SAIv2_API_Base.map_count),
            (0x180, 'tile_count_x', ctypes.c_uint32),
            (0x184, 'tile_count_y', ctypes.c_uint32),
            (0x8f8, 'name', ctypes.c_uint16 * 0x104),
            (0xb00, 'short_path', ctypes.c_uint16 * 0x104)
        ])

    def check_if_canvas_exists(self, canvas:SAICanvas) -> bool:
        return SAI_API_Base.check_if_canvas_exists(self, canvas)

@register_sai_api
class SAIv2_API_2026_10_19_alpha(SAIv2_API_Base):
    # [PivInk] build of Alpha.2026.10.19. Same new-layout field table as
    # 2026.09.25 — only the session offset moved (0x478280 -> 0x47a380).
    # Evidence is weaker than usual: the only open canvas was an empty
    # 4000x3000 (read back 1000x750, exactly 1:4, but pure white), so the
    # ratio checks out while the picture content can't confirm anything.
    # Tell the user to record a short clip before relying on it.
    version_name = 'SAI Ver.2 (64bit) Alpha.2026.10.19'
    exe_hash = '44aee0ba61ea9f215b1b4dcb4d6d23e0'
    session_offset = 0x47a380

    class SAICanvas(SAIv2CanvasBase):
        pass

    SAICanvas._fields_ = offset_fields([
            (0x000, 'next_canvas', RPOINTER(SAICanvas)),
            (0x008, 'prev_canvas', RPOINTER(SAICanvas)),
            (0x010, 'prev_user_canvas', RPOINTER(SAICanvas)),
            (0x018, 'next_user_canvas', RPOINTER(SAICanvas)),
            (0x028, 'width', ctypes.c_int32),
            (0x02c, 'height', ctypes.c_int32),
            (0x048, 'tile_maps', RPOINTER(RPOINTER(SAIv2_API_Base.SAICanvasTileMap)) * SAIv2_API_Base.map_count),
            (0x180, 'tile_count_x', ctypes.c_uint32),
            (0x184, 'tile_count_y', ctypes.c_uint32),
            (0x8f8, 'name', ctypes.c_uint16 * 0x104),
            (0xb00, 'short_path', ctypes.c_uint16 * 0x104)
        ])

    def check_if_canvas_exists(self, canvas:SAICanvas) -> bool:
        return SAI_API_Base.check_if_canvas_exists(self, canvas)

@register_sai_api
class SAIv2_API_2026_10_19_alpha_pivink(SAIv2_API_2026_10_19_alpha):
    # 同一个 Alpha.2026.10.19，PivInk 发过不止一个包：上面那条是 44aee0ba，
    # 这份是 2037d571（Sai2-Sniffer 报告 Sai2-Sniffer-2037d571.zip）。
    # 两者 session_offset 都探到 0x47a380，字段表完全一致，所以直接继承，
    # 只换指纹和显示名。不同包不同 MD5、但布局相同 —— 这正是外部版本表
    # 要解决的问题，把这条写进内置表是为了下个打包版不用再让用户手工丢 json。
    version_name = 'SAI Ver.2 (64bit) Alpha.2026.10.19 [PivInk 2037d571]'
    exe_hash = '2037d5713e06186684597bb495a55664'

@register_sai_api
class SAIv2_API_2026_10_19_alpha_b(SAIv2_API_Base):
    # Third distinct MD5 wearing the Alpha.2026.10.19 label (after 44ae0bae and
    # 44aee0ba — the localizers repacked it more than once). Same offset and
    # field table as 44aee0ba, so the structure is unchanged; only the exe is
    # smaller (5212160 vs 5537792).
    # Evidence again on the thin side: a 2016x1260 canvas read back 1008x630,
    # exactly 1:1.6, but the canvas was blank white (std 0).
    version_name = 'SAI Ver.2 (64bit) Alpha.2026.10.19'
    exe_hash = '2037d5713e06186684597bb495a55664'
    session_offset = 0x47a380

    class SAICanvas(SAIv2CanvasBase):
        pass

    SAICanvas._fields_ = offset_fields([
            (0x000, 'next_canvas', RPOINTER(SAICanvas)),
            (0x008, 'prev_canvas', RPOINTER(SAICanvas)),
            (0x010, 'prev_user_canvas', RPOINTER(SAICanvas)),
            (0x018, 'next_user_canvas', RPOINTER(SAICanvas)),
            (0x028, 'width', ctypes.c_int32),
            (0x02c, 'height', ctypes.c_int32),
            (0x048, 'tile_maps', RPOINTER(RPOINTER(SAIv2_API_Base.SAICanvasTileMap)) * SAIv2_API_Base.map_count),
            (0x180, 'tile_count_x', ctypes.c_uint32),
            (0x184, 'tile_count_y', ctypes.c_uint32),
            (0x8f8, 'name', ctypes.c_uint16 * 0x104),
            (0xb00, 'short_path', ctypes.c_uint16 * 0x104)
        ])

    def check_if_canvas_exists(self, canvas:SAICanvas) -> bool:
        return SAI_API_Base.check_if_canvas_exists(self, canvas)

@register_sai_api
class SAIv2_API_2024_11_23_c(SAIv2_API_Base):
    # Third build labelled Preview.2024.11.23 (after bd60d675 and a5eea6c0) —
    # another [PivInk] repack, 5705728 bytes. Same offset 0x322620 as the other
    # two, so the structure never moved across three separate repacks.
    # Verified with content: a 2918x3427 document read back 730x857 (exact 1:4),
    # showing three blue-tinted reference photos and a greyscale lineart on the
    # left half — strokes continuous, nothing torn or shifted.
    version_name = 'SAI Ver.2 (64bit) Preview.2024.11.23'
    exe_hash = '7935ea61ff984efcc31cfdfe68936ee3'
    session_offset = 0x322620

@register_sai_api
class SAIv2_API_2024_02_22_c(SAIv2_API_Base):
    # Third build labelled 2024.02.22 (after bc572fd7 and 24875bdd) — another
    # [Saier] bundle, same 5696000 bytes. Offset 0x320420 again, so all three
    # repacks share one structure.
    # Verified with content: a 3000x3000 canvas read back 750x750 (exact 1:4)
    # and rendered a greyscale half-body sketch signed "Rove" — strokes clean,
    # no tearing.
    version_name = 'SAI Ver.2 (64bit) 2024.02.22'
    exe_hash = 'd8d4db49ff3088bfde3a72980d878870'
    session_offset = 0x320420

def _find_pid_by_exe_names_win32(names:set[str]) -> int | None:
    """Locate a process by executable file name using Win32 APIs.

    psutil.process_iter() builds a Process object per pid and costs roughly
    5 ms each on a busy machine (~380 processes -> ~2 s). The GUI calls this
    every time it looks for SAI, so that latency was very visible. The Win32
    pair below does the same job in ~25 ms.
    """
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    arr_len = 4096
    arr = (wintypes.DWORD * arr_len)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        return None

    count = needed.value // ctypes.sizeof(wintypes.DWORD)
    buf = ctypes.create_unicode_buffer(1024)
    size = wintypes.DWORD(1024)

    for i in range(count):
        pid = arr[i]
        if not pid:
            continue
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            size.value = 1024
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                exe_name = buf.value.rsplit('\\', 1)[-1].lower()
                if exe_name in names:
                    return pid
        finally:
            kernel32.CloseHandle(handle)
    return None

def get_pid_by_name(name:str) -> int | None:
    if sys.platform == 'win32':
        pid = _find_pid_by_exe_names_win32({name.lower()})
        if pid is not None:
            return pid
    psutil.process_iter.cache_clear()
    for proc in psutil.process_iter(['pid', 'name']):
        if proc.name() == name:
            return proc.pid
    return None

def find_running_sai_pid() -> int | None:
    names = {SAIv1_API_Base.process_name.lower(), SAIv2_API_Base.process_name.lower()}
    if sys.platform == 'win32':
        pid = _find_pid_by_exe_names_win32(names)
        if pid is not None:
            return pid
    return get_pid_by_name(SAIv1_API_Base.process_name) or get_pid_by_name(SAIv2_API_Base.process_name)

def get_exe_path(proc:AbstractProcess) -> str:
    psproc = psutil.Process(proc.pid)
    if sys.platform == 'win32':
        return psproc.exe()
    else: # linux
        return get_region_data_by_name(proc, psproc.name())[0]

def get_exe_hash(proc:AbstractProcess) -> str | None:
    exe_path = get_exe_path(proc)
    if not exe_path:
        return None
    with open(exe_path, 'rb') as f:
        return hashlib.file_digest(f, 'md5').hexdigest()

def get_exe_hash_by_pid(pid:int) -> str | None:
    """按 pid 直接算 exe 的 MD5，不需要先打开进程。

    用在「哈希未命中」这条分支上：那个版本不在支持列表里，但用户的文件指纹
    必须记下来 —— 没有它既没法复现，也没法给他做适配。
    """
    try:
        path = psutil.Process(pid).exe()
        with open(path, 'rb') as f:
            return hashlib.file_digest(f, 'md5').hexdigest()
    except Exception:
        return None

class NoSaiProcessDetected(Exception):
    pass

def get_sai_api_from_pid(pid:int) -> SAI_API_Base | None:
    try:
        with OpenProcess(pid=pid) as proc:
            exe_hash = get_exe_hash(proc)
            return get_sai_api(exe_hash)
    except Exception as e:
        # Keep the original cause: the GUI logs it, and a bare
        # NoSaiProcessDetected() hides whether it was a permission error,
        # a missing exe path or an unsupported hash.
        raise NoSaiProcessDetected() from e

class SAI:
    def __init__(self, override_api:SAI_API_Base=None, pid:int=None):
        """``pid`` 用于「自动探测刚在这个进程上验过参数」的场景。

        不传就自己找；传了就必须用这个 pid —— 探测结果只对那一个进程有效，
        重新 find 一遍有可能拿到另一个 SAI2 实例，参数就对不上了。
        """
        self.proc = None
        if pid is None:
            pid = find_running_sai_pid()
        try:
            self.proc = OpenProcess(pid=pid)
            self.psutil_proc = psutil.Process(pid=pid)
        except:
            raise NoSaiProcessDetected()
        if override_api is None:
            api = get_sai_api_from_pid(pid)
        else:
            api = override_api
        self.api:SAI_API_Base = api(self.proc)

    def __enter__(self):
        return self

    def __exit__(self, *exc_args):
        self.close()

    def close(self):
        if self.proc is not None:
            self.proc.close()

    def is_running(self):
        return self.psutil_proc.is_running()

    def get_pid(self):
        return self.proc.pid

# ---------------------------------------------------------------- 外部版本表
# 加一个 SAI2 版本本来是「改 sai.py → 重新打包 80MB → 让所有人重下」。
# 这个机制让作者发一个 json、用户丢进目录就生效，程序不用换。
#
# 两个位置，后读的覆盖先读的：
#   1. 程序目录下的 sai-versions.json  —— 随包分发，便携版适用
#   2. 用户目录下的 .art-timelapse-versions.json —— 用户自己加的
# 内置表优先级最低：外部 json 可以用同一个 md5 覆盖内置条目（改错参数时不用发版）。

EXTERNAL_SCHEMA = 1

# 字段表里允许的类型。ptr 指向画布自身，要等类建好才能引用，见下。
EXTERNAL_FIELD_TYPES = {
    'int32': ctypes.c_int32,
    'uint32': ctypes.c_uint32,
    'tile_maps': (RPOINTER(RPOINTER(SAIv2_API_Base.SAICanvasTileMap))
                  * SAIv2_API_Base.map_count),
}


def _parse_offset(value) -> int:
    """'0x320420' / 3281920 / '3281920' 都要能认。"""
    if isinstance(value, str):
        text = value.strip()
        return int(text, 16) if text.lower().startswith('0x') else int(text, 10)
    return int(value)


def make_external_version_class(spec: dict):
    """按 json 里的一条定义造出 API 类。字段不合规就抛，调用方跳过这一条。"""
    exe_hash = str(spec.get('hash') or '').strip().lower()
    if not re.fullmatch(r'[0-9a-f]{32}', exe_hash):
        raise ValueError(f'MD5 不合法：{exe_hash!r}')

    offset = _parse_offset(spec.get('offset'))
    if not 0 < offset < 0x10000000:
        raise ValueError(f'offset 超出合理范围：{offset:#x}')

    name = str(spec.get('name') or '').strip() or f'SAI Ver.2 ({exe_hash[:8]})'
    layout = str(spec.get('layout') or 'base').strip().lower()
    fields = spec.get('fields')

    attrs = {
        'version_name': name,
        'exe_hash': exe_hash,
        'session_offset': offset,
    }
    cls = type(f'SAIv2_API_ext_{exe_hash[:8]}', (SAIv2_API_Base,), attrs)

    # base 布局直接用 SAIv2_API_Base 的 SAICanvas —— 绝大多数版本都是这种。
    # new 布局（2026.09.25 那种大重构）字段挪过，必须带上 fields，否则读出来是垃圾。
    if layout == 'new' or fields:
        if not fields:
            raise ValueError('layout=new 必须给出 fields 字段表')
        canvas = type('SAICanvas', (SAIv2CanvasBase,), {})
        prepared = []
        for item in fields:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                raise ValueError(f'字段定义格式不对：{item!r}')
            field_offset = _parse_offset(item[0])
            field_name = str(item[1])
            field_type = str(item[2]).strip().lower()
            if field_type == 'ptr':
                pytype = RPOINTER(canvas)          # 指向画布自身
            elif field_type == 'utf16':
                count = int(item[3]) if len(item) > 3 else 0x104
                pytype = ctypes.c_uint16 * count
            elif field_type in EXTERNAL_FIELD_TYPES:
                pytype = EXTERNAL_FIELD_TYPES[field_type]
            else:
                raise ValueError(f'不认识的字段类型：{field_type!r}')
            prepared.append((field_offset, field_name, pytype))
        canvas._fields_ = offset_fields(prepared)
        cls.SAICanvas = canvas
        # new 布局没有 id 字段（内置类都在 0x20 那行写着 "A canvas ID was here,
        # but seems to be removed now"）。不覆写就会走 SAIv2_API_Base 的版本，
        # 那里比的是 c.id —— 录制循环一调 check_if_canvas_exists 就
        # AttributeError，用户看到的是「一落笔就停止录制」。
        #
        # 这条以前只在嗅探器和内置类里补过，外部版本表这条路径漏了整整一个版本。
        # 2026-11-20 有人用「在 json 里补一个假的 id 字段」绕过：读得到、不抛，
        # 但比的是 0x20 处一个无关的 4 字节，多数时候恒为 0 —— 于是两个画布
        # 永远"相等"，画布关掉也判定为存在，录制会继续读已释放的内存。
        # 比指针（_self_ptr）才是真的身份判定。
        cls.check_if_canvas_exists = SAI_API_Base.check_if_canvas_exists

    return cls


def load_external_versions(paths=None) -> int:
    """读外部版本表 json，注册进 sai_api_lookup。返回实际载入的条数。

    任何问题都只记日志、不抛 —— 一个坏 json 不能让整个程序起不来。
    """
    paths = list(paths) if paths is not None else external_version_paths()
    total = 0
    for path in paths:
        try:
            raw = path.read_text(encoding='utf-8-sig')
        except Exception as exc:
            logging.warning('版本表读不出来（%s）：%s', path, exc)
            continue
        try:
            data = json.loads(raw)
        except Exception as exc:
            logging.warning('版本表不是合法 json（%s）：%s', path, exc)
            continue
        if not isinstance(data, dict) or int(data.get('schema') or 0) != EXTERNAL_SCHEMA:
            logging.warning('版本表格式版本不对（%s，期望 schema=%d）',
                            path, EXTERNAL_SCHEMA)
            continue

        items = data.get('versions')
        if not isinstance(items, list):
            logging.warning('版本表里没有 versions 数组（%s）', path)
            continue

        loaded = 0
        for index, spec in enumerate(items):
            if not isinstance(spec, dict):
                logging.warning('版本表 %s 第 %d 条不是对象，跳过', path, index + 1)
                continue
            try:
                cls = make_external_version_class(spec)
            except Exception as exc:
                logging.warning('版本表 %s 第 %d 条用不了（%s），跳过', path,
                                index + 1, exc)
                continue
            replacement = cls.exe_hash in sai_api_lookup
            sai_api_lookup[cls.exe_hash] = cls
            if replacement:
                logging.info('版本表 %s：覆盖已有条目 %s', path, cls.exe_hash[:8])
            loaded += 1

        logging.info('版本表 %s：载入 %d 条', path, loaded)
        total += loaded
    return total


def external_version_paths() -> list[Path]:
    """外部版本表的搜索顺序（后读的覆盖先读的）。"""
    candidates = []
    try:
        candidates.append(Path(sys.executable).resolve().parent / 'sai-versions.json')
    except Exception:
        pass
    try:
        candidates.append(Path.home() / '.art-timelapse-versions.json')
    except Exception:
        pass
    return [p for p in candidates if p.is_file()]
