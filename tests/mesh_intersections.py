"""Independent surface-intersection checks for real appearance regression fixtures."""


def _sub(a, b):
    return tuple(a[i]-b[i] for i in range(3))


def _dot(a, b):
    return sum(a[i]*b[i] for i in range(3))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _segment_triangle(start, end, triangle):
    direction = _sub(end, start)
    a, b, c = triangle
    edge1, edge2 = _sub(b, a), _sub(c, a)
    h = _cross(direction, edge2)
    determinant = _dot(edge1, h)
    if abs(determinant) < 1e-8:
        return False
    inverse = 1/determinant
    s = _sub(start, a)
    u = inverse*_dot(s, h)
    if not 0 <= u <= 1:
        return False
    q = _cross(s, edge1)
    v = inverse*_dot(direction, q)
    if v < 0 or u+v > 1:
        return False
    distance = inverse*_dot(edge2, q)
    return 1e-6 < distance < 1-1e-6


def _triangles(meshes):
    result = []
    for mesh in meshes:
        xyz = mesh['positions']
        points = [tuple(xyz[i:i+3]) for i in range(0, len(xyz), 3)]
        indices = mesh['indices']
        for i in range(0, len(indices), 3):
            triangle = tuple(points[k] for k in indices[i:i+3])
            bounds = tuple((min(p[a] for p in triangle), max(p[a] for p in triangle)) for a in range(3))
            result.append((triangle, bounds))
    return result


def intersection_count(body_meshes, equipment_meshes):
    """Count noncoplanar triangle pairs with interior edge crossings, without mutating meshes."""
    body = _triangles(body_meshes)
    collisions = 0
    for a, ab in _triangles(equipment_meshes):
        for b, bb in body:
            if any(ab[axis][1] < bb[axis][0] or ab[axis][0] > bb[axis][1] for axis in range(3)):
                continue
            if (any(_segment_triangle(a[i], a[(i+1)%3], b) for i in range(3)) or
                    any(_segment_triangle(b[i], b[(i+1)%3], a) for i in range(3))):
                collisions += 1
    return collisions
