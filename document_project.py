#!/usr/bin/env python3
"""
DocumentTheProject - erzeugt aus einem Eclipse 4diac(TM) Projekt eine PDF-Dokumentation.

Die Dokumentation beginnt bei der Ressource (z. B. EMB_RES) und steigt von dort
rekursiv in die Tiefe: jede Subapplikation / jeder Composite-FB des Projekts
bekommt ein Netzwerk-Blatt und ein Schnittstellen-Blatt. Jeder Typ wird genau
einmal dokumentiert; jede Instanz im Netzwerk verweist mit "-> S<n>" auf das
Blatt ihres Typs, und das Typ-Blatt listet umgekehrt alle Verwendungsstellen
("S5/C3") auf.

Jedes Blatt hat einen Zeichnungsrahmen nach DIN EN ISO 5457 (Heftrand links,
Feldeinteilung 1..n / A..x) und ein Schriftfeld mit Blattnummer "Blatt n von m".

Die Netzwerk- und Schnittstellengrafiken erzeugt der Konverter aus
IEC61499_FunctionBlock_SVG; das PDF wird mit reportlab + svglib gebaut
(wie in training1_cards).

Optional kommt ein Kapitel "Bedienoberfläche" dazu: die Masken des ISOBUS-
Objektpools als Bild (vt-render aus AgIsoVirtualTerminal) mit Objekttabellen
(list_mask_objects.py aus scripts_central) und Querverweisen auf die
4diac-Instanzen, die die Objekte verwenden.

Alle Pfade kommen als Parameter (siehe generate_doc.bat), keiner steht hier.
"""

import argparse
import datetime
import hashlib
import io
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import url2pathname

__version__ = "0.2.0"


def _locate_svg_tool(argv: List[str]) -> Optional[Path]:
    """The converter path is needed before argparse runs (module import)."""
    for i, arg in enumerate(argv):
        if arg == "--svg-tool" and i + 1 < len(argv):
            return Path(argv[i + 1])
        if arg.startswith("--svg-tool="):
            return Path(arg.split("=", 1)[1])
    env = os.environ.get("IEC61499_SVG_TOOL")
    return Path(env) if env else None


SVG_TOOL_DIR = _locate_svg_tool(sys.argv)
if SVG_TOOL_DIR is None:
    sys.exit("[FEHLER] Pfad zu IEC61499_FunctionBlock_SVG fehlt: --svg-tool <Pfad> "
             "oder Umgebungsvariable IEC61499_SVG_TOOL angeben (siehe generate_doc.bat).")
if str(SVG_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(SVG_TOOL_DIR))

try:
    import iec61499_network_to_svg as netsvg
    import iec61499_to_svg as fbsvg
except ImportError as exc:  # pragma: no cover - environment problem
    sys.exit(f"[FEHLER] IEC61499_FunctionBlock_SVG nicht gefunden unter {SVG_TOOL_DIR}: {exc}\n"
             f"         Pfad mit --svg-tool oder Umgebungsvariable IEC61499_SVG_TOOL angeben.")

from reportlab.graphics import renderPDF
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from svglib import svglib

# ===========================================================================
# Fonts
# ===========================================================================

FONT = "Helvetica"
FONT_ITALIC = "Helvetica-Oblique"
FONT_CODE = "Courier"


def init_fonts():
    """TGL-Normschrift für Rahmen, Text und die SVG-Grafiken registrieren."""
    global FONT, FONT_ITALIC
    tgl = SVG_TOOL_DIR / "tgl"
    regular = tgl / "TGL 0-17_std.ttf"
    italic = tgl / "TGL 0-16_std.ttf"
    if regular.exists():
        pdfmetrics.registerFont(TTFont("TGL", str(regular)))
        FONT = "TGL"
        # network converter: "TGL 0-17_std"; single FB converter: family "TGL" (+ italic)
        for name in ("TGL 0-17_std", "TGL 0-17", "TGL"):
            svglib.register_font(name, str(regular))
    if italic.exists():
        pdfmetrics.registerFont(TTFont("TGL-Italic", str(italic)))
        FONT_ITALIC = "TGL-Italic"
        for name in ("TGL 0-16_std", "TGL 0-16"):
            svglib.register_font(name, str(italic))
        svglib.register_font("TGL", str(italic), style="italic", rlgFontName="TGL-Italic")


# ===========================================================================
# Colors / geometry
# ===========================================================================

INK = colors.HexColor("#1A1A1A")
GREY = colors.HexColor("#6E6E6E")
LIGHT = colors.HexColor("#E8E8E8")
LINK = colors.HexColor("#0050B4")
ACCENT = colors.HexColor("#63B31F")   # 4diac event green

PAPER = {"A4": (297 * mm, 210 * mm), "A3": (420 * mm, 297 * mm)}

FRAME_LEFT = 20 * mm      # Heftrand
FRAME_OTHER = 10 * mm
GRID_BAND = 5 * mm        # band carrying the field labels
TITLE_H = 24 * mm         # Schriftfeld
AREA_PAD = 3 * mm
FIELD_SIZE = 45 * mm      # nominal field size of the reference grid
INDENT = 4 * mm           # table of contents indentation per hierarchy level

KIND_LABELS = {
    "SubApp": "Subapplikationstyp",
    "CompositeFB": "Composite-FB",
    "BasicFB": "Basis-FB",
    "SimpleFB": "Simple-FB",
    "ServiceInterfaceFB": "Service-Interface-FB",
    "Adapter": "Adaptertyp",
    "DataType": "Datentyp",
    "Function": "Funktion",
    "Resource": "Ressource",
    "UntypedSubApp": "Untypisierte Subapplikation",
}

TYPE_EXTENSIONS = (".fbt", ".sub", ".adp", ".dtp", ".fct", ".res")


# ===========================================================================
# Text helpers
# ===========================================================================

def clean(text: Optional[str]) -> str:
    # TGL has no backslash glyph; Windows paths are shown with forward slashes
    return (text or "").replace("\r", "").replace("\\", "/").strip()


def wrap(text: str, font: str, size: float, width: float) -> List[str]:
    """Word-wrap text to width (points); keeps explicit newlines."""
    lines: List[str] = []
    for para in clean(text).split("\n"):
        words = para.split(" ")
        line = ""
        for word in words:
            candidate = word if not line else f"{line} {word}"
            if pdfmetrics.stringWidth(candidate, font, size) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            # hard-break words that are longer than the column
            while pdfmetrics.stringWidth(word, font, size) > width and len(word) > 1:
                cut = len(word)
                while cut > 1 and pdfmetrics.stringWidth(word[:cut], font, size) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines or [""]


def fit(text: str, font: str, size: float, width: float) -> str:
    """Truncate a single line with an ellipsis."""
    if pdfmetrics.stringWidth(text, font, size) <= width:
        return text
    while text and pdfmetrics.stringWidth(text + "...", font, size) > width:
        text = text[:-1]
    return text + "..."


def short_name(type_name: str) -> str:
    return type_name.split("::")[-1]


# ===========================================================================
# Project model
# ===========================================================================

@dataclass
class TypeInfo:
    name: str
    full_name: str
    path: Optional[Path]
    root: ET.Element
    kind: str
    library: Optional[str] = None       # None = project type
    synthetic_key: Optional[str] = None  # untyped subapps / resources

    @property
    def key(self) -> str:
        return self.synthetic_key or f"type:{self.full_name}"

    @property
    def has_network(self) -> bool:
        return network_element(self.root) is not None


def type_kind(root: ET.Element) -> str:
    tag = root.tag
    if tag == "SubAppType":
        return "SubApp"
    if tag == "AdapterType":
        return "Adapter"
    if tag == "DataType":
        return "DataType"
    if tag == "Function":
        return "Function"
    if tag == "FBType":
        if root.find("BasicFB") is not None:
            return "BasicFB"
        if root.find("SimpleFB") is not None:
            return "SimpleFB"
        if root.find("FBNetwork") is not None or root.find("CompositeFB") is not None:
            return "CompositeFB"
        return "ServiceInterfaceFB"
    return tag


def network_element(root: ET.Element) -> Optional[ET.Element]:
    for tag in ("SubAppNetwork", "FBNetwork"):
        net = root.find(tag)
        if net is not None:
            return net
    return None


def package_of(root: ET.Element) -> str:
    ci = root.find("CompilerInfo")
    return ci.get("packageName", "") if ci is not None else ""


def load_type(path: Path, library: Optional[str]) -> Optional[TypeInfo]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        print(f"[WARNUNG] {path}: {exc}", file=sys.stderr)
        return None
    name = root.get("Name") or path.stem
    pkg = package_of(root)
    full = f"{pkg}::{name}" if pkg else name
    return TypeInfo(name=name, full_name=full, path=path, root=root,
                    kind=type_kind(root), library=library)


