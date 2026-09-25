"""Apply Xtreme1 track deletions to a copy of a nuScenes dataset."""

import argparse
import json
import os
import re
import shutil
import sys
import zipfile
from collections import defaultdict
from pathlib import Path


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_replacing_hardlink(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def metadata_folder(root, version):
    nested = root / version
    if nested.is_dir():
        return nested
    if root.name == version and (root / "scene.json").is_file():
        return root
    raise ValueError(
        f"nuScenes metadata klasörü bulunamadı: {nested} veya {root / 'scene.json'}"
    )


def scene_samples(scene, samples):
    by_token = {row["token"]: row for row in samples}
    result = []
    token = scene["first_sample_token"]
    seen = set()
    while token:
        if token in seen:
            raise ValueError("sample.next zincirinde döngü bulundu")
        seen.add(token)
        sample = by_token.get(token)
        if sample is None:
            raise ValueError(f"sample token bulunamadı: {token}")
        if sample["scene_token"] != scene["token"]:
            raise ValueError("sample.next başka scene içine geçti")
        result.append(sample)
        token = sample["next"]
    if len(result) != scene["nbr_samples"]:
        raise ValueError(
            f"Scene sample sayısı tutarsız: zincir={len(result)}, "
            f"scene.json={scene['nbr_samples']}"
        )
    return result


def select_result(payload, source_name, filename):
    rows = payload if isinstance(payload, list) else [payload]
    selected = [row for row in rows if row.get("sourceName") == source_name]
    if len(selected) != 1:
        sources = sorted({str(row.get("sourceName")) for row in rows})
        raise ValueError(
            f"{filename}: {source_name!r} sonucu tek değil; bulunan kaynaklar: {sources}"
        )
    objects = selected[0].get("objects")
    if not isinstance(objects, list):
        raise ValueError(f"{filename}: objects listesi yok")
    track_ids = []
    for number, obj in enumerate(objects):
        track_id = obj.get("trackId")
        if not isinstance(track_id, str) or not track_id:
            raise ValueError(f"{filename}: object {number} için trackId eksik")
        track_ids.append(track_id)
    if len(track_ids) != len(set(track_ids)):
        raise ValueError(f"{filename}: aynı trackId bir frame içinde birden fazla kez var")
    return set(track_ids), len(objects)


def read_xtreme1_tracks(zip_path, scene_name, source_name):
    expected_folder = "scene_" + scene_name
    pattern = re.compile(
        rf"(?:^|/){re.escape(expected_folder)}/result/frame_(\d+)\.json$"
    )
    tracks_by_index = {}
    object_count = 0
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.namelist():
            match = pattern.search(member)
            if not match:
                continue
            frame_index = int(match.group(1))
            if frame_index in tracks_by_index:
                raise ValueError(f"Export ZIP içinde tekrarlanan frame index: {frame_index}")
            payload = json.loads(archive.read(member).decode("utf-8-sig"))
            tracks, count = select_result(payload, source_name, member)
            tracks_by_index[frame_index] = tracks
            object_count += count
    if not tracks_by_index:
        raise ValueError(
            f"ZIP içinde {expected_folder}/result/frame_*.json bulunamadı"
        )
    return tracks_by_index, object_count


def link_or_copy(source, destination):
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination


def validate_tables(annotations, instances):
    annotation_by_token = {row["token"]: row for row in annotations}
    instance_by_token = {row["token"]: row for row in instances}
    grouped = defaultdict(list)
    for annotation in annotations:
        instance_token = annotation["instance_token"]
        if instance_token not in instance_by_token:
            raise ValueError(f"Annotation'ın instance kaydı yok: {annotation['token']}")
        grouped[instance_token].append(annotation)
        for field in ("prev", "next"):
            linked = annotation[field]
            if linked and linked not in annotation_by_token:
                raise ValueError(
                    f"Annotation {annotation['token']} için geçersiz {field}: {linked}"
                )
    for instance_token, instance in instance_by_token.items():
        rows = grouped.get(instance_token, [])
        if len(rows) != instance["nbr_annotations"]:
            raise ValueError(f"Instance annotation sayısı tutarsız: {instance_token}")
        first = annotation_by_token.get(instance["first_annotation_token"])
        last = annotation_by_token.get(instance["last_annotation_token"])
        if first is None or first["prev"]:
            raise ValueError(f"Instance first_annotation_token geçersiz: {instance_token}")
        if last is None or last["next"]:
            raise ValueError(f"Instance last_annotation_token geçersiz: {instance_token}")


def convert(args):
    root = args.dataroot.expanduser().resolve()
    source_metadata = metadata_folder(root, args.version)
    scenes = read_json(source_metadata / "scene.json")
    samples = read_json(source_metadata / "sample.json")
    annotations = read_json(source_metadata / "sample_annotation.json")
    instances = read_json(source_metadata / "instance.json")
    categories = read_json(source_metadata / "category.json")

    matching_scenes = [row for row in scenes if row["name"] == args.scene]
    if len(matching_scenes) != 1:
        available = ", ".join(sorted(row["name"] for row in scenes))
        raise ValueError(f"Scene bulunamadı: {args.scene}. Mevcut scene'ler: {available}")
    scene = matching_scenes[0]
    frames = scene_samples(scene, samples)
    tracks_by_index, exported_object_count = read_xtreme1_tracks(
        args.xtreme1_export.expanduser().resolve(), args.scene, args.source_name
    )

    expected_indexes = set(range(len(frames)))
    actual_indexes = set(tracks_by_index)
    if actual_indexes != expected_indexes:
        missing = sorted(expected_indexes - actual_indexes)
        extra = sorted(actual_indexes - expected_indexes)
        raise ValueError(
            f"Export tüm scene'i kapsamıyor. Eksik frame indexleri={missing}, "
            f"fazla frame indexleri={extra}"
        )

    annotations_by_sample = defaultdict(list)
    for annotation in annotations:
        annotations_by_sample[annotation["sample_token"]].append(annotation)

    deleted_tokens = set()
    scene_annotation_count = 0
    unknown_tracks = []
    for frame_index, sample in enumerate(frames):
        original_rows = annotations_by_sample[sample["token"]]
        original_ids = {row["instance_token"] for row in original_rows}
        exported_ids = tracks_by_index[frame_index]
        unknown = exported_ids - original_ids
        if unknown:
            unknown_tracks.extend((frame_index, token) for token in sorted(unknown))
        for annotation in original_rows:
            scene_annotation_count += 1
            if annotation["instance_token"] not in exported_ids:
                deleted_tokens.add(annotation["token"])
    if unknown_tracks:
        examples = ", ".join(
            f"frame_{index:05d}:{token}" for index, token in unknown_tracks[:5]
        )
        raise ValueError(
            "Exportta orijinal nuScenes frame'inde bulunmayan trackId var. "
            "Bu script yalnızca silme round-trip'i içindir. Örnekler: " + examples
        )

    kept_annotations = [
        annotation for annotation in annotations if annotation["token"] not in deleted_tokens
    ]
    sample_time = {row["token"]: row["timestamp"] for row in samples}
    kept_by_instance = defaultdict(list)
    for annotation in kept_annotations:
        kept_by_instance[annotation["instance_token"]].append(annotation)
    for rows in kept_by_instance.values():
        rows.sort(key=lambda row: sample_time[row["sample_token"]])
        for index, annotation in enumerate(rows):
            annotation["prev"] = rows[index - 1]["token"] if index else ""
            annotation["next"] = rows[index + 1]["token"] if index + 1 < len(rows) else ""

    kept_instances = []
    removed_instances = []
    for instance in instances:
        rows = kept_by_instance.get(instance["token"], [])
        if not rows:
            removed_instances.append(instance)
            continue
        instance["nbr_annotations"] = len(rows)
        instance["first_annotation_token"] = rows[0]["token"]
        instance["last_annotation_token"] = rows[-1]["token"]
        kept_instances.append(instance)

    validate_tables(kept_annotations, kept_instances)

    output = args.out.expanduser().resolve()
    if output.exists():
        raise ValueError(f"Çıktı klasörü zaten var: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(root, output, copy_function=link_or_copy)
        output_metadata = metadata_folder(output, args.version)
        write_json_replacing_hardlink(
            output_metadata / "sample_annotation.json", kept_annotations
        )
        write_json_replacing_hardlink(output_metadata / "instance.json", kept_instances)
        category_by_token = {row["token"]: row["name"] for row in categories}
        report = {
            "scene": args.scene,
            "frames": len(frames),
            "xtreme1_source": args.source_name,
            "original_scene_annotations": scene_annotation_count,
            "xtreme1_exported_objects": exported_object_count,
            "deleted_annotations": len(deleted_tokens),
            "removed_instances": [
                {
                    "instance_token": row["token"],
                    "category": category_by_token.get(row["category_token"]),
                    "original_annotation_count": row["nbr_annotations"],
                }
                for row in removed_instances
            ],
        }
        (output / "xtreme1_roundtrip_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        if output.exists():
            shutil.rmtree(output)
        raise

    print(f"Hazır: {output}")
    print(f"Scene: {args.scene} | frame: {len(frames)}")
    print(
        f"Silinen annotation: {len(deleted_tokens)} | "
        f"tamamen kaldırılan instance: {len(removed_instances)}"
    )
    print(f"Rapor: {output / 'xtreme1_roundtrip_report.json'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataroot", type=Path, required=True)
    parser.add_argument("--version", default="v1.0-mini")
    parser.add_argument("--xtreme1-export", type=Path, required=True)
    parser.add_argument("--scene", required=True, help="Örnek: scene-0061")
    parser.add_argument("--source-name", default="Ground Truth")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        convert(args)
    except (ValueError, OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"Hata: {exc}\n")


if __name__ == "__main__":
    main()
