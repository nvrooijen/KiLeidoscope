"""Where a finding's labels go: one set of rules for every label, a findings file's or KiCad DRC's
(the finding says what to label, never where). findings_draw makes the text, its plate and
the leader; this module only places them.

1. Never inside the board: just out from the outer surface the view looks at (the bottom
   one from below), the label's lowest edge LIFT_M clear of it as it turns to the eye's
   place (a tall block tilts more than the eye's direction says); a
   leader runs from the label's foot to its anchor, wherever that is (an inner layer,
   the far side).
2. Beside the finding, not on it: just outside its box (bbox_nm), on the first side, in
   screen terms, the framed view has room for: above, right, below, left. Several labels
   stack on the right (or left), apart on screen, the highest anchor first.
3. Sized to the finding: LABEL_SHARE of the extent findings_draw.frame_bounds frames, clamped.
4. (The text: findings_draw.label_text and _short, what is wrong over the measured value.)
5. A hole finding's cut open (hole_cut.py): flat on the cut face, a hair in front of it,
   both lines within the board between its outer copper, beside both holes on the side
   towards the board's middle, never across a barrel.
6. Never hidden by a part: a label with a part's box between it and the eye (or around
   it) comes towards the eye until it is in front of it, scaled by the share of the way
   it ends at, so it looks the same on screen (`pull`, `toward_eye`). A rendered view
   ignores "in front", so this is how a label wins there; its leader stays put.

Everything here is Blender metres; the eye is findings_draw's eye matrix (x the view's
right, y into the view, z its up).
"""

from collections import namedtuple

import numpy as np
from mathutils import Matrix, Vector

FRAME_MIN_M = 6e-3  # a finding is framed at least this wide (findings_draw.frame_bounds), so its neighbourhood shows
LABEL_SHARE = 1 / 40  # of the framed extent: a label's text size
LABEL_MIN_M = 0.1e-3
LABEL_MAX_M = 2.5e-3
LIFT_M = 0.1e-3  # a label's lowest edge out from the board's surface
GAP = 0.5  # of the text size: label to box, and label to label
REACH = (0.85, 0.5)  # of the framed extent: what the framed view shows right and up of its centre
MIN_SINE = 0.35  # a view this flat (20 degrees) or flatter stacks as if it were this steep
CUT_FILL = 0.8  # of the thickness between the outer copper: a label on the cut face, padding included
CUT_FRONT_M = 50e-6  # in front of the cut face
HOLE_CLEAR_M = (0.25e-3, 1.0e-3)  # past the farther hole centre: at least (a barrel), at most (a part's box)
PULL_MARGIN = 0.03  # of the way from the eye: a pulled label stays this far in front of the part it clears
PULL_MIN = 0.02  # never nearer the eye than this share of the way

# anchor: Blender point; width, height: the label's block (its plate) in its own axes;
# middle: the block's centre from the text's origin (x, y), all at its present size.
Label = namedtuple("Label", "anchor width height middle")


def extent(box):
    """What findings_draw.frame_bounds frames of a box (x0, y0, x1, y1)."""
    return max(box[2] - box[0], box[3] - box[1], FRAME_MIN_M)


def size(box):
    """A label's text size for a finding's box: the same on screen on any board."""
    return min(LABEL_MAX_M, max(LABEL_MIN_M, extent(box) * LABEL_SHARE))


