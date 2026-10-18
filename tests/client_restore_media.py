"""Write the short decodable MP4 that the client-restore driver plays.

The driver gates on real decoding -- ``readyState``, a real duration, playback
advancing and continuous play rolling into the next segment -- so it needs a genuine
H.264 file rather than a placeholder that only carries the right bytes. PyAV ships with
the project interpreter, so the suite produces the fixture instead of asking whoever
runs the suite to keep one around.
"""
from __future__ import annotations

import sys
from pathlib import Path

import av
import numpy as np

WIDTH, HEIGHT, FPS, FRAMES = 320, 180, 25, 50
BAR_WIDTH = 16
BAR_STEP = 6
BACKGROUND = 32


def write(path: Path) -> None:
    """Encode ``FRAMES`` frames of a moving bar into ``path``."""
    container = av.open(str(path), 'w')
    try:
        stream = container.add_stream('libx264', rate=FPS)
        stream.width, stream.height, stream.pix_fmt = WIDTH, HEIGHT, 'yuv420p'
        for index in range(FRAMES):
            # A moving bar rather than a still image: the driver reads a duration and a
            # playing position, and successive distinct frames make both meaningful.
            frame = np.full((HEIGHT, WIDTH, 3), BACKGROUND, dtype=np.uint8)
            column = (index * BAR_STEP) % WIDTH
            frame[:, column:column + BAR_WIDTH] = 236
            for packet in stream.encode(av.VideoFrame.from_ndarray(frame, format='rgb24')):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()


if __name__ == '__main__':
    target = Path(sys.argv[1])
    write(target)
    print(f'Wrote {target}')
