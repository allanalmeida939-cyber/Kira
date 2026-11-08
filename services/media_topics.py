"""Validacao de assunto de pesquisa: negacoes, fillers e consultas vazias nunca viram busca."""

from __future__ import annotations

import re
import unicodedata

STOPWORDS = frozenset(
    "a o as os um uma uns umas de do da dos das em no na nos nas por para pra com sem sobre ao aos e ou que "
    "me te se eu voce vc ela ele nao nem nenhum nenhuma nada sim ok certo claro hm hum ah eh uh oi ola opa "
    "sei pouco muito mais menos favor ai la aqui isso isto esse essa esses essas aquilo qual quais como quando "
    "onde quem pode poderia queria quero gostaria fala falar fale conta contar conte explica explicar explique "
    "mostra mostrar mostre ver veja imagens imagem fotos foto videos video artigo artigos historia tema assunto "
    "coisa coisas bem so tambem ta ne ainda entao tipo agora hoje obrigado obrigada valeu entendi pronto "
    "kira chefe".split()
)
GENERIC_TOPICS = frozenset({
    "historia", "tema", "assunto", "coisa", "isso", "aquilo", "novo", "nova", "volta", "janela", "janelas", "tela",
    "aqui", "ali", "ela", "ele", "noticia", "noticias", "novidades", "clima", "tempo", "previsao", "briefing",
    "lembrete", "lembretes", "agenda", "programa", "app", "aplicativo", "pasta", "arquivo",
})
MIN_LETTERS = 3


def fold(value: object) -> str:
    return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()


def clean_topic(raw: object, *, max_words: int = 6) -> str:
    """Palavras de conteudo do assunto (sem negacao/filler), ou '' se nao sobrar nada util."""
    words = [w for w in re.findall(r"[\w'-]+", fold(raw)) if w not in STOPWORDS]
    if not words or set(words) <= GENERIC_TOPICS or sum(ch.isalpha() for ch in "".join(words)) < MIN_LETTERS:
        return ""
    return " ".join(words[:max_words])


def topic_is_searchable(topic: object) -> bool:
    return bool(clean_topic(topic))


__all__ = ["STOPWORDS", "clean_topic", "fold", "topic_is_searchable"]


_USER_REQUEST_RE = re.compile(
    r"\b(?:(?:me\s+)?(?:mostra|mostre|mostrar|ver|veja)\s+(?:umas?\s+|algumas?\s+)?(?:imagens?|fotos?|videos?)?\s*(?:d[eoa]s?|sobre)\s+(?P<a>.+)"
    r"|(?:me\s+)?(?:fala|fale|falar|conta|conte|contar|explica|explique|explicar)\s+(?:um\s+pouco\s+)?(?:mais\s+)?(?:sobre|d[eoa]s?)\s+(?P<b>.+)"
    r"|(?:a\s+)?historia\s+d[eoa]s?\s+(?P<c>.+))",
)


def detect_user_media_request(user_text: str) -> str:
    """Devolve o tema quando a fala pede explicitamente para mostrar/falar sobre algo."""
    # "mostra de novo" / "mostra ele de volta" falam de janela, não pedem imagens.
    text = re.sub(r"\b(?:de\s+novo|denovo|de\s+volta|outra\s+vez)\b", " ", fold(user_text))
    match = _USER_REQUEST_RE.search(text)
    if not match:
        return ""
    return clean_topic(match.group("a") or match.group("b") or match.group("c") or "")
