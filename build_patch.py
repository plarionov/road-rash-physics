#!/usr/bin/env python3
"""Road Rash port of the 1.2.1 physics, taken from the Road Rash II builder.

Two images:
  physics       vanilla plus the physics
  physics+ti    his v1.1 image plus the physics, without his turn multiply
                and without the disc / mp3 music calls
"""
import argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Filled by configure(). Defaults: ROM and Ti IPS next to this script.
SRC = None
OUT_DIR = None
TI_IPS = None


def configure(rom=None, out=None, ti_ips=None):
    global SRC, OUT_DIR, TI_IPS
    OUT_DIR = Path(out).resolve() if out else HERE
    SRC = Path(rom).resolve() if rom else OUT_DIR / "Road Rash (USA, Europe).gen"
    TI_IPS = (Path(ti_ips).resolve() if ti_ips
              else OUT_DIR / "ROAD_RASH_Improvement_v1.1" / "ROAD_RASH_Improvement_v1.1.ips")
# His code ends near $C3B58 and the rest of the 1MB image is FF. The cave
# sits in that fill. The vanilla image is extended to the same size.
ROM_SIZE = 0x100000
ORG = 0xE0000
STEP = 15
CLAMP = 30
DELTA_CAP = 8

# The sound driver writes $FFFF01F2 on every new sample (a punch, a hit,
# an engine note) and uses $FFFF01F3 and $FFFF01F8 as well. Those addresses
# are the same RAM as $FF01F2. The clock lives above that block.
ACCUM = 0xFF02C0
VBL = 0xFF02C2
LAST = 0xFF02C4
INIT = 0xFF02C6
STEPF = 0xFF02C7
INDONE = 0xFF02C8
NEED = 0xFF02C9

# A 4.4 fixed ratio of 0x10 is stock speed. NTSC scanlines are 262.
RATIO = 0xFF02C0          # byte, 0 until calibrated, then >= $10
ENTRY = 0xFF02C1          # raw V counter, written after registers are saved
HAND = 0xFF02C4           # word, scanlines spent inside the vblank handler
OWED = 0xFF02C6           # word, stock main-thread lines the step must cover
SNAP_VBL = 0xFF02C8
SNAP_HAND = 0xFF02CA
SNAP_V = 0xFF02CC         # raw vertical counter byte
# Lane curvature is applied with a second multiply by the frame length, so a
# 1-vblank frame truncates the bend to zero. These two words batch the curve
# samples until a stock 2-vblank frame has elapsed. Nothing in the game
# references FF03F0 or FF03F2; the table at FF03F6 starts after them.
ACC_D6 = 0xFF03F0
ACC_V = 0xFF03F2
LANE_BANK = 0xFF03F4
# FF03F0-FF03F5 hold game data during a race. FF02E0-FF02EF stay zero.
EST16 = 0xFF02E0             # word, averaged 100% step in 1/16 vblank
EST_USE = 0xFF02E2           # word, 100% step the physics is rebuilt at
CLOUD_REM = 0xFF02E4         # word, carry of the scaled heading sum
CAL_VBL = 0xFF02E6           # word, vblank count when calibration started
HILL_REM = 0xFF02E8          # word, carry of the mountain scroll
HOR_HELD = 0xFF02EA          # word, far road row the background uses
HOR_DIR = 0xFF02EC           # word, +1 or -1, last move of that row
FRAME16 = 0xFF02F4           # word, EST_USE for this step or 0
# FF02F8-FF02FF have no absolute references in the game. The rider counter
# at FF0266 indexes at most eight slots and stops near FF02DE. The old
# cookie lived at FF03F8, the first pointer of the far-behind list, so a
# rider dropping back restarted the measure in the middle of a race.
RECHECK = 0xFF02FC
IN_VBL = 0xFF02FD           # set for the whole vblank callback
CALIB_HOLD = 0xFF02F8       # bit 0: Up+C is held; bit 1: backdrop is ours
CALIB_MARK = 0xFF02F9       # announcement frame, 0 when idle
CALIB_COLOR = 0xFF02FA
# Remembered upward speed of the player while a climb is held on the road.
# 50(a1) is copied into the shadow, so the speed cannot live there.
KICK = 0xFF02FA      # word, backdrop saved for the announcement
# Road scratch, rewritten every build. STEP is the slope the next row
# still has to climb. FIX is added once to the foreground raw height so
# the rider stays on the original curve; the polygon itself is not moved.
ROAD_FIX = 0xFF02F2
ROAD_STEP = 0xFF02FE
# VBlank of the rider step that just ran. The race loop does not wait, so
# once Ti's frame fits in one interrupt the loop steps the bikes again.
STEP_VBL = 0xFF02F0
# VBlank on which the in-race pause last ran the measure. FF369A is cleared
# on the way into pause, so it cannot be the "we are paused" signal.
PAUSE_VBL = 0xFF02F6
# Second rider's own camera offset. The stock word is shared, so player 2's
# view was shifted by whatever player 1 had just added.
P2_CAM = 0xFF02F4
# What the physics hooks scale by: the race frame against a stock 2-vblank
# frame, not the CPU speed. The race now steps once per frame it hands to
# the vblank, so a 30 fps frame is stock at any clock and only a 1-vblank
# frame (Ti's 60 fps) is twice as short. Bit 7 follows the calibration.
FRAME_RATIO = 0xFF02F4
# Vblanks the same race step would have taken at 100%, or 0 when the step
# runs as is. The physics hooks rebuild heading, sideways step and road
# bend at that length, then spread the step over the real frame.
FRAME_DT = 0xFF02F4
# Estimate from the step before: main-thread lines * CPU ratio + handler
# lines, rounded up to whole vblanks, never below the 2-vblank DMA floor.
STOCK_EST = 0xFF02F5
# Iterations the calibration loop finishes in two vblanks at 7.67 MHz,
# measured in the race after a savestate load. The live count divided by
# this is how much faster the CPU is. 15..17 in the 4.4 ratio collapses
# back to stock, so a 100% run does not add delay.
# Same spin as the RR2 port. Both games spend the vblank on a full-frame
# DMA, and 15..17 in the 4.4 ratio still collapses to a stock step.
STOCK_LINES = 1391         # calib iterations in one 100% NTSC frame, measured
PHYS_DONE_AT = 0xE001C0


class Asm:
    def __init__(self, org):
        self.org = org
        self.here = org
        self.buf = bytearray()
        self.labels = {}
        self.fixups = []

    def label(self, name):
        self.labels[name] = self.here

    def emit(self, data):
        self.buf += data
        self.here += len(data)

    def w(self, value):
        self.emit((value & 0xFFFF).to_bytes(2, "big"))

    def l(self, value):
        self.emit((value & 0xFFFFFFFF).to_bytes(4, "big"))

    def abs_w(self, opcode, addr):
        self.w(opcode)
        self.l(addr)

    def bcc(self, cond, name):
        # Bcc.w. Displacement is relative to the extension word.
        self.w(cond << 8)
        at = len(self.buf)
        self.w(0)
        self.fixups.append((at, name, self.org + at))
        self.here += 0  # w() already advanced here

    def dbf(self, reg, name):
        # DBcc always carries a 16-bit displacement from the extension word.
        self.w(0x51C8 + reg)
        at = len(self.buf)
        self.w(0)
        self.fixups.append((at, name, self.org + at))

    def finish(self):
        for at, name, pc_of_disp in self.fixups:
            target = self.labels[name]
            disp = (target - pc_of_disp) & 0xFFFF
            self.buf[at:at + 2] = disp.to_bytes(2, "big")
        return bytes(self.buf)


def build_code(org, ti=False):
    a = Asm(org)

    # ---- vblank entry, replaces 10 bytes at 0x21B6 ----
    # Count the interrupt the CPU actually took, then run the original handler.
    # A short-circuit for "already inside vblank" freezes the EA logo: the
    # handler later drops IPL to 3, the pending interrupt re-enters, and that
    # re-entry never returns to the game.
    a.label("vbl_entry")
    a.abs_w(0x5279, VBL)             # addq.w #1, vbl_counter
    a.w(0x48E7)
    a.w(0xFFFE)                      # movem.l d0-d7/a0-a6, -(sp)
    if ti:
        # Ti inserts the Sega CD bus request immediately after the movem.
        a.w(0x08F9)
        a.w(0x0008)
        a.l(0xA12000)                # bset #8, $A12000
        a.w(0x4EF9)
        a.l(0x21C2)
    else:
        a.abs_w(0x2079, 0xFF0226)    # movea.l callback, a0
        a.w(0x4EF9)
        a.l(0x21C0)                  # jmp original handler

    # ---- start of the race frame, jsr from 0x12D7C ----
    a.label("frame_start")
    if not ti:
        a.w(0x33FC)
        a.w(0x8134)
        a.l(0xC00004)                # display off, original first instruction
    a.abs_w(0x3239, 0xC00004)        # d1 = VDP status
    a.w(0x0801)
    a.w(0x0000)                      # btst #0, d1
    a.w(0x56C2)                      # sne d2  (PAL -> $FF)
    # A loaded savestate can hold $FF here. Only a marker we wrote counts.
    a.w(0x0C39)
    a.w(0x00A5)
    a.l(INIT)                        # cmpi.b #$A5, init
    a.bcc(0x67, "inited")
    a.w(0x13FC)
    a.w(0x00A5)
    a.l(INIT)                        # move.b #$A5, init
    a.abs_w(0x4279, ACCUM)           # clr.w accum
    a.abs_w(0x4239, STEPF)
    a.abs_w(0x4239, NEED)
    a.w(0x33F9)
    a.l(VBL)
    a.l(LAST)                        # last = counter, drop the pre-race gap
    a.w(0x7001)                      # moveq #1, d0
    a.bcc(0x60, "got_delta")
    a.label("inited")
    a.abs_w(0x3239, LAST)            # move.w last, d1
    a.abs_w(0x3039, VBL)             # move.w counter, d0
    a.w(0x33C0)
    a.l(LAST)                        # last = snapshot, a nested tick stays for next time
    a.w(0x9041)                      # sub.w d1, d0
    a.w(0x0C40)
    a.w(DELTA_CAP)                   # cmpi.w #8, d0
    a.bcc(0x63, "delta_ok")          # bls
    a.w(0x7001)                      # a pause between races is one frame
    a.label("delta_ok")
    a.label("got_delta")
    a.w(0x4A40)                      # tst.w d0
    a.bcc(0x67, "no_time")           # beq
    a.w(0x7200)                      # moveq #0, d1
    a.w(0x3200)                      # move.w d0, d1
    a.w(0x4A02)                      # tst.b d2
    a.bcc(0x66, "pal_units")
    a.w(0xC2FC)
    a.w(5)                           # mulu.w #5, d1
    a.bcc(0x60, "add_units")
    a.label("pal_units")
    a.w(0xC2FC)
    a.w(6)                           # mulu.w #6, d1
    a.label("add_units")
    a.abs_w(0xD379, ACCUM)           # add.w d1, accum
    a.label("no_time")
    a.abs_w(0x3039, ACCUM)
    a.w(0x0C40)
    a.w(CLAMP)                       # cmpi.w #30, d0
    a.bcc(0x63, "clamped")           # bls
    a.w(0x33FC)
    a.w(CLAMP)
    a.l(ACCUM)
    a.label("clamped")
    a.abs_w(0x4A39, STEPF)           # tst.b step_pending
    a.w(0x4E71)
    a.w(0x4E71)                      # arm a step on every finished frame
    a.abs_w(0x3039, ACCUM)
    a.w(0x0C40)
    a.w(STEP)
    a.w(0x4E71)
    a.w(0x4E71)
    a.w(0x0479)
    a.w(STEP)
    a.l(ACCUM)                       # subi.w #15, accum
    a.abs_w(0x50F9, STEPF)           # st step_pending
    a.abs_w(0x4239, INDONE)          # clr.b input_done
    a.abs_w(0x50F9, NEED)            # one pad sample is due for this step
    a.label("frame_done")
    if ti:
        a.w(0x4DF9)
        a.l(0xFF0000)                # lea $FF0000, a6  (the instruction we replaced)
    a.w(0x4E75)                      # rts

    if ti:
        # Ti inlined the HUD clock. Tick it once per physics step, the same
        # rate a finished frame has without overclock, then leave a0 on the
        # clock word for the rest of his routine.
        a.label("clock_tick")
        a.w(0x41EE)
        a.w(0x09A0)                  # lea $9A0(a6), a0
        a.abs_w(0x4A39, STEPF)       # kept so this site stays put
        a.w(0x4E71)
        a.w(0x4E71)                  # the HUD second follows the frame, as in the unpatched game
        a.abs_w(0x4A39, INDONE)
        a.w(0x4E71)
        a.w(0x4E71)
        a.abs_w(0x50F9, INDONE)
        a.w(0x5250)                  # addq.w #1, (a0)
        a.label("clock_done")
        a.w(0x4E75)
    # The stick and the clock run on every frame callback. Gating them to the
    # physics step left the lean at about a third of the stock value: after
    # 2.2 s of left the unpatched bike was on the stop (-2048) and these
    # builds were near -650. The movement step below stays on the 50 ms clock.
    a.label("input_gate")
    a.abs_w(0x4A39, NEED)
    a.w(0x4E71)
    a.w(0x4E71)                      # was beq input_skip
    a.abs_w(0x4239, NEED)
    if not ti:
        a.w(0x4EB9)
        a.l(0x12CA8)                 # HUD clock, once per step
    a.abs_w(0x4A79, 0xFF2534)
    a.w(0x4E75)
    a.label("input_skip")
    a.w(0x588F)                      # addq.l #4, sp
    a.w(0x4EF9)
    a.l(0x13038)

    # ---- physics gate, jsr from 0x13D76 ----
    # Run the body whenever the render buffer is free, once per finished
    # frame, which is what the stock game does. The 50 ms gate left the
    # skid decay (a quarter off, once per body call) running about ten
    # times a second while this course already finishes a frame every vblank.
    a.label("phys_gate")
    a.w(0x0CAF)
    a.l(0x185F86)
    a.w(4)                           # cmpi.l #$185F86, 4(sp)
    a.bcc(0x66, "phys_setup")
    a.abs_w(0x4A39, STEPF)
    a.bcc(0x67, "phys_skip")         # at most one body call per frame
    a.abs_w(0x2079, 0xFF278A)        # a0 = buffer flag
    a.w(0x4A50)                      # tst.w (a0)
    a.bcc(0x66, "phys_skip")         # buffer not consumed yet
    a.w(0x2EBC)                      # move.l #phys_done, (sp)
    a.l(0)
    done_at = len(a.buf) - 4
    a.abs_w(0x2079, 0xFF278A)
    a.w(0x4EF9)
    a.l(0x13D7C)
    a.label("phys_setup")
    a.abs_w(0x2079, 0xFF278A)
    a.w(0x4A50)
    a.bcc(0x66, "phys_skip")
    a.w(0x2EBC)                      # move.l #setup_done, (sp)
    a.l(0)
    setup_at = len(a.buf) - 4
    a.abs_w(0x2079, 0xFF278A)
    a.w(0x4EF9)
    a.l(0x13D7C)
    a.label("phys_skip")
    a.w(0x588F)
    a.w(0x4E75)
    a.label("phys_done")
    a.w(0x4EB9)
    count_at = len(a.buf)
    a.l(0)                           # jsr count_step, same size as clr.b
    a.w(0x4E75)
    a.label("setup_done")
    a.w(0x4E75)
    a.label("count_step")
    a.abs_w(0x4239, STEPF)
    a.w(0x5239)
    a.l(0xFF01FE)                    # steps completed, one byte
    a.w(0x4E75)
    a.buf[done_at:done_at + 4] = a.labels["phys_done"].to_bytes(4, "big")
    a.buf[setup_at:setup_at + 4] = a.labels["setup_done"].to_bytes(4, "big")
    a.buf[count_at:count_at + 4] = a.labels["count_step"].to_bytes(4, "big")

    code = a.finish()
    if ORG + len(code) > 0x200000:
        raise SystemExit("patch does not fit")
    return code, a.labels


