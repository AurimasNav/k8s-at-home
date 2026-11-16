"""Write Bambu Studio *project* 3MFs pre-set for the Bambu Lab P2S.

A plain 3MF opens in Bambu Studio as geometry only. A project 3MF (Application metadata
"BambuStudio-<ver>" + Metadata/project_settings.config + Metadata/model_settings.config) also
selects the printer, process and filament presets and carries per-project overrides.

How Bambu Studio loads it (BambuStudio v02.09.01.70 source, PresetBundle::load_config_file_config
and PresetCollection::load_external_preset): it looks the presets up by name
(printer_settings_id / print_settings_id / filament_settings_id). Every key NOT listed in
different_settings_to_system is replaced with the installed system preset's value, so only the
overridden keys have to be exact; everything else follows the user's installed profiles.

The presets are flattened from the official BBL profiles of the Bambu Studio that the
bambu-studio init container installed (bambu_studio.py), so they always match the slicer
version used to check the file. Stdlib only: runs under the CAD venv and /usr/bin/python3.

    import sys; sys.path.insert(0, "/opt/cad")
    import bambu3mf
    bambu3mf.write_project("part.3mf", [("Part", vertices_bed_mm, faces)], {"wall_loops": "3"})
"""
import functools
import json
import os
import uuid
import zipfile
from datetime import date
from pathlib import Path

APP = Path(os.environ.get("BAMBU_STUDIO_HOME", "/opt/data/apps/bambu-studio/current"))
PRINTER = "Bambu Lab P2S 0.4 nozzle"
PROCESS = "0.20mm Standard @BBL P2S"
FILAMENT = "Bambu PETG HF @BBL P2S 0.4 nozzle"
# Legacy keys the system JSONs still carry but PrintConfig no longer defines: Bambu Studio reports them
# as "unrecognized" in a dialog when a project carries them, and the system preset supplies their effect.
LEGACY_KEYS = {"extruder_clearance_radius", "only_one_wall_top", "wall_infill_order", "filament_long_retractions_when_ec",
               "filament_retraction_distances_when_ec", "filament_ingredients_safe", "filament_emission_safe",
               "filament_contact_safe"}
META = {"type", "name", "inherits", "from", "setting_id", "instantiation", "include", "description",
        "filament_id", "compatible_printers", "compatible_prints", "renamed_from"}


# ---------------- profiles (from the installed Bambu Studio) ----------------
def bbs_version(app=None):
    """Version of the installed Bambu Studio, e.g. '02.09.01.70'."""
    app = Path(app or APP)
    asset = app / "asset.json"       # written by bambu_studio.py right after the verified extract
    if not asset.is_file():
        raise RuntimeError(f"Bambu Studio is not installed at {app}: see `kubectl logs ... -c bambu-studio` "
                           "or run `python3 /opt/cad/bambu_studio.py status`")
    return json.loads(asset.read_text())["version"]


@functools.cache
def _index(app):
    profiles = Path(app) / "squashfs-root" / "resources" / "profiles"
    vendor = json.loads((profiles / "BBL.json").read_text())
    return {(kind, e["name"]): profiles / "BBL" / e["sub_path"]
            for kind in ("machine", "process", "filament") for e in vendor[f"{kind}_list"]}


def _flat(app, kind, name):
    idx = _index(str(app))
    if (kind, name) not in idx:
        raise KeyError(f"no {kind} preset {name!r} in Bambu Studio {bbs_version(app)}")
    d = json.loads(idx[(kind, name)].read_text())
    base = _flat(app, kind, d["inherits"]) if d.get("inherits") else {}
    for inc in d.get("include", []):
        base.update(json.loads(idx[(kind, inc)].read_text()))
    base.update(d)
    return base


@functools.cache
def load_profile(kind, name=None, app=None):
    """Flattened system preset: {'name', 'setting_id', 'filament_id', 'values'}. kind: printer/process/filament."""
    app = str(app or APP)
    name = name or {"printer": PRINTER, "process": PROCESS, "filament": FILAMENT}[kind]
    d = _flat(app, {"printer": "machine"}.get(kind, kind), name)
    return {"name": name, "setting_id": d.get("setting_id", ""), "filament_id": d.get("filament_id", ""),
            "values": {k: v for k, v in d.items() if k not in META}}