def beside(labels, box, eye, top, bottom, unit):
    """(location, foot) of each label facing the eye: just outside `box`, out from the
    surface the eye looks at (`top` or `bottom`, z); the foot is where its leader starts."""
    if not labels:
        return []
    right, up = eye.col[0].xyz.normalized(), eye.col[2].xyz.normalized()
    back = -eye.col[1].xyz.normalized()
    side = 1.0 if back.z >= 0 else -1.0
    across = Vector((right.x, right.y))
    across = across.normalized() if across.length > 1e-3 else Vector((1.0, 0.0))
    upward = Vector((-across.y, across.x))  # on the board, the way that reads as up on screen
    if upward.dot(up.xy) < 0:
        upward.negate()
    sine = max(abs(back.z), MIN_SINE)
    middle = Vector(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
    corners = [Vector((x, y)) - middle for x in (box[0], box[2]) for y in (box[1], box[3])]
    half = (max(abs(c.dot(across)) for c in corners), max(abs(c.dot(upward)) for c in corners))
    framed = extent(box)
    gap = GAP * unit
    tall = [label.height / sine for label in labels]  # screen height as a distance on the board
    stack = sum(tall) + gap * (len(labels) - 1)
    wide = max(label.width for label in labels)
    sides = [(1, 1), (0, 1), (1, -1), (0, -1)] if len(labels) == 1 else [(0, 1), (0, -1)]  # (axis, sign)

    def room(choice):
        axis, _ = choice
        need = (wide, stack)[axis]
        return framed * REACH[axis] / (1 if axis == 0 else sine) - half[axis] - gap - need
    axis, sign = next((choice for choice in sides if room(choice) >= 0), max(sides, key=room))
    out = (across, upward)[axis] * sign
    spots = []
    if axis == 1:  # one label, above or below the box
        centres = [middle + out * (half[1] + gap + tall[0] / 2)]
    else:  # stacked down the right (left) of the box, the highest anchor on top
        order = sorted(range(len(labels)), key=lambda i: -labels[i].anchor.xy.dot(upward))
        centres = [None] * len(labels)
        level = stack / 2
        for i in order:
            reach = half[0] + gap + labels[i].width / 2
            centres[i] = middle + out * reach + upward * (level - tall[i] / 2)
            level -= tall[i] + gap
    tallest = max(label.height for label in labels)  # one height for all: a stack stays apart on screen
    surface = top if side > 0 else bottom
    z = surface + side * (LIFT_M + tallest / 2 * abs(up.z) + wide / 2 * abs(right.z))
    for _ in range(3):  # each turned to the eye's place, not its direction: a tall block tilts more
        short = 0.0
        for label, centre in zip(labels, centres):
            x, y = facing(Vector((centre.x, centre.y, z)), eye)
            clear = side * (z - surface) - abs(y.z) * label.height / 2 - abs(x.z) * label.width / 2
            short = max(short, LIFT_M - clear)
        if short <= LIFT_M * 1e-3:
            break
        z += side * short  # the whole stack together: it stays apart on screen
    for label, centre in zip(labels, centres):
        at = Vector((centre.x, centre.y, z))
        x, y = facing(at, eye)
        towards = (right if axis == 0 else up) * sign  # from the box out: the foot is the near edge
        foot = at - towards * ((label.width if axis == 0 else label.height) / 2)
        spots.append((at - x * label.middle[0] - y * label.middle[1], foot))
    return spots


def facing(at, eye):
    """(x, y) of a label at `at` as its Track To turns it: its z at the eye's place, its y
    the eye's up as near as that allows."""
    normal = eye.translation - at
    if normal.length < 1e-12:
        return eye.col[0].xyz.normalized(), eye.col[2].xyz.normalized()
    normal.normalize()
    up = eye.col[2].xyz
    y = up - normal * up.dot(normal)
    if y.length < 1e-9:
        return eye.col[0].xyz.normalized(), eye.col[2].xyz.normalized()
    y.normalize()
    return y.cross(normal), y


def on_cut(labels, ends, normal, box, centre, low, high, unit):
    """(location, turn, scale) of each label written on a hole finding's cut face: flat
    in it (the text's x the viewer's right, its y up), CUT_FRONT_M in front, `scale` to
    fit between `low` and `high` (z), side by side past both holes `ends` (Blender XY)."""
    normal = normal.normalized()
    across = Vector((-normal.y, normal.x))  # the viewer's right, looking at the face along -normal
    a, b = ends
    middle = (a + b) / 2
    holes = max(abs((a - middle).dot(across)), abs((b - middle).dot(across)))
    sign = 1.0 if (centre - middle).dot(across) >= 0 else -1.0  # towards the board's middle: more wall
    beyond = max(sign * (Vector((x, y)) - middle).dot(across) for x in (box[0], box[2]) for y in (box[1], box[3]))
    reach = min(max(beyond, holes + HOLE_CLEAR_M[0]), holes + HOLE_CLEAR_M[1])
    across3, normal3 = Vector((across.x, across.y, 0.0)), Vector((normal.x, normal.y, 0.0))
    turn = Matrix((across3, (0.0, 0.0, 1.0), normal3)).transposed()  # columns: x, y, z of the text
    room = (high - low) * CUT_FILL
    spots = []
    for label in labels:
        scale = min(1.0, room / label.height) if label.height > 0 else 1.0
        width = label.width * scale
        reach += GAP * unit * scale
        centre3 = Vector((*(middle + across * sign * (reach + width / 2)), (low + high) / 2)) + \
            normal3 * CUT_FRONT_M
        reach += width
        spots.append((centre3 - turn @ Vector((label.middle[0], label.middle[1], 0.0)) * scale, turn, scale))
    return spots


def pull(points, boxes, eye, ortho=False, near=0.0):
    """The share of the way from the eye a label must come to (1: where it is) so no part
    box (low, high) hides any of its `points` (samples on its plate): just in front of the
    first box each line from the eye to a point meets, a point inside one included; never
    nearer the eye than `near` (its clip) or PULL_MIN. Orthographic: the lines run along
    the view from the eye's plane."""
    if not boxes or not points:
        return 1.0
    low = np.array([tuple(box[0]) for box in boxes], np.float64)
    high = np.array([tuple(box[1]) for box in boxes], np.float64)
    back = -eye.col[1].xyz.normalized()
    first, reach = 1.0, 0.0
    for point in points:
        length = (eye.translation - point).dot(back) if ortho else (eye.translation - point).length
        origin = point + back * length if ortho else eye.translation
        start, way = np.array(tuple(origin)), np.array(tuple(point - origin))
        with np.errstate(divide="ignore", invalid="ignore"):
            a, b = (low - start) / way, (high - start) / way
        enter = np.nanmax(np.minimum(a, b), axis=1)
        leave = np.nanmin(np.maximum(a, b), axis=1)
        hit = (enter <= leave) & (enter >= 0.0) & (enter < 1.0)  # the eye inside a box: not that box
        if hit.any():
            first = min(first, float(enter[hit].min()))
        reach = max(reach, length)
    if first >= 1.0:
        return 1.0
    return max(first * (1.0 - PULL_MARGIN), PULL_MIN, near / reach if reach > 0 else 0.0)


def toward_eye(points, share, eye, ortho=False):
    """(points, scale) of a label brought `share` of the way from the eye (`pull`): towards
    the eye's place and scaled by `share` in perspective, so it covers the same pixels;
    along the view, unscaled, in an orthographic one."""
    if share >= 1.0:
        return list(points), 1.0
    if ortho:
        back = -eye.col[1].xyz.normalized()
        shift = back * (1.0 - share) * (eye.translation - points[0]).dot(back)
        return [point + shift for point in points], 1.0
    return [eye.translation + (point - eye.translation) * share for point in points], share
