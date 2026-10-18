"""Validação e sanitização dos dados recebidos do formulário de contacto."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

FIELD_LIMITS = {'nome': (2, 80), 'email': (5, 254), 'assunto': (3, 120), 'mensagem': (10, 2000)}
DEFAULT_SUBJECT = 'Contacto pelo site'

# Caracteres de controlo, "zero-width" e de inversão bidirecional (usados para esconder texto ou enganar a leitura)
_CONTROL_RE = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]')
# Etiquetas HTML (têm de começar por letra ou "/letra", para não apagar texto como "a < b e c > d"), comentários e PI
_TAG_RE = re.compile(r'<!--.*?-->|<\?.*?\?>|</?[A-Za-z][^<>]*>', re.S)
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


@dataclass(frozen=True)
class ContactData:
    nome: str
    email: str
    assunto: str
    mensagem: str


def _clean(value: str) -> str:
    value = _CONTROL_RE.sub('', unicodedata.normalize('NFC', value))
    for _ in range(3):                      # repete: "<scr<b>ipt>" não pode sobreviver a uma só passagem
        stripped = _TAG_RE.sub('', value)
        if stripped == value:
            break
        value = stripped
    return value


def sanitize_line(value: str) -> str:
    """Texto de uma só linha (nome, e-mail, assunto): sem quebras de linha, o que impede injeção de cabeçalhos."""
    return re.sub(r'\s+', ' ', _clean(value)).strip()


def sanitize_text(value: str) -> str:
    """Texto multilinha (mensagem): normaliza quebras de linha e remove HTML e caracteres invisíveis."""
    value = _clean(value.replace('\r\n', '\n').replace('\r', '\n'))
    value = '\n'.join(line.rstrip() for line in value.split('\n')).strip()
    return re.sub(r'\n{3,}', '\n\n', value)


def is_valid_email(value: str) -> bool:
    return len(value) <= 254 and '..' not in value and bool(_EMAIL_RE.match(value))


def _too_short(field: str, lo: int, hi: int) -> str:
    return {'nome': 'Indica o teu nome (%d a %d caracteres).' % (lo, hi),
            'email': 'Indica um e-mail válido.',
            'assunto': 'Indica o assunto (%d a %d caracteres).' % (lo, hi),
            'mensagem': 'A mensagem deve ter entre %d e %d caracteres.' % (lo, hi)}[field]


def validate_contact(payload) -> tuple[ContactData | None, dict[str, str]]:
    """Devolve (dados limpos, {}) ou (None, {campo: mensagem de erro}). Um assunto em falta usa o valor por defeito."""
    if not isinstance(payload, dict):
        return None, {'_': 'Pedido inválido.'}
    clean: dict[str, str] = {}
    errors: dict[str, str] = {}
    for field, (lo, hi) in FIELD_LIMITS.items():
        raw = payload.get(field)
        if field == 'assunto' and (raw is None or raw == ''):
            clean[field] = DEFAULT_SUBJECT
            continue
        if not isinstance(raw, str):
            errors[field] = _too_short(field, lo, hi)
            continue
        if len(raw) > hi * 4:               # evita processar textos enormes
            errors[field] = 'Máximo de %d caracteres.' % hi
            continue
        value = sanitize_text(raw) if field == 'mensagem' else sanitize_line(raw)
        if len(value) > hi:
            errors[field] = 'Máximo de %d caracteres.' % hi
        elif len(value) < lo or (field == 'email' and not is_valid_email(value)):
            errors[field] = _too_short(field, lo, hi)
        clean[field] = value
    if errors:
        return None, errors
    return ContactData(**clean), {}