def header_checksum(rom):
    """Sega header word at 0x18E: sum of big-endian words from 0x200 to EOF."""
    total = 0
    for i in range(0x200, len(rom), 2):
        total = (total + int.from_bytes(rom[i:i + 2], "big")) & 0xFFFF
    return total


def internal_checksum(rom):
    """The sum at $FF8F8. It stops at the routine, skipping the header word."""
    total = 0
    addr = 0
    for _ in range(0x3FE3E):
        if addr == 0x18C:
            addr += 4
        else:
            total = (total + int.from_bytes(rom[addr:addr + 4], "big")) & 0xFFFFFFFF
            addr += 4
    return total


def apply_ips(rom, path):
    data = Path(path).read_bytes()
    if data[:5] != b"PATCH" or data[-3:] != b"EOF":
        raise SystemExit(f"not an IPS: {path}")
    i = 5
    while i < len(data) - 3:
        off = int.from_bytes(data[i:i + 3], "big")
        i += 3
        ln = int.from_bytes(data[i:i + 2], "big")
        i += 2
        if ln == 0:
            rle = int.from_bytes(data[i:i + 2], "big")
            i += 2
            rom[off:off + rle] = bytes([data[i]]) * rle
            i += 1
        else:
            rom[off:off + ln] = data[i:i + ln]
            i += ln


def ips_record(offset, data):
    if offset > 0xFFFFFF:
        raise SystemExit(f"offset too big for IPS: {offset:#x}")
    out = offset.to_bytes(3, "big")
    out += len(data).to_bytes(2, "big")
    out += data
    return out


def write_ips(original, rom, path):
    ranges = []
    start = None
    n = min(len(original), len(rom))
    for i in range(n):
        if original[i] != rom[i]:
            if start is None:
                start = i
            end = i + 1
        elif start is not None:
            ranges.append((start, end))
            start = None
    if start is not None:
        ranges.append((start, n))
    if len(rom) > len(original):
        ranges.append((len(original), len(rom)))
    ips = bytearray(b"PATCH")
    records = 0
    for start, end in ranges:
        at = start
        while at < end:
            chunk = rom[at:min(at + 0xFFFF, end)]
            ips += ips_record(at, bytes(chunk))
            at += len(chunk)
            records += 1
    ips += b"EOF"
    path.write_bytes(ips)
    return records, len(ips)


