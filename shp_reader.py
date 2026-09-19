"""
Pure-Python .shp reader + point-in-polygon clipping.
เขียนเองแทนการใช้ geopandas/fiona เพื่อให้ deploy บน Streamlit Cloud ได้เบาและเร็วกว่า
รองรับเฉพาะ Polygon shapefile (shape type 5) แบบที่ใช้กำหนดขอบเขตพื้นที่รับน้ำ (catchment)
"""
import struct


def read_shapefile_polygons(path):
    """อ่านไฟล์ .shp คืนค่าเป็น list ของ shapes
    แต่ละ shape = list ของ rings, แต่ละ ring = list ของ (x, y)"""
    with open(path, "rb") as f:
        data = f.read()
    shape_type = struct.unpack("<i", data[32:36])[0]
    if shape_type != 5:
        raise ValueError(f"รองรับเฉพาะ Polygon (type 5), ไฟล์นี้เป็น type {shape_type}")

    pos = 100
    shapes = []
    n = len(data)
    while pos < n:
        _, content_len_words = struct.unpack(">2i", data[pos : pos + 8])
        pos += 8
        content_start = pos
        rshape_type = struct.unpack("<i", data[pos : pos + 4])[0]
        pos += 4
        if rshape_type == 0:
            shapes.append([])
            pos = content_start + content_len_words * 2
            continue
        pos += 32  # skip bbox of this record
        num_parts, num_points = struct.unpack("<2i", data[pos : pos + 8])
        pos += 8
        parts = struct.unpack(f"<{num_parts}i", data[pos : pos + 4 * num_parts])
        pos += 4 * num_parts
        points = []
        for _ in range(num_points):
            x, y = struct.unpack("<2d", data[pos : pos + 16])
            pos += 16
            points.append((x, y))
        rings = []
        for i in range(num_parts):
            start = parts[i]
            end = parts[i + 1] if i + 1 < num_parts else num_points
            rings.append(points[start:end])
        shapes.append(rings)
        pos = content_start + content_len_words * 2
    return shapes


def get_bbox(shapes):
    xs, ys = [], []
    for rings in shapes:
        for ring in rings:
            for x, y in ring:
                xs.append(x)
                ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def _point_in_ring(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def point_in_shape(x, y, rings):
    """นับจำนวน ring ที่จุดอยู่ข้างใน (คี่ = อยู่ในพื้นที่จริง รองรับกรณีมีรูตรงกลาง)"""
    count = sum(1 for ring in rings if _point_in_ring(x, y, ring))
    return count % 2 == 1


def clip_points(points, shapes):
    """points: list ของ (lat, lon, ...extra). คืนเฉพาะจุดที่อยู่ในขอบเขต shapes (รวมทุก shape ในไฟล์)"""
    bbox = get_bbox(shapes)
    result = []
    for p in points:
        lat, lon = p[0], p[1]
        if not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
            continue
        for rings in shapes:
            if rings and point_in_shape(lon, lat, rings):
                result.append(p)
                break
    return result
