"""Export DocumentsAndDiagrams Markdown to Word and PDF.

Uses the macOS textutil for .docx and Google Chrome headless for PDF.
Run from the repository root:

    python3 DocumentsAndDiagrams/export_docs.py
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
SOURCES = [
    "Readme.md",
    "FINAL_REPORT.md",
    "CURRENT_SYSTEM_ARCHITECTURE_ANALYSIS.md",
    "CURRENT_DATABASE_ARCHITECTURE.md",
    "DATABASE_TABLE_REFERENCE.md",
    "LIBRARIES_AND_DEPENDENCIES.md",
]

CSS = """
body { font-family: Helvetica, Arial, sans-serif; color: #1c1917; line-height: 1.45;
       max-width: 920px; margin: 32px auto; padding: 0 24px; font-size: 12.5px; }
h1 { font-size: 22px; margin: 0 0 8px; }
h2 { font-size: 16px; margin-top: 28px; border-bottom: 1px solid #d6d3d1; padding-bottom: 4px; }
h3 { font-size: 14px; margin-top: 20px; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; }
th, td { border: 1px solid #d6d3d1; padding: 4px 6px; vertical-align: top; text-align: left; }
th { background: #f5f5f4; }
code { font-family: ui-monospace, Menlo, monospace; font-size: 11.5px; }
pre { background: #f5f5f4; padding: 10px; overflow: auto; white-space: pre-wrap; }
img { max-width: 100%; height: auto; margin: 8px 0; }
blockquote { margin-left: 0; padding-left: 12px; border-left: 3px solid #d6d3d1; color: #44403c; }
"""


def inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def markdown_to_html(md: str, title: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(html.escape(lines[i]))
                i += 1
            out.append("<pre><code>" + "\n".join(buf) + "</code></pre>")
            i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|\s*-+", lines[i + 1]):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(cells)
                i += 1
            head, body = rows[0], rows[2:]
            out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>")
            for row in body:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>")
            out.append("</tbody></table>")
            continue
        if line.startswith("### "):
            out.append(f"<h3>{inline(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("!["):
            match = re.match(r"!\[([^\]]*)\]\(([^)]+)\)", line.strip())
            if match:
                alt, src = match.group(1), match.group(2)
                out.append(f'<img alt="{html.escape(alt)}" src="{html.escape(src)}"/>')
            else:
                out.append(f"<p>{inline(line)}</p>")
        elif line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(f"<li>{inline(lines[i][2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        elif not line.strip():
            out.append("")
        else:
            out.append(f"<p>{inline(line)}</p>")
        i += 1
    body = "\n".join(out)
    body = body.replace('src="diagrams/', 'src="../diagrams/')
    body = body.replace(".svg\"", ".png\"")
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>{body}</body></html>"
    )


def _svg_size(svg: Path) -> tuple[str, str]:
    head = svg.read_text().split(">", 1)[0]
    width, height = "1100", "640"
    for token in head.split():
        if token.startswith("width="):
            width = token.split('"')[1]
        elif token.startswith("height="):
            height = token.split('"')[1]
    return width, height


def render_diagrams() -> None:
    """Write a PNG next to each SVG so Word and PDF can embed it."""
    for svg in sorted((ROOT / "diagrams").glob("*.svg")):
        png = svg.with_suffix(".png")
        width, height = _svg_size(svg)
        page = ROOT / "html" / f"{svg.stem}-shot.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<style>html,body{margin:0;background:#fafaf9}</style></head>"
            f"<body><img src='{svg.resolve().as_uri()}' width='{width}' height='{height}'></body></html>",
            encoding="utf-8",
        )
        subprocess.run(
            [
                str(CHROME),
                "--headless",
                "--disable-gpu",
                "--hide-scrollbars",
                f"--window-size={width},{height}",
                f"--screenshot={png.resolve()}",
                page.as_uri(),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def export_one(name: str) -> None:
    source = ROOT / name
    html_path = ROOT / "html" / (source.stem + ".html")
    docx_path = ROOT / "docx" / (source.stem + ".docx")
    pdf_path = ROOT / "pdf" / (source.stem + ".pdf")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    docx_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    page = markdown_to_html(source.read_text(), source.stem)
    html_path.write_text(page, encoding="utf-8")
    docx_html = html_path.with_suffix(".docx.html")
    diagrams = str((ROOT / "diagrams").resolve()) + "/"
    docx_html.write_text(page.replace('src="../diagrams/', f'src="{diagrams}'), encoding="utf-8")
    subprocess.run(["textutil", "-convert", "docx", "-output", str(docx_path), str(docx_html)], check=True)
    if source.name == "FINAL_REPORT.md":
        embed_report_figures(docx_path)
    subprocess.run(
        [
            str(CHROME),
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            html_path.as_uri(),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def embed_report_figures(docx_path: Path) -> None:
    """textutil drops images. Append the three diagrams to the report document."""
    import io
    import struct
    import zipfile

    figures = [
        ("architecture.png", "Figure 1. Current architecture"),
        ("workflow.png", "Figure 2. Review Crew and Trade Crew"),
        ("database.png", "Figure 3. The three databases"),
    ]
    with zipfile.ZipFile(docx_path) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}

    types = files["[Content_Types].xml"].decode()
    if "image/png" not in types:
        types = types.replace(
            "</Types>",
            '<Default Extension="png" ContentType="image/png"/></Types>',
        )
        files["[Content_Types].xml"] = types.encode()

    rels = files["word/_rels/document.xml.rels"].decode()
    body = files["word/document.xml"].decode()
    paragraphs = ["<w:p><w:r><w:t>Figures</w:t></w:r></w:p>"]
    for index, (filename, caption) in enumerate(figures, start=1):
        png = (ROOT / "diagrams" / filename).read_bytes()
        width, height = struct.unpack(">II", png[16:24])
        # Fit the page width. 6.5 inches at 914400 EMU per inch.
        cx = 6_500_000
        cy = int(cx * height / width)
        media = f"word/media/image{index}.png"
        rid = f"rIdFig{index}"
        files[media] = png
        rels = rels.replace(
            "</Relationships>",
            f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image{index}.png"/></Relationships>',
        )
        paragraphs.append(f"<w:p><w:r><w:t>{html.escape(caption)}</w:t></w:r></w:p>")
        paragraphs.append(
            "<w:p><w:r><w:drawing>"
            f'<wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>'
            f'<wp:docPr id="{index}" name="Figure {index}"/>'
            '<wp:cNvGraphicFramePr><a:graphicFrameLocks xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" noChangeAspect="1"/></wp:cNvGraphicFramePr>'
            '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            "<pic:nvPicPr>"
            f'<pic:cNvPr id="{index}" name="{filename}"/><pic:cNvPicPr/>'
            "</pic:nvPicPr><pic:blipFill>"
            f'<a:blip xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:embed="{rid}"/>'
            "<a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm>"
            f'<a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
            "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
        )
    files["word/_rels/document.xml.rels"] = rels.encode()
    files["word/document.xml"] = body.replace("</w:body>", "".join(paragraphs) + "</w:body>").encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    docx_path.write_bytes(buffer.getvalue())


def main() -> None:
    if not CHROME.exists():
        raise SystemExit(f"Chrome not found at {CHROME}")
    if shutil.which("textutil") is None:
        raise SystemExit("textutil is required to write .docx files")
    render_diagrams()
    for name in SOURCES:
        export_one(name)
        print(name)


if __name__ == "__main__":
    main()
