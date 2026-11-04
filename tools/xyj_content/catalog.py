"""Inspect both Object tables without collapsing duplicate IDs or changing files."""

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
import csv
import io

from .binary import FixedTable, decode_table, read_bytes
from .files import atomic_write, output_path, sha256, write_json
from .language import LanguageTable, decode_language
from .objects import ObjectView, require_object_table, view_record, without_text


@dataclass(frozen=True)
class Inputs:
    """Explicit source files; these paths are only ever opened for reading."""

    server: Path
    client: Path
    language: Path

    @classmethod
    def from_root(cls, root: Path) -> "Inputs":
        """Select the researched local server and the currently usable Main client."""
        return cls(root / "XYJ_SEVVER/qingfeng/Object.dbf",
                   root / "swdol/Main/Object.dbf", root / "swdol/Main/Language.DBF")


@dataclass(frozen=True)
class Loaded:
    """Parsed source values, their blobs and paths for reproducible offline work."""

    paths: Inputs
    blobs: tuple[bytes, bytes, bytes]
    server: FixedTable
    client: FixedTable
    language: LanguageTable


def load_inputs(paths: Inputs) -> Loaded:
    """Read three bounded files and require the supported Object/RTS layouts."""
    server_blob, client_blob, language_blob = map(read_bytes, (paths.server, paths.client, paths.language))
    server, client = decode_table(server_blob), decode_table(client_blob)
    require_object_table(server)
    require_object_table(client)
    language = decode_language(language_blob)
    return Loaded(paths, (server_blob, client_blob, language_blob), server, client, language)


def source_hashes(loaded: Loaded) -> dict:
    """Identify the three exact local input files; this is not a live-server fingerprint."""
    return {key: {"path": str(path.resolve()), "sha256": sha256(blob), "bytes": len(blob)}
            for key, path, blob in zip(("server_object", "client_object", "client_language"),
                                      (loaded.paths.server, loaded.paths.client, loaded.paths.language),
                                      loaded.blobs)}


def inspect_loaded(loaded: Loaded) -> tuple[dict, list[dict], list[dict]]:
    """Produce full row-preserving catalogs and an explicitly bounded cross-side comparison."""
    server_views = [view_record(r, i) for i, r in enumerate(loaded.server.records)]
    client_views = [view_record(r, i, loaded.language) for i, r in enumerate(loaded.client.records)]
    sg, cg = defaultdict(list), defaultdict(list)
    for view in server_views:
        sg[view.object_id].append(view)
    for view in client_views:
        cg[view.object_id].append(view)
    shared = sorted(set(sg) & set(cg))
    eligible = [key for key in shared if key and len(sg[key]) == len(cg[key]) == 1]
    names_differ, outside_text_differ, descriptions_differ = [], [], []
    for key in eligible:
        sv, cv = sg[key][0], cg[key][0]
        if sv.name != cv.name:
            names_differ.append(key)
        if sv.description != cv.description:
            descriptions_differ.append(key)
        if without_text(loaded.server.records[sv.row]) != without_text(loaded.client.records[cv.row]):
            outside_text_differ.append(key)
    summary = {
        "format": "xyj-content-catalog/v1",
        "sources": source_hashes(loaded),
        "server_rows": len(server_views), "client_rows": len(client_views),
        "language_entries": len(loaded.language.entries),
        "server_type_codes": dict(Counter(v.type_code for v in server_views)),
        "client_type_codes": dict(Counter(v.type_code for v in client_views)),
        "server_duplicate_ids": {str(k): [asdict(v) for v in values] for k, values in sg.items() if len(values) > 1},
        "client_duplicate_ids": {str(k): [asdict(v) for v in values] for k, values in cg.items() if len(values) > 1},
        "compared_unique_nonzero_ids": len(eligible),
        "server_only_ids": sorted(set(sg) - set(cg)),
        "client_only_ids": sorted(set(cg) - set(sg)),
        "name_difference_ids": names_differ,
        "description_difference_ids": descriptions_differ,
        "non_text_difference_ids": outside_text_differ,
        "limits": ["分类码和偏移 28 的字只作原始值展示，未确认为资源 ID。",
                   "ID 0 的全部重复记录保留，不参与唯一 ID 比较。",
                   "本地服务端文件不代表远端服务器的实际运行版本。"]
    }
    return summary, [asdict(v) for v in server_views], [asdict(v) for v in client_views]


def csv_cell(value: object) -> object:
    """Escape spreadsheet formula prefixes when exporting game-supplied text."""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_catalog(root: Path, requested: Path, loaded: Loaded) -> dict:
    """Write JSON and spreadsheet-readable CSV catalogs only below workspace/output."""
    destination = output_path(root, requested)
    summary, server, client = inspect_loaded(loaded)
    write_json(destination / "summary.json", summary)
    for side, records in (("server", server), ("client", client)):
        write_json(destination / f"{side}_objects.json", records)
        stream = io.StringIO(newline="")
        fields = list(ObjectView.__dataclass_fields__)
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: csv_cell(v) for k, v in row.items()} for row in records)
        atomic_write(destination / f"{side}_objects.csv", stream.getvalue().encode("utf-8-sig"))
    return summary
