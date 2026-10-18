"""Cliente da API HTTP do Resend (https://resend.com/docs/api-reference/emails/send-email).

Toda a configuração vem de variáveis de ambiente; nunca existem valores fixos no código:

    RESEND_API_KEY      chave da API do Resend (começa por "re_")
    CONTACT_EMAIL       destinatário das mensagens recebidas pelo site
    RESEND_FROM_EMAIL   endereço remetente (domínio verificado no Resend)
    RESEND_FROM_NAME    nome apresentado como remetente (opcional)
    RESEND_TIMEOUT      timeout da chamada, em segundos (opcional, 1-30; por defeito 10)

Usa apenas a biblioteca padrão, por isso não acrescenta dependências ao Railway.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = 'https://api.resend.com/emails'
DEFAULT_TIMEOUT = 10.0
_ADDRESS_RE = re.compile(r'^[^@\s<>",;]+@[^@\s<>",;]+\.[^@\s<>",;]+$')
_NAME_UNSAFE_RE = re.compile(r'[<>",;\r\n\x00-\x1f]')


class EmailError(Exception):
    """Falha ao enviar o e-mail.

    detail          descrição técnica (só para logs e painel de administração)
    public_message  texto seguro para mostrar ao utilizador final
    status          código HTTP sugerido para a resposta da API
    """
    status = 502
    public_message = 'Não foi possível enviar o e-mail neste momento.'

    def __init__(self, detail: str = '', *, status: int | None = None, public_message: str | None = None):
        super().__init__(detail or self.public_message)
        self.detail = detail or self.public_message
        if status is not None:
            self.status = status
        if public_message is not None:
            self.public_message = public_message


class EmailNotConfigured(EmailError):
    status = 503
    public_message = 'O envio de e-mail não está disponível de momento.'


def _env(name: str) -> str:
    return (os.getenv(name) or '').strip()


def _timeout_from_env() -> float:
    try:
        return min(30.0, max(1.0, float(_env('RESEND_TIMEOUT') or DEFAULT_TIMEOUT)))
    except ValueError:
        return DEFAULT_TIMEOUT


@dataclass(frozen=True)
class EmailConfig:
    api_key: str
    to: str
    from_email: str
    from_name: str
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls) -> 'EmailConfig':
        return cls(api_key=_env('RESEND_API_KEY'), to=_env('CONTACT_EMAIL'), from_email=_env('RESEND_FROM_EMAIL'),
                   from_name=_env('RESEND_FROM_NAME'), timeout=_timeout_from_env())

    def problems(self) -> list[str]:
        """Lista de problemas de configuração (vazia = tudo pronto). Nunca inclui valores secretos."""
        out = []
        if not self.api_key:
            out.append('RESEND_API_KEY em falta')
        for var, value in (('CONTACT_EMAIL', self.to), ('RESEND_FROM_EMAIL', self.from_email)):
            if not value:
                out.append(var + ' em falta')
            elif not _ADDRESS_RE.match(value):
                out.append(var + ' não é um e-mail válido')
        return out

    def from_header(self) -> str:
        name = _NAME_UNSAFE_RE.sub('', self.from_name).strip()[:60]
        return f'{name} <{self.from_email}>' if name else self.from_email


class EmailService:
    """Envia e-mails através do Resend. A configuração é lida a cada envio (ver EmailConfig.from_env)."""

    def __init__(self, config_loader=EmailConfig.from_env):
        self._load = config_loader

    @property
    def config(self) -> EmailConfig:
        return self._load()

    def problems(self) -> list[str]:
        return self.config.problems()

    @property
    def configured(self) -> bool:
        return not self.problems()

    def send(self, *, subject: str, text: str, html: str | None = None, reply_to: str | None = None) -> str:
        """Envia um e-mail para CONTACT_EMAIL e devolve o id atribuído pelo Resend.

        Levanta EmailNotConfigured (503) ou EmailError (502/503/504).
        """
        cfg = self.config
        problems = cfg.problems()
        if problems:
            logger.error('Resend não configurado: %s', '; '.join(problems))
            raise EmailNotConfigured('; '.join(problems))

        payload = {'from': cfg.from_header(), 'to': [cfg.to],
                   'subject': re.sub(r'\s+', ' ', subject).strip()[:200], 'text': text}
        if html:
            payload['html'] = html
        if reply_to and _ADDRESS_RE.match(reply_to):
            payload['reply_to'] = reply_to

        request = urllib.request.Request(
            RESEND_ENDPOINT, data=json.dumps(payload).encode('utf-8'), method='POST',
            headers={'Authorization': 'Bearer ' + cfg.api_key, 'Content-Type': 'application/json',
                     'Accept': 'application/json', 'User-Agent': 'envloument-site/1.0'})
        try:
            with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
                body = response.read(65536)
        except urllib.error.HTTPError as exc:
            raise self._from_http_error(exc) from None
        except (socket.timeout, TimeoutError):
            logger.error('Resend: tempo esgotado ao fim de %.0fs', cfg.timeout)
            raise EmailError('timeout', status=504, public_message='O serviço de e-mail demorou demasiado a responder.') from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                logger.error('Resend: tempo esgotado ao fim de %.0fs', cfg.timeout)
                raise EmailError('timeout', status=504, public_message='O serviço de e-mail demorou demasiado a responder.') from None
            logger.error('Resend: falha de ligação (%s)', exc.reason)
            raise EmailError('falha de ligação: %s' % exc.reason) from None

        try:
            email_id = str(json.loads(body)['id'])
        except (ValueError, KeyError, TypeError):
            logger.error('Resend: resposta inesperada')
            raise EmailError('resposta inesperada do Resend') from None
        logger.info('Resend: e-mail enviado (id=%s)', email_id)
        return email_id

    @staticmethod
    def _from_http_error(exc: urllib.error.HTTPError) -> EmailError:
        message = ''
        try:
            message = str(json.loads(exc.read(8192)).get('message', ''))[:240]
        except Exception:                       # corpo vazio ou não-JSON
            pass
        finally:
            exc.close()
        detail = 'Resend HTTP %s%s' % (exc.code, ': ' + message if message else '')
        logger.error('%s', detail)
        # 4xx = configuração/conta (chave inválida, domínio por verificar, destinatário não permitido): culpa nossa, não do utilizador
        status = 503 if exc.code == 429 else 502
        return EmailError(detail, status=status)


email_service = EmailService()
