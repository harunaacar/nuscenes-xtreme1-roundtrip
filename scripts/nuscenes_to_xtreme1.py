"""nuScenes v1.0-mini -> Xtreme1 LIDAR_FUSION ZIP with stable trackId labels.

Requires: pip install numpy
Run: py -3.12 nuscenes_to_xtreme1.py --help
"""

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np


CAMERAS = (
    "CAM_FRONT_LEFT", "CAM_FRONT", "CAM_FRONT_RIGHT",
    "CAM_BACK_LEFT", "CAM_BACK", "CAM_BACK_RIGHT",
)


def table(folder, name):
    path = folder / (name + ".json")
    if not path.is_file():
        raise ValueError(f"Eksik nuScenes dosyası: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def quaternion_matrix(values):
    q = np.asarray(values, dtype=np.float64)
    if q.shape != (4,) or not np.isfinite(q).all() or np.linalg.norm(q) < 1e-10:
        raise ValueError(f"Geçersiz quaternion: {values}")
    w, x, y, z = q / np.linalg.norm(q)
    return np.array([
        [1 - 2*(y*y + z*z), 2*(x*y - z*w), 2*(x*z + y*w)],
        [2*(x*y + z*w), 1 - 2*(x*x + z*z), 2*(y*z - x*w)],
        [2*(x*z - y*w), 2*(y*z + x*w), 1 - 2*(x*x + y*y)],
    ])


def pose(record):
    matrix = np.eye(4)
    matrix[:3, :3] = quaternion_matrix(record["rotation"])
    matrix[:3, 3] = record["translation"]
    return matrix


def euler_xyz(matrix):
    r = matrix[:3, :3]
    y = math.asin(max(-1.0, min(1.0, -float(r[2, 0]))))
    if abs(r[2, 0]) < 1 - 1e-8:
        x = math.atan2(float(r[2, 1]), float(r[2, 2]))
        z = math.atan2(float(r[1, 0]), float(r[0, 0]))
    else:
        x = math.atan2(-float(r[1, 2]), float(r[1, 1]))
        z = 0.0
    return {"x": x, "y": y, "z": z}


def xyz(vector):
    return dict(zip(("x", "y", "z"), (float(v) for v in vector)))


def nu_file(root, filename):
    root = root.resolve()
    path = (root / filename).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"Sensör dosyası eksik veya izin verilmeyen yol: {filename}")
    return path


def pcd_bytes(bin_path):
    raw = np.fromfile(bin_path, dtype="<f4")
    if raw.size % 5:
        raise ValueError(f"nuScenes LiDAR kaydı 5 float/point değil: {bin_path}")
    points = np.ascontiguousarray(raw.reshape(-1, 5)[:, :4], dtype="<f4")
    header = (
        "# .PCD v0.7 - Point Cloud Data file format\nVERSION 0.7\n"
        "FIELDS x y z intensity\nSIZE 4 4 4 4\nTYPE F F F F\n"
        f"COUNT 1 1 1 1\nWIDTH {len(points)}\nHEIGHT 1\n"
        "VIEWPOINT 0 0 0 1 0 0 0\n"
        f"POINTS {len(points)}\nDATA binary\n"
    )
    return header.encode("ascii") + points.tobytes()


def docker_database(compose_file):
    compose_file = compose_file.expanduser().resolve()
    if not compose_file.is_file():
        raise ValueError(f"Xtreme1 docker-compose.yml bulunamadı: {compose_file}")
    content = compose_file.read_text(encoding="utf-8")
    values = {}
    for name in ("MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD"):
        match = re.search(r"^\s+" + name + r":\s*['\"]?([^\s'\"#]+)", content, re.MULTILINE)
        if not match:
            raise ValueError(f"docker-compose.yml içinde {name} bulunamadı")
        values[name] = match.group(1)

    def query(sql):
        command = ["docker", "compose", "-f", str(compose_file), "exec", "-T", "mysql",
                   "mysql", "--default-character-set=utf8mb4", "-N", "-B",
                   "-u" + values["MYSQL_USER"], "-p" + values["MYSQL_PASSWORD"],
                   "-D", values["MYSQL_DATABASE"], "-e", sql]
        try:
            done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError("Docker bulunamadı; Docker Desktop açık olmalı") from exc
        if done.returncode:
            raise ValueError("Xtreme1 MySQL bağlantısı başarısız: " + done.stderr.strip()[-400:])
        return [line.split("\t") for line in done.stdout.splitlines() if line.strip()]

    return query