class Project:
    """A 4diac project plus the libraries it links."""

    def __init__(self, project_dir: Path, extra_libs: List[Path]):
        self.dir = project_dir
        self.workspace = project_dir.parent
        self.name = project_dir.name
        self.manifest = self._read_xml(project_dir / "MANIFEST.MF")
        self.warnings: List[str] = []
        self.problems: Dict[str, str] = {}   # unresolved type name -> reason
        self.lib_dirs = self._linked_libraries() + [(p.name, p) for p in extra_libs]
        self.types: Dict[str, TypeInfo] = {}
        self.types_by_short: Dict[str, List[TypeInfo]] = {}
        self._lib_files: Optional[Dict[str, List[Tuple[str, Path]]]] = None
        self._lib_cache: Dict[Path, Optional[TypeInfo]] = {}
        self._identifiers: Optional[set] = None
        self.systems: List[Tuple[Path, ET.Element]] = []
        self.global_constants: List[Tuple[Path, ET.Element]] = []
        self._scan()

    @staticmethod
    def _read_xml(path: Path) -> Optional[ET.Element]:
        try:
            return ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            return None

    def _linked_libraries(self) -> List[Tuple[str, Path]]:
        """Folder links from .project (External Libraries/<name> -> .lib/<name-ver>/typelib)."""
        libs: List[Tuple[str, Path]] = []
        proj = self._read_xml(self.dir / ".project")
        if proj is not None:
            for link in proj.iter("link"):
                name = link.findtext("name", "")
                uri = link.findtext("locationURI", "")
                if link.findtext("type") != "2" or not name.startswith("External Libraries/"):
                    continue
                path = self._resolve_uri(uri)
                if path and path.is_dir():
                    libs.append((name.split("/", 1)[1], path))
                else:
                    self.warnings.append(f"Bibliothek {name.split('/', 1)[1]} nicht gefunden: {uri}")
        if not libs:
            lib_root = self.workspace / ".lib"
            if lib_root.is_dir():
                for d in sorted(lib_root.iterdir()):
                    if (d / "typelib").is_dir():
                        libs.append((d.name, d / "typelib"))
        return libs

    def _resolve_uri(self, uri: str) -> Optional[Path]:
        if uri.startswith("WORKSPACE_LOC/"):
            return self.workspace / uri[len("WORKSPACE_LOC/"):]
        if uri.startswith("PROJECT_LOC/"):
            return self.dir / uri[len("PROJECT_LOC/"):]
        m = re.match(r"PARENT-(\d+)-PROJECT_LOC/(.*)", uri)
        if m:
            base = self.dir
            for _ in range(int(m.group(1))):
                base = base.parent
            return base / m.group(2)
        if uri.startswith("file:"):
            # Eclipse writes file:/C:/lib or file:///C:/lib
            return Path(url2pathname(urlparse(uri).path))
        if uri and Path(uri).is_absolute():
            return Path(uri)
        return None

    def _scan(self):
        for path in sorted(self.dir.rglob("*")):
            if any(part.startswith(".") for part in path.relative_to(self.dir).parts):
                continue
            suffix = path.suffix.lower()
            if suffix in TYPE_EXTENSIONS:
                info = load_type(path, None)
                if info:
                    self.types[info.full_name] = info
                    self.types_by_short.setdefault(info.name, []).append(info)
            elif suffix == ".sys":
                root = self._read_xml(path)
                if root is not None:
                    self.systems.append((path, root))
            elif suffix == ".gcf":
                root = self._read_xml(path)
                if root is not None:
                    self.global_constants.append((path, root))

    def identifiers(self) -> set:
        """Every name used in a parameter value, initial value or ST code of the project's
        types and systems, e.g. to tell which global constants are used."""
        if self._identifiers is None:
            names = set()
            roots = [t.root for t in self.types.values()] + [root for _, root in self.systems]
            for root in roots:
                for elem in root.iter():
                    texts = [elem.get("Value"), elem.get("InitialValue")]
                    if elem.tag == "ST":
                        texts += [elem.get("Text"), elem.text]
                    for text in texts:
                        if text:
                            names.update(re.findall(r"[A-Za-z_]\w*", text))
            self._identifiers = names
        return self._identifiers

    def _index_libs(self):
        self._lib_files = {}
        for lib_name, lib_dir in self.lib_dirs:
            for path in lib_dir.rglob("*"):
                if path.suffix.lower() in TYPE_EXTENSIONS + (".dev",):
                    self._lib_files.setdefault(path.stem, []).append((lib_name, path))

    def resolve(self, type_name: str) -> Optional[TypeInfo]:
        """Project types win over library types. A qualified name (a::b::C) must match the
        type's namespace; an unqualified name must be unique. Otherwise the reason is kept
        in self.problems and None is returned."""
        if not type_name:
            return None
        if type_name in self.types:
            return self.types[type_name]
        short = short_name(type_name)
        if self._lib_files is None:
            self._index_libs()
        hits = self._lib_files.get(short, [])
        if "::" in type_name:
            for lib_name, lib_dir in self.lib_dirs:
                for hit_lib, path in hits:
                    if hit_lib != lib_name:
                        continue
                    info = self._load_lib(lib_name, path)
                    # namespace from <CompilerInfo packageName> or, as in older libraries, from the folder
                    folder_name = "::".join(path.relative_to(lib_dir).with_suffix("").parts)
                    if info and type_name in (info.full_name, folder_name):
                        return info
            found = [t.full_name for t in self.types_by_short.get(short, [])] + [str(p) for _, p in hits]
            self.problems[type_name] = ("Namensraum passt zu keinem Kandidaten: " + ", ".join(found)) if found                 else "nicht gefunden"
            return None
        candidates = self.types_by_short.get(short, [])
        if len(candidates) == 1:
            return candidates[0]
        if not candidates and len(hits) == 1:
            return self._load_lib(*hits[0])
        found = [t.full_name for t in candidates] or [str(p) for _, p in hits]
        self.problems[type_name] = ("mehrdeutig: " + ", ".join(found)) if found else "nicht gefunden"
        return None

    def _load_lib(self, lib_name: str, path: Path) -> Optional[TypeInfo]:
        if path not in self._lib_cache:
            self._lib_cache[path] = load_type(path, lib_name)
        return self._lib_cache[path]

    def rel(self, path: Optional[Path]) -> str:
        if path is None:
            return ""
        for base in (self.dir, self.workspace):
            try:
                return str(path.relative_to(base)).replace("\\", "/")
            except ValueError:
                continue
        return str(path)


# ===========================================================================
# Diagrams
# ===========================================================================

@dataclass
class Diagram:
    drawing: object
    vb: Tuple[float, float, float, float]
    instances: List[object] = field(default_factory=list)   # netsvg.FBInstance

    @property
    def px_to_pt(self) -> float:
        return self.drawing.width / self.vb[2] if self.vb[2] else 0.75

    @property
    def width(self) -> float:
        return self.drawing.width

    @property
    def height(self) -> float:
        return self.drawing.height


def _viewbox(svg: str) -> Tuple[float, float, float, float]:
    m = re.search(r'viewBox="([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)"', svg)
    return tuple(float(v) for v in m.groups()) if m else (0.0, 0.0, 100.0, 100.0)


class DiagramFactory:
    def __init__(self, project: Project):
        paths = [str(p) for _, p in project.lib_dirs] + [str(project.dir)]
        self.resolver = netsvg.TypeResolver(paths)
        self.settings = netsvg.BlockSizeSettings()
        self._cache: Dict[str, Optional[Diagram]] = {}

    def network(self, key: str, xml: str) -> Optional[Diagram]:
        if key not in self._cache:
            try:
                model = netsvg.NetworkParser().parse(xml)
                self.resolver.resolve(model)
                layout = netsvg.NetworkLayoutEngine(settings=self.settings)
                layout.layout(model)
                svg = netsvg.NetworkSVGRenderer(show_shadow=False, settings=self.settings).render(model, layout)
                self._cache[key] = Diagram(self._to_drawing(svg), _viewbox(svg), list(model.instances))
            except Exception as exc:  # converter limitations must not stop the document
                print(f"[WARNUNG] Netzwerk {key}: {exc}", file=sys.stderr)
                self._cache[key] = None
        return self._cache[key]

    def interface(self, key: str, path: Path) -> Optional[Diagram]:
        if key not in self._cache:
            try:
                svg = fbsvg.convert_fbt_to_svg(str(path), show_shadow=False)
                self._cache[key] = Diagram(self._to_drawing(svg), _viewbox(svg))
            except Exception as exc:
                print(f"[WARNUNG] Schnittstelle {key}: {exc}", file=sys.stderr)
                self._cache[key] = None
        return self._cache[key]

    @staticmethod
    def _to_drawing(svg: str):
        drawing = svglib.svg2rlg(io.BytesIO(svg.encode("utf-8")))
        if drawing is None or not drawing.width or not drawing.height:
            raise ValueError("svglib konnte die SVG-Grafik nicht umsetzen")
        return drawing


# ===========================================================================
# ISOBUS object pool (VT GUI)
# ===========================================================================

VT_MASK_LABELS = {"CDataMask": "Datenmaske", "CAlarmMask": "Alarmmaske", "CSoftKeyMask": "Soft-Key-Maske"}
VT_IMAGE_PREFIX = {"CDataMask": "screen_", "CAlarmMask": "am_", "CSoftKeyMask": "skm_"}


VtRect = Tuple[float, float, float, float]   # x, y, w, h in VT pixels, y downwards


@dataclass
class VtImage:
    pil: object         # PIL image, held in memory: vt-render writes into a temporary folder
    reader: object      # ImageReader of it
    width: int
    height: int

    @classmethod
    def of(cls, pil) -> "VtImage":
        return cls(pil, ImageReader(pil), pil.width, pil.height)

    def crop(self, rect: VtRect) -> Optional["VtImage"]:
        x, y, w, h = (round(v) for v in rect)
        if w <= 0 or h <= 0:
            return None
        return VtImage.of(self.pil.crop((x, y, x + w, y + h)))


@dataclass
class VtObject:
    id: int
    cls: str
    name: str
    depth: int = 0
    rect: Optional[VtRect] = None   # in the image of the mask's sheet; children carry their top object's
    own: bool = False               # rect is the object's own (top-level object or soft key)

    @property
    def pos(self) -> Optional[Tuple[float, float]]:
        if self.rect is None:
            return None
        x, y, w, h = self.rect
        return x + w / 2, y + h / 2


@dataclass
class VtMask:
    id: int
    cls: str
    name: str
    image: Optional[VtImage] = None
    objects: List[VtObject] = field(default_factory=list)
    soft_key_mask: Optional[int] = None