def build_base(org, cd=False, fps_tick=False):
    """Keep the original once-per-frame step, and stretch it when the CPU
    finishes the main-thread work sooner than a 7.67 MHz machine would.

    The render slices already take the same real time at any clock. Only the
    main-thread part of $13D76 scales with overclock, so the next step waits
    until the time since this step started covers that work at stock speed.
    """
    a = Asm(org)

    a.label("vbl_entry")
    # The interrupted instruction's registers have to be on the stack before
    # anything here uses d0. Saving them afterwards put the scanline into the
    # slot the game expected to get back, and the race loop never returned.
    a.w(0x48E7)
    a.w(0xFFFE)                      # movem.l d0-d7/a0-a6, -(sp)
    # His upload lowers IPL while a VDP command is still being written.
    # Reading the control port here throws away the half-written word, and
    # the next store lands on the pointer block as E8560007. Leave the
    # pending bit for vbl_leave, which reads the port only after the
    # callback has returned. The interrupted SR is raised so rte does not
    # take the same interrupt again.
    a.abs_w(0x4A39, IN_VBL)          # tst.b in_vbl
    a.bcc(0x67, "vbl_live")          # beq
    a.w(0x006F)                      # ori.w #$0600, 60(sp)  (IPL >= 6)
    a.w(0x0600)
    a.w(60)
    a.w(0x4CDF)
    a.w(0x7FFF)
    a.w(0x4E73)                      # movem / rte
    a.label("vbl_live")
    if fps_tick:
        # Ti's frame counts vblanks here and the FPS drawer samples that
        # flag. This build puts the stock frame back, so the count lives
        # on the vblank entry instead.
        a.abs_w(0x5339, 0xFF1BE7)    # subq.b #1, period
        a.bcc(0x66, "fps_period")
        a.w(0x33FC)
        a.w(0x013C)
        a.l(0xFF1BE6)                # move.w #$13C, period
        a.label("fps_period")
    a.abs_w(0x5279, VBL)             # addq.w #1, vbl
    a.w(0x1039)
    a.l(0xC00008)                    # move.b HV, d0  (V is the high byte)
    a.w(0x13C0)
    a.l(ENTRY)                       # move.b d0, entry
    a.abs_w(0x2079, 0xFF1A62)        # movea.l callback, a0
    a.w(0x08F9)
    a.w(0x0000)
    a.l(IN_VBL)                      # bset #0, in_vbl
    if False:
        # Ti requests the Sega CD bus here and then calls the frame
        # pointer himself. $21C0 sits inside that request. $21CE is the
        # first instruction after his call.
        a.w(0x08F9)
        a.w(0x0008)
        a.l(0xA12000)                # bset #8, $A12000
        # While the callback is still the intro, the driver runs every
        # vblank and the song runs ahead. The race frame is when vanilla
        # stops that. Split pushes one call of its own; solo does not.
        # Keeping the vblank call through the race leaves the fast tempo on.
        a.w(0xB1FC)
        a.l(0xE038)                  # cmpa.l #frame, a0
        a.bcc(0x67, "vbl_frame")     # beq
        a.w(0x4EB9)
        a.l(0x2BCA4)                 # jsr music
        a.label("vbl_frame")
        a.w(0xB1FC)
        a.l(0)                       # cmpa.l #0, a0
        a.bcc(0x67, "vbl_nocall")
        a.w(0x4E90)                  # jsr (a0)
        a.label("vbl_nocall")
        a.w(0x4EF9)
        a.l(0x11DE)
    else:
        a.w(0x4EF9)
        a.l(0x116E)

    # Replaces the movem/rte at $221A. The race upload has already lowered
    # IPL, so a vblank is often waiting. Clear the in-handler flag only
    # after interrupts are masked; otherwise that vblank steps the music
    # and the upload a second time.
    a.label("vbl_leave")
    a.w(0x007C)
    a.w(0x0700)                      # ori.w #$0700, sr
    a.w(0x08B9)
    a.w(0x0000)
    a.l(IN_VBL)                      # bclr #0, in_vbl
    a.w(0x7000)                      # moveq #0, d0
    a.w(0x1039)
    a.l(ENTRY)                       # move.b entry, d0
    a.bcc(0x61, "vline")
    a.w(0x3400)                      # move.w d0, d2
    a.abs_w(0x3039, 0xC00008)
    a.w(0xE048)                      # lsr.w #8, d0
    a.bcc(0x61, "vline")
    a.w(0x9042)                      # sub.w d2, d0
    a.bcc(0x64, "hand_pos")          # bcc: delta did not wrap
    a.w(0x0640)
    a.w(262)                         # addi.w #262, d0
    a.label("hand_pos")
    a.abs_w(0xD179, HAND)            # add.w d0, hand
    a.bcc(0x61, "show_cal")          # bsr: backdrop, after the game's own vblank
    # Do not read $C00004 here. A status read mid-command (main thread or
    # Ti's frame after #$2000) leaves E8560007 in the DMA pointers. The
    # frame already polls the port and clears the pending bit.
    a.w(0x4CDF)
    a.w(0x7FFF)                      # movem.l (sp)+, d0-d7/a0-a6
    a.w(0x4E73)                      # rte

    a.label("phys_gate")
    # The race loop is the only caller. Other callers just run the step.
    a.w(0x0CAF)
    a.l(0xC0B4)
    a.w(4)                           # cmpi.l #return, 4(sp)
    a.bcc(0x66, "phys_setup")
    a.w(0x4EB9)
    calib_at = len(a.buf)
    a.l(0)                           # jsr calib
    a.abs_w(0x1039, RATIO)
    a.w(0x0800)
    a.w(0x0007)                      # btst #7, d0
    a.bcc(0x60, "phys_body")         # the step is timed from race_gate now
    if STOCK_LINES:
        a.w(0x4EB9)
        scale_at = len(a.buf)
        a.l(0)
    a.w(0x4EB9)
    snap_at = len(a.buf)
    a.l(0)
    a.label("phys_body")
    a.w(0x2EBC)
    done_slot = len(a.buf)
    a.l(0)                           # move.l #phys_done, (sp)
    a.w(0x08F9)
    a.w(0x0006)
    a.l(0xFF1F12)                    # bset #6, the flag the hook replaced
    a.w(0x4EF9)
    a.l(0x10E6E)
    a.label("phys_setup")
    a.w(0x588F)                      # addq.l #4, sp
    a.w(0x08F9)
    a.w(0x0006)
    a.l(0xFF1F12)
    a.w(0x4EF9)
    a.l(0x10E6E)
    setup_at = None

    a.label("phys_done")
    if STOCK_LINES:
        a.w(0x4EB9)
        charge_at = len(a.buf)
        a.l(0)
    a.w(0x4E75)
    a.label("setup_done")
    a.w(0x4E75)

    # d0 = raw V -> NTSC scanline. PAL returns 0; the wall clock then counts
    # whole frames only, and calibration refuses to speed-scale PAL.
    a.label("vline")
    a.abs_w(0x1239, 0xA10001)        # move.b version, d1
    a.w(0x0801)
    a.w(0x0006)                      # btst #6, d1
    a.bcc(0x66, "vline_pal")
    a.w(0x0C40)
    a.w(0x00EA)                      # cmpi.w #$EA, d0
    a.bcc(0x63, "vline_done")        # bls
    a.w(0x0440)
    a.w(0x00E5)                      # subi.w #$E5, d0
    a.w(0x0640)
    a.w(235)
    a.bcc(0x60, "vline_done")
    a.label("vline_pal")
    a.w(0x4240)                      # clr.w d0
    a.label("vline_done")
    a.w(0x4E75)

    a.label("snap")
    a.abs_w(0x33F9, VBL)
    a.l(SNAP_VBL)                    # move.w vbl, snap_vbl
    a.abs_w(0x33F9, HAND)
    a.l(SNAP_HAND)
    a.abs_w(0x3039, 0xC00008)
    a.w(0xE048)                      # lsr.w #8, d0
    a.w(0x13C0)
    a.l(SNAP_V)                      # move.b d0, snap_v
    a.w(0x4E75)

    # d0 = real scanlines since the snapshot. Long, never negative.
    a.label("wall")
    a.abs_w(0x3439, VBL)             # move.w vbl, d2
    a.abs_w(0x9479, SNAP_VBL)        # sub.w snap_vbl, d2
    a.w(0x0282)
    a.l(0x0000FFFF)                  # andi.l #$ffff, d2
    a.abs_w(0x1239, 0xA10001)
    a.w(0x0801)
    a.w(0x0006)
    a.bcc(0x66, "wall_pal")
    a.w(0xC4FC)
    a.w(262)                         # mulu.w #262, d2
    a.w(0x2F02)                      # move.l d2, -(sp)
    a.abs_w(0x3039, 0xC00008)
    a.w(0xE048)                      # lsr.w #8, d0
    a.bcc(0x61, "vline")
    a.w(0x3200)                      # move.w d0, d1
    a.w(0x1039)
    a.l(SNAP_V)                      # move.b snap_v, d0
    a.w(0x0240)
    a.w(0x00FF)
    a.bcc(0x61, "vline")
    a.w(0x9240)                      # sub.w d0, d1
    a.w(0x201F)                      # move.l (sp)+, d0
    a.w(0x48C1)                      # ext.l d1
    a.w(0xD081)                      # add.l d1, d0
    a.w(0x4A80)                      # tst.l d0
    a.bcc(0x6A, "wall_done")
    a.w(0x0680)
    a.l(262)
    a.bcc(0x60, "wall_done")
    a.label("wall_pal")
    a.w(0xC4FC)
    a.w(313)                         # mulu.w #313, d2
    a.w(0x2002)                      # move.l d2, d0
    a.label("wall_done")
    a.w(0x4E75)

    a.label("calib")
    # Bit 7 means the timing is committed. Bit 6 means the first race entry
    # has been seen and SNAP_VBL holds that vblank count. Genesis Plus GX
    # keeps the CPU at stock for 100 frames after a load, so the loop waits
    # 160 vblanks and only then counts lines. A savestate with a plain $10
    # has neither bit, so it is timed again.
    # A savestate keeps the ratio of the clock it was recorded on. A new
    # build clears that and measures again, once, after the load.
    # While the race is paused, Up+C (Start released) forgets the stored
    # ratio and runs this same wait and measure.
    a.abs_w(0x4A79, 0xFF3696)        # tst.w pause
    a.bcc(0x67, "calib_cookie")      # beq: not paused
    # The frame already sampled the pad into FF1AD8. Reading the port
    # again returns an empty byte.
    a.abs_w(0x1039, 0xFF1AD8)        # move.b pad, d0
    a.label("calib_chord")
    a.w(0x0200)
    a.w(0x00A1)                      # andi.b #$A1, d0  (Start, Up, C)
    a.w(0x0C00)
    a.w(0x0021)                      # cmpi.b #$21, d0  Up+C, Start up
    a.bcc(0x66, "calib_release")
    a.label("calib_held")
    a.w(0x0839)
    a.w(0x0000)
    a.l(CALIB_HOLD)                  # btst #0, hold
    a.bcc(0x66, "calib_cookie")
    a.w(0x08F9)
    a.w(0x0000)
    a.l(CALIB_HOLD)                  # bset #0, hold
    a.abs_w(0x1039, RATIO)
    a.w(0x0800)
    a.w(0x0007)                      # btst #7, d0
    a.bcc(0x67, "calib_cookie")      # beq: a measure is already running
    a.abs_w(0x4239, RATIO)           # clr.b ratio
    a.abs_w(0x4239, CALIB_MARK)
    a.w(0x08B9)
    a.w(0x0002)
    a.l(CALIB_HOLD)                  # bclr #2, hold
    a.w(0x08B9)
    a.w(0x0003)
    a.l(CALIB_HOLD)                  # bclr #3, hold
    a.bcc(0x61, "crash_scrub")       # the stuck road offset goes with the old clock
    a.bcc(0x60, "calib_cookie")
    a.label("calib_release")
    a.w(0x08B9)
    a.w(0x0000)
    a.l(CALIB_HOLD)                  # bclr #0, hold
    a.label("calib_cookie")
    a.abs_w(0x1039, RECHECK)         # move.b recheck, d0
    a.w(0x0C00)
    a.w(0x005A)                      # cmpi.b #$5A, d0
    a.bcc(0x67, "calib_known")
    a.w(0x13FC)
    a.w(0x005A)
    a.l(RECHECK)                     # move.b #$5A, recheck
    a.abs_w(0x4239, RATIO)           # clr.b ratio
    a.w(0x08B9)
    a.w(0x0002)
    a.l(CALIB_HOLD)                  # bclr #2, hold
    a.w(0x08B9)
    a.w(0x0003)
    a.l(CALIB_HOLD)                  # bclr #3, hold
    a.label("calib_known")
    a.abs_w(0x1039, RATIO)           # move.b ratio, d0
    a.w(0x0800)
    a.w(0x0007)                      # btst #7, d0
    a.bcc(0x66, "calib_done")        # bne
    a.abs_w(0x1239, 0xA10001)
    a.w(0x0801)
    a.w(0x0006)                      # btst #6, d1
    a.bcc(0x66, "calib_pal")
    a.w(0x0800)
    a.w(0x0006)                      # btst #6, d0
    a.bcc(0x66, "calib_wait")
    a.w(0x33F9)
    a.l(VBL)
    a.l(CAL_VBL)                     # move.w vbl, cal_vbl
    a.w(0x13FC)
    a.w(0x0040)
    a.l(RATIO)                       # move.b #$40, ratio
    a.abs_w(0x4239, CALIB_MARK)      # announcement starts when the measure lands
    a.w(0x4E75)
    a.label("calib_wait")
    a.abs_w(0x3039, VBL)             # move.w vbl, d0
    a.abs_w(0x9079, CAL_VBL)         # sub.w cal_vbl, d0
    a.w(0x0C40)
    a.w(160)
    a.bcc(0x65, "calib_done")        # bcs: overclock may still be delayed
    # A sample inside the handler cannot see the next vblank. On the main
    # thread count one frame at a time and return between the two frames.
    # Holding the main thread for the old three-frame spin let the render
    # queue overwrite the road pointers with E8560007.
    a.w(0x0839)
    a.w(0x0000)
    a.l(IN_VBL)                      # btst #0, in_vbl
    a.bcc(0x66, "calib_done")        # bne: this call is the vblank callback
    a.w(0x40E7)                      # move.w sr, -(sp)
    a.w(0x46FC)
    a.w(0x2300)                      # move #$2300, sr
    # Start on a vblank edge, then count one whole frame.
    a.abs_w(0x3839, VBL)             # move.w vbl, d4
    a.w(0x7400)                      # moveq #0, d2
    a.label("calib_sync")
    a.w(0x5282)                      # addq.l #1, d2
    a.w(0x0C82)
    a.l(80000)
    a.bcc(0x62, "calib_abort")
    a.abs_w(0x3039, VBL)
    a.w(0xB044)                      # cmp.w d4, d0
    a.bcc(0x67, "calib_sync")
    a.w(0x3800)                      # move.w d0, d4
    a.w(0x7400)                      # moveq #0, d2
    a.label("calib_spin")
    a.w(0x5282)                      # addq.l #1, d2
    a.w(0x0C82)
    a.l(80000)
    a.bcc(0x62, "calib_abort")       # bhi: this call never saw a vblank
    a.abs_w(0x3039, VBL)
    a.w(0xB044)                      # cmp.w d4, d0
    a.bcc(0x67, "calib_spin")
    a.abs_w(0x33C2, STEP_VBL)        # save total
    a.label("calib_commit")
    a.w(0x08B9)
    a.w(0x0002)
    a.l(CALIB_HOLD)                  # bclr #2, hold
    a.w(0x08B9)
    a.w(0x0003)
    a.l(CALIB_HOLD)                  # bclr #3, hold
    a.w(0x46DF)                      # move.w (sp)+, sr
    a.w(0x7400)                      # moveq #0, d2
    a.abs_w(0x3439, STEP_VBL)        # move.w total, d2
    a.bcc(0x60, "calib_ratio")
    a.label("calib_abort")
    a.w(0x08B9)
    a.w(0x0002)
    a.l(CALIB_HOLD)
    a.w(0x08B9)
    a.w(0x0003)
    a.l(CALIB_HOLD)
    a.w(0x46DF)                      # move.w (sp)+, sr
    a.w(0x4E75)
    a.label("calib_ratio")
    # d2 = measured iterations. ratio = measured * 16 / stock.
    a.w(0x0C82)
    a.l(500)
    a.bcc(0x65, "calib_done")        # bcs: the vblank counter did not move
    a.w(0x0C82)
    a.l(40000)
    a.bcc(0x62, "calib_done")        # bhi
    a.w(0x2002)                      # move.l d2, d0
    a.w(0xE988)                      # lsl.l #4, d0
    a.w(0x80FC)
    a.w(STOCK_LINES)                 # divu.w #stock, d0
    a.w(0x0C40)
    a.w(15)
    a.bcc(0x65, "ratio_hi")          # bcs: below the stock band
    a.w(0x0C40)
    a.w(17)
    a.bcc(0x62, "ratio_hi")          # bhi
    a.w(0x303C)
    a.w(16)                          # a near-stock reading adds no delay
    a.bcc(0x60, "ratio_store")
    a.label("ratio_hi")
    a.w(0x0C40)
    a.w(255)
    a.bcc(0x63, "ratio_store")       # bls
    a.w(0x303C)
    a.w(255)
    a.label("ratio_store")
    a.w(0x0000)
    a.w(0x0080)                      # ori.b #$80, d0
    a.w(0x13C0)
    a.l(RATIO)
    a.w(0x08B9)
    a.w(0x0002)
    a.l(CALIB_HOLD)                  # bclr #2, hold
    a.bcc(0x60, "calib_mark")
    a.label("calib_pal")
    a.w(0x13FC)
    a.w(0x0090)
    a.l(RATIO)                       # PAL keeps the original step
    a.label("calib_mark")
    a.w(0x13FC)
    a.w(0x0001)
    a.l(CALIB_MARK)                  # move.b #1, mark
    a.bcc(0x61, "crash_scrub")
    if STOCK_LINES == 0:
        a.abs_w(0x33C2, OWED)        # d2 is the NTSC line count; PAL left it alone
    else:
        a.abs_w(0x4279, OWED)
    a.label("calib_done")
    a.w(0x4E75)
    a.label("spin_abort")
    a.bcc(0x61, "spin_show")         # bsr
    a.bcc(0x60, "calib_done")        # bra
    a.label("spin_show")
    a.w(0x4A46)                      # tst.w d6
    a.bcc(0x67, "spin_kept")         # beq: solo, nothing was hidden
    a.abs_w(0x33C6, 0xFF0304)        # move.w d6, two players
    a.label("spin_kept")
    a.w(0x4E75)
    # A crash can leave FF36E0 and the rider speed set. The road then
    # scrolls under a bike that is already still. Calibration drops both,
    # for the two human riders only, and only while the crash bit is set.
    a.label("crash_scrub")
    a.w(0x2F09)                      # move.l a1, -(sp)
    a.abs_w(0x42B9, 0xFFD340)        # clr.l camera offset
    a.w(0x43F9)
    a.l(0xFF1EAA)
    a.bcc(0x61, "scrub_one")
    a.w(0x225F)                      # move.l (sp)+, a1
    a.w(0x4E75)
    a.label("scrub_one")
    a.w(0x0829)
    a.w(0x0004)
    a.w(0x0068)                      # btst #4, 104(a1)
    a.bcc(0x67, "scrub_done")
    a.w(0x4269)
    a.w(18)                          # clr.w 18(a1)
    a.w(0x42A9)
    a.w(0x0026)                      # clr.l 38(a1)
    a.w(0x08A9)
    a.w(0x0004)
    a.w(0x0068)                      # bclr #4, 104(a1)
    a.label("scrub_done")
    a.w(0x4E75)

    # Loading waits call the frame from the main thread. The in-race pause
    # clears FF369A and later skips the race, so call calib once per vblank
    # here as well. The sample itself returns between its two frames.
    a.label("pause_gate")
    a.abs_w(0x4A79, 0xFF3696)        # tst.w pause
    a.bcc(0x67, "pause_race")
    a.abs_w(0x3039, VBL)             # move.w vbl, d0
    a.abs_w(0xB079, PAUSE_VBL)
    a.bcc(0x67, "pause_seen")
    a.abs_w(0x33C0, PAUSE_VBL)
    a.w(0x4EB9)
    pause_gate_at = len(a.buf)
    a.l(0)                           # jsr calib
    a.label("pause_seen")
    a.abs_w(0x4A79, 0xFF369A)
    a.bcc(0x67, "pause_race")        # beq: the fade still steps the race
    a.w(0x4EF9)
    a.l(0x7E8B4)                     # the loop tail, race not run
    a.label("pause_race")
    a.w(0x4EF9)
    a.l(0x7E864)
    # The race loop does not wait for the vblank. Riders step at $C0AE and
    # only $C160 checks whether the next draw buffer is free, so a fast CPU
    # ran the whole race several times per frame with the same FF304A.
    # Skip the race until the vblank has taken the last buffer.
    a.label("race_gate")
    a.w(0x2F08)                      # move.l a0, -(sp)
    a.abs_w(0x2079, 0xFF38B4)        # movea.l draw buffer, a0
    a.w(0x4A50)                      # tst.w (a0)
    a.bcc(0x67, "race_free")
    a.w(0x205F)                      # move.l (sp)+, a0
    a.w(0x588F)                      # addq.l #4, sp  race not run
    a.w(0x4E75)
    a.label("race_free")
    a.w(0x205F)
    a.abs_w(0x1039, RATIO)           # move.b ratio, d0
    a.w(0x0800)
    a.w(0x0007)                      # btst #7, d0
    a.bcc(0x67, "race_plain")
    a.w(0x0200)
    a.w(0x007F)                      # andi.b #$7F, d0
    a.w(0x0C00)
    a.w(16)
    a.bcc(0x67, "race_plain")        # stock clock: the step is the original
    a.w(0x7000)                      # moveq #0, d0
    a.abs_w(0x3039, 0xFF304A)        # move.w frame, d0
    a.w(0xE948)                      # lsl.w #4, d0
    a.abs_w(0xB079, EST_USE)         # cmp.w est, d0
    a.bcc(0x64, "race_plain")        # bcc: a 100% frame would not be longer
    a.abs_w(0x3039, EST_USE)
    a.bcc(0x60, "race_dt")
    a.label("race_plain")
    a.w(0x7000)
    a.label("race_dt")
    a.abs_w(0x33C0, FRAME16)         # move.w d0, frame16
    # Time the whole step, race and draw, from here to the rts of $C00C.
    a.bcc(0x61, "snap")
    a.w(0x0CAF)
    a.l(0x7E88A)
    a.w(4)                           # cmpi.l #loop, 4(sp)
    a.bcc(0x66, "race_go")           # another caller: not timed
    a.w(0x2F7C)
    race_end_at = len(a.buf)
    a.l(0)
    a.w(4)                           # move.l #race_end, 4(sp)
    a.label("race_go")
    a.w(0x0C79)
    a.w(0x0020)
    a.l(0xFF1B26)                    # cmpi.w #$20, the instruction replaced
    a.w(0x4E75)                      # flags survive rts for the bmi at $C014

    a.label("race_end")
    a.w(0x48E7)
    a.w(0xE000)                      # movem.l d0-d2, -(sp)
    a.abs_w(0x1039, RATIO)
    a.w(0x0800)
    a.w(0x0007)
    a.bcc(0x67, "end_out")
    # The overclocked frame is CPU-bound, so a 100% frame is this one times
    # the clock ratio. Timing the step from $C00C read a third of that at 300%.
    a.w(0x7200)
    a.abs_w(0x1239, RATIO)
    a.w(0x0241)
    a.w(0x007F)                      # andi.w #$7F, d1
    a.w(0x7000)
    a.abs_w(0x3039, 0xFF304A)        # move.w frame, d0
    a.w(0xC0C1)                      # mulu.w d1, d0  sixteenths of a vblank
    a.w(0x0C80)
    a.l(160)
    a.bcc(0x63, "end_cap")           # bls
    a.w(0x203C)
    a.l(160)                         # ten vblanks at most
    a.label("end_cap")
    # A rounded estimate that flips between two lengths swaps the heading
    # and the hill steps with it. Average over about four steps.
    a.abs_w(0x3239, EST16)           # move.w avg, d1
    a.bcc(0x66, "end_blend")
    a.w(0x3200)                      # move.w d0, d1  first sample
    a.bcc(0x60, "end_have")
    a.label("end_blend")
    a.w(0x3401)                      # move.w d1, d2
    a.w(0xD241)
    a.w(0xD242)                      # d1 = 3 * avg
    a.w(0xD240)
    a.w(0xE449)                      # lsr.w #2, d1
    a.label("end_have")
    a.abs_w(0x33C1, EST16)
    a.w(0x3001)                      # move.w d1, d0
    a.w(0x0C40)
    a.w(32)
    a.bcc(0x64, "end_lo")
    a.w(0x7020)                      # moveq #32, d0
    a.label("end_lo")
    a.w(0x0C40)
    a.w(160)
    a.bcc(0x63, "end_store")
    a.w(0x303C)
    a.w(160)                         # move.w #160, d0
    a.label("end_store")
    a.abs_w(0x33C0, EST_USE)
    a.label("end_out")
    a.w(0x4CDF)
    a.w(0x0007)                      # movem.l (sp)+, d0-d2
    a.w(0x4EF9)
    a.l(0x7E88A)

    a.label("pause_frame")
    a.w(0x4EB9)
    a.l(0xBA76)                      # jsr frame
    a.w(0x4EB9)
    pause_jsr = len(a.buf)
    a.l(0)                           # jsr calib
    a.w(0x4E75)
    race_jsr = None

    # The race DMA-loads all 64 colors at the start of this vblank, and the
    # picture covers the backdrop, so color 0 alone never shows. Paint the
    # whole palette after that load. The next vblank loads the real colors
    # again before this runs, so stopping the paint restores the picture.
    # A faint yellow tint through the 160-frame wait, then one short light
    # flash per 100%.
    a.label("show_cal")
    a.abs_w(0x1039, RATIO)           # move.b ratio, d0
    a.w(0x0800)
    a.w(0x0007)                      # btst #7, d0
    a.bcc(0x66, "show_ann")
    a.w(0x0800)
    a.w(0x0006)                      # btst #6, d0
    a.bcc(0x67, "show_ret")          # beq: idle
    a.w(0x323C)
    a.w(0x0044)                      # move.w #$0044, d1  faint yellow
    a.bcc(0x61, "tint")
    a.w(0x4E75)
    a.label("show_ann")
    a.w(0x7200)                      # moveq #0, d1
    a.abs_w(0x1239, CALIB_MARK)      # move.b mark, d1
    a.bcc(0x67, "show_ret")          # beq: d0 still holds the ratio
    a.w(0x0240)
    a.w(0x007F)                      # andi.w #$7F, d0
    a.w(0xE848)                      # lsr.w #4, d0
    a.bcc(0x66, "show_cnt")
    a.w(0x7001)                      # moveq #1, d0
    a.label("show_cnt")
    a.w(0x0C40)
    a.w(10)
    a.bcc(0x63, "show_cap")          # bls
    a.w(0x700A)                      # moveq #10, d0
    a.label("show_cap")
    a.w(0xC0FC)
    a.w(20)                          # mulu.w #20, d0
    a.w(0xB240)                      # cmp.w d0, d1
    a.bcc(0x64, "show_fin")          # bcc: announcement is over
    a.w(0x82FC)
    a.w(20)                          # divu.w #20, d1
    a.w(0x4841)                      # swap d1  (remainder)
    a.w(0x0C41)
    a.w(4)
    a.bcc(0x65, "show_on")           # bcs: four frames lit of every twenty
    # Pause does not DMA the palette back, so a white frame would stick
    # and the blinks would only show up after unpausing.
    a.bcc(0x61, "unpaint")
    a.abs_w(0x5239, CALIB_MARK)      # addq.b #1, mark
    a.w(0x4E75)
    a.label("show_on")
    a.w(0x323C)
    a.w(0x0666)                      # move.w #$0666, d1  light flash
    a.bcc(0x61, "tint")
    a.abs_w(0x5239, CALIB_MARK)      # addq.b #1, mark
    a.w(0x4E75)
    a.label("show_fin")
    a.bcc(0x61, "unpaint")
    a.abs_w(0x4239, CALIB_MARK)
    a.w(0x08B9)
    a.w(0x0001)
    a.l(CALIB_HOLD)                  # bclr #1, hold
    a.label("show_ret")
    a.w(0x4E75)
    # The race palette with d1 ORed into every color. The picture stays
    # readable; only its brightness and hue change.
    a.label("tint")
    a.abs_w(0x2079, 0xFF305A)        # movea.l palette, a0
    a.w(0xB1FC)
    a.l(0)
    a.bcc(0x67, "paint")             # no palette yet: plain color
    a.w(0x23FC)
    a.l(0xC0000000)
    a.l(0x00C00004)
    a.w(0x343C)
    a.w(63)
    a.label("tint_loop")
    a.w(0x3018)                      # move.w (a0)+, d0
    a.w(0x8041)                      # or.w d1, d0
    a.abs_w(0x33C0, 0xC00000)        # move.w d0, cram
    a.dbf(2, "tint_loop")
    a.w(0x4E75)
    a.label("paint")
    a.w(0x23FC)
    a.l(0xC0000000)                  # CRAM address 0, write
    a.l(0x00C00004)
    a.w(0x343C)
    a.w(63)                          # move.w #63, d2
    a.label("paint_loop")
    a.abs_w(0x33C1, 0xC00000)        # move.w d1, cram
    a.dbf(2, "paint_loop")
    a.w(0x4E75)
    # Copy the race palette the game DMA'd from. Writing CRAM directly left
    # the last white frame in place for the whole pause.
    a.label("unpaint")
    a.abs_w(0x2079, 0xFF305A)        # movea.l palette, a0
    a.w(0xB1FC)
    a.l(0)                           # cmpa.l #0, a0
    a.bcc(0x67, "show_ret")          # beq
    a.w(0x23FC)
    a.l(0xC0000000)
    a.l(0x00C00004)
    a.w(0x343C)
    a.w(63)                          # move.w #63, d2
    a.label("unpaint_loop")
    a.w(0x33D8)
    a.l(0x00C00000)                  # move.w (a0)+, cram
    a.dbf(2, "unpaint_loop")
    a.w(0x4E75)

    if STOCK_LINES:
        # OWED arrives as the previous step's stock cost. Leave 1..16 there:
        # 16 means this frame lasted at least as long as that cost.
        a.label("scale_now")
        a.abs_w(0x3239, OWED)        # move.w owed, d1
        a.bcc(0x67, "scale_full")    # beq
        a.w(0x3F01)                  # move.w d1, -(sp)
        a.w(0x4EB9)
        wall_abs = len(a.buf)
        a.l(0)                       # jsr wall, d0 = lines since the last step
        a.w(0x7400)                  # moveq #0, d2
        a.w(0x341F)                  # move.w (sp)+, d2
        a.w(0xB082)                  # cmp.l d2, d0
        a.bcc(0x64, "scale_full")    # bcc: this frame was not shorter than stock
        a.w(0xE988)                  # lsl.l #4, d0
        a.w(0x80C2)                  # divu.w d2, d0
        a.w(0x4A40)                  # tst.w d0
        a.bcc(0x66, "scale_store")   # bne
        a.w(0x7001)                  # moveq #1, d0
        a.bcc(0x60, "scale_store")
        a.label("scale_full")
        a.w(0x7010)                  # moveq #16, d0
        a.label("scale_store")
        a.abs_w(0x33C0, OWED)        # move.w d0, owed
        a.w(0x4E75)

        # d0.l signed *= owed/16. 0 and 16+ leave d0 alone (full vanilla step).
        a.label("scale_delta")
        a.w(0x2F01)                  # move.l d1, -(sp)
        a.abs_w(0x3239, OWED)        # move.w owed, d1
        a.bcc(0x67, "delta_pop")
        a.w(0x0C41)
        a.w(16)
        a.bcc(0x64, "delta_pop")     # bcc: full step, d0 stays
        a.w(0x2F02)                  # move.l d2, -(sp)
        a.w(0x3401)                  # move.w d1, d2
        a.w(0x4A80)                  # tst.l d0
        a.bcc(0x6A, "delta_pos")
        a.w(0x4480)                  # neg.l d0
        a.w(0x3F3C)
        a.w(1)
        a.bcc(0x60, "delta_mul")
        a.label("delta_pos")
        a.w(0x4267)                  # clr.w -(sp)
        a.label("delta_mul")
        a.w(0x2200)                  # move.l d0, d1
        a.w(0x4841)                  # swap d1
        a.w(0xC2C2)                  # mulu.w d2, d1
        a.w(0x4841)
        a.w(0x4241)                  # clr.w d1
        a.w(0xC0C2)                  # mulu.w d2, d0
        a.w(0xD081)                  # add.l d1, d0
        a.w(0xE888)                  # lsr.l #4, d0
        a.w(0x4A5F)                  # tst.w (sp)+
        a.bcc(0x67, "delta_nosign")
        a.w(0x4480)                  # neg.l d0
        a.label("delta_nosign")
        a.w(0x241F)                  # move.l (sp)+, d2
        a.label("delta_pop")
        a.w(0x221F)                  # move.l (sp)+, d1
        a.w(0x4E75)
        # Crash camera. Bit 4 keeps FF36E0 and either adds the frame
        # step or pulls the offset back by a quarter. The bike itself has
        # already stopped updating, so the road keeps sliding under it
        # until that offset is gone. A straight throw never stores one.
        # On an overclock, a stopped bike drops the offset now.
        a.label("cam_idle")
        a.w(0x7200)                  # moveq #0, d1
        a.abs_w(0x1239, FRAME_RATIO)       # move.b ratio, d1
        a.w(0x0801)
        a.w(0x0007)                  # btst #7, d1
        a.bcc(0x67, "cam_go")
        a.w(0x0241)
        a.w(0x007F)
        a.w(0x0C41)
        a.w(16)
        a.bcc(0x67, "cam_go")        # beq: stock camera
        a.abs_w(0x4A79, 0xFF1EBC)    # tst.w player speed
        a.bcc(0x66, "cam_go")
        a.abs_w(0x42B9, 0xFFD340)    # clr.l skid
        a.w(0x7000)                  # moveq #0, d0
        a.w(0x7201)                  # moveq #1, d1
        a.w(0x4E75)
        a.label("cam_go")
        a.w(0x7200)                  # moveq #0, d1
        a.w(0x4E75)
        # 38(a1) = speed * frame length, then the bike advances by it.
        # After a crash, bit 4 stays set until the riding state returns,
        # and that state returns only once speed hits zero. Friction is
        # not applied in between, so a sideways hit leaves the speed in
        # and the road keeps scrolling under a rider who is already back
        # on the bike. A straight hit often does not. On an overclock,
        # a human who is on the wheels again and is not holding gas drops
        # that speed. Running back (bit 3 of 105) still uses it.
        a.label("coast_stop")
        a.w(0x7200)
        a.abs_w(0x1239, FRAME_RATIO)
        a.w(0x0801)
        a.w(0x0007)
        a.bcc(0x67, "coast_mul")
        a.w(0x0241)
        a.w(0x007F)
        a.w(0x0C41)
        a.w(16)
        a.bcc(0x67, "coast_mul")
        a.w(0x0829)
        a.w(0x0004)
        a.w(0x0068)                  # btst #4, 104(a1)
        a.bcc(0x67, "coast_mul")
        a.w(0x0829)
        a.w(0x0003)
        a.w(0x0069)                  # btst #3, 105(a1)
        a.bcc(0x66, "coast_mul")     # bne: still off the bike
        a.w(0xB3FC)
        a.l(0xFF1EAA)
        a.bcc(0x66, "coast_mul")     # opponent keeps the original coast
        a.abs_w(0x1239, 0xFF1AD8)
        a.label("coast_gas")
        a.w(0x0801)
        a.w(0x0004)                  # btst #4, d1  gas
        a.bcc(0x66, "coast_mul")
        a.w(0x4240)                  # clr.w d0
        a.w(0x4269)
        a.w(18)                      # clr.w 18(a1)
        a.label("coast_mul")
        a.abs_w(0x3439, 0xFF304A)    # move.w frame, d2
        a.w(0xC0C2)                  # mulu.w d2, d0
        a.w(0x2340)
        a.w(0x0026)                  # move.l d0, 38(a1)
        a.w(0x4E75)

        a.label("scale_sub")
        a.bcc(0x61, "cam_idle")
        a.w(0x4A41)                  # tst.w d1
        a.bcc(0x66, "scale_sub_done")
        a.bcc(0x61, "scale_delta")
        a.w(0x91B9)
        a.l(0xFFD340)                # sub.l d0, skid
        a.label("scale_sub_done")
        a.w(0x4E75)
        a.label("scale_add")
        a.bcc(0x61, "cam_idle")
        a.w(0x4A41)                  # tst.w d1
        a.bcc(0x66, "scale_add_done")
        a.bcc(0x61, "scale_delta")
        a.w(0xD1B9)
        a.l(0xFFD340)                # add.l d0, skid
        a.label("scale_add_done")
        a.w(0x4E75)

        # Player 2's camera call. Keep his offset out of the shared word
        # while his view is built, then put player 1's word back.
        a.label("cam_p2")
        a.w(0x2F39)
        a.l(0xFFD340)                # move.l skid, -(sp)
        a.w(0x23F9)
        a.l(P2_CAM)
        a.l(0xFFD340)                # move.l p2_cam, skid
        a.w(0x4EB9)
        a.l(0x12AB6)                 # jsr camera
        a.w(0x23F9)
        a.l(0xFFD340)
        a.l(P2_CAM)                  # move.l skid, p2_cam
        a.w(0x23DF)
        a.l(0xFFD340)                # move.l (sp)+, skid
        a.w(0x4E75)

        # His 1P digits sit in the window buffer. A partial upload never
        # sends that buffer, so write the same two cells straight to VRAM.
        # a2 is the VDP control port and a1 the data port, as in his frame.
        a.label("fps_full")
        a.bcc(0x61, "fps_poke")
        a.w(0x4EF9)
        a.l(0x12FA4)
        a.label("fps_part")
        a.bcc(0x61, "fps_poke")
        a.w(0x4EF9)
        a.l(0x12FD2)
        a.label("fps_poke")
        a.abs_w(0x4A79, 0xFF022A)
        a.bcc(0x66, "fps_ret")
        a.abs_w(0x4A79, 0xFF753E)    # SHOW FPS
        a.bcc(0x67, "fps_ret")
        a.w(0x24BC)
        a.l(0x7D1A0000)              # VRAM $3D1A
        a.w(0x22B9)
        a.l(0xFFEAC8)                # move.l digits, (a1)
        a.label("fps_ret")
        a.w(0x4E75)

        a.label("charge")
        a.abs_w(0x1039, RATIO)
        a.w(0x0800)
        a.w(0x0007)                  # btst #7, d0
        a.bcc(0x67, "charge_bail")   # beq: timing not committed yet
        a.bcc(0x60, "charge_run")
        a.label("charge_bail")
        a.w(0x4E75)
        a.label("charge_run")
        a.w(0x4EB9)
        wall_chg = len(a.buf)
        a.l(0)
        a.w(0x2400)                  # move.l d0, d2
        a.abs_w(0x3039, HAND)
        a.abs_w(0x9079, SNAP_HAND)   # sub.w snap_hand, d0
        a.w(0x0280)
        a.l(0x0000FFFF)
        a.w(0x9480)                  # sub.l d0, d2
        a.bcc(0x6A, "main_pos")
        a.w(0x7400)                  # moveq #0, d2
        a.label("main_pos")
        a.w(0x7200)
        a.w(0x1239)
        a.l(RATIO)                   # move.b ratio, d1
        a.w(0x0241)
        a.w(0x007F)                  # andi.w #$7F, d1  (drop the committed bit)
        a.w(0x2002)                  # move.l d2, d0
        a.w(0xC0C1)                  # mulu.w d1, d0
        a.w(0xE888)                  # lsr.l #4, d0
        a.w(0x33C0)
        a.l(OWED)
        a.w(0x4E75)

        # Heading is (speed * vblanks >> 8) * yaw >> 8, and the sideways step
        # multiplies (heading - curve) by that speed term again. At 100% a
        # race step usually takes three vblanks, so a shorter overclocked
        # frame loses turn and the slide threshold. Rebuild both at the
        # 100% frame length. d1 is the speed term $11412 multiplies by.
        a.label("scale_hdg")
        a.abs_w(0x4A79, FRAME16)     # tst.w frame16
        a.bcc(0x67, "hdg_plain")
        a.w(0x3229)
        a.w(0x0012)                  # move.w 18(a1), d1
        a.abs_w(0xC2F9, FRAME16)     # mulu.w frame16, d1
        a.w(0xE889)                  # lsr.l #4, d1
        a.w(0xE089)                  # lsr.l #8, d1
        a.w(0x2001)                  # move.l d1, d0
        a.w(0xC1E9)
        a.w(0x0042)                  # muls.w 66(a1), d0
        a.label("hdg_plain")
        a.w(0xE080)                  # asr.l #8, d0
        a.w(0x3340)
        a.w(0x000C)                  # move.w d0, 12(a1)
        a.w(0x4E75)

        # d0 is the sideways step of a 100% frame. 34(a1) keeps that for the
        # lean and the rider contacts. The position takes dt/stock of it,
        # and the remainder waits in 126(a1).
        a.label("lat_step")
        a.w(0x3340)
        a.w(0x0022)                  # move.w d0, 34(a1)
        a.abs_w(0x4A79, FRAME16)
        a.bcc(0x67, "lat_full")
        a.w(0x48E7)
        a.w(0x6000)                  # movem.l d1-d2, -(sp)
        a.abs_w(0xC1F9, 0xFF304A)    # muls.w frame, d0
        a.w(0xE980)                  # asl.l #4, d0
        a.w(0x3229)
        a.w(0x007E)                  # move.w 126(a1), d1
        a.w(0x48C1)                  # ext.l d1
        a.w(0xD081)                  # add.l d1, d0
        a.abs_w(0x81F9, FRAME16)     # divs.w frame16, d0
        a.w(0x4840)                  # swap d0
        a.w(0x3340)
        a.w(0x007E)                  # move.w d0, 126(a1)  remainder
        a.w(0x4840)
        a.w(0x4CDF)
        a.w(0x0006)                  # movem.l (sp)+, d1-d2
        a.bcc(0x60, "lat_add")
        a.label("lat_full")
        a.w(0x4269)
        a.w(0x007E)                  # clr.w 126(a1)
        a.label("lat_add")
        a.w(0xD169)
        a.w(0x0000)                  # add.w d0, 0(a1)
        a.w(0x4E75)

        # FF1E5C sums the heading every step and scrolls the sky. A heading
        # of a 100% frame added every overclocked step runs the clouds fast.
        a.label("sky_add")
        a.abs_w(0x4A79, FRAME16)
        a.bcc(0x67, "sky_plain")
        a.w(0x48E7)
        a.w(0xC000)                  # movem.l d0-d1, -(sp)
        a.abs_w(0xC1F9, 0xFF304A)    # muls.w frame, d0
        a.w(0xE980)                  # asl.l #4, d0
        a.abs_w(0x3239, CLOUD_REM)
        a.w(0x48C1)                  # ext.l d1
        a.w(0xD081)                  # add.l d1, d0
        a.abs_w(0x81F9, FRAME16)     # divs.w frame16, d0
        a.w(0x2200)                  # move.l d0, d1
        a.w(0x4841)                  # swap d1
        a.abs_w(0x33C1, CLOUD_REM)
        a.w(0x48C0)                  # ext.l d0
        a.abs_w(0xD1B9, 0xFF1E5C)    # add.l d0, sum
        a.w(0x4CDF)
        a.w(0x0003)                  # movem.l (sp)+, d0-d1
        a.w(0x4E75)
        a.label("sky_plain")
        a.abs_w(0xD1B9, 0xFF1E5C)
        a.w(0x4E75)

        # $E488 drifts the clouds one pixel per race step.
        a.label("cloud_step")
        a.abs_w(0x4A79, FRAME16)
        a.bcc(0x66, "cloud_part")
        a.w(0x06B9)
        a.l(0x10000)
        a.l(0xFF14DC)                # addi.l #$10000, drift
        a.w(0x4E75)
        a.label("cloud_part")
        a.w(0x2F00)                  # move.l d0, -(sp)
        a.w(0x7000)
        a.abs_w(0x3039, 0xFF304A)    # move.w frame, d0
        a.w(0x4840)                  # swap d0
        a.w(0xE988)                  # lsl.l #4, d0  frame16 << 16
        a.abs_w(0x80F9, FRAME16)     # divu.w frame16, d0
        a.w(0x0280)
        a.l(0xFFFF)
        a.abs_w(0xD1B9, 0xFF14DC)    # add.l d0, drift
        a.w(0x201F)
        a.w(0x4E75)

        # $EE68: the near row sits below the camera by the climbed part of
        # this segment's rise, but the rows after it add the next segment's
        # rise. At a segment change the whole road jumps by the difference.
        # At 100% a frame crosses more than a segment and hides it; at 300%
        # the hills saw up and down. Take the climbed part of the next rise,
        # which meets the next frame's rows exactly. d0-d2 are free here.
        a.label("rise_next")
        a.abs_w(0x1039, RATIO)       # move.b ratio, d0
        a.bcc(0x6A, "rise_plain")    # bpl: not committed
        a.w(0x0200)
        a.w(0x007F)
        a.w(0x0C00)
        a.w(16)
        a.bcc(0x67, "rise_plain")
        a.w(0x1629)
        a.w(0x0001)                  # move.b 1(a1), d3  next rise
        a.label("rise_plain")
        a.w(0x4883)                  # ext.w d3
        a.bcc(0x67, "rise_out")
        a.bcc(0x6B, "rise_neg")
        a.w(0xC6C5)                  # mulu.w d5, d3
        a.w(0x4843)                  # swap d3
        a.w(0x4E75)
        a.label("rise_neg")
        a.w(0x4443)
        a.w(0xC6C5)
        a.w(0x4843)
        a.w(0x4443)
        a.label("rise_out")
        a.w(0x4E75)

        # $E088 and $E0EA place the mountain and cloud bands from the far
        # road row FF3274. At 300% that row rounds back and forth by one line
        # and the whole background bobs. A one-line step against the last
        # move is held; a second line or a step the same way goes through.
        a.label("hor_y")
        a.abs_w(0x3039, 0xFF3274)    # move.w far row, d0
        a.abs_w(0x1239, RATIO)
        a.bcc(0x6A, "hor_out")       # bpl: not committed
        a.w(0x0201)
        a.w(0x007F)
        a.w(0x0C01)
        a.w(16)
        a.bcc(0x67, "hor_out")
        a.w(0x3200)                  # move.w d0, d1
        a.abs_w(0x9279, HOR_HELD)    # sub.w held, d1
        a.bcc(0x67, "hor_out")
        a.w(0x0C41)
        a.w(1)
        a.bcc(0x67, "hor_small")
        a.w(0x0C41)
        a.w(0xFFFF)
        a.bcc(0x66, "hor_take")
        a.label("hor_small")
        a.abs_w(0x4A79, HOR_DIR)
        a.bcc(0x67, "hor_take")      # no history yet
        a.abs_w(0xB279, HOR_DIR)     # cmp.w dir, d1
        a.bcc(0x67, "hor_take")      # same direction
        a.abs_w(0x3039, HOR_HELD)    # a one-line reversal keeps the old row
        a.w(0x4E75)
        a.label("hor_take")
        a.w(0x4A41)                  # tst.w d1
        a.bcc(0x6B, "hor_down")
        a.w(0x33FC)
        a.w(1)
        a.l(HOR_DIR)
        a.bcc(0x60, "hor_set")
        a.label("hor_down")
        a.w(0x33FC)
        a.w(0xFFFF)
        a.l(HOR_DIR)
        a.label("hor_set")
        a.abs_w(0x33C0, HOR_HELD)
        a.label("hor_out")
        a.w(0x4E75)

        # $E3EC scrolls the mountains by the player's sideways step. 34(a1)
        # holds the step of a 100% frame, so take the part this frame moved.
        a.label("hill_lat")
        a.abs_w(0x3239, 0xFF1ECC)    # move.w step, d1
        a.abs_w(0x4A79, FRAME16)
        a.bcc(0x67, "hill_lat_out")
        a.w(0x2F00)                  # move.l d0, -(sp)
        a.w(0x3001)                  # move.w d1, d0
        a.abs_w(0xC1F9, 0xFF304A)    # muls.w frame, d0
        a.w(0xE980)                  # asl.l #4, d0
        a.abs_w(0x3239, HILL_REM)
        a.w(0x48C1)                  # ext.l d1
        a.w(0xD081)                  # add.l d1, d0
        a.abs_w(0x81F9, FRAME16)     # divs.w frame16, d0
        a.w(0x3200)                  # move.w d0, d1
        a.w(0x4840)
        a.abs_w(0x33C0, HILL_REM)    # remainder
        a.w(0x201F)
        a.label("hill_lat_out")
        a.w(0x4E75)

        # $2266E writes the slip term to 64(a1) and, separately, a sideways
        # step in d0. Scaling the step throws the bike at the edge of the
        # screen. Only the stored term is scaled, so the turn stays as it
        # was and the 768 fall check can still be reached. 3/2 covers a
        # long stock frame without throwing the rider early.
        a.label("scale_w64")
        a.w(0x7000)                  # moveq #0, d0
        a.abs_w(0x1039, FRAME_RATIO)       # move.b ratio, d0
        a.w(0x0800)
        a.w(0x0007)                  # btst #7, d0
        a.bcc(0x67, "w64_store")
        a.w(0x0240)
        a.w(0x007F)
        a.w(0x0C40)
        a.w(16)
        a.bcc(0x67, "w64_store")
        a.w(0x4A42)                  # tst.w d2
        a.bcc(0x6A, "w64_pos")
        a.w(0x4442)                  # neg.w d2
        a.w(0x3F3C)
        a.w(1)
        a.bcc(0x60, "w64_mul")
        a.label("w64_pos")
        a.w(0x4267)                  # clr.w -(sp)
        a.label("w64_mul")
        a.w(0xC4C0)                  # mulu.w d0, d2
        a.w(0xE88A)                  # lsr.l #4, d2
        # 3/2 brings the player's own fall in line with a stock frame.
        # Other bikes already reach the 768 gate from a shove or a kick
        # that never connects when they get the full clock scale, so they
        # keep only half of it.
        a.w(0xB3FC)
        a.l(0xFF1EAA)                # cmpa.l #player, a1
        a.bcc(0x67, "w64_player")    # beq player
        a.w(0xE28A)                  # lsr.l #1, d2   opponents: half
        a.bcc(0x60, "w64_sign")
        a.label("w64_player")
        a.w(0x2002)                  # move.l d2, d0
        a.w(0xE288)                  # lsr.l #1, d0
        a.w(0xD480)                  # add.l d0, d2   * 3/2
        a.label("w64_sign")
        a.w(0x4A5F)                  # tst.w (sp)+
        a.bcc(0x67, "w64_store")
        a.w(0x4442)                  # neg.w d2
        a.label("w64_store")
        a.w(0x3342)
        a.w(0x0040)                  # move.w d2, 64(a1)
        a.w(0x3001)                  # move.w d1, d0
        a.w(0x4E75)

        # The road bend is (speed * vblanks >> 8) * curve step, shifted down
        # twice. A one-vblank frame halves the first factor and truncates
        # small bends to zero, and batching them made the push pulse when
        # the frame alternates between one and two vblanks. Feed a one-vblank
        # frame the speed of a stock two-vblank frame instead, every frame.
        a.label("scale_lane")
        a.w(0x2F00)                  # move.l d0, -(sp)
        a.w(0x2F02)                  # move.l d2, -(sp)
        a.w(0x2229)
        a.w(0x0026)                  # move.l 38(a1), d1
        a.abs_w(0x3039, FRAME16)     # move.w frame16, d0
        a.bcc(0x67, "lane_plain")
        a.w(0x3229)
        a.w(0x0012)                  # move.w 18(a1), d1
        a.w(0xC2C0)                  # mulu.w d0, d1  speed of a 100% frame
        a.w(0xE889)                  # lsr.l #4, d1
        a.label("lane_plain")
        a.w(0xE089)                  # lsr.l #8, d1
        a.w(0xC3C6)                  # muls.w d6, d1
        a.w(0xE081)
        a.w(0xC3FC)
        a.w(0x0300)
        a.w(0xE081)
        a.label("lane_out")
        a.w(0x241F)                  # move.l (sp)+, d2
        a.w(0x201F)                  # move.l (sp)+, d0
        a.w(0x4E75)

        # $1E712 adds the whole hill step on the frame a segment boundary is
        # crossed, then $1E894 copies that into the drawn height. At 300% the
        # frames between crossings draw nothing, so a rise or a jump pops.
        # Spread the segment's height across the distance actually traveled.
        # The lane step is left alone. 100% still executes the original add.
        # 126(a1) is the unused last word of a rider slot and holds the
        # fractional unit so the pieces add back up to the original step.
        a.label("height_add")
        a.w(0x2F02)                  # move.l d2, -(sp)
        a.w(0x7000)                  # moveq #0, d0
        a.abs_w(0x1039, FRAME_RATIO)       # move.b ratio, d0
        a.w(0x0800)
        a.w(0x0007)                  # btst #7, d0
        a.bcc(0x67, "height_plain")
        a.w(0x0240)
        a.w(0x007F)
        a.w(0x0C40)
        a.w(16)
        a.bcc(0x67, "height_plain")
        a.w(0xB3FC)
        a.l(0xFF1EAA)                # cmpa.l #first rider, a1
        a.bcc(0x65, "height_plain")  # blo
        a.w(0xB3FC)
        a.l(0xFF3000)                # cmpa.l #past last rider, a1
        a.bcc(0x64, "height_plain")  # bhs
        a.w(0x2429)
        a.w(0x0026)                  # move.l 38(a1), d2  distance
        a.w(0x4842)                  # swap d2
        a.w(0x4A42)                  # tst.w d2
        a.bcc(0x66, "height_plain")  # bne: more than one segment, keep the lump
        a.w(0x4842)                  # swap d2
        a.w(0x3029)
        a.w(0x0006)                  # move.w 6(a1), d0  position inside the segment
        a.w(0xB042)                  # cmp.w d2, d0
        a.bcc(0x65, "height_cross")  # bcs: this frame finished a segment
        a.w(0x102C)
        a.w(0x0001)                  # move.b 1(a4), d0  slope of the current segment
        a.w(0x4880)                  # ext.w d0
        a.w(0x3202)                  # move.w d2, d1
        a.bcc(0x61, "hmul")
        a.bcc(0x60, "height_out")
        a.label("height_cross")
        a.w(0x3202)                  # move.w d2, d1
        a.w(0x9240)                  # sub.w d0, d1  distance still on the old slope
        a.w(0x3F00)                  # move.w d0, -(sp)  distance already on the new one
        a.w(0x3007)                  # move.w d7, d0  old slope
        a.bcc(0x61, "hmul")
        a.w(0x321F)                  # move.w (sp)+, d1
        a.w(0x102C)
        a.w(0x0001)                  # move.b 1(a4), d0
        a.w(0x4880)                  # ext.w d0
        a.bcc(0x61, "hmul")
        a.bcc(0x60, "height_out")
        a.label("height_plain")
        a.w(0xDF69)
        a.w(0x003A)                  # add.w d7, 58(a1)
        a.label("height_out")
        a.w(0x241F)                  # move.l (sp)+, d2
        a.w(0x3029)
        a.w(0x0006)                  # move.w 6(a1), d0
        a.w(0x4E75)
        # d0 = signed slope, d1 = distance. Fold both into 58(a1).
        a.label("hmul")
        a.w(0x3401)                  # move.w d1, d2
        a.w(0xE24A)                  # lsr.w #1, d2
        a.w(0xC5C0)                  # muls.w d0, d2
        a.w(0xD482)                  # add.l d2, d2
        a.w(0x0801)
        a.w(0x0000)                  # btst #0, d1
        a.bcc(0x67, "hmul_even")
        a.w(0x3200)                  # move.w d0, d1
        a.w(0x48C1)                  # ext.l d1
        a.w(0xD481)                  # add.l d1, d2
        a.label("hmul_even")
        a.w(0x7200)                  # moveq #0, d1
        a.w(0x3229)
        a.w(0x007E)                  # move.w 126(a1), d1
        a.w(0xD481)                  # add.l d1, d2
        a.w(0x3342)
        a.w(0x007E)                  # move.w d2, 126(a1)
        a.w(0x4842)                  # swap d2
        a.w(0xD569)
        a.w(0x003A)                  # add.w d2, 58(a1)
        a.w(0x4E75)

        # $1E778 already adds (slope * position in the segment) on top of
        # 58(a1). The spread above puts that same partial into 58(a1), so the
        # bike climbs and then drops by a whole segment every crossing. When
        # the spread is active, draw 58(a1) alone. 60(a1) still keeps the
        # original partial. A multi-segment frame keeps the original sum.
        a.label("height_join")
        a.w(0x3341)
        a.w(0x003C)                  # move.w d1, 60(a1)
        a.w(0x7000)                  # moveq #0, d0
        a.abs_w(0x1039, FRAME_RATIO)
        a.w(0x0800)
        a.w(0x0007)                  # btst #7, d0
        a.bcc(0x67, "join_keep")
        a.w(0x0240)
        a.w(0x007F)
        a.w(0x0C40)
        a.w(16)
        a.bcc(0x67, "join_keep")
        a.w(0xB3FC)
        a.l(0xFF1EAA)
        a.bcc(0x65, "join_keep")     # blo
        a.w(0xB3FC)
        a.l(0xFF3000)
        a.bcc(0x64, "join_keep")     # bhs
        a.w(0x2029)
        a.w(0x0026)                  # move.l 38(a1), d0
        a.w(0x4840)                  # swap d0
        a.w(0x4A40)                  # tst.w d0
        a.bcc(0x66, "join_keep")     # bne: more than one segment
        a.w(0x7200)                  # moveq #0, d1
        a.label("join_keep")
        a.w(0xD269)
        a.w(0x003A)                  # add.w 58(a1), d1
        a.w(0x4E75)

        # $1E7F8. FF098A is cleared every call and rewritten only when the
        # slope increases, to the height ahead of the bike. Using that as
        # the launch base on overclock lifts the bike off the road: the
        # shadow comes back and the climb steers as if the wheels are light.
        # 58(a1) is the height already spread along the slope. Stock still
        # reads FF098A, because at 100% that word matches the original curve.
        a.label("curve_base")
        a.w(0x7000)                  # moveq #0, d0
        a.abs_w(0x1039, FRAME_RATIO)
        a.w(0x0800)
        a.w(0x0007)
        a.bcc(0x67, "curve_stock")
        a.w(0x0240)
        a.w(0x007F)
        a.w(0x0C40)
        a.w(16)
        a.bcc(0x67, "curve_stock")
        a.w(0x3029)
        a.w(0x003A)                  # move.w 58(a1), d0
        a.w(0x4E75)
        a.label("curve_stock")
        a.abs_w(0x3039, 0xFF1E92)    # move.w FF098A, d0
        a.w(0x4E75)

        # $1E82C. Distance/speed on a 2-vblank frame is often 0, and the
        # game then forces one full frame of flight. That lift clears the
        # road while it is still rising, so the bike is airborne for a hop:
        # the lateral step is skipped, heading keeps integrating, and a held
        # turn falls off. If the road has started to fall, a kick saved on
        # the previous climb frame is larger than the new slope's kick and
        # replaces it, so the crest still leaves the ground. Stock is the
        # original store and divide. d5 is restored by the caller.
        a.label("kick_keep")
        a.w(0x3340)
        a.w(0x002C)                  # move.w d0, 44(a1)
        a.w(0x7000)
        a.abs_w(0x1039, FRAME_RATIO)
        a.w(0x0800)
        a.w(0x0007)
        a.bcc(0x67, "kick_div")
        a.w(0x0240)
        a.w(0x007F)
        a.w(0x0C40)
        a.w(16)
        a.bcc(0x67, "kick_div")
        a.w(0xB3FC)
        a.l(0xFF1EAA)
        a.bcc(0x66, "kick_div")      # bne: only the player carries a kick
        a.abs_w(0x3039, KICK)        # move.w kick, d0
        a.bcc(0x67, "kick_div")
        a.abs_w(0x4279, KICK)        # clr.w kick
        a.w(0x3A29)
        a.w(0x003A)                  # move.w 58(a1), d5
        a.w(0x9A69)
        a.w(0x0002)                  # sub.w 2(a1), d5
        a.bcc(0x6A, "kick_div")      # bpl: road has not fallen
        a.w(0xB069)
        a.w(0x002C)                  # cmp.w 44(a1), d0
        a.bcc(0x6F, "kick_div")      # ble
        a.w(0x3340)
        a.w(0x002C)                  # move.w d0, 44(a1)
        a.label("kick_div")
        a.w(0x88C3)                  # divu.w d3, d4
        a.w(0x4E75)

        # $1E872. Above, with the road still rising or flat, draw the road
        # and do not set the airborne bit. 44(a1) stays, and the player's
        # kick is copied aside for the crest. A falling road takes the air
        # path. The original compare is kept for stock and for a bike that
        # is already flying.
        a.label("stick")
        a.w(0x0829)
        a.w(0x0000)
        a.w(0x0068)                  # btst #0, 104(a1)
        a.bcc(0x66, "stick_cmp")
        a.w(0x7000)
        a.abs_w(0x1039, FRAME_RATIO)
        a.w(0x0800)
        a.w(0x0007)
        a.bcc(0x67, "stick_cmp")
        a.w(0x0240)
        a.w(0x007F)
        a.w(0x0C40)
        a.w(16)
        a.bcc(0x67, "stick_cmp")
        a.w(0x3002)                  # move.w d2, d0
        a.w(0x9041)                  # sub.w d1, d0
        a.bcc(0x6F, "stick_down")    # ble: curve is not above the road
        a.w(0x3629)
        a.w(0x003A)                  # move.w 58(a1), d3
        a.w(0x9669)
        a.w(0x0002)                  # sub.w 2(a1), d3
        a.bcc(0x6B, "stick_fly")     # bmi: the road fell
        a.w(0x3001)                  # move.w d1, d0
        a.bcc(0x61, "damp")
        a.w(0xB3FC)
        a.l(0xFF1EAA)
        a.bcc(0x66, "stick_hold")
        a.w(0x3029)
        a.w(0x002C)                  # move.w 44(a1), d0
        a.abs_w(0x33C0, KICK)        # move.w d0, kick
        a.label("stick_hold")
        a.w(0x4269)
        a.w(0x002E)                  # clr.w 46(a1)
        a.w(0x4EF9)
        a.l(0x116E0)               # return, leaving 44(a1) intact
        a.label("stick_cmp")
        a.w(0xB242)                  # cmp.w d2, d1
        a.bcc(0x6A, "stick_down")
        a.label("stick_fly")
        a.w(0xB3FC)
        a.l(0xFF1EAA)
        a.bcc(0x66, "stick_air")
        a.abs_w(0x4279, KICK)
        a.label("stick_air")
        a.w(0x4EF9)
        a.l(0x1168E)
        a.label("stick_down")
        a.w(0xB3FC)
        a.l(0xFF1EAA)
        a.bcc(0x66, "stick_go")
        a.abs_w(0x4279, KICK)
        a.label("stick_go")
        a.w(0x4EF9)
        a.l(0x116AA)

        # $1A3C0 and the copy at $1A5C4. The climbed fraction of this
        # segment's slope is a sawtooth on every strip. Holding the rows
        # at one screen height snaps the near edge when the segment
        # changes. The foreground stays put and the unclimbed remainder
        # goes to the next row, which meets the row after it. The rider's
        # raw height gets that fraction back once, so the bike does not
        # bob. Stock still negates into d3. Returns past the vanilla
        # block (return address + $1C). d0 and d2 are free; d5 is only read.
        a.label("road_slope")
        a.w(0x4883)                  # ext.w d3
        a.w(0x3003)                  # move.w d3, d0  full slope
        a.bcc(0x67, "rs_part")
        a.bcc(0x6B, "rs_neg")
        a.w(0xC6C5)                  # mulu.w d5, d3
        a.w(0x4843)                  # swap d3
        a.bcc(0x60, "rs_part")
        a.label("rs_neg")
        a.w(0x4443)                  # neg.w d3
        a.w(0xC6C5)
        a.w(0x4843)
        a.w(0x4443)                  # neg.w d3  signed partial
        a.label("rs_part")
        a.abs_w(0x33C3, 0xFF1E5A)    # move.w d3, partial
        a.w(0x3400)                  # move.w d0, d2
        a.w(0x9443)                  # sub.w d3, d2  remaining = full - partial
        a.w(0x4443)                  # neg.w d3     vanilla foreground = -partial
        a.w(0x7200)                  # moveq #0, d1
        a.abs_w(0x1239, FRAME_RATIO)       # move.b ratio, d1
        a.w(0x0801)
        a.w(0x0007)
        a.bcc(0x67, "rs_exit")
        a.w(0x0241)
        a.w(0x007F)
        a.w(0x0C41)
        a.w(16)
        a.bcc(0x67, "rs_exit")
        a.abs_w(0x33C2, ROAD_STEP)   # remainder goes to the next row
        a.abs_w(0x3239, 0xFF1E5A)    # move.w partial, d1
        a.abs_w(0x33C1, ROAD_FIX)    # rider raw gets it once
        a.w(0x7600)                  # moveq #0, d3  foreground stays put
        a.label("rs_exit")
        a.w(0x2017)                  # move.l (sp), d0
        a.w(0x0680)
        a.l(0x1C)                    # addi.l #$1C, d0
        a.w(0x2E80)                  # move.l d0, (sp)
        a.w(0x4E75)

        # Bottom of the strip loop. Stock adds the slope just read.
        # Overclock adds the saved step, keeps this slope for the next
        # row, and on the first row only puts the climbed fraction back
        # into the raw height the rider uses. The polygon already used d3.
        a.label("slope_add")
        a.w(0x4882)                  # ext.w d2
        a.w(0x7200)
        a.abs_w(0x1239, FRAME_RATIO)
        a.w(0x0801)
        a.w(0x0007)
        a.bcc(0x67, "sa_stock")
        a.w(0x0241)
        a.w(0x007F)
        a.w(0x0C41)
        a.w(16)
        a.bcc(0x67, "sa_stock")
        a.w(0x3228)
        a.w(0xFFFE)                  # move.w -2(a0), d1
        a.abs_w(0xD279, ROAD_FIX)    # add.w fix, d1
        a.w(0x3141)
        a.w(0xFFFE)                  # move.w d1, -2(a0)
        a.abs_w(0x4279, ROAD_FIX)    # clr.w fix  (once)
        a.abs_w(0x3239, ROAD_STEP)   # move.w step, d1
        a.w(0xD641)                  # add.w d1, d3
        a.abs_w(0x33C2, ROAD_STEP)   # move.w d2, step
        a.w(0x4E75)
        a.label("sa_stock")
        a.w(0xD642)                  # add.w d2, d3
        a.w(0x4E75)
        # The loop head is more than 32 KB away, so dbf stays local and
        # an absolute jump returns to the strip. Falling off the dbf
        # returns to the instruction after the hook.
        a.label("slope_step_a")
        a.bcc(0x61, "slope_add")
        a.dbf(0, "sa_more_a")
        a.w(0x4E75)
        a.label("sa_more_a")
        a.w(0x4EF9)
        a.l(0xEEA0)
        a.label("slope_step_b")
        a.bcc(0x61, "slope_add")
        a.dbf(0, "sa_more_b")
        a.w(0x4E75)
        a.label("sa_more_b")
        a.w(0x4EF9)
        a.l(0x14DEE)

        # Two players upload one picture across several vblanks (FF3236).
        # The DMA itself is the same size as the one-player upload and does
        # not get faster when the CPU does. A fast CPU finishes the copy
        # early enough that the whole upload fits in this vblank. A milder
        # overclock does not: the copy takes the first vblank, the DMA the
        # next, so the top of the screen is not overwritten. 100% keeps
        # the original table. 3.0x and above is the one-vblank path.
        # Entered by jmp in place of that table; every exit is a jump.
        a.label("two_dma")
        a.w(0x7200)
        a.abs_w(0x1239, RATIO)
        a.w(0x0801)
        a.w(0x0007)
        a.bcc(0x67, "two_stock")
        a.w(0x0241)
        a.w(0x007F)
        a.w(0x0C41)
        a.w(16)
        a.bcc(0x67, "two_stock")
        a.w(0x0C41)
        a.w(48)
        a.bcc(0x64, "two_fast")      # bcc: 3.0x and above
        a.w(0x0C79)
        a.w(1)
        a.l(0xFF3236)
        a.bcc(0x67, "two_rows")      # second vblank is the DMA
        a.w(0x33FC)
        a.w(1)
        a.l(0xFF3236)
        a.w(0x4EB9)
        a.l(0x13CE2)
        a.w(0x0C79)
        a.w(1)
        a.l(0xFF7A32)
        a.bcc(0x66, "two_half")
        a.abs_w(0x4279, 0xFF7A32)
        a.w(0x203C)
        a.l(0xFA00)
        a.w(0x41F9)
        a.l(0xFF59A6)
        a.w(0x4EB9)
        a.l(0x11BA8)
        a.label("two_half")
        a.w(0x4EF9)
        a.l(0x12FD2)
        a.label("two_fast")
        a.w(0x4EB9)
        a.l(0x13CE2)
        a.w(0x0C79)
        a.w(1)
        a.l(0xFF7A32)                # cmpi.w #1, FF7A32
        a.bcc(0x66, "two_rows")
        a.abs_w(0x4279, 0xFF7A32)
        a.w(0x203C)
        a.l(0xFA00)
        a.w(0x41F9)
        a.l(0xFF59A6)
        a.w(0x4EB9)
        a.l(0x11BA8)
        a.label("two_rows")
        a.abs_w(0x2039, 0xFF27A2)
        a.w(0x323C)
        a.w(0x3000)
        a.w(0x343C)
        a.w(0x0400)
        a.w(0x4EB9)
        a.l(0x17996)
        a.abs_w(0x2039, 0xFF27A2)
        a.w(0x0640)
        a.w(0x0800)
        a.w(0x323C)
        a.w(0x3800)
        a.w(0x343C)
        a.w(0x0340)
        a.w(0x4EB9)
        a.l(0x17996)
        a.abs_w(0x4A79, 0xFF2546)
        a.bcc(0x6B, "two_spr")
        a.abs_w(0x2039, 0xFF305A)
        a.w(0x323C)
        a.w(0)
        a.w(0x343C)
        a.w(0x0040)
        a.w(0x4EB9)
        a.l(0x17A6A)
        a.label("two_spr")
        a.abs_w(0x2039, 0xFF2776)
        a.w(0x323C)
        a.w(0xC000)
        a.w(0x343C)
        a.w(0x0340)
        a.w(0x4EB9)
        a.l(0x17996)
        a.w(0x4EB9)
        a.l(0x13088)
        a.w(0x4EB9)
        a.l(0x12C5E)
        a.abs_w(0x4279, 0xFF3236)
        a.w(0x4EF9)
        a.l(0x12FD2)
        a.label("two_stock")
        a.abs_w(0x3039, 0xFF3236)
        a.w(0xE540)
        a.w(0x41F9)
        a.l(0x12E5E)
        a.w(0x4EF0)
        a.w(0)

        # Restart is a soft reset: $200 sees $A1000C and skips the RAM
        # clear, then jumps here. A one-player race left in RAM is then
        # still there when two players start, and the second view scrolls
        # with the first. Wipe work RAM. A committed ratio and its cookie
        # stay, so the clock is not measured again. The reset stack lives
        # at $FFFFF6 and is not touched.
        a.label("soft_clr")
        a.abs_w(0x1439, RATIO)       # move.b ratio, d2
        a.w(0x0802)
        a.w(0x0007)                  # btst #7, d2
        a.bcc(0x66, "soft_cookie")
        a.w(0x7400)                  # moveq #0, d2
        a.label("soft_cookie")
        a.abs_w(0x1C39, RECHECK)     # move.b recheck, d6
        a.w(0x0C06)
        a.w(0x005A)
        a.bcc(0x67, "soft_wipe")
        a.w(0x7C00)                  # moveq #0, d6
        a.label("soft_wipe")
        a.w(0x7000)                  # moveq #0, d0
        a.w(0x41F9)
        a.l(0xFF0000)                # lea FF0000, a0
        a.w(0x323C)
        a.w(0x007B)                  # 124 longs, through FF01EF
        a.label("soft_lo")
        a.w(0x20C0)                  # move.l d0, (a0)+
        a.dbf(1, "soft_lo")
        a.w(0x4218)                  # clr.b (a0)+  FF01F0
        a.w(0x4218)                  # clr.b (a0)+  FF01F1
        a.w(0x4218)                  # clr.b (a0)+  sound sample byte
        a.w(0x4218)                  # clr.b (a0)+  FF01F3
        a.w(0x720A)                  # moveq #10, d1
        a.label("soft_mid")
        a.w(0x4218)
        a.dbf(1, "soft_mid")         # FF01F4..FF01FE
        a.w(0x4218)                  # clr.b (a0)+  FF01FF, a0 = FF0200
        a.w(0x323C)
        a.w(0x3F7C)                  # longs through FFFFF3
        a.label("soft_hi")
        a.w(0x20C0)
        a.dbf(1, "soft_hi")
        a.w(0x4218)                  # FFFFF4
        a.w(0x4218)                  # FFFFF5
        a.w(0x13C6)
        a.l(RECHECK)                 # move.b d6, recheck
        a.abs_w(0x13C2, RATIO)       # move.b d2, ratio
        a.w(0x4EF9)
        a.l(0x7E6A2)                 # soft reset continues here

        # Drawn height follows the road, then a crest or the end of a jump
        # reverses it in one frame. That reads as the rider bobbing. On a
        # reversal, take half of the step; a climb that keeps its direction
        # is stored whole. 125(a1) remembers the direction. Stock applies
        # the whole step. d0 is the target height.
        a.label("damp")
        a.w(0x3229)
        a.w(0x0002)                  # move.w 2(a1), d1
        a.w(0x9041)                  # sub.w d1, d0   delta
        a.w(0x3F00)                  # move.w d0, -(sp)
        a.w(0x7000)
        a.abs_w(0x1039, FRAME_RATIO)
        a.w(0x0800)
        a.w(0x0007)
        a.bcc(0x67, "damp_full")
        a.w(0x0240)
        a.w(0x007F)
        a.w(0x0C40)
        a.w(16)
        a.bcc(0x67, "damp_full")
        a.w(0x1029)
        a.w(0x007D)                  # move.b 125(a1), d0
        a.w(0x0C00)
        a.w(1)
        a.bcc(0x67, "damp_opp")
        a.w(0x0C00)
        a.w(0x00FF)
        a.bcc(0x66, "damp_full")
        a.label("damp_opp")
        a.w(0x4880)                  # ext.w d0
        a.w(0x3217)                  # move.w (sp), d1
        a.w(0xB141)                  # eor.w d1, d0
        a.bcc(0x6A, "damp_full")     # bpl: same direction
        a.w(0x3217)
        a.w(0xE241)                  # asr.w #1, d1
        a.w(0x3E81)                  # move.w d1, (sp)
        a.label("damp_full")
        a.w(0x301F)                  # move.w (sp)+, d0
        a.w(0x4A40)                  # tst.w d0
        a.bcc(0x67, "damp_add")
        a.bcc(0x6B, "damp_neg")
        a.w(0x137C)
        a.w(0x0001)
        a.w(0x007D)                  # move.b #1, 125(a1)
        a.bcc(0x60, "damp_add")
        a.label("damp_neg")
        a.w(0x137C)
        a.w(0x00FF)
        a.w(0x007D)
        a.label("damp_add")
        a.w(0xD069)
        a.w(0x0002)                  # add.w 2(a1), d0
        a.w(0x3340)
        a.w(0x0002)                  # move.w d0, 2(a1)
        a.w(0x4E75)

        a.label("air_vis")
        a.w(0x2F00)                  # move.l d0, -(sp)
        a.w(0x2F01)                  # move.l d1, -(sp)
        a.w(0x3002)                  # move.w d2, d0
        a.bcc(0x61, "damp")
        a.w(0x221F)                  # move.l (sp)+, d1
        a.w(0x201F)                  # move.l (sp)+, d0
        a.w(0x08E9)
        a.w(0x0000)
        a.w(0x0068)                  # bset #0, 104(a1)
        a.w(0x4E75)

        a.label("ground_vis")
        a.w(0x2F00)
        a.w(0x2F01)
        a.w(0x2F02)                  # move.l d2, -(sp)
        a.w(0x3001)                  # move.w d1, d0
        a.bcc(0x61, "damp")
        a.w(0x241F)                  # move.l (sp)+, d2
        a.w(0x221F)
        a.w(0x201F)
        a.w(0x337C)
        a.w(0x0000)
        a.w(0x0032)                  # move.w #0, 50(a1)
        a.w(0x4E75)

    a.buf[done_slot:done_slot + 4] = a.labels["phys_done"].to_bytes(4, "big")
    a.buf[calib_at:calib_at + 4] = a.labels["calib"].to_bytes(4, "big")
    a.buf[pause_jsr:pause_jsr + 4] = a.labels["calib"].to_bytes(4, "big")
    a.buf[pause_gate_at:pause_gate_at + 4] = a.labels["calib"].to_bytes(4, "big")
    a.buf[snap_at:snap_at + 4] = a.labels["snap"].to_bytes(4, "big")
    a.buf[race_end_at:race_end_at + 4] = a.labels["race_end"].to_bytes(4, "big")
    if STOCK_LINES:
        a.buf[scale_at:scale_at + 4] = a.labels["scale_now"].to_bytes(4, "big")
        a.buf[charge_at:charge_at + 4] = a.labels["charge"].to_bytes(4, "big")
        a.buf[wall_abs:wall_abs + 4] = a.labels["wall"].to_bytes(4, "big")
        a.buf[wall_chg:wall_chg + 4] = a.labels["wall"].to_bytes(4, "big")

    code = a.finish()
    return code, a.labels


