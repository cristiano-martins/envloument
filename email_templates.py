"""Modelos (HTML + texto simples) dos e-mails enviados pelo site."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

from contact_validation import ContactData

WAT = timezone(timedelta(hours=1), 'WAT')      # Angola: UTC+1, sem horário de verão


def _html_multiline(value: str) -> str:
    return escape(value, quote=True).replace('\n', '<br>')


def render_contact_email(data: ContactData, *, ip: str | None, received_at: datetime | None = None) -> tuple[str, str, str]:
    """Devolve (assunto, texto, html). Todos os campos são escapados: nunca há HTML do utilizador no e-mail."""
    moment = (received_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    when = '%s (WAT, UTC+1) · %s UTC' % (moment.astimezone(WAT).strftime('%d/%m/%Y %H:%M:%S'), moment.strftime('%H:%M:%S'))
    ip = ip or 'indisponível'
    subject = 'Contacto: ' + data.assunto

    text = ('Nova mensagem recebida pelo site\n\n'
            f'Nome: {data.nome}\nE-mail: {data.email}\nAssunto: {data.assunto}\n'
            f'Data/Hora: {when}\nIP: {ip}\n\nMensagem:\n{data.mensagem}\n')

    rows = ''.join(
        '<tr><td style="padding:6px 16px 6px 0;color:#64748b;font-size:13px;white-space:nowrap;vertical-align:top">%s</td>'
        '<td style="padding:6px 0;font-size:14px;color:#0f172a">%s</td></tr>' % (label, value)
        for label, value in (
            ('Nome', escape(data.nome)),
            ('E-mail', '<a href="mailto:%s" style="color:#2563eb">%s</a>' % (escape(data.email), escape(data.email))),
            ('Assunto', escape(data.assunto)),
            ('Data/Hora', escape(when)),
            ('IP', escape(ip))))
    html = (
        '<!doctype html><html lang="pt"><body style="margin:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden">'
        '<tr><td style="background:#0f172a;color:#ffffff;padding:18px 24px;font-size:18px;font-weight:bold">Nova mensagem pelo site</td></tr>'
        '<tr><td style="padding:24px"><table role="presentation" cellpadding="0" cellspacing="0">' + rows + '</table>'
        '<p style="margin:22px 0 8px;font-size:13px;color:#64748b">Mensagem</p>'
        '<div style="background:#f8fafc;border-left:4px solid #2563eb;padding:14px 16px;font-size:14px;line-height:1.6;color:#0f172a">'
        + _html_multiline(data.mensagem) + '</div></td></tr>'
        '<tr><td style="padding:14px 24px;font-size:12px;color:#94a3b8;border-top:1px solid #e2e8f0">'
        'Ao responder a este e-mail, a resposta segue diretamente para ' + escape(data.email) + '.</td></tr>'
        '</table></td></tr></table></body></html>')
    return subject, text, html