class VtPool:
    """An ISO-Designer object pool: object tree from list_mask_objects.py,
    mask images from vt-render."""

    def __init__(self, pool_dir: Path, scripts_dir: Path, vt_render: Optional[Path],
                 softkeys: Optional[int], mask_size: Optional[int], title: str, org: str):
        sys.path.insert(0, str(scripts_dir))
        import list_mask_objects as lmo
        self.lmo = lmo
        jops = sorted(pool_dir.glob("*.jop"))
        if len(jops) != 1:
            raise ValueError(f"{pool_dir}: genau eine .jop-Datei erwartet, gefunden {len(jops)}")
        self.dir = pool_dir
        self.jop = jops[0]
        self.iop = pool_dir / "Output" / f"{self.jop.stem}.iop"
        (self.cls, self.names, self.children, self.paths, _, _) = lmo.load_pool(str(self.jop))
        self.sizes = self._sizes(self.jop)
        self.ids_by_name = {n: i for i, n in sorted(self.names.items()) if n}
        self.manifest: dict = {}
        self.images: Dict[str, VtImage] = {}
        self.render_error = ""

        roots = {jid: self._components(jid) for jid, c in self.cls.items() if c in VT_MASK_LABELS}
        key_counts = [len(r) for jid, r in roots.items() if self.cls[jid] == "CSoftKeyMask"]
        softkeys = softkeys or (max(key_counts) if key_counts else None)
        mask_size = mask_size or self._define("ISO_MASK_SIZE")
        if vt_render is not None:
            self._render(vt_render, softkeys, mask_size, title, org)
        settings = self.manifest.get("render_settings", {})
        self.mask_size = settings.get("data_mask_size", mask_size or 480)

        self.masks: List[VtMask] = []
        for cls in ("CDataMask", "CAlarmMask", "CSoftKeyMask"):
            for jid in sorted(j for j, c in self.cls.items() if c == cls):
                self.masks.append(self._mask(jid, roots[jid], roots, settings))
        # the sheet an object is documented on: soft keys on their soft key mask, the rest on its mask
        self.home: Dict[int, int] = {}
        for mask in sorted(self.masks, key=lambda m: m.cls != "CSoftKeyMask"):
            for obj in mask.objects:
                self.home.setdefault(obj.id, mask.id)
        self.aux = [jid for jid in sorted(self.cls) if self.cls[jid] == "CAuxFunction"]

    # ----- ISO-Designer files -----

    @staticmethod
    def _sizes(jop: Path) -> Dict[int, Tuple[int, int]]:
        sizes = {}
        for obj in ET.parse(jop).getroot().iter("Object"):
            jid = obj.get("JVS-ID")
            w = obj.find("./PropertySheet/Property[@Name='Width']/Value")
            h = obj.find("./PropertySheet/Property[@Name='Height']/Value")
            if jid is not None and w is not None and h is not None:
                try:
                    sizes[int(jid)] = (int(float(w.text)), int(float(h.text)))
                except (TypeError, ValueError):
                    pass
        return sizes

    def _jvi(self, jid: int) -> Optional[Path]:
        rel = self.paths.get(jid)
        path = (self.dir / rel) if rel else None
        return path if path is not None and path.exists() else None

    def _components(self, jid: int) -> List[Tuple[int, Optional[VtRect]]]:
        """Top-level objects of a mask's .jvi in pool order, with the rectangle of each."""
        jvi = self._jvi(jid)
        if jvi is None:
            return [(c, None) for c in self.children.get(jid, [])]
        positions = {}
        for comp in ET.parse(jvi).getroot().iter("Component"):
            left = comp.find("./PropertySheets/PropertySheet[@Name='Proxy']/Property[@Name='Left']/Value")
            top = comp.find("./PropertySheets/PropertySheet[@Name='Proxy']/Property[@Name='Top']/Value")
            if left is None or top is None:
                continue
            for o in comp.findall("./Objects/Object"):
                oid = int(o.get("JVS-ID"))
                w, h = self.sizes.get(oid, (0, 0))
                positions[oid] = (float(left.text), float(top.text), w, h)
        return [(c, positions.get(c)) for c in self.lmo.jvi_component_roots(str(jvi))]

    def _define(self, name: str) -> Optional[int]:
        header = self.iop.with_name(self.iop.name + ".h")
        if header.exists():
            m = re.search(rf"#define\s+{name}\s+(\d+)", header.read_text(encoding="latin-1"))
            if m:
                return int(m.group(1))
        return None

    # ----- vt-render -----

    def _render(self, exe: Path, softkeys: Optional[int], mask_size: Optional[int], title: str, org: str):
        if not self.iop.exists():
            self.render_error = f"{self.iop} fehlt (Pool im ISO-Designer übersetzen)"
            return
        data = self.iop.read_bytes()
        with tempfile.TemporaryDirectory(prefix="vt-render-") as tmp:
            meta = Path(tmp) / "meta.yaml"
            meta.write_text("\n".join([
                f'id: "{hashlib.sha256(data).hexdigest()[:8]}"',
                f"name: {title}",
                f"manufacturer: {org or 'TODO'}",
                "public: false",
                "render:",
                f"  data_mask_size: {mask_size or 'TODO'}",
                f"  softkey_count: {softkeys or 'TODO'}",
                "  softkey_width: TODO",
                "  softkey_height: TODO",
                "known_issues:", ""]), encoding="utf-8")
            out = Path(tmp) / "out"
            try:
                proc = subprocess.run([str(exe), "--pool", str(self.iop), "--meta", str(meta), "--out", str(out)],
                                      capture_output=True, text=True, timeout=300)
            except OSError as exc:
                self.render_error = f"vt-render konnte nicht gestartet werden: {exc}"
                return
            print(f"  vt-render: {proc.stdout.strip() or proc.stderr.strip()}")
            try:
                import json
                self.manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.render_error = f"vt-render (Exit-Code {proc.returncode}): kein manifest.json ({exc})"
                return
            load = self.manifest.get("load", {})
            if not load.get("ok"):
                self.render_error = f"vt-render konnte den Pool nicht laden: {load.get('error')}"
            for err in self.manifest.get("errors", []):
                print(f"[WARNUNG] vt-render {err.get('file')}: {err.get('error')}", file=sys.stderr)
            from PIL import Image
            for entry in self.manifest.get("images", []):
                with Image.open(out / entry["file"]) as img:
                    img.load()
                    self.images[entry["file"]] = VtImage.of(img.convert("RGB"))

    # ----- masks -----

    def _mask(self, jid: int, components, roots, settings: dict) -> VtMask:
        cls = self.cls[jid]
        mask = VtMask(jid, cls, self.names.get(jid, ""), self.images.get(f"{VT_IMAGE_PREFIX[cls]}{jid}.png"))
        if cls == "CSoftKeyMask":
            mask.objects = self._tree(self._key_positions(components, settings, 0))
            return mask
        mask.objects = self._tree(components)
        jvi = self._jvi(jid)
        skm = self.lmo.jvi_softkeymask_id(str(jvi)) if jvi is not None else None
        if skm is not None and self.cls.get(skm) == "CSoftKeyMask":
            mask.soft_key_mask = skm
            if cls == "CDataMask":
                # the screen image shows the soft key mask right of the data mask
                keys = self._tree(self._key_positions(roots[skm], settings, self.mask_size))
                for obj in keys:
                    obj.depth += 1
                mask.objects += [VtObject(skm, "CSoftKeyMask", self.names.get(skm, ""))] + keys
        return mask

    @staticmethod
    def _key_positions(components, settings: dict, x0: float):
        """Key rectangles as the VT lays them out: columns filled top to bottom, right column first."""
        rows, cols = settings.get("softkey_rows", 0), settings.get("softkey_columns", 0)
        pad, w, h = (settings.get(k, 0) for k in ("softkey_padding", "softkey_width", "softkey_height"))
        out = []
        for i, (jid, _) in enumerate(components):
            rect = None
            if rows and cols and i < rows * cols:
                col, row = cols - 1 - i // rows, i % rows
                rect = (x0 + pad + col * (w + pad), pad + row * (h + pad), w, h)
            out.append((jid, rect))
        return out

    def _tree(self, components) -> List[VtObject]:
        """All objects reachable from the mask's top-level objects; CProxy hops are left out."""
        objects: List[VtObject] = []
        for jid, rect in components:
            shown: Dict[int, int] = {}
            for depth, child in self.lmo.resolve_tree([jid], self.children, set()):
                level = shown.get(depth - 1, 0) if depth else 0
                if self.cls.get(child) == "CProxy":
                    shown[depth] = level
                    continue
                shown[depth] = level + 1
                objects.append(VtObject(child, self.cls.get(child, "?"), self.names.get(child, ""), level, rect,
                                        own=child == jid))
        return objects

    def lookup(self, value: str, numeric: bool) -> Optional[int]:
        """Object id a 4diac parameter value refers to: an object name, or (numeric) a plain
        object id; plain numbers only count on ISOBUS blocks, elsewhere they are times, limits, ..."""
        value = value.strip().strip("'\"").split("#")[-1]
        if value in self.ids_by_name:
            return self.ids_by_name[value]
        if numeric and value.isdigit() and int(value) in self.cls and int(value) != 0:
            return int(value)
        return None

    def label(self, jid: int) -> str:
        name = self.names.get(jid, "")
        return f"{name} ({jid})" if name else str(jid)


# ===========================================================================
# Sheet layout primitives
# ===========================================================================

@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def top(self) -> float:
        return self.y + self.h


@dataclass
class Cell:
    text: str
    link: Optional[str] = None     # bookmark key
    font: Optional[str] = None
    indent: int = 0                # indentation steps (table of contents)
    parts: Optional[List[Tuple[str, Optional[str]]]] = None   # ", "-separated items with own links
    image: Optional["VtImage"] = None   # drawn instead of text, image_h high
    image_h: float = 0.0
    bold: bool = False             # TGL has no bold face: drawn with an outline

    def lines(self, size: float, width: float) -> List[List[Tuple[str, Optional[str], float]]]:
        """Wrapped lines as segments (text, link, x offset)."""
        font = self.font or FONT
        width -= self.indent * INDENT
        if self.parts is None:
            return [[(line, self.link, 0.0)] for line in wrap(self.text, font, size, width)]
        lines: List[List[Tuple[str, Optional[str], float]]] = [[]]
        x = 0.0
        for i, (text, link) in enumerate(self.parts):
            label = text + ("," if i < len(self.parts) - 1 else "")
            w = pdfmetrics.stringWidth(label, font, size)
            gap = pdfmetrics.stringWidth(" ", font, size) if lines[-1] else 0.0
            if lines[-1] and x + gap + w > width:
                lines.append([])
                x, gap = 0.0, 0.0
            lines[-1].append((label, link, x + gap))
            x += gap + w
        return lines


@dataclass
class Sheet:
    key: str
    title: str
    subtitle: str = ""
    level: int = 0
    landscape: bool = True
    type_info: Optional[TypeInfo] = None
    ops: List[Callable] = field(default_factory=list)   # (canvas, doc) -> None
    outline: bool = True
    number: int = 0


class Geometry:
    """Frame, field grid, title block and drawing area of one sheet."""

    def __init__(self, paper: str, landscape: bool):
        w, h = PAPER[paper]
        self.page_w, self.page_h = (w, h) if landscape else (h, w)
        self.frame = Rect(FRAME_LEFT, FRAME_OTHER, self.page_w - FRAME_LEFT - FRAME_OTHER,
                          self.page_h - 2 * FRAME_OTHER)
        f = self.frame
        self.inner = Rect(f.x + GRID_BAND, f.y + GRID_BAND, f.w - 2 * GRID_BAND, f.h - 2 * GRID_BAND)
        self.cols = max(4, round(self.inner.w / FIELD_SIZE))
        self.rows = max(3, round(self.inner.h / FIELD_SIZE))
        self.title = Rect(self.inner.x, self.inner.y, self.inner.w, TITLE_H)
        self.area = Rect(self.inner.x + AREA_PAD, self.inner.y + TITLE_H + AREA_PAD,
                         self.inner.w - 2 * AREA_PAD, self.inner.h - TITLE_H - 2 * AREA_PAD)

    def field_of(self, x: float, y: float) -> str:
        i = self.inner
        col = min(self.cols, max(1, int((x - i.x) / (i.w / self.cols)) + 1))
        row = min(self.rows, max(1, int((i.top - y) / (i.h / self.rows)) + 1))
        return f"{chr(ord('A') + row - 1)}{col}"


