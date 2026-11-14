"""
PayHarbor Connect — API segura
"""

import os
import csv
import io
import json
import base64
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.utils import formataddr

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build


SHEET_CSV_URL = os.environ.get(
    "SHEET_CSV_URL",
    "https://docs.google.com/spreadsheets/d/18CG-se5mEnz6QnxcE6zUtQFHMQA4JNpmhRIKKZ9OlYk/export?format=csv&gid=1792218208"
)

GMAIL_TOKEN_JSON = os.environ.get("GMAIL_TOKEN_JSON", "")
GMAIL_CREDENTIALS_JSON = os.environ.get("GMAIL_CREDENTIALS_JSON", "")
NOME_REMETENTE = os.environ.get("NOME_REMETENTE", "Mayflower Payroll")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.metadata",
]

app = FastAPI(title="PayHarbor Connect API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ValidarLicencaRequest(BaseModel):
    chave: str = Field(..., min_length=5)
    machine_id: str = Field(..., min_length=5)


class ValidarLicencaResponse(BaseModel):
    valida: bool
    mensagem: str


class EnviarPaystubRequest(BaseModel):
    chave: str = Field(..., min_length=5)
    machine_id: str = Field(..., min_length=5)
    destinatario: EmailStr
    nome_funcionario: str
    periodo: str
    pdf_base64: str
    nome_arquivo: str = "paystub.pdf"


class EnviarPaystubResponse(BaseModel):
    sucesso: bool
    mensagem: str
    message_id: str | None = None


def consultar_status_chave(chave: str):
    chave_norm = chave.strip().upper()
    try:
        with urllib.request.urlopen(SHEET_CSV_URL, timeout=20) as resp:
            conteudo = resp.read().decode("utf-8-sig", errors="replace")
    except Exception as e:
        return None, f"Erro ao acessar planilha: {type(e).__name__}: {e}"

    try:
        linhas = list(csv.reader(io.StringIO(conteudo)))
    except Exception as e:
        return None, f"Erro ao ler CSV: {e}"

    for i, linha in enumerate(linhas):
        if i == 0:
            continue
        if len(linha) < 2:
            continue
        if str(linha[0]).strip().upper() == chave_norm:
            status = str(linha[1]).strip().lower()
            if status in ("ativa", "active", "ativo"):
                return "Ativa", None
            if status in ("bloqueada", "blocked", "bloqueado", "inativa", "inactive"):
                return "Bloqueada", None
            return status, None

    return None, "Chave nao encontrada na planilha."


def obter_servico_gmail():
    if not GMAIL_TOKEN_JSON:
        raise RuntimeError("GMAIL_TOKEN_JSON nao configurado no servidor.")

    try:
        token_info = json.loads(GMAIL_TOKEN_JSON)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"GMAIL_TOKEN_JSON invalido: {e}")

    creds = Credentials.from_authorized_user_info(token_info, SCOPES)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds.valid:
        raise RuntimeError("Credenciais Gmail invalidas no servidor.")

    service = build("gmail", "v1", credentials=creds)
    perfil = service.users().getProfile(userId="me").execute()
    email_remetente = perfil.get("emailAddress", "").strip()

    if not email_remetente:
        raise RuntimeError("Nao foi possivel obter o e-mail da conta Gmail.")

    return service, email_remetente


def enviar_email_gmail(service, email_remetente, destinatario, nome_funcionario, periodo, pdf_bytes, nome_arquivo):
    mensagem = MIMEMultipart()
    mensagem["to"] = destinatario
    mensagem["from"] = formataddr((NOME_REMETENTE, email_remetente))
    mensagem["subject"] = f"Your Paystub - {periodo}"

    corpo = (
        f"Hello {nome_funcionario},\n\n"
        "Please find attached your paystub "
        "for the following pay period:\n\n"
        f"{periodo}\n\n"
        "Please contact the payroll department "
        "if you have any questions.\n\n"
        "Best regards,\n"
        "Mayflower Payroll"
    )
    mensagem.attach(MIMEText(corpo, "plain", "utf-8"))

    anexo = MIMEApplication(pdf_bytes, _subtype="pdf")
    anexo.add_header("Content-Disposition", "attachment", filename=nome_arquivo)
    mensagem.attach(anexo)

    raw = base64.urlsafe_b64encode(mensagem.as_bytes()).decode()
    resultado = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return resultado.get("id", "")


@app.get("/")
def raiz():
    return {
        "servico": "PayHarbor Connect API",
        "status": "online",
        "versao": "1.0.0",
        "hora": datetime.utcnow().isoformat() + "Z",
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/validar-licenca", response_model=ValidarLicencaResponse)
def validar_licenca(req: ValidarLicencaRequest):
    status, erro = consultar_status_chave(req.chave)

    if status is None:
        return ValidarLicencaResponse(valida=False, mensagem=erro or "Chave invalida.")

    if status != "Ativa":
        return ValidarLicencaResponse(valida=False, mensagem=f"Chave com status: {status}. Acesso negado.")

    return ValidarLicencaResponse(valida=True, mensagem="Licenca valida.")


@app.post("/enviar-paystub", response_model=EnviarPaystubResponse)
def enviar_paystub(req: EnviarPaystubRequest):
    status, erro = consultar_status_chave(req.chave)
    if status != "Ativa":
        raise HTTPException(status_code=403, detail=erro or f"Licenca invalida (status: {status}).")

    try:
        pdf_bytes = base64.b64decode(req.pdf_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="PDF em base64 invalido.")

    if len(pdf_bytes) < 100:
        raise HTTPException(status_code=400, detail="PDF muito pequeno ou vazio.")

    try:
        service, email_remetente = obter_servico_gmail()
        message_id = enviar_email_gmail(
            service=service,
            email_remetente=email_remetente,
            destinatario=req.destinatario,
            nome_funcionario=req.nome_funcionario,
            periodo=req.periodo,
            pdf_bytes=pdf_bytes,
            nome_arquivo=req.nome_arquivo,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao enviar e-mail: {type(e).__name__}: {e}")

    return EnviarPaystubResponse(sucesso=True, mensagem="E-mail aceito pelo Gmail.", message_id=message_id)