# ---------------- project config ----------------
def project_config(overrides=None, filament=FILAMENT, colour="#FFFFFF", bed="Textured PEI Plate", app=None):
    """Full project_settings.config dict. overrides: process keys -> value (str or list of str)."""
    pr, ps, fl = (load_profile("printer", app=app), load_profile("process", app=app),
                  load_profile("filament", filament, app=app))
    cfg = {}
    for p in (pr, ps, fl):
        cfg.update(p["values"])
    for k in LEGACY_KEYS:
        cfg.pop(k, None)
    n_var = len(fl["values"].get("filament_extruder_variant", [""]))
    diff = []
    for k, v in (overrides or {}).items():
        sys_v = ps["values"].get(k)
        if isinstance(sys_v, list):
            v = v if isinstance(v, list) else [str(v)] * len(sys_v)
            if len(v) != len(sys_v):
                raise ValueError(f"{k}: need {len(sys_v)} values (one per extruder variant)")
        v = [str(x) for x in v] if isinstance(v, list) else str(v)
        if v != sys_v:
            diff.append(k)
        cfg[k] = v
    cfg.update({
        "name": "project_settings", "from": "project", "version": bbs_version(app),
        "printer_settings_id": pr["name"], "print_settings_id": ps["name"], "filament_settings_id": [fl["name"]],
        "print_compatible_printers": [pr["name"]],
        "filament_ids": [fl["filament_id"]], "filament_colour": [colour],
        "filament_self_index": ["1"] * n_var,
        "inherits_group": ["", "", ""],
        "different_settings_to_system": [";".join(sorted(diff)), "", ""],
        "nozzle_volume_type": ["Standard"],
        "curr_bed_type": bed,
    })
    return cfg, sorted(diff)


# ---------------- 3MF writer ----------------
NS = ('xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
      'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
      'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p"')
HDR = '<?xml version="1.0" encoding="UTF-8"?>\n'
REL_3DMODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
IDENT = "1 0 0 0 1 0 0 0 1"


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _mesh_xml(oid, v, f):
    vs = "".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in v)
    ts = "".join(f'<triangle v1="{int(a)}" v2="{int(b)}" v3="{int(c)}"/>' for a, b, c in f)
    return (f'{HDR}<model unit="millimeter" xml:lang="en-US" {NS}>\n'
            ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n <resources>\n'
            f'  <object id="{oid}" p:UUID="{uuid.uuid4()}" type="model">\n'
            f'   <mesh><vertices>{vs}</vertices><triangles>{ts}</triangles></mesh>\n  </object>\n'
            ' </resources>\n <build/>\n</model>\n')


def write_project(path, objects, overrides=None, title="", filament=FILAMENT, colour="#FFFFFF", app=None):
    """objects: [(name, vertices (bed coords, mm), faces)], all on plate 1 where they are.
    Returns the overridden keys (Bambu Studio shows them as modified)."""
    n = len(objects)
    cfg, diff = project_config(overrides, filament, colour, app=app)
    ver = bbs_version(app)
    today = date.today().isoformat()
    main_objs, items, rels, settings, instances, assemble = [], [], [], [], [], []
    files = {}
    for i, (name, v, f) in enumerate(objects, start=1):
        v = [tuple(float(x) for x in p) for p in v]
        c = [(min(p[a] for p in v) + max(p[a] for p in v)) / 2 for a in range(3)]
        mid, oid = i, n + i
        files[f"3D/Objects/object_{i}.model"] = _mesh_xml(mid, [(x - c[0], y - c[1], z - c[2]) for x, y, z in v], f)
        rels.append(f' <Relationship Target="/3D/Objects/object_{i}.model" Id="rel-{i}" Type="{REL_3DMODEL}"/>')
        main_objs.append(f'  <object id="{oid}" p:UUID="{uuid.uuid4()}" type="model">\n   <components>\n'
                         f'    <component p:path="/3D/Objects/object_{i}.model" objectid="{mid}" '
                         f'p:UUID="{uuid.uuid4()}" transform="{IDENT} 0 0 0"/>\n   </components>\n  </object>')
        tr = f"{IDENT} {c[0]:.4f} {c[1]:.4f} {c[2]:.4f}"
        items.append(f'  <item objectid="{oid}" p:UUID="{uuid.uuid4()}" transform="{tr}" printable="1"/>')
        settings.append(
            f'  <object id="{oid}">\n    <metadata key="name" value="{_esc(name)}"/>\n'
            f'    <metadata key="extruder" value="1"/>\n    <metadata face_count="{len(f)}"/>\n'
            f'    <part id="{mid}" subtype="normal_part">\n      <metadata key="name" value="{_esc(name)}"/>\n'
            '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
            f'      <mesh_stat face_count="{len(f)}" edges_fixed="0" degenerate_facets="0" facets_removed="0" '
            'facets_reversed="0" backwards_edges="0"/>\n    </part>\n  </object>')
        instances.append(f'    <model_instance>\n      <metadata key="object_id" value="{oid}"/>\n'
                         '      <metadata key="instance_id" value="0"/>\n'
                         f'      <metadata key="identify_id" value="{100 + i}"/>\n    </model_instance>')
        assemble.append(f'   <assemble_item object_id="{oid}" instance_id="0" transform="{tr}" offset="0 0 0" />')

    meta = "".join(f' <metadata name="{k}">{_esc(v)}</metadata>\n' for k, v in [
        ("Application", f"BambuStudio-{ver}"), ("BambuStudio:3mfVersion", "1"), ("Title", title),
        ("CreationDate", today), ("ModificationDate", today)])
    files["3D/3dmodel.model"] = (f'{HDR}<model unit="millimeter" xml:lang="en-US" {NS}>\n{meta} <resources>\n'
                                 + "\n".join(main_objs) + f'\n </resources>\n <build p:UUID="{uuid.uuid4()}">\n'
                                 + "\n".join(items) + "\n </build>\n</model>\n")
    files["3D/_rels/3dmodel.model.rels"] = (f'{HDR}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
                                            + "\n".join(rels) + "\n</Relationships>\n")
    files["_rels/.rels"] = (f'{HDR}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
                            f' <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="{REL_3DMODEL}"/>\n</Relationships>\n')
    files["[Content_Types].xml"] = (
        f'{HDR}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        ' <Default Extension="png" ContentType="image/png"/>\n'
        ' <Default Extension="gcode" ContentType="text/x.gcode"/>\n</Types>\n')
    files["Metadata/model_settings.config"] = (
        f'{HDR}<config>\n' + "\n".join(settings) + '\n  <plate>\n    <metadata key="plater_id" value="1"/>\n'
        '    <metadata key="plater_name" value=""/>\n    <metadata key="locked" value="false"/>\n'
        + "\n".join(instances) + "\n  </plate>\n  <assemble>\n" + "\n".join(assemble) + "\n  </assemble>\n</config>\n")
    files["Metadata/project_settings.config"] = json.dumps(cfg, indent=4, ensure_ascii=False) + "\n"
    files["Metadata/slice_info.config"] = (
        f'{HDR}<config>\n  <header>\n    <header_item key="X-BBL-Client-Type" value="slicer"/>\n'
        f'    <header_item key="X-BBL-Client-Version" value="{ver}"/>\n  </header>\n</config>\n')
    order = ["[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model", "3D/_rels/3dmodel.model.rels"]
    order += sorted(k for k in files if k.startswith("3D/Objects/")) + sorted(k for k in files if k.startswith("Metadata/"))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for k in order:
            z.writestr(k, files[k])
    return diff