class Flow:
    """Places headings, paragraphs, figures and tables onto continuation sheets."""

    BODY = 7.5
    LEAD = 1.3

    def __init__(self, doc: "Document", make_sheet: Callable[[bool], Sheet]):
        self.doc = doc
        self.make_sheet = make_sheet
        self.sheets: List[Sheet] = []
        self.sheet: Optional[Sheet] = None
        self.geo: Optional[Geometry] = None
        self.y = 0.0
        self._new_sheet()

    def _new_sheet(self):
        self.sheet = self.make_sheet(not self.sheets)
        self.sheets.append(self.sheet)
        self.geo = self.doc.geometry(self.sheet.landscape)
        self.y = self.geo.area.top

    def _need(self, h: float):
        if self.y - h < self.geo.area.y and self.y < self.geo.area.top:
            self._new_sheet()

    @property
    def width(self) -> float:
        return self.geo.area.w

    def space(self, h: float = 3 * mm):
        self.y -= h

    def heading(self, text: str, size: float = 11):
        self._need(size * 2.2 + 12 * mm)
        x, y = self.geo.area.x, self.y - size
        self.sheet.ops.append(lambda c, d: _text(c, x, y, text, FONT, size, INK))
        self.sheet.ops.append(lambda c, d: _hline(c, x, y - 1.5 * mm, x + 60 * mm, ACCENT, 1.2))
        self.y = y - 4 * mm

    def subheading(self, text: str, size: float = 8.5):
        self._need(size * 1.6 + 10 * mm)
        x, y = self.geo.area.x, self.y - size
        self.sheet.ops.append(lambda c, d: _text(c, x, y, text, FONT, size, INK))
        self.y = y - 2 * mm

    def para(self, text: str, size: float = BODY, font: Optional[str] = None, color=INK,
             link: Optional[str] = None):
        font = font or FONT
        lead = size * self.LEAD
        for line in wrap(text, font, size, self.width):
            self._need(lead)
            x, y = self.geo.area.x, self.y - size
            self.sheet.ops.append(lambda c, d, line=line, x=x, y=y: _text(c, x, y, line, font, size, color))
            if link:
                w = pdfmetrics.stringWidth(line, font, size)
                self.sheet.ops.append(lambda c, d, x=x, y=y, w=w: _link(c, link, x, y - 1, w, size + 2))
            self.y -= lead

    def code(self, text: str, size: float = 6.5):
        lead = size * 1.25
        for line in text.replace("\t", "    ").split("\n"):
            self._need(lead)
            x, y = self.geo.area.x + 2 * mm, self.y - size
            self.sheet.ops.append(lambda c, d, line=line, x=x, y=y: _text(c, x, y, line, FONT_CODE, size, INK))
            self.y -= lead

    def figure(self, diagram: Optional[Diagram], max_h: float, max_scale: float = 1.0):
        if diagram is None or diagram.width <= 0:
            return
        scale = min(self.width / diagram.width, max_h / diagram.height, max_scale)
        h = diagram.height * scale
        self._need(h + 2 * mm)
        x, y = self.geo.area.x, self.y - h
        self.sheet.ops.append(lambda c, d: _draw_diagram(c, diagram, x, y, scale))
        self.y = y - 3 * mm

    def image(self, img: Optional[VtImage], max_h: float, max_scale: float = 1.0):
        if img is None:
            return
        scale = min(self.width / img.width, max_h / img.height, max_scale)
        w, h = img.width * scale, img.height * scale
        self._need(h + 2 * mm)
        x, y = self.geo.area.x, self.y - h
        self.sheet.ops.append(lambda c, d: _draw_image(c, img, x, y, w, h))
        self.y = y - 3 * mm

    def table(self, columns: List[Tuple[str, float]], rows: List[List[Cell]], size: float = 6.8):
        """columns: (title, relative width)."""
        if not rows:
            return
        total = sum(w for _, w in columns)
        widths = [self.width * w / total for _, w in columns]
        pad = 1.2 * mm
        lead = size * 1.25

        def height(wrapped, cells: List[Cell] = ()) -> float:
            text = max(len(lines) for lines in wrapped) * lead + 2 * pad - (lead - size)
            return max([text] + [cell.image_h + 2 * pad for cell in cells if cell.image is not None])

        header = [Cell(t) for t, _ in columns]
        hw = [cell.lines(size, w - 2 * pad) for cell, w in zip(header, widths)]
        hh = height(hw)
        # rows taller than a whole sheet are split into several rows
        max_lines = max(1, int((self.geo.area.h - hh - 2 * pad + (lead - size)) / lead))

        def emit(cells: List[Cell], wrapped, is_header: bool):
            h = height(wrapped, cells)
            x0, y0 = self.geo.area.x, self.y - h
            items = list(zip(cells, wrapped, widths))
            self.sheet.ops.append(lambda c, d: _table_row(c, x0, y0, h, items, size, lead, pad, is_header))
            self.y = y0

        first = True
        for row in rows:
            full = [cell.lines(size, w - 2 * pad) for cell, w in zip(row, widths)]
            n = max(len(lines) for lines in full)
            for start in range(0, n, max_lines):
                wrapped = [lines[start:start + max_lines] or [[]] for lines in full]
                h = height(wrapped, row)
                if first:
                    self._need(hh + h)
                    emit(header, hw, True)
                    first = False
                elif self.y - h < self.geo.area.y:
                    self._new_sheet()
                    emit(header, hw, True)
                emit(row, wrapped, False)
        self.y -= 4 * mm


# ===========================================================================
# Drawing helpers
# ===========================================================================

def _text(c, x, y, text, font, size, color=INK, anchor="left"):
    c.setFont(font, size)
    c.setFillColor(color)
    if anchor == "center":
        c.drawCentredString(x, y, text)
    elif anchor == "right":
        c.drawRightString(x, y, text)
    else:
        c.drawString(x, y, text)


def _hline(c, x1, y, x2, color=INK, width=0.5):
    c.setStrokeColor(color)
    c.setLineWidth(width)
    c.line(x1, y, x2, y)


def _link(c, key, x, y, w, h):
    c.linkRect("", key, (x, y, x + w, y + h), relative=0, thickness=0)


def _draw_diagram(c, diagram: Diagram, x, y, scale):
    d = diagram.drawing
    c.saveState()
    # svglib does not clip to the viewBox; connections routed outside it would cross the frame
    clip = c.beginPath()
    clip.rect(x, y, d.width * scale, d.height * scale)
    c.clipPath(clip, stroke=0, fill=0)
    c.translate(x, y)
    c.scale(scale, scale)
    renderPDF.draw(d, c, 0, 0)
    c.restoreState()


def _draw_image(c, img: VtImage, x, y, w, h):
    c.drawImage(img.reader, x, y, w, h)
    c.setStrokeColor(GREY)
    c.setLineWidth(0.3)
    c.rect(x, y, w, h, stroke=1, fill=0)


def _table_row(c, x, y, h, items, size, lead, pad, is_header):
    if is_header:
        c.setFillColor(LIGHT)
        c.rect(x, y, sum(w for _, _, w in items), h, stroke=0, fill=1)
    cx = x
    for cell, lines, w in items:
        if cell.image is not None:
            img = cell.image
            s = min(cell.image_h / img.height, (w - 2 * pad) / img.width)
            c.drawImage(img.reader, cx + pad, y + h - pad - img.height * s, img.width * s, img.height * s)
        font = cell.font or FONT
        ty = y + h - pad - size
        for line in lines:
            for text, link, dx in line:
                tx = cx + pad + cell.indent * INDENT + dx
                if cell.bold:
                    # the text render mode outlives the text object: keep it inside a saved state
                    c.saveState()
                    c.setStrokeColor(LINK if link else INK)
                    c.setFillColor(LINK if link else INK)
                    c.setLineWidth(size * 0.06)
                    t = c.beginText(tx, ty)
                    t.setFont(font, size)
                    t.setTextRenderMode(2)   # fill and stroke
                    t.textOut(text)
                    c.drawText(t)
                    c.restoreState()
                    continue
                _text(c, tx, ty, text, font, size, LINK if link else INK)
                if link and cell.parts is not None:
                    _link(c, link, tx, ty - 1.5, pdfmetrics.stringWidth(text, font, size), size + 2.5)
            ty -= lead
        if cell.link and cell.parts is None:
            _link(c, cell.link, cx, y, w, h)
        c.setStrokeColor(GREY)
        c.setLineWidth(0.3)
        c.rect(cx, y, w, h, stroke=1, fill=0)
        cx += w


# ===========================================================================
# Document
# ===========================================================================

@dataclass
class Usage:
    sheet_key: str
    instance: str
    field: str = ""


