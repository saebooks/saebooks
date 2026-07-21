import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="SAE Books LaTeX API", version="1.0.0")

TEMPLATES_DIR = Path("/opt/latex-templates/templates")
ASSETS_DIR = Path("/opt/latex-templates/assets")
OUTPUT_DIR = Path("/tmp/latex-output")
OUTPUT_DIR.mkdir(exist_ok=True)

# Clean up outputs older than 2 hours on startup
def cleanup_old_outputs():
    cutoff = time.time() - 7200
    for f in OUTPUT_DIR.glob("*.pdf"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)

cleanup_old_outputs()


class CompileRequest(BaseModel):
    latex: str
    filename: str = "document.pdf"


class TemplateRequest(BaseModel):
    template: str
    vars: dict = {}
    filename: str = "document.pdf"


def run_xelatex(latex_source: str, job_name: str) -> Path:
    """Compile LaTeX source and return path to output PDF."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        tex_file = tmp / f"{job_name}.tex"
        tex_file.write_text(latex_source, encoding="utf-8")

        env = os.environ.copy()
        env["TEXMFHOME"] = str(tmp)

        for _ in range(2):  # run twice for cross-references/TOC
            result = subprocess.run(
                [
                    "xelatex",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    f"-output-directory={tmpdir}",
                    str(tex_file),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )

        pdf_src = tmp / f"{job_name}.pdf"
        if not pdf_src.exists():
            log = (tmp / f"{job_name}.log").read_text(errors="replace") if (tmp / f"{job_name}.log").exists() else result.stderr
            raise RuntimeError(f"Compilation failed:\n{log[-3000:]}")

        out_id = str(uuid.uuid4())
        out_path = OUTPUT_DIR / f"{out_id}.pdf"
        shutil.copy2(pdf_src, out_path)
        return out_path, out_id


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/templates")
def list_templates():
    if not TEMPLATES_DIR.exists():
        return {"templates": []}
    templates = [f.stem for f in TEMPLATES_DIR.glob("*.tex")]
    return {"templates": templates}


@app.post("/compile")
def compile_latex(req: CompileRequest):
    """Compile raw LaTeX source and return PDF download URL."""
    job_name = "document"
    try:
        out_path, out_id = run_xelatex(req.latex, job_name)
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Compilation timed out (>120s)")

    return {
        "status": "ok",
        "pdf_url": f"/files/{out_id}.pdf",
        "id": out_id,
    }


@app.post("/compile-template")
def compile_template(req: TemplateRequest):
    """Load a named template, substitute vars, compile and return PDF URL."""
    tpl_path = TEMPLATES_DIR / f"{req.template}.tex"
    if not tpl_path.exists():
        available = [f.stem for f in TEMPLATES_DIR.glob("*.tex")] if TEMPLATES_DIR.exists() else []
        raise HTTPException(status_code=404, detail=f"Template '{req.template}' not found. Available: {available}")

    source = tpl_path.read_text(encoding="utf-8")

    # Simple {{VAR}} substitution
    for key, value in req.vars.items():
        source = source.replace(f"{{{{{key}}}}}", str(value))

    job_name = req.template
    try:
        out_path, out_id = run_xelatex(source, job_name)
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Compilation timed out (>120s)")

    return {
        "status": "ok",
        "pdf_url": f"/files/{out_id}.pdf",
        "id": out_id,
        "template": req.template,
    }


@app.get("/files/{file_id}")
def get_pdf(file_id: str):
    # Sanitise — only allow uuid.pdf pattern
    if not file_id.endswith(".pdf") or "/" in file_id or ".." in file_id:
        raise HTTPException(status_code=400, detail="Invalid file ID")
    pdf_path = OUTPUT_DIR / file_id
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="PDF not found or expired")
    return FileResponse(pdf_path, media_type="application/pdf", filename=file_id)