def resolve_classes(categories, compose_file, requested_dataset_id):
    query = docker_database(compose_file)
    datasets = query("SELECT id,name FROM dataset WHERE type='LIDAR_FUSION' AND is_deleted=b'0' ORDER BY id")
    if not datasets:
        raise ValueError("Xtreme1'de LIDAR_FUSION dataset bulunamadı; önce arayüzden oluşturun")
    if requested_dataset_id is None:
        if len(datasets) == 1:
            dataset_id = int(datasets[0][0])
        else:
            print("LIDAR_FUSION dataset'ler:")
            for row in datasets:
                print(f"  {row[0]}: {row[1]}")
            dataset_id = int(input("Bu ZIP'in yükleneceği dataset ID: ").strip())
    else:
        dataset_id = requested_dataset_id
    if dataset_id not in {int(row[0]) for row in datasets}:
        raise ValueError(f"LIDAR_FUSION dataset ID geçersiz: {dataset_id}")
    existing = query(f"SELECT id,name,tool_type FROM dataset_class WHERE dataset_id={dataset_id}")
    by_name = {row[1]: int(row[0]) for row in existing if row[2] == "CUBOID"}
    missing = sorted(set(categories) - set(by_name))
    for name in missing:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", name):
            raise ValueError(f"Geçersiz kategori adı: {name}")
    if missing:
        print(f"Dataset {dataset_id} için {len(missing)} CUBOID sınıfı oluşturuluyor...")
    for name in missing:
        color = "#" + hashlib.sha256(name.encode()).hexdigest()[:6]
        query("INSERT INTO dataset_class (dataset_id,name,tool_type,color,tool_type_options,attributes) "
              f"VALUES ({dataset_id},'{name}','CUBOID','{color}','{{}}','[]')")
    if missing:
        existing = query(f"SELECT id,name,tool_type FROM dataset_class WHERE dataset_id={dataset_id}")
        by_name = {row[1]: int(row[0]) for row in existing if row[2] == "CUBOID"}
    if set(categories) - set(by_name):
        raise ValueError("Oluşturulan sınıflar Xtreme1 veritabanında doğrulanamadı")
    print(f"Xtreme1 dataset ID: {dataset_id} | sınıf: {len(categories)}")
    return {name: {"id": by_name[name], "name": name} for name in categories}


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def convert(args):
    root = args.dataroot.expanduser().resolve()
    folder = root / args.version
    if not folder.is_dir() and root.name == args.version:
        folder = root
    scenes = table(folder, "scene")
    samples = {r["token"]: r for r in table(folder, "sample")}
    sample_data = table(folder, "sample_data")
    calibrated = {r["token"]: r for r in table(folder, "calibrated_sensor")}
    sensors = {r["token"]: r for r in table(folder, "sensor")}
    ego_poses = {r["token"]: r for r in table(folder, "ego_pose")}
    instances = {r["token"]: r for r in table(folder, "instance")}
    categories = {r["token"]: r["name"] for r in table(folder, "category")}
    annotations = defaultdict(list)
    for ann in table(folder, "sample_annotation"):
        annotations[ann["sample_token"]].append(ann)
    per_sample = defaultdict(dict)
    for sd in sample_data:
        if sd["is_key_frame"]:
            channel = sensors[calibrated[sd["calibrated_sensor_token"]]["sensor_token"]]["channel"]
            if channel in per_sample[sd["sample_token"]]:
                raise ValueError(f"Aynı sample/channel iki kez geçiyor: {sd['sample_token']} {channel}")
            per_sample[sd["sample_token"]][channel] = sd
    if not 0 <= args.scene_index < len(scenes):
        raise ValueError(f"scene-index 0 ile {len(scenes)-1} arasında olmalı")
    scene = scenes[args.scene_index]
    frames = []
    token = scene["first_sample_token"]
    seen = set()
    while token and (args.max_frames is None or len(frames) < args.max_frames):
        if token in seen:
            raise ValueError("sample.next zincirinde döngü var")
        seen.add(token)
        frame = samples[token]
        if frame["scene_token"] != scene["token"]:
            raise ValueError("sample.next farklı bir scene'e geçti")
        frames.append(frame)
        token = frame["next"]
    if not frames:
        raise ValueError("Scene'de frame bulunamadı")
    counts = Counter(categories[instances[a["instance_token"]]["category_token"]]
                     for frame in frames for a in annotations[frame["token"]])
    if not counts:
        raise ValueError("Seçilen frame'lerde ground truth annotation bulunamadı")
    # Xtreme1 tracks objects with trackId, but its Instances panel also expects
    # a stable, numeric-looking trackName. Assign it once per nuScenes instance
    # in first-seen order and reuse it in every frame.
    track_names = {}
    for frame in frames:
        for ann in annotations[frame["token"]]:
            instance_token = ann["instance_token"]
            if instance_token not in track_names:
                track_names[instance_token] = str(len(track_names) + 1)
    if args.list_categories:
        print(f"Scene: {scene['name']} | frame: {len(frames)}")
        for name, count in sorted(counts.items()):
            print(f"{name}: {count}")
        return
    if args.output is None:
        raise ValueError("ZIP üretmek için --output gerekli")
    classes = resolve_classes(counts, args.compose_file, args.dataset_id)
    unsupported = set(counts) - set(classes)
    if unsupported:
        raise ValueError("Eksik Xtreme1 sınıfları: " + ", ".join(sorted(unsupported)))
    output = args.output.expanduser().resolve()
    if output.exists():
        raise ValueError(f"Çıktı dosyası zaten var: {output}. Önce farklı bir ad seçin")
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"scene_{scene['name']}"
    identity_counts = Counter()
    try:
        with zipfile.ZipFile(output, "w", allowZip64=True) as archive:
            for index, frame in enumerate(frames):
                frame_name = f"frame_{index:05d}"
                sensor_data = per_sample[frame["token"]]
                if "LIDAR_TOP" not in sensor_data:
                    raise ValueError(f"LiDAR keyframe yok: {frame['token']}")
                lidar = sensor_data["LIDAR_TOP"]
                t_global_lidar = pose(ego_poses[lidar["ego_pose_token"]]) @ pose(calibrated[lidar["calibrated_sensor_token"]])
                t_lidar_global = np.linalg.inv(t_global_lidar)
                archive.writestr(f"{prefix}/lidar_point_cloud_0/{frame_name}.pcd",
                                 pcd_bytes(nu_file(root, lidar["filename"])))
                cameras = []
                for camera_index, channel in enumerate(CAMERAS):
                    if channel not in sensor_data:
                        raise ValueError(f"{frame_name}: {channel} keyframe yok")
                    sd = sensor_data[channel]
                    cal = calibrated[sd["calibrated_sensor_token"]]
                    t_global_camera = pose(ego_poses[sd["ego_pose_token"]]) @ pose(cal)
                    t_camera_lidar = np.linalg.inv(t_global_camera) @ t_global_lidar
                    intrinsic = np.asarray(cal["camera_intrinsic"], dtype=float)
                    cameras.append({
                        "cameraInternal": {"fx": float(intrinsic[0, 0]), "fy": float(intrinsic[1, 1]),
                                           "cx": float(intrinsic[0, 2]), "cy": float(intrinsic[1, 2])},
                        "cameraExternal": t_camera_lidar.reshape(-1).tolist(),
                        "width": sd["width"], "height": sd["height"], "rowMajor": True,
                    })
                    image = nu_file(root, sd["filename"])
                    archive.write(image, f"{prefix}/camera_image_{camera_index}/{frame_name}{image.suffix.lower()}",
                                  compress_type=zipfile.ZIP_STORED)
                archive.writestr(f"{prefix}/camera_config/{frame_name}.json", json_bytes(cameras))
                objects = []
                for ann in annotations[frame["token"]]:
                    category = categories[instances[ann["instance_token"]]["category_token"]]
                    t_global_box = pose(ann)
                    t_lidar_box = t_lidar_global @ t_global_box
                    w, length, height = (float(n) for n in ann["size"])
                    if min(w, length, height) <= 0:
                        raise ValueError(f"Geçersiz box boyutu: {ann['token']}")
                    spec = classes[category]
                    obj = {
                        "type": "3D_BOX", "trackId": ann["instance_token"],
                        "trackName": track_names[ann["instance_token"]],
                        "classId": spec["id"], "className": spec["name"],
                        "classValues": [],
                        "contour": {"center3D": xyz(t_lidar_box[:3, 3]),
                                    "size3D": xyz((length, w, height)),
                                    "rotation3D": euler_xyz(t_lidar_box)},
                    }
                    objects.append(obj)
                    identity_counts[ann["instance_token"]] += 1
                if objects:
                    archive.writestr(f"{prefix}/result/{frame_name}.json", json_bytes({"objects": objects}))
    except Exception:
        output.unlink(missing_ok=True)
        raise
    print(f"Hazır: {output}")
    print(f"Frame: {len(frames)} | etiket: {sum(identity_counts.values())} | ayrı trackId: {len(identity_counts)}")
    print(f"Birden fazla frame'de görünen track: {sum(1 for n in identity_counts.values() if n > 1)}")
    print("Xtreme1'de LIDAR_FUSION dataset'e ZIP'i yüklerken Contains annotation result > Ground Truth seçin.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataroot", type=Path, required=True, help="nuScenes ana klasörü (samples/ ve v1.0-mini/ içerir)")
    parser.add_argument("--version", default="v1.0-mini")
    parser.add_argument("--scene-index", type=int, default=0)
    parser.add_argument("--max-frames", type=int, default=None, help="İsteğe bağlı frame sınırı; verilmezse scene'in tamamı")
    parser.add_argument("--list-categories", action="store_true", help="Önce scene'deki kategorileri göster")
    parser.add_argument("--dataset-id", type=int, help="Birden fazla LIDAR_FUSION dataset varsa ID")
    parser.add_argument("--compose-file", type=Path,
                        default=Path.home() / "Desktop" / "xtreme1-instance-tracking-patch" / "xtreme1-main" / "docker-compose.yml",
                        help="Xtreme1 docker-compose.yml yolu")
    parser.add_argument("--output", type=Path, help="Üretilecek yeni ZIP yolu")
    args = parser.parse_args()
    if args.max_frames is not None and args.max_frames < 1:
        parser.error("--max-frames en az 1 olmalı")
    try:
        convert(args)
    except (ValueError, KeyError, OSError, IndexError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Hata: {exc}\n")


if __name__ == "__main__":
    main()
