#!/usr/bin/env python3
"""
preview.py — render datameter screens to plain text (dev tool / docs).

Runs datameter.py inside a pty, replays the ANSI output onto a character
grid, and prints the result. Useful for screenshots in the README and for
checking layout without a real terminal.

  python3 preview.py                # dashboard
  python3 preview.py s              # dashboard, then open settings
  python3 preview.py '?'            # help screen
  python3 preview.py '' 130 48      # custom size (cols rows)
"""
import os
import pty
import re
import select
import sys
import time


def replay(raw: str, cols: int, rows: int) -> str:
    # Prefer pyte (a real terminal emulator) when available for faithful output.
    try:
        import pyte
        screen = pyte.Screen(cols, rows)
        stream = pyte.Stream(screen)
        stream.feed(raw)
        return "\n".join(screen.display[i].rstrip() for i in range(rows))
    except ImportError:
        pass

    grid = [[" "] * cols for _ in range(rows)]
    r = c = 0
    i = 0

    def put(ch):
        nonlocal r, c
        if 0 <= r < rows and 0 <= c < cols:
            grid[r][c] = ch
        c += 1

    while i < len(raw):
        ch = raw[i]
        if ch == "\x1b":
            m = re.match(r"\x1b\[([0-9;?]*)([A-Za-z])", raw[i:])
            if m:
                params, cmd = m.group(1), m.group(2)
                nums = [int(x) for x in params.lstrip("?").split(";") if x.isdigit()]
                n = nums[0] if nums else 1
                if cmd in ("H", "f"):
                    r = (nums[0] - 1) if nums else 0
                    c = (nums[1] - 1) if len(nums) > 1 else 0
                elif cmd == "G":
                    c = (nums[0] - 1) if nums else 0
                elif cmd == "d":
                    r = (nums[0] - 1) if nums else 0
                elif cmd == "A":
                    r = max(0, r - n)
                elif cmd == "B":
                    r = min(rows - 1, r + n)
                elif cmd == "C":
                    c = min(cols - 1, c + n)
                elif cmd == "D":
                    c = max(0, c - n)
                elif cmd == "J":
                    if not nums or nums[0] in (2, 3):
                        grid = [[" "] * cols for _ in range(rows)]
                        r = c = 0
                elif cmd == "K":
                    for x in range(c, cols):
                        grid[r][x] = " "
                i += m.end()
                continue
            m = re.match(r"\x1b[()][A-Z0-9]", raw[i:])
            if m:
                i += m.end()
                continue
            i += 1
            continue
        if ch == "\r":
            c = 0
        elif ch == "\n":
            r += 1
            c = 0
        elif ch == "\b":
            c = max(0, c - 1)
        elif ch >= " ":
            put(ch)
        i += 1
    return "\n".join("".join(row).rstrip() for row in grid)


def capture(key_tokens, cols: int, rows: int, settle: float = 1.4) -> str:
    keymap = {
        "up": b"\x1bOA", "down": b"\x1bOB", "right": b"\x1bOC", "left": b"\x1bOD",
        "enter": b"\n", "esc": b"\x1b", "space": b" ",
    }
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.update(TERM="xterm-256color", LINES=str(rows), COLUMNS=str(cols))
        os.execvp("python3", ["python3", os.path.join(os.path.dirname(__file__), "datameter.py")])
    buf = b""
    time.sleep(settle)
    for tok in key_tokens:
        data = keymap.get(tok, tok.encode())
        os.write(fd, data)
        time.sleep(0.4)
    end = time.time() + 1.2
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                buf += os.read(fd, 1 << 20)
            except OSError:
                break
    try:
        os.write(fd, b"q")
    except OSError:
        pass
    time.sleep(0.2)
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass
    return buf.decode(errors="replace")


if __name__ == "__main__":
    tokens = sys.argv[1].split() if len(sys.argv) > 1 and sys.argv[1] else []
    cols = int(sys.argv[2]) if len(sys.argv) > 2 else 110
    rows = int(sys.argv[3]) if len(sys.argv) > 3 else 40
    print(replay(capture(tokens, cols, rows), cols, rows))
