"""Professora da Kira (Fase 3): biblioteca de estudo por curso, simulado com correção e exportação.

Tudo fica em runtime/tutor.sqlite3 (fora do git). A busca é por palavras (SQLite FTS5, sem acento), não por
embeddings: rápida e sem custo; quando faltar trecho na biblioteca a questão sai marcada "sem fonte".
O Gemini só escreve aulas/questões A PARTIR dos trechos guardados e diz a fonte; nada é inventado como fonte.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from fastapi import APIRouter, Body, Header, HTTPException, Request

from services import documents
from services.local_request import is_local_request

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "runtime" / "tutor.sqlite3"
FILES = ROOT / "runtime" / "tutor_files"
CHUNK = 1200


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(text or "").lower()) if unicodedata.category(c) != "Mn")


class Tutor:
    def __init__(self, db: Path = DB) -> None:
        db.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db, check_same_thread=False)
        self._lock = threading.Lock()
        self.jobs: dict[str, dict[str, Any]] = {}
        with self._lock:
            self._db.executescript("""
            CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY, course TEXT, title TEXT, source TEXT, sha TEXT, added REAL, chars INT);
            CREATE UNIQUE INDEX IF NOT EXISTS docs_sha ON docs(course, sha);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(text, doc_id UNINDEXED, course UNINDEXED, tokenize='unicode61 remove_diacritics 2');
            CREATE TABLE IF NOT EXISTS quizzes(id TEXT PRIMARY KEY, course TEXT, topic TEXT, data TEXT, answers TEXT, result TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS links(course TEXT, url TEXT, kind TEXT, found REAL, PRIMARY KEY(course, url));
            CREATE TABLE IF NOT EXISTS lessons(id INTEGER PRIMARY KEY, course TEXT, topic TEXT, at REAL);
            CREATE TABLE IF NOT EXISTS cards(id INTEGER PRIMARY KEY, course TEXT, topic TEXT, front TEXT, back TEXT, box INT DEFAULT 0, due REAL);
            """)

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            self._db.commit()
            return rows

    def forget_course(self, course: str) -> None:
        """Remove biblioteca indexada, simulados, links e aulas de um curso (os arquivos baixados ficam no disco)."""
        for sql in ("DELETE FROM chunks WHERE course=?", "DELETE FROM docs WHERE course=?", "DELETE FROM quizzes WHERE course=?",
                    "DELETE FROM links WHERE course=?", "DELETE FROM lessons WHERE course=?"):
            self._q(sql, (course,))

    # ---- biblioteca: baixar/ler, extrair, indexar (progresso real por etapa) ----
    def ingest(self, course: str, source: str, on_change: Callable[[dict], None] | None = None) -> str:
        job_id = uuid.uuid4().hex[:8]
        job = {"id": job_id, "course": course, "source": source, "stage": "baixando", "value": 0, "total": 0, "state": "running", "text": ""}
        self.jobs[job_id] = job
        threading.Thread(target=self._run, args=(job, on_change or (lambda _j: None)), daemon=True).start()
        return job_id

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job and job["state"] == "running":
            job["state"] = "cancelled"
            return True
        return False

    def _run(self, job: dict, emit: Callable[[dict], None]) -> None:
        def step(**kw):
            job.update(kw)
            emit(dict(job))
            if job["state"] == "cancelled":
                raise InterruptedError

        try:
            source = job["source"].strip()
            FILES.mkdir(parents=True, exist_ok=True)
            if re.match(r"https?://", source):
                with httpx.stream("GET", source, follow_redirects=True, timeout=60, headers={"User-Agent": "Mozilla/5.0 KiraTutor"}) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length") or 0)
                    kind = r.headers.get("content-type", "")
                    data = bytearray()
                    for part in r.iter_bytes(65536):
                        data.extend(part)
                        step(value=len(data), total=total)
                ext = ".pdf" if "pdf" in kind or source.lower().endswith(".pdf") else ".html"
                title = Path(source.split("?")[0]).name or source
                path = FILES / f"{hashlib.sha256(data).hexdigest()[:16]}{ext}"
                path.write_bytes(bytes(data))
            else:
                found = Path(source) if Path(source).exists() else next(iter(documents.find_files(source, limit=1)), None)
                if not found:
                    raise FileNotFoundError("arquivo não encontrado")
                path, title = found, found.name
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            if self._q("SELECT 1 FROM docs WHERE course=? AND sha=?", (job["course"], sha)):
                step(state="done", stage="já estava na biblioteca", text=title)
                return
            step(stage="extraindo texto", value=0, total=0)
            if path.suffix == ".html":
                html = path.read_text("utf-8", "replace")
                text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", html)))
            else:
                text = documents.read_text(path, limit=None)
            if len(text.strip()) < 200:
                raise ValueError("sem texto (PDF escaneado precisa de OCR)")
            parts = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
            with self._lock:
                cur = self._db.execute("INSERT INTO docs(course,title,source,sha,added,chars) VALUES(?,?,?,?,?,?)",
                                       (job["course"], title[:120], source[:500], sha, time.time(), len(text)))
                doc_id = cur.lastrowid
                self._db.commit()
            for i, part in enumerate(parts):
                self._q("INSERT INTO chunks(text,doc_id,course) VALUES(?,?,?)", (part, doc_id, job["course"]))
                if i % 20 == 0 or i == len(parts) - 1:
                    step(stage="indexando", value=i + 1, total=len(parts))
            step(state="done", stage="pronto", text=f"{title} · {len(parts)} trechos")
        except InterruptedError:
            emit(dict(job))
        except Exception as exc:  # noqa: BLE001 - qualquer falha vira estado visível
            job.update(state="failed", text=str(exc)[:160])
            emit(dict(job))

    # ---- pesquisa grátis (DuckDuckGo lite): a Kira não tem chave Brave/Serper e o buscador dela só via a Wikipédia ----
    @staticmethod
    def web(query: str, limit: int = 8) -> list[str]:
        """Links de resultado sem chave de API: Yahoo, depois DuckDuckGo lite, depois Bing (um cai pro outro).
        2026-11-05: o DuckDuckGo passou a pedir captcha para buscas automáticas e a pesquisa voltava vazia."""
        import base64
        from urllib.parse import unquote
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
                   "Accept-Language": "pt-BR,pt;q=0.9"}

        def bing(code: str) -> str:
            try:
                return base64.urlsafe_b64decode(code[2:] + "=" * (-len(code[2:]) % 4)).decode("utf-8", "ignore")
            except ValueError:
                return ""

        engines = (
            ("https://search.yahoo.com/search", {"p": query}, lambda html: [unquote(m) for m in re.findall(r"/RU=([^/\"']+)/R[KS]=", html)]),
            ("https://lite.duckduckgo.com/lite/", {"q": query}, lambda html: [unquote(m) for m in re.findall(r"uddg=([^&\"']+)", html)]),
            ("https://www.bing.com/search", {"q": query, "setlang": "pt-br"}, lambda html: [bing(m) for m in re.findall(r"[?&]u=(a1[\w-]+)", html)]),
        )
        for url, params, parse in engines:
            try:
                html = httpx.get(url, params=params, headers=headers, timeout=15, follow_redirects=True).text
            except httpx.HTTPError:
                continue
            found = list(dict.fromkeys(u for u in parse(html) if u.startswith("http") and "yahoo.com" not in u and "bing.com" not in u))
            if found:
                return found[:limit]
        return []

    def research(self, course: str, emit: Callable[[dict], None]) -> dict[str, Any]:
        """Edital e até 5 provas/gabaritos (PDF) do objetivo; guarda os links e baixa sem repetir."""
        found = {"edital": [], "prova": []}
        cached = self._q("SELECT url, kind FROM links WHERE course=? AND found > ?", (course, time.time() - 7 * 86400))
        if cached:  # pesquisa da semana: não busca de novo
            for url, kind in cached:
                found[kind].append(url)
        else:
            emit({"course": course, "source": "pesquisando na web", "stage": "buscando", "state": "running", "value": 0, "total": 0})
            for kind, q in (("edital", f"{course} edital pdf"), ("prova", f"{course} prova gabarito pdf"), ("prova", f"{course} prova anterior caderno pdf")):
                for url in self.web(q):
                    if url.lower().split("?")[0].endswith(".pdf") and url not in found["edital"] + found["prova"]:
                        found[kind].append(url)
                        self._q("INSERT OR IGNORE INTO links(course,url,kind,found) VALUES(?,?,?,?)", (course, url, kind, time.time()))
        picks = found["edital"][:1] + found["prova"][:4]
        jobs = [self.ingest(course, url, emit) for url in picks]
        return {"editais": found["edital"][:5], "provas": found["prova"][:8], "baixando": len(jobs)}

    def search(self, course: str, query: str, limit: int = 6) -> list[dict[str, Any]]:
        words = [w for w in re.findall(r"\w{3,}", fold(query))][:12]
        if not words:
            return []
        rows = self._q("SELECT c.text, d.title, d.source FROM chunks c JOIN docs d ON d.id=c.doc_id WHERE chunks MATCH ? AND c.course=? "
                       "ORDER BY bm25(chunks) LIMIT ?", (" OR ".join(words), course, limit))
        return [{"text": t, "title": ti, "source": s} for t, ti, s in rows]

    def add_text(self, course: str, title: str, source: str, text: str) -> int:
        """Indexa um texto já extraído (arquivo solto na tela); 0 se já estava. Mesma biblioteca/FTS da professora."""
        sha = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        if self._q("SELECT 1 FROM docs WHERE course=? AND sha=?", (course, sha)):
            return 0
        with self._lock:
            doc_id = self._db.execute("INSERT INTO docs(course,title,source,sha,added,chars) VALUES(?,?,?,?,?,?)",
                                      (course, title[:120], source[:500], sha, time.time(), len(text))).lastrowid
            self._db.executemany("INSERT INTO chunks(text,doc_id,course) VALUES(?,?,?)",
                                 [(text[i:i + CHUNK], doc_id, course) for i in range(0, len(text), CHUNK)])
            self._db.commit()
        return doc_id

    def search_all(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        """Busca em toda a biblioteca (cursos e conhecimento solto), com o curso de cada trecho."""
        words = re.findall(r"\w{3,}", fold(query))[:12]
        if not words:
            return []
        rows = self._q("SELECT c.text, d.title, d.source, d.course FROM chunks c JOIN docs d ON d.id=c.doc_id WHERE chunks MATCH ? "
                       "ORDER BY bm25(chunks) LIMIT ?", (" OR ".join(words), limit))
        return [{"text": t, "title": ti, "source": s, "course": co} for t, ti, s, co in rows]

    def courses(self) -> list[str]:
        return [r[0] for r in self._q("SELECT DISTINCT course FROM docs UNION SELECT DISTINCT course FROM quizzes")]

    def dashboard(self, course: str = "") -> dict[str, Any]:
        out = []
        for c in [course] if course else self.courses():
            docs = self._q("SELECT title, source FROM docs WHERE course=? ORDER BY added DESC", (c,))
            stats: dict[str, list[int]] = {}
            for (res,) in self._q("SELECT result FROM quizzes WHERE course=? AND result IS NOT NULL", (c,)):
                for item in json.loads(res).get("items", []):
                    s = stats.setdefault(item.get("tema") or "geral", [0, 0])
                    s[0] += 1 if item.get("certa") else 0
                    s[1] += 1
            temas = sorted(({"tema": k, "acertos": v[0], "total": v[1], "pct": round(100 * v[0] / v[1])} for k, v in stats.items()), key=lambda x: x["pct"])
            lessons = self._q("SELECT topic FROM lessons WHERE course=? ORDER BY at DESC LIMIT 5", (c,))
            past = [{"id": i, "tema": t, "nota": json.loads(r)["nota"] if r else None, "data": time.strftime("%d/%m", time.localtime(at))}
                    for i, t, r, at in self._q("SELECT id, topic, result, created FROM quizzes WHERE course=? ORDER BY created DESC LIMIT 20", (c,))]
            notas = [x["nota"] for x in past if x["nota"] is not None]
            out.append({"curso": c, "documentos": [{"titulo": t, "fonte": s} for t, s in docs[:30]], "temas": temas,
                        "aulas": [t for (t,) in lessons], "simulados": past,
                        "media": round(sum(notas) / len(notas), 1) if notas else None, "evolucao": notas[::-1], "proxima": temas[0]["tema"] if temas and temas[0]["pct"] < 70 else ""})
        return {"cursos": out}

    # ---- aula e simulado (o Gemini escreve a partir dos trechos guardados) ----
    async def lesson(self, course: str, topic: str) -> dict[str, Any]:
        self._q("INSERT INTO lessons(course,topic,at) VALUES(?,?,?)", (course, topic, time.time()))
        hits = self.search(course, topic)
        return {"trechos": [f"[{h['title']}] {h['text'][:700]}" for h in hits], "sem_fonte": not hits}

    async def make_quiz(self, generate: Callable[[str], Awaitable[str]], course: str, topic: str, count: int = 5, banca: str = "") -> dict[str, Any]:
        hits = self.search(course, f"{topic} {banca}", limit=8)
        context = "\n\n".join(f"FONTE {i + 1} ({h['title']}): {h['text']}" for i, h in enumerate(hits))
        prompt = (f"Crie {count} questões em português sobre '{topic}' para o curso/concurso '{course}'"
                  f"{f', no estilo da banca {banca}' if banca else ''}. Misture: múltipla escolha (5 alternativas A-E), "
                  "verdadeiro/falso e no máximo 1 discursiva. Use SÓ as fontes abaixo; se não houver fontes, use conhecimento "
                  "geral e marque fonte como 'sem fonte da biblioteca'. Responda JSON: {\"questoes\":[{\"tipo\":\"multipla|vf|discursiva\","
                  "\"tema\":\"...\",\"enunciado\":\"...\",\"alternativas\":[\"A) ...\"],\"gabarito\":\"A|V|F|texto esperado\","
                  "\"justificativas\":{\"A\":\"por que certa/errada\"},\"fonte\":\"FONTE n (titulo)\"}]}\n\n" + (context or "(sem fontes)"))
        raw = await generate(prompt)
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        questions = [q for q in data.get("questoes", []) if q.get("enunciado")][:count]
        if not questions:
            raise ValueError("o gerador não devolveu questões")
        quiz_id = uuid.uuid4().hex[:8]
        quiz = {"id": quiz_id, "curso": course, "tema": topic, "banca": banca, "questoes": questions, "sem_fonte": not hits}
        self._q("INSERT INTO quizzes(id,course,topic,data,answers,created) VALUES(?,?,?,?,?,?)", (quiz_id, course, topic, json.dumps(quiz, ensure_ascii=False), "{}", time.time()))
        return quiz

    # ---------- flashcards com revisão espaçada (caixas de Leitner) ----------
    BOX_DAYS = (0, 1, 3, 7, 15, 30)

    async def make_cards(self, generate: Callable[[str], Awaitable[str]], course: str, topic: str, count: int = 8) -> list[dict[str, Any]]:
        hits = self.search(course, topic, limit=6)
        context = "\n\n".join(f"FONTE {i + 1}: {h['text']}" for i, h in enumerate(hits))
        raw = await generate(f"Crie {count} flashcards curtos em português sobre '{topic}' ({course}): frente = pergunta ou termo, "
                             "verso = resposta em 1-2 frases. Use as fontes se houver. Responda JSON: "
                             '{"cards":[{"frente":"...","verso":"..."}]}\n\n' + (context or "(sem fontes)"))
        cards = [c for c in json.loads(re.search(r"\{.*\}", raw, re.S).group(0)).get("cards", []) if c.get("frente") and c.get("verso")][:count]
        if not cards:
            raise ValueError("o gerador não devolveu flashcards")
        now = time.time()
        for c in cards:
            self._q("INSERT INTO cards(course,topic,front,back,box,due) VALUES(?,?,?,?,0,?)", (course, topic, c["frente"][:300], c["verso"][:600], now))
        return self.due_cards(course, limit=count, topic=topic)

    def due_cards(self, course: str = "", limit: int = 12, topic: str = "") -> list[dict[str, Any]]:
        sql = "SELECT id, front, back, box FROM cards WHERE due<=?" + (" AND course=?" if course else "") + (" AND topic=?" if topic else "") + " ORDER BY due LIMIT ?"
        args = (time.time(), *([course] if course else []), *([topic] if topic else []), limit)
        return [{"id": r[0], "frente": r[1], "verso": r[2], "caixa": r[3]} for r in self._q(sql, args)]

    def answer_card(self, card_id: int, right: bool) -> dict[str, Any]:
        rows = self._q("SELECT box FROM cards WHERE id=?", (int(card_id),))
        if not rows:
            raise KeyError(card_id)
        box = min(len(self.BOX_DAYS) - 1, rows[0][0] + 1) if right else 0  # errou: volta para o começo
        due = time.time() + self.BOX_DAYS[box] * 86400 + (0 if box else 600)  # errou: revê em 10 min
        self._q("UPDATE cards SET box=?, due=? WHERE id=?", (box, due, int(card_id)))
        return {"caixa": box, "dias": self.BOX_DAYS[box]}

    def quiz(self, quiz_id: str) -> dict[str, Any]:
        rows = self._q("SELECT data, answers, result FROM quizzes WHERE id=?", (quiz_id,))
        if not rows:
            raise KeyError(quiz_id)
        data, answers, result = rows[0]
        return {**json.loads(data), "respostas": json.loads(answers or "{}"), "resultado": json.loads(result) if result else None}

    def save_answers(self, quiz_id: str, answers: dict) -> None:
        self._q("UPDATE quizzes SET answers=? WHERE id=?", (json.dumps(answers, ensure_ascii=False)[:20000], quiz_id))

    async def grade(self, generate: Callable[[str], Awaitable[str]], quiz_id: str, answers: dict) -> dict[str, Any]:
        quiz = self.quiz(quiz_id)
        self.save_answers(quiz_id, answers)
        items, score, total = [], 0.0, 0
        for i, q in enumerate(quiz["questoes"]):
            given = str(answers.get(str(i), "")).strip()
            expected = str(q.get("gabarito", "")).strip()
            item = {"n": i + 1, "tema": q.get("tema") or quiz["tema"], "resposta": given, "gabarito": expected,
                    "justificativas": q.get("justificativas") or {}, "fonte": q.get("fonte", "")}
            if q.get("tipo") == "discursiva":
                try:
                    raw = await generate(f"Corrija a resposta discursiva com rubrica (0 a 1). Questão: {q['enunciado']}\nEsperado: {expected}\n"
                                         f"Resposta do aluno: {given or '(em branco)'}\nResponda JSON {{\"nota\":0.0,\"comentario\":\"...\"}}")
                    rub = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
                    item.update(nota=max(0.0, min(1.0, float(rub.get("nota", 0)))), comentario=rub.get("comentario", ""), revisao_humana=True)
                except Exception:  # noqa: BLE001
                    item.update(nota=0.0, comentario="Não consegui corrigir agora.", revisao_humana=True)
                item["certa"] = item["nota"] >= 0.6
                score += item["nota"]
            else:
                item["certa"] = bool(given) and given[:1].upper() == expected[:1].upper()
                score += 1 if item["certa"] else 0
            total += 1
            items.append(item)
        result = {"nota": round(10 * score / max(1, total), 1), "acertos": sum(1 for x in items if x["certa"]), "total": total, "items": items}
        self._q("UPDATE quizzes SET result=? WHERE id=?", (json.dumps(result, ensure_ascii=False), quiz_id))
        return result

    def export(self, quiz_id: str, fmt: str = "pdf") -> list[dict[str, Any]]:
        quiz = self.quiz(quiz_id)
        prova, gab = [f"# {quiz['tema']}", f"Curso: {quiz['curso']}" + (f" · Banca: {quiz['banca']}" if quiz["banca"] else ""), ""], ["# Gabarito", ""]
        for i, q in enumerate(quiz["questoes"]):
            prova += [f"## Questão {i + 1}", q["enunciado"], *[f"- {a}" for a in q.get("alternativas") or []], ""]
            gab += [f"## Questão {i + 1}: {q.get('gabarito', '')}", *[f"- {k}: {v}" for k, v in (q.get("justificativas") or {}).items()], f"Fonte: {q.get('fonte', '')}", ""]
        return [documents.create(f"Simulado {quiz['tema']}", "\n".join(prova), fmt).as_dict(),
                documents.create(f"Gabarito {quiz['tema']}", "\n".join(gab), fmt).as_dict()]


def create_tutor_router(tutor: Tutor, *, generate: Callable[[str], Awaitable[str]], publish: Callable[[dict], Awaitable[Any]],
                        token_provider: Callable[[], str], education: Callable[[], Any] = lambda: None) -> APIRouter:
    router = APIRouter()

    def sync(course: str, *, urls: list[str] | None = None, result: dict | None = None) -> None:
        """Espelha na página Estudos (services/education.py, mesmo banco): curso, links achados e nota corrigida."""
        store = education()
        if store is None:
            return
        try:
            found = store._course_by_name(course) or store.create_course({"name": course, "kind": "concurso"})
            if urls:
                store.import_materials(int(found["id"]), urls, "pesquisa")
            if result:
                store.record_quiz_result(course, result)
        except Exception as exc:  # noqa: BLE001 - a professora segue mesmo se a página falhar
            import logging
            logging.getLogger(__name__).info("TUTOR_EDUCATION_SYNC_FAILED reason=%s", exc)
    loop_ref: dict[str, Any] = {}

    def allow(request: Request, token: str | None) -> None:
        if not (is_local_request(request) or (token and token == token_provider())):
            raise HTTPException(status_code=403, detail="origem nao autorizada")

    def panel(props: dict, focus: bool = True) -> Awaitable[Any]:
        return publish({"type": "open_panel", "kind": "tutor", "id": "tutor", "title": "Estudos", "props": props, "focus": focus})

    def emit(job: dict) -> None:  # chamado da thread do download: volta pro laço do servidor
        loop = loop_ref.get("loop")
        if loop:
            asyncio.run_coroutine_threadsafe(publish({"type": "open_panel", "kind": "tutor", "id": "tutor", "title": "Estudos",
                                                      "props": {"mode": "job", "job": job}, "focus": False}), loop)

    @router.get("/api/tutor/dados")
    async def dados(request: Request) -> dict[str, Any]:
        allow(request, None)
        return {"ok": True, **tutor.dashboard()}

    @router.get("/api/tutor/quiz/{quiz_id}")
    async def get_quiz(quiz_id: str, request: Request) -> dict[str, Any]:
        allow(request, None)
        try:
            quiz = tutor.quiz(quiz_id)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail={"code": "quiz_not_found", "message": "Simulado não encontrado."},
            ) from None
        return {"ok": True, "quiz": quiz}

    @router.post("/api/tutor/{action}")
    async def act(action: str, request: Request, body: dict = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        loop_ref["loop"] = asyncio.get_running_loop()
        course = str(body.get("curso") or "geral")[:80]
        try:
            if action == "baixar":
                return {"ok": True, "job": tutor.ingest(course, str(body.get("fonte") or ""), emit)}
            if action == "pesquisar":
                found = await asyncio.to_thread(tutor.research, course, emit)
                sync(course, urls=(found.get("editais") or []) + (found.get("provas") or []))
                return {"ok": True, **found}
            if action == "enviar":  # PDF solto no painel Estudos
                import base64
                FILES.mkdir(parents=True, exist_ok=True)
                name = re.sub(r"[^\w. -]", "_", str(body.get("nome") or "arquivo.pdf"))[:80]
                path = FILES / f"{uuid.uuid4().hex[:6]}-{name}"
                path.write_bytes(base64.b64decode(str(body.get("dados") or "")))
                return {"ok": True, "job": tutor.ingest(course, str(path), emit)}
            if action == "mostrar":  # painel da aula: texto, checklist '- [ ]', imagem da Wikipédia (crédito)
                image = None
                term = str(body.get("imagem") or "").strip()
                if term:
                    try:
                        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "KiraBot/8.0"}) as client:
                            w = (await client.get(f"https://pt.wikipedia.org/api/rest_v1/page/summary/{term.replace(' ', '_')}")).json()
                        if (w.get("thumbnail") or {}).get("source"):
                            image = {"url": (w.get("originalimage") or w["thumbnail"])["source"], "credito": f"Wikipédia · {w.get('title')}"}
                    except (httpx.HTTPError, ValueError):
                        pass
                sent = await publish({"type": "open_panel", "kind": "tutor", "id": f"tutor-card-{uuid.uuid4().hex[:6]}", "title": str(body.get("tema") or "Aula")[:60],
                               "props": {"mode": "card", "titulo": str(body.get("tema") or ""), "texto": str(body.get("fonte") or "")[:4000], "imagem": image}, "focus": False})
                # entregue = quantas telas receberam; 0 = nenhuma tela conectada (nada apareceu)
                delivered = int(sent) if isinstance(sent, (int, float)) and not isinstance(sent, bool) else None
                return {"ok": True, "imagem": bool(image), "delivered": delivered}
            if action == "cancelar":
                return {"ok": tutor.cancel(str(body.get("job") or ""))}
            if action == "buscar":
                return {"ok": True, "trechos": tutor.search(course, str(body.get("tema") or ""))}
            if action == "aula":
                return {"ok": True, **await tutor.lesson(course, str(body.get("tema") or ""))}
            if action == "simulado":
                async def build() -> None:
                    try:
                        quiz = await tutor.make_quiz(generate, course, str(body.get("tema") or course), int(body.get("quantidade") or 5), str(body.get("banca") or ""))
                        await panel({"mode": "quiz", "quiz": {**quiz, "respostas": {}}})
                    except Exception as exc:  # noqa: BLE001
                        await panel({"mode": "job", "job": {"course": course, "source": "simulado", "state": "failed", "text": str(exc)[:120]}})
                asyncio.ensure_future(build())
                return {"ok": True, "gerando": True}
            if action in {"flashcards", "revisar_cards"}:
                cards = (await tutor.make_cards(generate, course, str(body.get("tema") or course), int(body.get("quantidade") or 8))
                         if action == "flashcards" else tutor.due_cards("" if course == "geral" else course))
                if not cards:
                    return {"ok": True, "cards": 0}
                title = f"Flashcards · {str(body.get('tema') or course)[:40]}" if action == "flashcards" else "Revisão de hoje"
                sent = await publish({"type": "open_panel", "kind": "quadro", "id": f"quadro-cards-{uuid.uuid4().hex[:6]}", "title": title,
                                      "props": {"tipo": "flashcards", "cards": cards}, "focus": True})
                return {"ok": True, "cards": len(cards), "delivered": sent}
            if action == "card":  # botão Acertei/Errei do painel de flashcards
                return {"ok": True, **tutor.answer_card(int(body.get("id") or 0), bool(body.get("acertou")))}
            if action == "responder":
                tutor.save_answers(str(body["id"]), body.get("respostas") or {})
                return {"ok": True}
            if action == "finalizar":
                result = await tutor.grade(generate, str(body["id"]), body.get("respostas") or {})
                sync(tutor.quiz(str(body["id"]))["curso"], result=result)
                await panel({"mode": "result", "quiz": tutor.quiz(str(body["id"]))})
                return {"ok": True, **{k: result[k] for k in ("nota", "acertos", "total")},
                        "erros": [f"{x['n']} ({x['tema']})" for x in result["items"] if not x["certa"]]}
            if action == "exportar":
                quiz_id = str(body.get("id") or (tutor._q("SELECT id FROM quizzes ORDER BY created DESC LIMIT 1") or [[""]])[0][0])
                files = await asyncio.to_thread(tutor.export, quiz_id, str(body.get("formato") or "pdf"))
                return {"ok": True, "arquivos": [f["arquivo"] for f in files]}
            if action == "painel":
                data = tutor.dashboard(str(body.get("curso") or ""))
                await panel({"mode": "dashboard", **data})
                return {"ok": True, **data}
        except KeyError:
            return {"ok": False, "error": "simulado não encontrado"}
        except (ValueError, httpx.HTTPError, AttributeError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": str(exc)[:200]}
        raise HTTPException(status_code=404, detail="acao desconhecida")

    return router