class Document:
    def __init__(self, project: Project, paper: str, title: Optional[str], vt: Optional[VtPool] = None):
        self.project = project
        self.paper = paper
        self.vt = vt
        self.diagrams = DiagramFactory(project)
        self.custom_title = title
        self.sheets: List[Sheet] = []
        self.page_of: Dict[str, int] = {}
        self.final_usages: Dict[str, List[Usage]] = {}
        self.today = datetime.date.today().strftime("%d.%m.%Y")
        self._geo: Dict[bool, Geometry] = {}
        system = project.systems[0][1] if project.systems else None
        self.system = system
        vi = system.find("VersionInfo") if system is not None else None
        self.org = vi.get("Organization", "") if vi is not None else ""
        self.author = vi.get("Author", "") if vi is not None else ""
        self.date = vi.get("Date", "") if vi is not None else ""
        product = project.manifest.find("Product") if project.manifest is not None else None
        pvi = product.find("VersionInfo") if product is not None else None
        self.version = pvi.get("Version", "") if pvi is not None else (vi.get("Version", "") if vi is not None else "")
        self.project_title = title or (system.get("Name") if system is not None else project.name)

    # ----- helpers -----

    def geometry(self, landscape: bool) -> Geometry:
        if landscape not in self._geo:
            self._geo[landscape] = Geometry(self.paper, landscape)
        return self._geo[landscape]

    def ref(self, key: str) -> str:
        return f"S{self.page_of.get(key, 99)}"

    def total(self) -> int:
        return max(len(self.sheets), 1)

    # ----- planning -----

    def plan(self):
        """Lay out all sheets. Repeated until page numbers are stable, since
        references ('-> S9') are part of the layout."""
        for _ in range(10):
            before = dict(self.page_of)
            self._plan_once()
            self.page_of = {}
            for n, sheet in enumerate(self.sheets, 1):
                sheet.number = n
                self.page_of.setdefault(sheet.key, n)
            if self.page_of == before and before:
                return
        raise RuntimeError("Blattnummern stabilisieren sich nicht - Verweise wären falsch")

    def _plan_once(self):
        self.sheets = []
        self.documented: Dict[str, TypeInfo] = {}
        self.usages: Dict[str, List[Usage]] = {}
        self.lib_usages: Dict[str, Tuple[TypeInfo, List[Usage]]] = {}
        self.missing: Dict[str, List[Usage]] = {}
        self.vt_usages: Dict[int, List[Usage]] = {}

        body: List[Sheet] = []
        self._out = body
        roots = 0
        for sys_path, system in self.project.systems:
            for device in system.findall("Device"):
                for resource in device.findall("Resource"):
                    self._plan_resource(sys_path, system, device, resource)
                    roots += 1
        if roots == 0:
            print("[WARNUNG] Keine Ressource im System gefunden - dokumentiere alle Projekttypen.",
                  file=sys.stderr)
        # project types not reached from a resource, then the project's data types
        unused = [t for t in self.project.types.values()
                  if t.key not in self.documented and t.kind != "ResourceType"]
        for t in sorted(unused, key=lambda t: (t.kind == "DataType", t.full_name.lower())):
            if t.key not in self.documented:
                self._plan_type(t, path=t.name, level=0, unused=True)
        if self.vt is not None:
            # after all networks: their instance tables collect the VT object usages
            self._plan_vt()
        self._plan_appendix()
        self.final_usages = self.usages

        cover = self._plan_cover()
        toc = self._plan_toc(body)
        self.sheets = [cover] + toc + body

    # ----- cover / toc -----

    def _plan_cover(self) -> Sheet:
        sheet = Sheet("cover", "Deckblatt", self.project_title, landscape=True, outline=True)
        geo = self.geometry(True)
        a = geo.area
        system = self.system
        ident = system.find("Identification") if system is not None else None
        lines: List[Tuple[str, str]] = []
        if ident is not None:
            for attr, label in (("Standard", "Norm"), ("Classification", "Klassifikation"),
                                ("ApplicationDomain", "Anwendungsbereich"), ("Function", "Funktion"),
                                ("Type", "Typ")):
                if ident.get(attr):
                    lines.append((label, ident.get(attr)))
        if self.org:
            lines.append(("Organisation", self.org))
        if self.author:
            lines.append(("Bearbeiter", self.author))
        if self.version:
            lines.append(("Version", self.version))
        if self.date:
            lines.append(("Datum", self.date))
        resources = [f"{d.get('Name')}.{r.get('Name')}" for _, s in self.project.systems
                     for d in s.findall("Device") for r in d.findall("Resource")]
        if resources:
            lines.append(("Ressourcen", ", ".join(resources)))
        lines.append(("Projektordner", clean(str(self.project.dir))))
        lines.append(("Erzeugt", f"{self.today} mit DocumentTheProject {__version__} / "
                                 f"IEC61499_FunctionBlock_SVG {netsvg.__version__}"))

        title = self.project_title
        description = clean(ident.get("Description")) if ident is not None else ""

        def draw(c, d):
            cx = a.x + a.w / 2
            y = a.top - 30 * mm
            _text(c, cx, y, "Projektdokumentation", FONT, 14, GREY, "center")
            _text(c, cx, y - 16 * mm, title, FONT, 30, INK, "center")
            _hline(c, cx - 60 * mm, y - 21 * mm, cx + 60 * mm, ACCENT, 2)
            _text(c, cx, y - 29 * mm, "Eclipse 4diac™ / IEC 61499", FONT, 11, GREY, "center")
            ty = y - 44 * mm
            lx = a.x + 30 * mm
            for label, value in lines:
                _text(c, lx, ty, label, FONT, 8, GREY)
                for i, line in enumerate(wrap(value, FONT, 9, a.w * 0.55 - 42 * mm)[:3]):
                    _text(c, lx + 38 * mm, ty - i * 11, line, FONT, 9)
                    if i:
                        ty -= 11
                ty -= 13
            if description:
                dx = a.x + a.w * 0.58
                dy = y - 44 * mm
                _text(c, dx, dy, "Beschreibung", FONT, 8, GREY)
                for line in wrap(description, FONT, 7.5, a.w * 0.40)[:18]:
                    dy -= 10
                    _text(c, dx, dy, line, FONT, 7.5)

        sheet.ops.append(draw)
        return sheet

    def _plan_toc(self, body: List[Sheet]) -> List[Sheet]:
        made: List[Sheet] = []

        def make(first: bool) -> Sheet:
            s = Sheet("toc" if first else f"toc{len(made)}", "Inhaltsverzeichnis",
                      level=0, outline=first)
            made.append(s)
            return s

        flow = Flow(self, make)
        flow.heading("Inhaltsverzeichnis")
        rows = []
        for sheet in body:
            rows.append([Cell(self.ref(sheet.key), sheet.key), Cell(sheet.title, sheet.key, indent=sheet.level),
                         Cell(sheet.subtitle)])
        flow.table([("Blatt", 1.2), ("Blattinhalt", 9), ("Hierarchie / Datei", 9)], rows, size=7)
        return made

    # ----- resource -----

    def _plan_resource(self, sys_path: Path, system: ET.Element, device: ET.Element, resource: ET.Element):
        dev_name, res_name = device.get("Name", ""), resource.get("Name", "")
        path = f"{dev_name}.{res_name}"
        # system file in the key: several .sys files may contain the same device/resource names
        key = f"res:{self.project.rel(sys_path)}:{path}"
        res_type = self.project.resolve(resource.get("Type", ""))
        mapped = self._mapped_elements(system, path)
        xml = self._resource_network_xml(res_name, resource, res_type, mapped)
        root = ET.fromstring(xml)
        info = TypeInfo(name=res_name, full_name=path, path=sys_path, root=root, kind="Resource",
                        synthetic_key=key)
        self.documented[key] = info
        subtitle = f"{path} – Typ {resource.get('Type', '')}"
        net_sheet = self._plan_network_sheet(info, f"Ressource {res_name}", subtitle, path, 0)
        flow = self._details_flow(info, f"Ressource {res_name} – Übersicht", subtitle, 1)
        flow.heading(f"Ressource {path}")
        table = [[Cell("Gerät"), Cell(f"{dev_name} ({device.get('Type', '')})")],
                 [Cell("Ressource"), Cell(f"{res_name} ({resource.get('Type', '')})")],
                 [Cell("Systemdatei"), Cell(self.project.rel(sys_path))]]
        for p in device.findall("Parameter"):
            table.append([Cell(f"Geräteparameter {p.get('Name')}"),
                          Cell(f"{p.get('Value', '')}  {clean(p.get('Comment'))}")])
        for p in resource.findall("Parameter"):
            table.append([Cell(f"Ressourcenparameter {p.get('Name')}"), Cell(p.get("Value", ""))])
        if res_type is not None:
            comment = clean(res_type.root.get("Comment"))
            if comment:
                table.append([Cell("Ressourcentyp"), Cell(comment)])
        flow.table([("Eigenschaft", 3), ("Wert", 9)], table)
        if mapped:
            flow.subheading("Zugeordnete Applikationselemente (Mapping)")
            flow.table([("Applikation", 4), ("Element", 4), ("Typ", 4)],
                       [[Cell(app), Cell(elem.get("Name", "")), Cell(elem.get("Type", "") or "(untypisiert)")]
                        for app, elem, _ in mapped])
        segments = system.findall("Segment")
        links = [l for l in system.findall("Link") if l.get("CommResource") in (dev_name, path)]
        if links:
            flow.subheading("Kommunikation")
            flow.table([("Segment", 4), ("Segmenttyp", 4), ("Kommentar", 6)],
                       [[Cell(l.get("SegmentName", "")),
                         Cell(next((s.get("Type", "") for s in segments if s.get("Name") == l.get("SegmentName")), "")),
                         Cell(clean(l.get("Comment")))] for l in links])
        self._instance_table(flow, info, net_sheet)
        self._out.extend(flow.sheets)
        self._recurse(info, path, net_sheet, 1)

    @staticmethod
    def _mapped_elements(system: ET.Element, target: str) -> List[Tuple[str, ET.Element, ET.Element]]:
        """(application name, mapped element, network containing it) for each mapping to target."""
        found = []
        for mapping in system.findall("Mapping"):
            if mapping.get("To") != target:
                continue
            app_name, _, elem_path = mapping.get("From", "").partition(".")
            app = next((a for a in system.findall("Application") if a.get("Name") == app_name), None)
            net = app.find("SubAppNetwork") if app is not None else None
            elem, parent = None, None
            for part in elem_path.split("."):
                if net is None:
                    elem = None
                    break
                parent = net
                elem = next((e for e in list(net) if e.get("Name") == part and e.tag in ("FB", "SubApp")), None)
                net = elem.find("SubAppNetwork") if elem is not None else None
            if elem is not None:
                found.append((app_name, elem, parent))
        return found

    @staticmethod
    def _resource_network_xml(name: str, resource: ET.Element, res_type: Optional[TypeInfo],
                              mapped: List[Tuple[str, ET.Element, ET.Element]]) -> str:
        """The resource as 4diac shows it: resource-type FBs (START), the resource's own
        FB network, and the mapped application elements placed below them."""
        root = ET.Element("SubAppType", Name=name, Comment=resource.get("Comment", ""))
        ET.SubElement(root, "SubAppInterfaceList")
        net = ET.SubElement(root, "SubAppNetwork")
        sources = []
        if res_type is not None and res_type.root.find("FBNetwork") is not None:
            sources.append(res_type.root.find("FBNetwork"))
        if resource.find("FBNetwork") is not None:
            sources.append(resource.find("FBNetwork"))
        conns: Dict[str, ET.Element] = {}
        max_y = 0.0
        for src in sources:
            for elem in src:
                if elem.tag in ("FB", "SubApp"):
                    net.append(elem)
                    max_y = max(max_y, float(elem.get("y", "0")))
                elif elem.tag.endswith("Connections"):
                    target = conns.setdefault(elem.tag, ET.SubElement(net, elem.tag))
                    target.extend(list(elem))
        if mapped:
            min_x = min(float(e.get("x", "0")) for _, e, _ in mapped)
            min_y = min(float(e.get("y", "0")) for _, e, _ in mapped)
            for _, elem, _ in mapped:
                copy = ET.fromstring(ET.tostring(elem))
                copy.set("x", str(float(elem.get("x", "0")) - min_x + 100))
                copy.set("y", str(float(elem.get("y", "0")) - min_y + max_y + 1200))
                net.append(copy)
            # application connections between mapped elements of the same network
            for parent in {id(p): p for _, _, p in mapped}.values():
                names = {e.get("Name") for _, e, p in mapped if p is parent}
                for section in parent:
                    if not section.tag.endswith("Connections"):
                        continue
                    for conn in section.findall("Connection"):
                        ends = (conn.get("Source", ""), conn.get("Destination", ""))
                        if all("." in e and e.split(".")[0] in names for e in ends):
                            target = conns.setdefault(section.tag, ET.SubElement(net, section.tag))
                            target.append(conn)
        return ET.tostring(root, encoding="unicode")

    # ----- types -----

    def _plan_type(self, t: TypeInfo, path: str, level: int, unused: bool = False):
        self.documented[t.key] = t
        label = KIND_LABELS.get(t.kind, t.kind)
        if not unused:
            subtitle = path
        elif t.kind == "DataType":
            subtitle = self.project.rel(t.path)
        else:
            subtitle = f"{self.project.rel(t.path)} (nicht in einer Ressource verwendet)"
        net_sheet = None
        if t.has_network:
            net_sheet = self._plan_network_sheet(t, f"{label} {t.name}", subtitle, path, level)
        flow = self._details_flow(t, f"{t.name} – Schnittstelle", subtitle,
                                  level + 1 if net_sheet else level, first_key=None if net_sheet else t.key)
        self._type_details(flow, t)
        if net_sheet:
            self._instance_table(flow, t, net_sheet)
        self._out.extend(flow.sheets)
        if net_sheet:
            self._recurse(t, path, net_sheet, level + 1)

    def _details_flow(self, t: TypeInfo, title: str, subtitle: str, level: int,
                      first_key: Optional[str] = None) -> Flow:
        base = first_key or f"{t.key}#details"

        def make(first: bool) -> Sheet:
            n = len(flow_sheets)
            s = Sheet(base if first else f"{base}#{n}", title if first else f"{title} (Forts.)",
                      subtitle, level=level, landscape=True, type_info=t, outline=first)
            flow_sheets.append(s)
            return s

        flow_sheets: List[Sheet] = []
        return Flow(self, make)

    def _plan_network_sheet(self, t: TypeInfo, title: str, subtitle: str, path: str, level: int) -> Sheet:
        xml = ET.tostring(t.root, encoding="unicode") if t.path is None or t.synthetic_key else str(t.path)
        diagram = self.diagrams.network(t.key, xml)
        landscape = True
        if diagram is not None:
            land, port = self.geometry(True).area, self.geometry(False).area
            s_land = min(land.w / diagram.width, land.h / diagram.height)
            s_port = min(port.w / diagram.width, port.h / diagram.height)
            landscape = s_land >= s_port
        sheet = Sheet(t.key, title, subtitle, level=level, landscape=landscape, type_info=t)
        sheet.diagram = diagram
        sheet.inst_boxes = {}
        geo = self.geometry(landscape)
        a = geo.area
        if diagram is None:
            sheet.ops.append(lambda c, d: _text(c, a.x, a.top - 10, "(Netzwerk konnte nicht dargestellt werden)",
                                                FONT_ITALIC, 9, GREY))
            self._out.append(sheet)
            return sheet
        scale = min(a.w / diagram.width, a.h / diagram.height, 1.6)
        x = a.x + (a.w - diagram.width * scale) / 2
        y = a.y + (a.h - diagram.height * scale) / 2
        sheet.ops.append(lambda c, d: _draw_diagram(c, diagram, x, y, scale))
        k = diagram.px_to_pt * scale
        vbx, vby, _, vbh = diagram.vb
        for inst in diagram.instances:
            bw = inst.block_width or inst.figure_width
            x0 = x + (inst.render_x - vbx) * k
            y_top = y + (vbh - (inst.render_y - vby)) * k
            y0 = y_top - inst.block_height * k
            sheet.inst_boxes[inst.name] = (x0, y0, bw * k, inst.block_height * k)
        self._out.append(sheet)
        return sheet

    def _children(self, t: TypeInfo) -> List[Tuple[ET.Element, Optional[TypeInfo]]]:
        net = network_element(t.root)
        if net is None:
            return []
        out = []
        for elem in net:
            if elem.tag not in ("FB", "SubApp"):
                continue
            type_name = elem.get("Type", "")
            if not type_name and elem.tag == "SubApp" and elem.find("SubAppNetwork") is not None:
                out.append((elem, self._untyped_subapp(t, elem)))
            else:
                out.append((elem, self.project.resolve(type_name)))
        return out

    def _untyped_subapp(self, parent: TypeInfo, elem: ET.Element) -> TypeInfo:
        root = ET.Element("SubAppType", Name=elem.get("Name", ""), Comment=elem.get("Comment", ""))
        iface = elem.find("SubAppInterfaceList")
        root.append(iface if iface is not None else ET.Element("SubAppInterfaceList"))
        root.append(elem.find("SubAppNetwork"))
        key = f"{parent.key}/{elem.get('Name')}"
        return TypeInfo(name=elem.get("Name", ""), full_name=key, path=None, root=root,
                        kind="UntypedSubApp", synthetic_key=key)

    def _recurse(self, t: TypeInfo, path: str, net_sheet: Sheet, level: int):
        for elem, child in self._children(t):
            name = elem.get("Name", "")
            usage = Usage(net_sheet.key, name, self._field(net_sheet, name))
            if child is None:
                self.missing.setdefault(elem.get("Type", ""), []).append(usage)
                continue
            if child.library is not None:
                entry = self.lib_usages.setdefault(child.full_name, (child, []))
                entry[1].append(usage)
                continue
            self.usages.setdefault(child.key, []).append(usage)
            if child.key not in self.documented:
                self._plan_type(child, f"{path}.{name}", level)

    def _field(self, sheet: Sheet, inst: str) -> str:
        box = getattr(sheet, "inst_boxes", {}).get(inst)
        if not box:
            return ""
        x, y, w, h = box
        return self.geometry(sheet.landscape).field_of(x + w / 2, y + h / 2)

    def _usage_cell(self, usages: List[Usage]) -> Cell:
        """'S8/E3 (Klappen), S8/B4 (Verteiler)' - every item links to its network sheet."""
        parts = [(f"{self.ref(u.sheet_key)}/{u.field} ({u.instance})" if u.field
                  else f"{self.ref(u.sheet_key)} ({u.instance})", u.sheet_key) for u in usages]
        return Cell(", ".join(text for text, _ in parts), parts=parts)

    def _instance_table(self, flow: Flow, t: TypeInfo, net_sheet: Sheet):
        rows = []
        for elem, child in self._children(t):
            name = elem.get("Name", "")
            params = ", ".join(f"{p.get('Name')} = {p.get('Value', '')}" for p in elem.findall("Parameter"))
            field_ = self._field(net_sheet, name)
            if child is not None and child.library is None:
                ref = Cell(f"{self.ref(child.key)}", child.key)
                type_cell = Cell(child.name if child.kind != "UntypedSubApp" else "(untypisiert)", child.key)
            elif child is not None:
                ref = Cell(child.library or "")
                type_cell = Cell(elem.get("Type", ""))
            else:
                ref = Cell("nicht gefunden")
                type_cell = Cell(elem.get("Type", ""))
            vt_ids = self._vt_refs(elem)
            for oid in vt_ids:
                self.vt_usages.setdefault(oid, []).append(Usage(net_sheet.key, name, field_))
            if vt_ids and (child is None or child.library is not None):
                key = self._vt_key(vt_ids[0])
                ref = Cell(f"{ref.text}, VT {self.ref(key)}".lstrip(", "), key)
            where = Cell(f"{self.ref(net_sheet.key)}/{field_}", net_sheet.key) if field_ else Cell("")
            rows.append([Cell(name), where, type_cell, Cell(params), ref])
            # instance -> type reference next to the block on the network sheet
            box = getattr(net_sheet, "inst_boxes", {}).get(name)
            if box and child is not None and child.library is None:
                net_sheet.ops.append(self._instance_ref_op(box, child.key))
        if rows:
            flow.subheading("Instanzen")
            flow.table([("Instanz", 3), ("Blatt/Feld", 1.3), ("Typ", 3.5), ("Parameter", 8), ("Verweis", 2)], rows)
        net = network_element(t.root)
        groups = [g for g in net if g.tag == "Group"] if net is not None else []
        if groups:
            flow.subheading("Gruppen")
            flow.table([("Gruppe", 3), ("Kommentar", 9)],
                       [[Cell(g.get("Name", "")), Cell(clean(g.get("Comment")))] for g in groups])

    def _vt_refs(self, elem: ET.Element) -> List[int]:
        """VT objects named in the instance's parameters, e.g. QI := SoftKey_Licht."""
        if self.vt is None:
            return []
        numeric = "isobus" in elem.get("Type", "").lower()
        ids = [self.vt.lookup(p.get("Value", ""), numeric) for p in elem.findall("Parameter")]
        return [i for i in dict.fromkeys(ids) if i is not None]

    def _vt_key(self, oid: int) -> str:
        if self.vt is None:
            return "vt:pool"
        if self.vt.cls.get(oid) in VT_MASK_LABELS:
            return f"vt:{oid}"
        home = self.vt.home.get(oid)
        return f"vt:{home}" if home is not None else "vt:pool"

    def _instance_ref_op(self, box, key):
        x, y, w, h = box

        def op(c, d):
            label = f"-> {d.ref(key)}"
            size = 6
            tw = pdfmetrics.stringWidth(label, FONT, size)
            tx, ty = x + w - tw, y - size - 1
            c.setFillColor(colors.white)
            c.rect(tx - 0.8, ty - 1.2, tw + 1.6, size + 1.8, stroke=0, fill=1)
            _text(c, tx, ty, label, FONT, size, LINK)
            _link(c, key, tx - 1, ty - 1.5, tw + 2, size + 2.5)
            _link(c, key, x, y, w, h)
        return op

    def _type_details(self, flow: Flow, t: TypeInfo):
        root = t.root
        flow.heading(f"{KIND_LABELS.get(t.kind, t.kind)} {t.full_name}")
        props = []
        if t.path is not None and t.kind != "Resource":
            props.append([Cell("Datei"), Cell(self.project.rel(t.path))])
        if clean(root.get("Comment")):
            props.append([Cell("Kommentar"), Cell(clean(root.get("Comment")))])
        ident = root.find("Identification")
        if ident is not None:
            for attr in ("Standard", "Classification", "ApplicationDomain", "Function", "Type", "Description"):
                if clean(ident.get(attr)):
                    props.append([Cell(attr), Cell(clean(ident.get(attr)))])
        for vi in root.findall("VersionInfo"):
            props.append([Cell("Version"), Cell("  ".join(
                f"{k}: {vi.get(k)}" for k in ("Version", "Author", "Organization", "Date", "Remarks") if vi.get(k)))])
        # usages of the previous planning pass: later instances are not known yet in this one
        usages = self.final_usages.get(t.key, [])
        if usages:
            props.append([Cell("Verwendet in"), self._usage_cell(usages)])
        if props:
            flow.table([("Eigenschaft", 2.5), ("Wert", 12)], props)

        if t.path is not None and t.kind not in ("DataType", "Resource", "UntypedSubApp") and self._has_ports(root):
            flow.subheading("Schnittstelle")
            flow.figure(self.diagrams.interface(f"{t.key}#iface", t.path), max_h=flow.geo.area.h * 0.55)
        elif t.kind == "UntypedSubApp" and self._has_ports(root):
            flow.subheading("Schnittstelle")
        self._port_tables(flow, root)
        if t.kind == "BasicFB":
            self._basic_fb(flow, root.find("BasicFB"))
        elif t.kind == "SimpleFB":
            self._algorithms(flow, root.find("SimpleFB"))
        elif t.kind == "Function":
            body = root.find("ST")
            if body is not None and clean(body.get("Text") or body.text):
                flow.subheading("Funktionsrumpf")
                flow.code(clean(body.get("Text") or body.text))
        elif t.kind == "DataType":
            st = root.find("StructuredType")
            if st is not None:
                flow.subheading("Struktur")
                flow.table([("Element", 3), ("Typ", 3), ("Initialwert", 3), ("Kommentar", 6)],
                           [[Cell(v.get("Name", "")), Cell(self._var_type(v)), Cell(v.get("InitialValue", "")),
                             Cell(clean(v.get("Comment")))] for v in st.findall("VarDeclaration")])

    @staticmethod
    def _has_ports(root: ET.Element) -> bool:
        iface = root.find("InterfaceList")
        if iface is None:
            iface = root.find("SubAppInterfaceList")
        if iface is None:
            return False
        return any(e.tag in ("VarDeclaration", "Event", "SubAppEvent", "AdapterDeclaration") for e in iface.iter())

    @staticmethod
    def _var_type(v: ET.Element) -> str:
        t = v.get("Type", "")
        if v.get("ArraySize"):
            t = f"ARRAY [{v.get('ArraySize')}] OF {t}"
        return t

    def _port_tables(self, flow: Flow, root: ET.Element):
        iface = root.find("InterfaceList")
        if iface is None:
            iface = root.find("SubAppInterfaceList")
        if iface is None:
            return
        events = []
        for section, direction in (("EventInputs", "Eingang"), ("SubAppEventInputs", "Eingang"),
                                   ("EventOutputs", "Ausgang"), ("SubAppEventOutputs", "Ausgang")):
            for sec in iface.findall(section):
                for ev in sec:
                    if ev.tag not in ("Event", "SubAppEvent"):
                        continue
                    with_ = ", ".join(w.get("Var", "") for w in ev.findall("With"))
                    events.append([Cell(ev.get("Name", "")), Cell(direction), Cell(ev.get("Type", "Event")),
                                   Cell(with_), Cell(clean(ev.get("Comment")))])
        if events:
            flow.subheading("Ereignisse")
            flow.table([("Name", 3), ("Richtung", 1.6), ("Typ", 2), ("WITH", 4), ("Kommentar", 7)], events)
        data = []
        for section, direction in (("InputVars", "Eingang"), ("OutputVars", "Ausgang"),
                                   ("InOutVars", "Ein/Aus")):
            for sec in iface.findall(section):
                for v in sec.findall("VarDeclaration"):
                    data.append([Cell(v.get("Name", "")), Cell(direction), Cell(self._var_type(v)),
                                 Cell(v.get("InitialValue", "")), Cell(clean(v.get("Comment")))])
        if data:
            flow.subheading("Daten")
            flow.table([("Name", 3), ("Richtung", 1.6), ("Typ", 3), ("Initialwert", 3), ("Kommentar", 7)], data)
        adapters = []
        for section, direction in (("Sockets", "Socket"), ("Plugs", "Plug")):
            for sec in iface.findall(section):
                for a in sec.findall("AdapterDeclaration"):
                    adapters.append([Cell(a.get("Name", "")), Cell(direction), Cell(a.get("Type", "")),
                                     Cell(clean(a.get("Comment")))])
        if adapters:
            flow.subheading("Adapter")
            flow.table([("Name", 3), ("Art", 1.6), ("Typ", 6), ("Kommentar", 7)], adapters)
        for container in (root.find("BasicFB"), root.find("SimpleFB")):
            if container is None:
                continue
            for tag, label in (("InternalVars", "Interne Variablen"), ("InternalConstVars", "Interne Konstanten")):
                sec = container.find(tag)
                if sec is not None and sec.findall("VarDeclaration"):
                    flow.subheading(label)
                    flow.table([("Name", 3), ("Typ", 3), ("Initialwert", 3), ("Kommentar", 7)],
                               [[Cell(v.get("Name", "")), Cell(self._var_type(v)), Cell(v.get("InitialValue", "")),
                                 Cell(clean(v.get("Comment")))] for v in sec.findall("VarDeclaration")])

    def _basic_fb(self, flow: Flow, basic: Optional[ET.Element]):
        if basic is None:
            return
        ecc = basic.find("ECC")
        if ecc is not None:
            states = [[Cell(s.get("Name", "")),
                       Cell(", ".join(f"{a.get('Algorithm', '-')} -> {a.get('Output', '-')}"
                                      for a in s.findall("ECAction"))),
                       Cell(clean(s.get("Comment")))] for s in ecc.findall("ECState")]
            if states:
                flow.subheading("ECC - Zustände")
                flow.table([("Zustand", 3), ("Aktionen (Algorithmus -> Ereignis)", 7), ("Kommentar", 6)], states)
            trans = [[Cell(tr.get("Source", "")), Cell(tr.get("Destination", "")), Cell(tr.get("Condition", "")),
                      Cell(clean(tr.get("Comment")))] for tr in ecc.findall("ECTransition")]
            if trans:
                flow.subheading("ECC - Transitionen")
                flow.table([("Von", 3), ("Nach", 3), ("Bedingung", 6), ("Kommentar", 5)], trans)
        self._algorithms(flow, basic)

    def _algorithms(self, flow: Flow, container: Optional[ET.Element]):
        if container is None:
            return
        for alg in container.findall("Algorithm"):
            st = alg.find("ST")
            text = clean(st.get("Text") or st.text) if st is not None else ""
            flow.subheading(f"Algorithmus {alg.get('Name', '')}" +
                            (f" – {clean(alg.get('Comment'))}" if clean(alg.get("Comment")) else ""))
            flow.code(text or "(leer)")

    # ----- VT GUI -----

    def _vt_flow(self, key: str, title: str, subtitle: str, level: int) -> Flow:
        sheets: List[Sheet] = []

        def make(first: bool) -> Sheet:
            s = Sheet(key if first else f"{key}#{len(sheets)}", title if first else f"{title} (Forts.)",
                      subtitle, level=level, outline=first)
            sheets.append(s)
            return s
        return Flow(self, make)

    def _vt_usage_cell(self, oid: int) -> Cell:
        return self._usage_cell(self.vt_usages.get(oid, []))

    def _plan_vt(self):
        vt = self.vt
        flow = self._vt_flow("vt:pool", "Bedienoberfläche (ISOBUS-VT)", clean(str(vt.dir)), 0)
        flow.heading("Bedienoberfläche – ISOBUS-Objektpool")
        props = [[Cell("Pool (ISO-Designer)"), Cell(clean(str(vt.jop)))],
                 [Cell("Pool (übersetzt)"), Cell(clean(str(vt.iop)))]]
        m = vt.manifest
        if m:
            s = m.get("render_settings", {})
            props += [
                [Cell("SHA-256"), Cell(f"{m.get('pool_sha256', '')} ({m.get('pool_size', '')} Byte)")],
                [Cell("Datenmaske"), Cell(f"{s.get('data_mask_size')} x {s.get('data_mask_size')} Pixel")],
                [Cell("Soft-Keys"), Cell(f"{s.get('softkey_count')} Tasten in {s.get('softkey_columns')} Spalten "
                                         f"zu {s.get('softkey_rows')}, je {s.get('softkey_width')} x "
                                         f"{s.get('softkey_height')} Pixel")],
                [Cell("Bilder"), Cell(f"vt-render aus AgIsoVirtualTerminal {m.get('vt_commit', '')[:10]}"
                                      f"{' (geändert)' if m.get('vt_commit_dirty') else ''}, "
                                      f"{s.get('juce_version', '')}. Text in DejaVu Sans Mono statt der Schrift "
                                      "des Terminals, blinkende Objekte im sichtbaren Zustand.")]]
        else:
            props.append([Cell("Bilder"), Cell("keine (--vt-render nicht angegeben)")])
        if vt.render_error:
            props.append([Cell("Fehler"), Cell(vt.render_error)])
        flow.table([("Eigenschaft", 3), ("Wert", 12)], props)
        designator = vt.images.get("ws_designator.png")
        if designator is not None:
            flow.subheading("Working-Set-Symbol")
            flow.image(designator, 30 * mm)

        flow.subheading("Masken")
        flow.table([("Blatt", 1.2), ("Art", 3), ("Maske", 5), ("Soft-Key-Maske", 5), ("Objekte", 1.5)],
                   [[Cell(self.ref(f"vt:{mk.id}"), f"vt:{mk.id}"), Cell(VT_MASK_LABELS[mk.cls]),
                     Cell(vt.label(mk.id), f"vt:{mk.id}"),
                     Cell(vt.label(mk.soft_key_mask), f"vt:{mk.soft_key_mask}") if mk.soft_key_mask is not None
                     else Cell(""),
                     Cell(str(sum(o.cls != "CSoftKeyMask" for o in mk.objects)))] for mk in vt.masks])
        if vt.aux:
            flow.subheading("Auxiliary-Funktionen")
            flow.table([("ID", 1.2), ("Name", 5), ("Verwendet in (4diac)", 9)],
                       [[Cell(str(a)), Cell(vt.names.get(a, "")), self._vt_usage_cell(a)] for a in vt.aux])
        unsupported = [u for u in m.get("unsupported_objects", []) if u.get("status") == "unsupported"]
        if unsupported:
            flow.subheading("Vom Renderer nicht gezeichnete Objekte")
            flow.table([("Objekt", 5), ("Typ", 3), ("Grund", 8)],
                       [[Cell(vt.label(u["object_id"])), Cell(u.get("object_type", "")), Cell(u.get("reason", ""))]
                        for u in unsupported])
        self._out.extend(flow.sheets)
        for mask in vt.masks:
            self._plan_vt_mask(mask)

    def _plan_vt_mask(self, mask: VtMask):
        vt = self.vt
        key = f"vt:{mask.id}"
        label = VT_MASK_LABELS[mask.cls]
        subtitle = f"Objekt {mask.id}" + (f", Soft-Key-Maske {vt.label(mask.soft_key_mask)}"
                                          if mask.soft_key_mask is not None else "")
        img = mask.image
        landscape = True
        if img is not None:
            land, port = self.geometry(True).area, self.geometry(False).area
            landscape = min(land.w / img.width, land.h / img.height) >= min(port.w / img.width, port.h / img.height)
        sheet = Sheet(key, f"{label} {mask.name or mask.id}", subtitle, level=1, landscape=landscape)
        geo = self.geometry(landscape)
        a = geo.area
        fields: Dict[int, str] = {}
        if img is None:
            sheet.ops.append(lambda c, d: _text(c, a.x, a.top - 10, "(kein Bild: vt-render nicht angegeben "
                                                "oder fehlgeschlagen)", FONT_ITALIC, 9, GREY))
        else:
            scale = min(a.w / img.width, a.h / img.height, 2.0)
            w, h = img.width * scale, img.height * scale
            x, y = a.x + (a.w - w) / 2, a.y + (a.h - h) / 2
            sheet.ops.append(lambda c, d: _draw_image(c, img, x, y, w, h))
            for i, obj in enumerate(mask.objects):
                if obj.pos is not None:
                    fields[i] = geo.field_of(x + obj.pos[0] * scale, y + h - obj.pos[1] * scale)
                if obj.own and obj.rect is not None:
                    # clicking a key or button in the image jumps to the 4diac block using it,
                    # else to the object table
                    usages = self.vt_usages.get(obj.id)
                    target = usages[0].sheet_key if usages else f"{key}#objects"
                    rx, ry, rw, rh = obj.rect
                    sheet.ops.append(lambda c, d, r=(x + rx * scale, y + h - (ry + rh) * scale, rw * scale,
                                                     rh * scale), t=target: _link(c, t, *r))
        self._out.append(sheet)

        flow = self._vt_flow(f"{key}#objects", f"{mask.name or mask.id} – Objekte", subtitle, 2)
        flow.heading(f"{label} {vt.label(mask.id)}")
        rows = []
        for i, obj in enumerate(mask.objects):
            where = Cell(f"{self.ref(key)}/{fields[i]}", key) if i in fields else Cell("")
            link = f"vt:{obj.id}" if obj.cls in VT_MASK_LABELS else None
            thumb = img.crop(obj.rect) if img is not None and obj.own and obj.rect is not None else None
            rows.append([Cell(str(obj.id)), Cell("", image=thumb, image_h=9 * mm),
                         Cell(obj.name, link, indent=obj.depth),
                         Cell(obj.cls[1:] if obj.cls.startswith("C") else obj.cls), where,
                         self._vt_usage_cell(obj.id)])
        flow.table([("ID", 1.2), ("Bild", 2.2), ("Name", 5), ("Klasse", 2.3), ("Blatt/Feld", 1.4),
                    ("Verwendet in (4diac)", 7)], rows)
        self._out.extend(flow.sheets)

    # ----- appendix -----

    def _plan_appendix(self):
        def make_maker(key: str, title: str):
            sheets: List[Sheet] = []

            def make(first: bool) -> Sheet:
                s = Sheet(key if first else f"{key}#{len(sheets)}", title if first else f"{title} (Forts.)",
                          "Anhang", level=0, outline=first)
                sheets.append(s)
                return s
            return make

        if self.lib_usages:
            flow = Flow(self, make_maker("app:lib", "Verwendete Bibliothekstypen"))
            flow.heading("Verwendete Bibliothekstypen")
            rows = []
            for full, (t, usages) in sorted(self.lib_usages.items(), key=lambda kv: kv[0].lower()):
                rows.append([Cell(full), Cell(t.library or ""), Cell(KIND_LABELS.get(t.kind, t.kind)),
                             Cell(clean(t.root.get("Comment"))), self._usage_cell(usages)])
            flow.table([("Typ", 5), ("Bibliothek", 3), ("Art", 2.5), ("Kommentar", 5), ("Verwendet in", 6)], rows)
            self._out.extend(flow.sheets)

        if self.missing or self.project.warnings:
            flow = Flow(self, make_maker("app:problems", "Nicht aufgelöste Typen"))
            flow.heading("Nicht aufgelöste Typen und Hinweise")
            for warning in self.project.warnings:
                flow.para(warning)
            flow.space()
            flow.table([("Typ", 5), ("Grund", 7), ("Verwendet in", 6)],
                       [[Cell(k or "(leer)"), Cell(self.project.problems.get(k, "nicht gefunden")), self._usage_cell(v)]
                        for k, v in sorted(self.missing.items())])
            self._out.extend(flow.sheets)

        for path, gcf in self.project.global_constants:
            name = gcf.get("Name", path.stem)
            flow = Flow(self, make_maker(f"app:gcf:{name}", f"Globale Konstanten {name}"))
            flow.heading(f"Globale Konstanten {name}")
            flow.para(self.project.rel(path), color=GREY)
            used = self.project.identifiers()
            decls = list(gcf.iter("VarDeclaration"))
            flow.para(f"Fett: im Projekt verwendet ({sum(v.get('Name') in used for v in decls)} von {len(decls)}).",
                      color=GREY)
            flow.space()
            rows = [[Cell(v.get("Name", ""), bold=v.get("Name") in used), Cell(self._var_type(v)),
                     Cell(v.get("InitialValue", "")), Cell(clean(v.get("Comment")))] for v in decls]
            flow.table([("Name", 6), ("Typ", 2), ("Wert", 3), ("Kommentar", 6)], rows)
            self._out.extend(flow.sheets)

        deps = self.project.manifest.find("Dependencies") if self.project.manifest is not None else None
        if deps is not None and len(deps):
            flow = Flow(self, make_maker("app:deps", "Bibliotheksabhängigkeiten"))
            flow.heading("Bibliotheksabhängigkeiten (MANIFEST.MF)")
            flow.table([("Bibliothek", 6), ("Version", 3)],
                       [[Cell(r.get("SymbolicName", "")), Cell(r.get("Version", ""))] for r in deps])
            self._out.extend(flow.sheets)

    # ----- rendering -----

    def render(self, output: Path):
        """Write to a temporary file next to output and replace output only after a successful
        save: a failed run keeps the previous PDF, and a symlink at output is replaced, not followed."""
        fd, tmp = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
        os.close(fd)
        try:
            self._render_to(tmp)
            os.replace(tmp, output)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _render_to(self, path: str):
        first = self.geometry(self.sheets[0].landscape)
        c = canvas.Canvas(path, pagesize=(first.page_w, first.page_h))
        c.setTitle(f"{self.project_title} – Projektdokumentation")
        c.setAuthor(self.author or self.org)
        c.setSubject("Eclipse 4diac / IEC 61499 Projektdokumentation")
        c.setCreator(f"DocumentTheProject {__version__}")
        c.showOutline()
        last_level = -1
        for sheet in self.sheets:
            geo = self.geometry(sheet.landscape)
            c.setPageSize((geo.page_w, geo.page_h))
            c.bookmarkPage(sheet.key)
            if sheet.outline:
                level = min(sheet.level, last_level + 1)
                c.addOutlineEntry(f"S{sheet.number}  {sheet.title}", sheet.key, level=level,
                                  closed=level >= 1)
                last_level = level
            for op in sheet.ops:
                op(c, self)
            self._draw_frame(c, geo, sheet)
            c.showPage()
        c.save()

    def _draw_frame(self, c, geo: Geometry, sheet: Sheet):
        f, i = geo.frame, geo.inner
        c.setStrokeColor(INK)
        c.setLineWidth(0.7)
        c.rect(f.x, f.y, f.w, f.h, stroke=1, fill=0)
        c.setLineWidth(0.35)
        c.rect(i.x, i.y, i.w, i.h, stroke=1, fill=0)
        # field grid: columns 1..n top/bottom, rows A.. left/right
        cw, rh = i.w / geo.cols, i.h / geo.rows
        for n in range(geo.cols):
            x = i.x + n * cw
            if n:
                for y0, y1 in ((f.y, i.y), (i.top, f.top)):
                    c.line(x, y0, x, y1)
            for y in (f.y + 1.6 * mm, i.top + 1.6 * mm):
                _text(c, x + cw / 2, y, str(n + 1), FONT, 7, INK, "center")
        for n in range(geo.rows):
            y = i.top - (n + 1) * rh
            if n < geo.rows - 1:
                for x0, x1 in ((f.x, i.x), (i.x + i.w, f.x + f.w)):
                    c.setStrokeColor(INK)
                    c.line(x0, y, x1, y)
            for x in (f.x + GRID_BAND / 2, i.x + i.w + GRID_BAND / 2):
                _text(c, x, y + rh / 2 - 2.5, chr(ord("A") + n), FONT, 7, INK, "center")
        self._draw_title_block(c, geo, sheet)

    def _draw_title_block(self, c, geo: Geometry, sheet: Sheet):
        t = geo.title
        c.setFillColor(colors.white)
        c.rect(t.x, t.y, t.w, t.h, stroke=0, fill=1)
        c.setStrokeColor(INK)
        c.setLineWidth(0.5)
        c.rect(t.x, t.y, t.w, t.h, stroke=1, fill=0)
        bounds = [0.0, 0.17, 0.39, 0.73, 0.89, 1.0]
        xs = [t.x + t.w * b for b in bounds]
        for x in xs[1:-1]:
            c.line(x, t.y, x, t.top)

        def cell(idx, label, lines, sizes):
            x0, x1 = xs[idx] + 1.5 * mm, xs[idx + 1] - 1.5 * mm
            _text(c, x0, t.top - 2.6 * mm, label, FONT, 5, GREY)
            y = t.top - 2.6 * mm
            for line, size in zip(lines, sizes):
                y -= size * 1.3
                # shrink long titles before truncating them
                while size > 7 and pdfmetrics.stringWidth(line, FONT, size) > x1 - x0:
                    size -= 0.5
                _text(c, x0, y, fit(line, FONT, size, x1 - x0), FONT, size)

        ti = sheet.type_info
        cell(0, "Erstellt", [self.org or "", f"Bearb.: {self.author}" if self.author else "",
                             f"Ausgabe: {self.today}"], [7, 6.5, 6.5])
        cell(1, "Projekt", [self.project_title, f"Version {self.version}" if self.version else "",
                            "Eclipse 4diac™ / IEC 61499"], [10, 6.5, 6.5])
        cell(2, "Blattinhalt", [sheet.title, sheet.subtitle], [11, 6.5])
        if ti is not None and ti.kind != "Resource":
            vi = ti.root.find("VersionInfo")
            meta = " ".join(v for v in ((vi.get("Version") and f"V{vi.get('Version')}"),
                                        vi.get("Author"), vi.get("Date")) if v) if vi is not None else ""
            cell(3, "Typ", [ti.name, KIND_LABELS.get(ti.kind, ti.kind), meta], [8, 6.5, 6.5])
        else:
            cell(3, "Typ", ["", "", ""], [8, 6.5, 6.5])
        x0, x1 = xs[4], xs[5]
        _text(c, x0 + 1.5 * mm, t.top - 2.6 * mm, "Blatt", FONT, 5, GREY)
        _text(c, (x0 + x1) / 2, t.y + t.h / 2 - 2 * mm, str(sheet.number), FONT, 20, INK, "center")
        _text(c, (x0 + x1) / 2, t.y + 2 * mm, f"von {self.total()}", FONT, 6.5, INK, "center")


