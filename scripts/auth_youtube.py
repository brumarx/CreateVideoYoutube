#!/usr/bin/env python3
"""Roda o fluxo de consentimento OAuth do YouTube e salva o token do canal.

Uso: python3 scripts/auth_youtube.py --channel curiosidades
Imprime uma URL de autorização — abra num navegador, faça login com a conta
Google do canal e autorize. O navegador vai tentar redirecionar pra
http://localhost/?code=... (essa página não existe de verdade — é esperado
dar erro de conexão). Copie o valor do parâmetro `code` da URL que aparece
na barra de endereço e cole aqui quando o script pedir.

Não usa run_local_server porque nem sempre o navegador que faz o login
consegue alcançar a porta local deste processo (ex.: navegador rodando em
outra máquina/sandbox).
"""
import argparse
import sys
import time
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parent.parent
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]
# Porta explícita e incomum — nada precisa estar escutando nela (não usamos
# run_local_server), só evita colidir com algum outro serviço já rodando em
# "http://localhost" puro (porta 80), que reescreveria a URL antes de a
# gente conseguir ler o parâmetro code.
REDIRECT_URI = "http://localhost:45231"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True, help="nome do canal (ex.: curiosidades)")
    parser.add_argument(
        "--client-secret",
        default=str(ROOT / "credentials" / "client_secret.json"),
    )
    args = parser.parse_args()

    client_secret_path = Path(args.client_secret)
    if not client_secret_path.exists():
        sys.exit(f"Não encontrei {client_secret_path}. Rode a Fase 0 primeiro.")

    token_path = ROOT / "credentials" / f"token_{args.channel}.json"

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
    flow.redirect_uri = REDIRECT_URI
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

    print(f"AUTH_URL: {auth_url}", flush=True)

    code_file = ROOT / "credentials" / f".pending_code_{args.channel}"
    code_file.unlink(missing_ok=True)
    print(f"Aguardando código em {code_file} (polling)...", flush=True)
    while not code_file.exists():
        time.sleep(1)
    code = code_file.read_text().strip()
    code_file.unlink()

    flow.fetch_token(code=code)
    creds = flow.credentials

    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)
    print(f"Token salvo em {token_path}")


if __name__ == "__main__":
    main()
