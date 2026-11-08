"""Passo a passo de configuração que aparece nas Configurações e que a Kira lê quando perguntam ("como conecto o Gmail?").
Um texto só para os dois lugares: se mudar aqui, muda na tela e na resposta dela."""

from __future__ import annotations

GOOGLE = {
    "titulo": "Conectar Agenda e Gmail (Google)",
    "resumo": ("A Kira lê a sua Agenda e o seu Gmail (só leitura). Precisa de uma credencial de app sua no Google Cloud "
               "(é de graça, uma vez só) e depois autorizar a conta."),
    "passos": [
        "No computador, abra console.cloud.google.com e entre com a conta Google que você quer conectar.",
        "No topo, clique no seletor de projeto > Novo projeto. Nome: Kira. Criar, e selecione o projeto.",
        "Menu ☰ > APIs e serviços > Biblioteca: procure e ative a \"Gmail API\" e a \"Google Calendar API\".",
        "APIs e serviços > Tela de consentimento OAuth (ou Google Auth Platform): tipo Externo, nome do app Kira, "
        "seu e-mail como suporte e contato. Em Público-alvo / Usuários de teste, adicione o seu próprio e-mail.",
        "APIs e serviços > Credenciais > Criar credenciais > ID do cliente OAuth. Tipo: App para computador (Desktop). "
        "Nome: Kira. Criar.",
        "Copie o ID do cliente (termina em .apps.googleusercontent.com) e a Chave secreta do cliente.",
        "Na Kira: Configurações > Kira > Recursos > Credencial do Google: cole os dois e toque em Salvar.",
        "Ainda na Kira, toque em Conectar Agenda e Conectar Gmail. O navegador do PC abre: escolha a conta, toque em "
        "Avançado > Acessar Kira (app em teste, é o seu) e permita. Aparece \"Conta conectada\".",
        "Ligue Agenda e E-mail em Recursos. Pronto: pergunte \"o que tenho hoje?\" ou \"chegou e-mail importante?\".",
    ],
    "celular": ("Pelo celular dá para usar tudo depois de conectado. A autorização (passo 8) abre no navegador do PC, "
                "porque o Google devolve a resposta para o próprio PC; faça essa parte com o PC à mão, uma vez só."),
    "problemas": [
        "\"Acesso bloqueado / app não verificado\": o seu e-mail não está em Usuários de teste (passo 4).",
        "\"redirect_uri_mismatch\": a credencial não é do tipo App para computador (passo 5); crie outra desse tipo.",
        "Parou de funcionar depois de 7 dias: app em modo teste expira a autorização; toque em Conectar de novo "
        "(ou publique o app na tela de consentimento).",
    ],
}

INSTAGRAM = {
    "titulo": "Conectar o Instagram",
    "resumo": ("A Kira usa o Instagram pelo navegador dela, com a sua conta logada nele. O Instagram recusa login "
               "automático, então o login é feito por você uma vez."),
    "passos": [
        "Peça \"Kira, entra no Instagram\" (ela abre o login na aba Internet) ou abra a aba Internet e vá em instagram.com.",
        "Vá em instagram.com e entre com a sua conta, como num navegador normal (inclusive o código de verificação).",
        "Pronto: o login fica guardado no navegador da Kira. Pergunte \"qual a novidade do meu Instagram hoje?\", \"quem não me segue de volta?\" ou \"quantas Marias eu sigo?\".",
    ],
    "celular": "O login pode ser feito pela aba Internet no celular também.",
    "problemas": ["Se o Instagram pedir para confirmar o acesso, confirme no app do Instagram e entre de novo."],
}

TOPICS = {"google": GOOGLE, "gmail": GOOGLE, "email": GOOGLE, "agenda": GOOGLE, "calendar": GOOGLE, "instagram": INSTAGRAM}


def as_text(topic: str) -> str:
    """Texto corrido para a Kira falar ou ler (passos numerados)."""
    item = TOPICS.get(str(topic or "").strip().lower())
    if item is None:
        return "Tenho o passo a passo de: Agenda e Gmail (Google) e Instagram."
    steps = " ".join(f"{index}) {step}" for index, step in enumerate(item["passos"], start=1))
    return f"{item['titulo']}. {item['resumo']} Passos: {steps} Celular: {item['celular']} Se der erro: {' '.join(item['problemas'])}"


__all__ = ["GOOGLE", "INSTAGRAM", "TOPICS", "as_text"]