# ---------------- self check ----------------
def check_project(path, app=None):
    """Structural checks mirroring what Bambu Studio's importer needs. Returns a list of problems."""
    import xml.etree.ElementTree as ET  # noqa: S405 - parses files this module wrote itself
    probs = []
    z = zipfile.ZipFile(path)
    names = set(z.namelist())
    for need in ("[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model", "Metadata/project_settings.config",
                 "Metadata/model_settings.config"):
        if need not in names:
            probs.append(f"missing {need}")
    for n in names:
        if n.endswith((".model", ".rels", ".xml", ".config")) and n != "Metadata/project_settings.config":
            try:
                ET.fromstring(z.read(n))  # noqa: S314
            except ET.ParseError as e:
                probs.append(f"{n}: XML {e}")
    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    main = ET.fromstring(z.read("3D/3dmodel.model"))  # noqa: S314
    app_meta = [m.text for m in main.findall("m:metadata", ns) if m.get("name") == "Application"]
    if not app_meta or not (app_meta[0] or "").startswith("BambuStudio-"):
        probs.append("Application metadata must start with BambuStudio- (else geometry only)")
    pp = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"
    main_ids = set()
    for o in main.find("m:resources", ns).findall("m:object", ns):
        main_ids.add(o.get("id"))
        for c in o.find("m:components", ns).findall("m:component", ns):
            sub = c.get(pp).lstrip("/")
            if sub not in names:
                probs.append(f"component path {sub} missing")
                continue
            so = ET.fromstring(z.read(sub)).find("m:resources", ns).findall("m:object", ns)  # noqa: S314
            if c.get("objectid") not in {x.get("id") for x in so}:
                probs.append(f"{sub}: no object {c.get('objectid')}")
    for it in main.find("m:build", ns).findall("m:item", ns):
        if it.get("objectid") not in main_ids:
            probs.append(f"build item -> unknown object {it.get('objectid')}")
    ms = ET.fromstring(z.read("Metadata/model_settings.config"))  # noqa: S314
    ms_ids = {o.get("id") for o in ms.findall("object")}
    if ms_ids != main_ids:
        probs.append(f"model_settings objects {ms_ids} != model objects {main_ids}")
    cfg = json.loads(z.read("Metadata/project_settings.config"))
    nf = len(cfg.get("filament_colour", []))
    if nf == 0:
        probs.append("filament_colour missing (Bambu Studio throws 'Invalid configuration file')")
    if len(cfg.get("different_settings_to_system", [])) != nf + 2:
        probs.append("different_settings_to_system must have num_filaments + 2 entries")
    if "extruder_variant_list" in cfg:
        fev = cfg.get("filament_extruder_variant", [])
        if len(fev) != len(cfg.get("filament_self_index", [])) or len(fev) < nf:
            probs.append("filament_extruder_variant / filament_self_index size mismatch")
    for k, kind in (("printer_settings_id", "printer"), ("print_settings_id", "process")):
        if cfg.get(k) != load_profile(kind, app=app)["name"]:
            probs.append(f"{k} {cfg.get(k)!r} not the installed system preset")
    nvar = len(cfg.get("print_extruder_variant", []))
    ps = load_profile("process", app=app)["values"]
    for k in cfg["different_settings_to_system"][0].split(";"):
        if k and isinstance(ps.get(k), list) and len(cfg[k]) != nvar:
            probs.append(f"override {k} has {len(cfg[k])} values, need {nvar}")
    return probs