def extend(rom):
    if len(rom) < ROM_SIZE:
        rom.extend(b"\xFF" * (ROM_SIZE - len(rom)))


def assemble(kind):
    # base: vanilla plus the physics.
    # ti: his v1.1 image plus the physics. His *6/frame turn is removed,
    # and the calls into the disc / mp3 driver are put back or nopped.
    ti = kind == "ti"
    if kind == "base":
        out_rom = OUT_DIR / "Road Rash (physics).gen"
        out_ips = OUT_DIR / "road-rash-1-physics.ips"
    elif kind == "ti":
        out_rom = OUT_DIR / "Road Rash (physics+ti).gen"
        out_ips = OUT_DIR / "road-rash-1-physics-ti.ips"
    else:
        raise SystemExit(kind)
    vanilla = SRC.read_bytes()
    if len(vanilla) != 0xC0000:
        raise SystemExit(f"source ROM size {len(vanilla):#x}")
    code, labels = build_base(ORG)
    rom = bytearray(vanilla)
    extend(rom)
    if ti:
        apply_ips(rom, TI_IPS)
        extend(rom)
        # His image points VBlank at $C2500. That handler only requests the
        # Sega CD bus, then returns. The counter, the backdrop and the
        # nested acknowledge never run, so the rider step sees a stuck 0
        # and drops every bike. HBlank stays on the original vector.
        if int.from_bytes(rom[0x70:0x74], "big") != 0x145A:
            raise SystemExit("HBlank vector is not the original")
        rom[0x78:0x7C] = (0x1164).to_bytes(4, "big")
    rom[0x1A4:0x1A8] = (ROM_SIZE - 1).to_bytes(4, "big")

    def expect(addr, data):
        got = bytes(rom[addr:addr + len(data)])
        if got != data:
            raise SystemExit(f"{addr:#x}: {got.hex()} != {data.hex()}")

    def patch(addr, data):
        rom[addr:addr + len(data)] = data

    if ti:
        # Disc and mp3 calls go back to the original bytes. The pause
        # flag writes around two of them stay; only the jsr is nopped.
        for addr, n in (
            (0x2FA, 0x10), (0x62B7, 3), (0xA719, 5),
            (0xBF98, 0xA), (0xBFBC, 0xA), (0xC00C, 6),
            (0x7E6A2, 6), (0x7E736, 6), (0x7FF98, 0x4E),
            (0x496C, 0xE), (0x4998, 5), (0x49B0, 0xE),
            (0x49DC, 4), (0x4D5E, 0x11),
        ):
            rom[addr:addr + n] = vanilla[addr:addr + n]
        for addr in (0xBBB4, 0xBE4E):
            rom[addr:addr + 6] = b"\x4E\x71" * 3
        # His turn multiply does not return once the vanilla body is back,
        # so the call site returns to muls/asr as well.
        rom[0x11182:0x11192] = vanilla[0x11182:0x11192]
        expect(0x11412, bytes.fromhex("4EBAFD6E"))
        patch(0x11412, bytes.fromhex("C1C1E080"))
    def jsr(addr, name, nops=0):
        patch(addr, bytes.fromhex("4EB9") + labels[name].to_bytes(4, "big") + b"\x4E\x71" * nops)

    def jmp(addr, name, nops=0):
        patch(addr, bytes.fromhex("4EF9") + labels[name].to_bytes(4, "big") + b"\x4E\x71" * nops)

    expect(0x1164, bytes.fromhex("48E7FFFE207900FF1A62"))
    jmp(0x1164, "vbl_entry", 2)
    expect(0x117A, bytes.fromhex("4CDF7FFF4E73"))
    jmp(0x117A, "vbl_leave")
    expect(0x10E66, bytes.fromhex("08F9000600FF1F12"))
    jsr(0x10E66, "phys_gate", 1)
    expect(0x113D6, bytes.fromhex("E0803340000C"))
    jsr(0x113D6, "scale_hdg")
    expect(0x1143E, bytes.fromhex("33400022D1690000"))
    jsr(0x1143E, "lat_step", 1)
    expect(0x11406, bytes.fromhex("D1B900FF1E5C"))
    jsr(0x11406, "sky_add")
    expect(0xE488, bytes.fromhex("06B90001000000FF14DC"))
    jsr(0xE488, "cloud_step", 2)
    expect(0xE3EC, bytes.fromhex("323900FF1ECC"))
    jsr(0xE3EC, "hill_lat")
    expect(0xEE68, bytes.fromhex("4883670000166B00000AC6C548436000000A4443C6C548434443"))
    jsr(0xEE68, "rise_next", 10)
    expect(0xE088, bytes.fromhex("303900FF3274"))
    jsr(0xE088, "hor_y")
    expect(0xE0EA, bytes.fromhex("303900FF3274"))
    jsr(0xE0EA, "hor_y")
    expect(0x11586, bytes.fromhex("22290026E089C3C6670000044E71E081C3FC0300E081"))
    jsr(0x11586, "scale_lane", 8)
    # The RR3 height, slope, camera, width and coast hooks are not installed.
    # This game multiplies those by FF304A itself, and Ti's 60 fps image
    # only needed the turn. On a frame that alternates between one and
    # two vblanks they switched on and off and shook the bike on hills.
    if False:
        for addr in (0x11552, 0x117C0):
            expect(addr, bytes.fromhex("DF69003A30290006"))
            jsr(addr, "height_add", 1)
        for addr in (0x115C6, 0x11B5C):
            expect(addr, bytes.fromhex("3341003CD269003A"))
            jsr(addr, "height_join", 1)
        expect(0x11610, bytes.fromhex("303900FF1E92"))
        jsr(0x11610, "curve_base")
        expect(0x11644, bytes.fromhex("3340002C88C3"))
        jsr(0x11644, "kick_keep")
        expect(0x11688, bytes.fromhex("B2426A00001E"))
        jmp(0x11688, "stick")
        expect(0x1168E, bytes.fromhex("3342000208E900000068"))
        jsr(0x1168E, "air_vis", 2)
        expect(0x116AA, bytes.fromhex("33410002337C00000032"))
        jsr(0x116AA, "ground_vis", 2)
        expect(0x140B4, bytes.fromhex("334200403001"))
        jsr(0x140B4, "scale_w64")
        coast = bytes.fromhex("343900FF304AC0C223400026")
        for addr in (0x10EBC, 0x11860):
            expect(addr, coast)
            jsr(addr, "coast_stop", 3)
        expect(0xEE68, bytes.fromhex("488367000016"))
        jsr(0xEE68, "road_slope")
        expect(0xEF86, bytes.fromhex("4882D64251C8FF14"))
        jsr(0xEF86, "slope_step_a", 1)
        expect(0xDEC8, bytes.fromhex("91B900FFD340"))
        jsr(0xDEC8, "scale_sub")
        expect(0xDEE8, bytes.fromhex("D1B900FFD340"))
        jsr(0xDEE8, "scale_add")
    for addr in (0xA5F4, 0xA61A):
        expect(addr, bytes.fromhex("4EB90000BA76"))
        jsr(addr, "pause_frame")
    # In-race pause skips the race once FF369A is set. The measure and the
    # Up+C chord run on that branch, on the main thread.
    expect(0x7E850, bytes.fromhex("4A7900FF36966700000C4A7900FF369A66000052"))
    jmp(0x7E850, "pause_gate", 7)
    expect(0xC00C, bytes.fromhex("0C79002000FF1B26"))
    jsr(0xC00C, "race_gate", 1)
    expect(0x360, bytes.fromhex("4EF90007E6A2"))
    jmp(0x360, "soft_clr")
    # The boot sum includes the hooks. On a mismatch the routine turns the
    # display off and loops, which is the black screen. Skip that branch.
    expect(0x7FFD0, bytes.fromhex("6602"))
    patch(0x7FFD0, bytes.fromhex("4E71"))
    pad = bytes(rom[ORG:ORG + len(code)])
    if not pad or pad[0] not in (0x00, 0xFF) or any(b != pad[0] for b in pad):
        raise SystemExit("no free space at the code origin")
    patch(ORG, code)

    if int.from_bytes(rom[0x70:0x74], "big") != 0x145A:
        raise SystemExit("HBlank vector changed")
    if ti and int.from_bytes(rom[0x78:0x7C], "big") != 0x1164:
        raise SystemExit("Ti VBlank vector is not the physics handler")
    rom[0x18E:0x190] = header_checksum(rom).to_bytes(2, "big")
    if header_checksum(rom) != int.from_bytes(rom[0x18E:0x190], "big"):
        raise SystemExit("header checksum does not stick")

    out_rom.write_bytes(rom)
    nranges, nips = write_ips(vanilla, rom, out_ips)
    print(f"{kind} code {ORG:#x}..{ORG + len(code):#x} ({len(code)} bytes)")
    print(f"  rom {out_rom.name}  ips {out_ips.name} ({nips} bytes, {nranges} ranges)")
    print(f"  header checksum {int.from_bytes(rom[0x18E:0x190], 'big'):#06x}")


def main():
    p = argparse.ArgumentParser(description="Build Road Rash physics ROM and IPS")
    p.add_argument("--rom", type=Path, help="vanilla ROM (.gen)")
    p.add_argument("--out", type=Path, help="output directory (default: script directory)")
    p.add_argument("--ti-ips", type=Path, help="Ti Improvement IPS")
    args = p.parse_args()
    configure(args.rom, args.out, args.ti_ips)
    if not SRC.is_file():
        raise SystemExit(f"vanilla ROM not found: {SRC}")
    assemble("base")
    assemble("ti")


if __name__ == "__main__":
    main()
