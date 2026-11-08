"""Skills da Kira (2026-11-05): pastas com SKILL.md (formato Agent Skills, o mesmo do GitHub/Claude).

Ficam em Documentos\\Kira\\Skills\\<nome>\\. São só instruções: a Kira lê o SKILL.md antes da tarefa; scripts que
vierem junto ficam guardados, mas nunca são executados sozinhos. Instala por arquivo solto na tela (SKILL.md ou .zip)
ou baixando de um repositório do GitHub.
"""

from __future__ import annotations

import io
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from services.local_request import is_local_request

ROOT = Path.home() / "Documents" / "Kira" / "Skills"
MAX_ZIP = 30 * 1024 * 1024  # 30 MB baixado/enviado
MAX_UNZIPPED = 80 * 1024 * 1024
GITHUB = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/tree/([\w./-]+?))?/?$")
STOP = {"com", "uma", "para", "que", "dos", "das", "sobre", "cria", "criar", "faz", "fazer", "mim", "the", "and", "for", "use", "when"}
SHORT = re.compile(r"^([\w.-]+)/([\w.-]+)$")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")[:60] or "skill"


def parse_skill(text: str) -> dict[str, str]:
    """Frontmatter YAML simples (name/description) + corpo."""
    meta: dict[str, str] = {}
    body = text
    match = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if match:
        body = text[match.end():]
        for line in match.group(1).splitlines():
            key, _, value = line.partition(":")
            if value and key.strip() in {"name", "description"}:
                meta[key.strip()] = value.strip().strip("'\"")
    return {"name": meta.get("name", ""), "description": meta.get("description", ""), "body": body.strip()}


def list_skills(root: Path = ROOT) -> list[dict[str, str]]:
    out = []
    for path in sorted(root.glob("*/SKILL.md")) if root.exists() else []:
        info = parse_skill(path.read_text(encoding="utf-8", errors="replace"))
        out.append({"name": info["name"] or path.parent.name, "folder": path.parent.name, "description": info["description"][:400]})
    return out


def read_skill(name: str, root: Path = ROOT) -> dict[str, Any] | None:
    folder = root / _slug(name)
    if not (folder / "SKILL.md").exists():
        hit = [s for s in list_skills(root) if _slug(s["name"]) == _slug(name)]
        folder = root / hit[0]["folder"] if hit else folder
    skill = folder / "SKILL.md"
    if not skill.exists():
        return None
    info = parse_skill(skill.read_text(encoding="utf-8", errors="replace"))
    extras = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file() and p.name != "SKILL.md")[:40]
    return {**info, "folder": folder.name, "files": extras}


def find_skills(task: str, root: Path = ROOT, limit: int = 3) -> list[dict[str, str]]:
    """Skills cujo nome/descrição têm palavras da tarefa (3+ letras, sem palavras comuns), da mais parecida para a menos."""
    words = {w for w in re.findall(r"[a-zà-ú0-9]{3,}", task.lower())} - STOP
    scored = []
    for skill in list_skills(root):
        hay = f"{skill['name']} {skill['description']}".lower()
        score = sum(1 for w in words if w in hay)
        if score:
            scored.append((score, skill))
    return [s for _, s in sorted(scored, key=lambda x: -x[0])[:limit]]


def _install_tree(files: dict[str, bytes], root: Path, only: str = "") -> list[str]:
    """files: caminho relativo -> bytes. Cada pasta com SKILL.md vira uma skill (só ela e o que estiver embaixo)."""
    homes = sorted({p.rsplit("/", 1)[0] if "/" in p else "" for p in files if p.rsplit("/", 1)[-1] == "SKILL.md"}, key=len)
    installed = []
    for home in homes:
        info = parse_skill(files[f"{home}/SKILL.md" if home else "SKILL.md"].decode("utf-8", "replace"))
        name = _slug(info["name"] or home.rsplit("/", 1)[-1] or "skill")
        if only and _slug(only) not in {name, _slug(home.rsplit("/", 1)[-1])}:
            continue
        target = root / name
        if target.exists():
            shutil.rmtree(target)  # reinstalar = versão nova por cima
        prefix = f"{home}/" if home else ""
        for rel, data in files.items():
            if rel.startswith(prefix) and not any(rel.startswith(f"{h}/") for h in homes if h != home and h.startswith(prefix) and h):
                dest = (target / rel[len(prefix):]).resolve()
                if target.resolve() not in dest.parents:
                    continue  # zip-slip
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
        installed.append(name)
    return installed