# ===========================================================================
# CLI
# ===========================================================================

def find_project_dir(path: Path) -> Path:
    if (path / ".project").exists():
        return path
    candidates = [d for d in path.iterdir() if d.is_dir() and not d.name.startswith(".")
                  and (d / ".project").exists()] if path.is_dir() else []
    if len(candidates) == 1:
        return candidates[0]
    names = ", ".join(d.name for d in candidates) or "keine"
    sys.exit(f"[FEHLER] {path} ist kein 4diac-Projektordner (gefundene Projekte: {names}).")


def default_output_dir(project_dir: Path) -> Path:
    """Root of the git repository holding the project (e.g. 4diac-SlurryTanker_AX-sample),
    else the project folder itself."""
    for d in (project_dir, *project_dir.parents):
        if (d / ".git").exists():
            return d
    return project_dir


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Erzeugt eine PDF-Dokumentation eines Eclipse 4diac Projekts, ausgehend von der Ressource.")
    parser.add_argument("project",
                        help="4diac-Projektordner (mit .project) oder Workspace mit genau einem Projekt")
    parser.add_argument("-o", "--output", help="Ausgabe-PDF (Standard: <Projekt>_Dokumentation.pdf)")
    parser.add_argument("--lib", action="append", default=[], help="Zusätzliche Typbibliothek (mehrfach möglich)")
    parser.add_argument("--paper", choices=sorted(PAPER), default="A4", help="Papierformat (Standard: A4)")
    parser.add_argument("--title", help="Projekttitel auf Deckblatt und Schriftfeld")
    parser.add_argument("--svg-tool", default=str(SVG_TOOL_DIR),
                        help="Pfad zu IEC61499_FunctionBlock_SVG (oder Umgebungsvariable IEC61499_SVG_TOOL)")
    vt = parser.add_argument_group("Bedienoberfläche (ISOBUS-VT)")
    vt.add_argument("--vt-pool", help="ISO-Designer-Poolordner (mit .jop und Output/<Pool>.iop)")
    vt.add_argument("--pool-scripts", help="Ordner mit list_mask_objects.py (scripts_central)")
    vt.add_argument("--vt-render", help="vt-render.exe aus AgIsoVirtualTerminal (ohne: nur Tabellen, keine Bilder)")
    vt.add_argument("--vt-softkeys", type=int,
                    help="Soft-Keys des Terminals (Standard: größte Soft-Key-Maske des Pools)")
    vt.add_argument("--vt-mask-size", type=int, help="Datenmaskengröße in Pixel (Standard: ISO_MASK_SIZE aus .iop.h)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.vt_pool and not args.pool_scripts:
        parser.error("--vt-pool braucht --pool-scripts")

    init_fonts()
    missing_libs = [p for p in args.lib if not Path(p).is_dir()]
    if missing_libs:
        sys.exit(f"[FEHLER] Bibliothek nicht gefunden: {', '.join(missing_libs)}")
    project = Project(find_project_dir(Path(args.project).resolve()), [Path(p) for p in args.lib])
    for warning in project.warnings:
        print(f"[WARNUNG] {warning}", file=sys.stderr)
    print(f"Projekt: {project.dir}")
    print(f"  {len(project.types)} Projekttypen, {len(project.systems)} System(e), "
          f"{len(project.lib_dirs)} Bibliotheken")
    doc = Document(project, args.paper, args.title)
    if args.vt_pool:
        try:
            doc.vt = VtPool(Path(args.vt_pool), Path(args.pool_scripts),
                            Path(args.vt_render) if args.vt_render else None,
                            args.vt_softkeys, args.vt_mask_size, doc.project_title, doc.org)
            print(f"Objektpool: {doc.vt.jop} ({len(doc.vt.cls)} Objekte, {len(doc.vt.masks)} Masken, "
                  f"{len(doc.vt.images)} Bilder)")
        except (ImportError, OSError, ValueError, ET.ParseError) as exc:
            print(f"[WARNUNG] Objektpool wird nicht dokumentiert: {exc}", file=sys.stderr)
    doc.plan()
    for type_name in sorted(doc.missing):
        print(f"[WARNUNG] Typ {type_name or '(leer)'}: {project.problems.get(type_name, 'nicht gefunden')}",
              file=sys.stderr)
    output = Path(args.output) if args.output else default_output_dir(project.dir) / f"{project.name}_Dokumentation.pdf"
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc.render(output)
    except PermissionError:
        sys.exit(f"[FEHLER] {output} kann nicht ersetzt werden - ist das PDF noch in einem Viewer geöffnet?")
    print(f"PDF geschrieben: {output} ({doc.total()} Blatt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
