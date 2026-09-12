# /// script
# dependencies = ["pyobjc-framework-Cocoa"]
# ///
"""Draw the disk image's Finder background: heygent on the left, the
Applications folder on the right, an arrow and one line between them.

    python3 scripts/dmg_background.py <out.png> [width height]

The icon slots are at ICON_LEFT and ICON_RIGHT (Finder coordinates, the
icon's centre), which release.sh gives Finder as the icon positions.
"""
import sys

from AppKit import (NSBezierPath, NSBitmapImageRep, NSColor, NSFont,
                    NSFontAttributeName, NSForegroundColorAttributeName,
                    NSGraphicsContext, NSImage, NSMakePoint, NSMakeRect,
                    NSPNGFileType, NSString)

WIDTH, HEIGHT = 600, 400
ICON_LEFT = (160, 190)
ICON_RIGHT = (440, 190)
TEXT = "Drag heygent to the Applications folder"


def draw(width: int, height: int) -> bytes:
    image = NSImage.alloc().initWithSize_((width, height))
    image.lockFocus()
    NSColor.colorWithCalibratedWhite_alpha_(0.96, 1.0).set()
    NSBezierPath.fillRect_(NSMakeRect(0, 0, width, height))

    # The arrow, between the two icon slots. Finder's y grows downward,
    # AppKit's upward, so the slots' y is flipped for drawing.
    y = height - ICON_LEFT[1]
    start, end = ICON_LEFT[0] + 80, ICON_RIGHT[0] - 80
    NSColor.colorWithCalibratedWhite_alpha_(0.55, 1.0).set()
    shaft = NSBezierPath.bezierPath()
    shaft.setLineWidth_(6)
    shaft.moveToPoint_(NSMakePoint(start, y))
    shaft.lineToPoint_(NSMakePoint(end - 14, y))
    shaft.stroke()
    head = NSBezierPath.bezierPath()
    head.moveToPoint_(NSMakePoint(end, y))
    head.lineToPoint_(NSMakePoint(end - 26, y + 16))
    head.lineToPoint_(NSMakePoint(end - 26, y - 16))
    head.closePath()
    head.fill()

    attributes = {
        NSFontAttributeName: NSFont.systemFontOfSize_(17),
        NSForegroundColorAttributeName:
            NSColor.colorWithCalibratedWhite_alpha_(0.3, 1.0),
    }
    text = NSString.stringWithString_(TEXT)
    size = text.sizeWithAttributes_(attributes)
    text.drawAtPoint_withAttributes_(
        NSMakePoint((width - size.width) / 2, 64), attributes)
    image.unlockFocus()

    rep = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
    return bytes(rep.representationUsingType_properties_(NSPNGFileType, None))


def main() -> int:
    out = sys.argv[1]
    width = int(sys.argv[2]) if len(sys.argv) > 2 else WIDTH
    height = int(sys.argv[3]) if len(sys.argv) > 3 else HEIGHT
    with open(out, "wb") as f:
        f.write(draw(width, height))
    return 0


if __name__ == "__main__":
    sys.exit(main())