def install_zip(data: bytes, root: Path = ROOT, only: str = "") -> list[str]:
    if len(data) > MAX_ZIP:
        raise ValueError("arquivo grande demais (máx. 30 MB)")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > MAX_UNZIPPED:
            raise ValueError("conteúdo grande demais depois de descompactar")
        files = {i.filename.replace("\\", "/"): zf.read(i) for i in infos if ".." not in i.filename}
    return _install_tree(files, root, only)


async def install_github(url: str, skill: str = "", root: Path = ROOT) -> list[str]:
    """github.com/dono/repo[/tree/ramo/pasta] ou dono/repo. Baixa o zip do repositório e instala as skills de dentro."""
    url = url.strip()
    m = GITHUB.match(url) or SHORT.match(url)
    if not m:
        raise ValueError("link do GitHub inválido (use github.com/dono/repositorio)")
    owner, repo = m.group(1), m.group(2)
    tree = m.group(3) if m.re is GITHUB else None
    branch, sub = (tree.split("/", 1) + [""])[:2] if tree else ("HEAD", "")
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        r = await client.get(f"https://codeload.github.com/{owner}/{repo}/zip/{branch}")
    if r.status_code != 200:
        raise ValueError(f"não achei o repositório {owner}/{repo} (HTTP {r.status_code})")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        top = zf.namelist()[0].split("/", 1)[0] + "/"
        keep = top + (sub.strip("/") + "/" if sub else "")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            for i in zf.infolist():
                if not i.is_dir() and i.filename.startswith(keep):
                    out.writestr(i.filename[len(keep):], zf.read(i))
    return install_zip(buf.getvalue(), root, skill)


def remove_skill(name: str, root: Path = ROOT) -> bool:
    found = read_skill(name, root)
    if not found:
        return False
    shutil.rmtree(root / found["folder"])
    return True


def create_skills_router(root: Path = ROOT) -> APIRouter:
    router = APIRouter()

    def allow(request: Request) -> None:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")

    def done(names: list[str]) -> dict[str, Any]:
        if not names:
            return {"ok": False, "error": "não achei nenhum SKILL.md ali"}
        return {"ok": True, "installed": names, "folder": str(root)}

    @router.get("/api/skills")
    async def skills(request: Request, tarefa: str = "") -> dict[str, Any]:
        allow(request)
        return {"ok": True, "skills": find_skills(tarefa, root) if tarefa else list_skills(root), "folder": str(root)}

    @router.get("/api/skills/read")
    async def read(request: Request, name: str) -> dict[str, Any]:
        allow(request)
        found = read_skill(name, root)
        return {"ok": True, **found} if found else {"ok": False, "error": "skill não instalada"}

    @router.post("/api/skills/install")
    async def install(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
        """Arquivo solto na tela: SKILL.md sozinho ou .zip com SKILL.md."""
        allow(request)
        data = await file.read(MAX_ZIP + 1)
        try:
            if (file.filename or "").lower().endswith(".md"):
                return done(_install_tree({"SKILL.md": data}, root))
            return done(install_zip(data, root))
        except (ValueError, zipfile.BadZipFile) as exc:
            return {"ok": False, "error": str(exc) or "arquivo inválido"}

    @router.post("/api/skills/github")
    async def github(request: Request, payload: dict) -> dict[str, Any]:
        allow(request)
        try:
            return done(await install_github(str(payload.get("url") or ""), str(payload.get("skill") or ""), root))
        except (ValueError, zipfile.BadZipFile, httpx.HTTPError) as exc:
            return {"ok": False, "error": str(exc) or "falhou ao baixar"}

    @router.post("/api/skills/remove")
    async def remove(request: Request, payload: dict) -> dict[str, Any]:
        allow(request)
        return {"ok": remove_skill(str(payload.get("name") or ""), root)}

    return router
