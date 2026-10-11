"""Bounded deterministic parsing of untrusted Office/PDF/text input."""

import io
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree


def extract_text_isolated(filename, data):
    if len(data) > 20_000_000:
        raise ValueError("Document exceeds 20 MB")
    try:
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL")
            if key in os.environ
        }
        result = subprocess.run(
            [sys.executable, "-I", "-m", "jarvis.document_worker", filename],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=25,
            check=False,
            env=environment,
        )
        if result.returncode or len(result.stdout) > 2_000_000:
            raise ValueError("Document parsing exceeded its limits or failed")
        parsed = json.loads(result.stdout)
        if "error" in parsed or not isinstance(parsed.get("text"), str):
            raise ValueError(
                "Document could not be parsed safely; it may be encrypted, damaged or unsupported"
            )
        return parsed["text"]
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        raise ValueError("Document parsing exceeded its limits or failed") from error


def xml_root(archive, name):
    xml = archive.read(name)
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise ValueError("XML entity declarations are not accepted")
    return ElementTree.fromstring(xml)


def spreadsheet_text(archive):
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    shared = []
    if "xl/sharedStrings.xml" in archive.namelist():
        for item in xml_root(archive, "xl/sharedStrings.xml").findall("s:si", ns):
            shared.append("".join(node.text or "" for node in item.findall(".//s:t", ns)))
    sheets = sorted(
        (name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
        key=lambda name: int(re.search(r"sheet(\d+)", name).group(1)),
    )
    output = []
    for name in sheets:
        output.append(name)
        for cell in xml_root(archive, name).findall(".//s:c", ns):
            kind = cell.get("t")
            value = cell.findtext("s:v", default="", namespaces=ns)
            if kind == "s":
                try:
                    index = int(value)
                    value = shared[index] if 0 <= index < len(shared) else "[invalid shared string]"
                except ValueError:
                    value = "[invalid shared string]"
            elif kind == "inlineStr":
                value = "".join(node.text or "" for node in cell.findall(".//s:t", ns))
            elif kind == "b":
                value = "TRUE" if value == "1" else "FALSE"
            formula = cell.findtext("s:f", namespaces=ns)
            if formula is not None:
                value = f"formula: ={formula}; cached value: {value or '[not available]'}"
            output.append(f"{cell.get('r', '?')}: {value}")
    return "\n".join(output)[:300000]


def extract_text(filename, data):
    from pypdf.errors import PdfReadError

    try:
        return _extract_text(filename, data)
    except (zipfile.BadZipFile, ElementTree.ParseError, PdfReadError) as error:
        raise ValueError("Document is damaged or not a supported valid file") from error


def _extract_text(filename, data):
    if len(data) > 20_000_000:
        raise ValueError("Document exceeds 20 MB")
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md", ".vtt", ".csv", ".tsv"}:
        return data.decode("utf-8-sig", errors="replace")[:300000]
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF requires an authorized decrypted copy")
        if len(reader.pages) > 300:
            raise ValueError("PDF exceeds 300 pages")
        return "\n".join(page.extract_text() or "" for page in reader.pages)[:300000]
    if suffix not in {".docx", ".pptx", ".xlsx"}:
        raise ValueError("Supported formats: PDF, DOCX, PPTX, XLSX, TXT, MD, VTT, CSV, TSV")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > 5000 or sum(i.file_size for i in infos) > 80_000_000:
            raise ValueError("Office archive exceeds expanded-size limits")
        if suffix == ".xlsx":
            return spreadsheet_text(archive)
        texts = []
        for info in infos:
            name = info.filename
            selected = (
                name == "word/document.xml"
                or re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                or name == "xl/sharedStrings.xml"
                or re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
            )
            if not selected:
                continue
            root = xml_root(archive, name)
            text = " ".join(n.text or "" for n in root.iter() if n.tag.rsplit("}", 1)[-1] in {"t", "v"})
            texts.append(name + "\n" + text)
        return "\n\n".join(texts)[:300000]
