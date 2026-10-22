"""
Envloument — MVP "Solicitar Demonstração"
-----------------------------------------
Fluxo: formulário → credenciais temporárias (24h) → login demo → dashboard.

Executar:
    pip install -r requirements.txt
    python app.py            → http://127.0.0.1:5000

Variáveis de ambiente (opcionais):
    SESSION_SECRET   chave das sessões (define uma fixa em produção)
    ADMIN_PASSWORD   ativa /admin (utilizador: admin) para ver os pedidos recebidos
    DEMO_HORAS       validade das credenciais (por defeito 24)
    RESEND_API_KEY, CONTACT_EMAIL, RESEND_FROM_EMAIL, RESEND_FROM_NAME   e-mails via Resend (ver .env.example)
    PORT / FLASK_DEBUG
"""
import glob
import gzip
import hmac
import json
import smtplib
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
import base64
import csv
import io
import logging
import os
import re
import secrets
import sqlite3
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps
from time import sleep, time

from flask import (Flask, Response, abort, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

try:                                             # miniaturas dos modelos (gerar_miniaturas.py); a app arranca mesmo sem este ficheiro
    from miniaturas import MINIATURAS
except ImportError:
    MINIATURAS = {}
from email_service import EmailError, email_service
from email_templates import render_contact_email
from contact_validation import is_valid_email, sanitize_line, sanitize_text, validate_contact

logging.basicConfig(level=(os.getenv('LOG_LEVEL') or 'INFO').upper(),
                    format='%(asctime)s %(levelname)s %(name)s: %(message)s')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_VOLUME = os.getenv('RAILWAY_VOLUME_MOUNT_PATH')   # definido automaticamente pelo Railway quando há Volume
DB_PATH = os.getenv('DATABASE_PATH') or os.path.join(_VOLUME or BASE_DIR, 'envloument.db')
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
DEMO_HORAS = int(os.getenv('DEMO_HORAS', '24'))
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
ADMIN_IPS = {x.strip() for x in os.getenv('ADMIN_IPS', '').split(',') if x.strip()}   # opcional: restringe /admin a estes IPs

CONTACT_EMAIL = (os.getenv('CONTACT_EMAIL') or '').strip()   # destinatário (também usado pelo SMTP opcional)
WHATSAPP_NUM = os.getenv('WHATSAPP_NUM', '244952393828')
SMTP_HOST = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER', '')            # ex.: envlomenttechnologies@gmail.com
SMTP_PASS = os.getenv('SMTP_PASS', '')            # palavra-passe de aplicação do Gmail
CALLMEBOT_KEY = os.getenv('CALLMEBOT_APIKEY', '') # opcional: avisos automáticos no WhatsApp
ANTHROPIC_KEY = os.getenv('ANTHROPIC_API_KEY', '')  # opcional: ativa o assistente com IA
ANTHROPIC_MODEL = os.getenv('ANTHROPIC_MODEL', 'claude-haiku-5-5')

app = Flask(__name__, static_folder='static', static_url_path='/static')


def _chave_sessao():
    """SESSION_SECRET se definida; senão gera uma chave e guarda-a em disco,
    para que todos os workers (gunicorn) e reinícios partilhem a mesma chave."""
    env = os.getenv('SESSION_SECRET')
    if env:
        return env
    caminho = os.path.join(os.path.dirname(DB_PATH), '.session_key')
    try:
        with open(caminho) as f:
            return f.read().strip()
    except OSError:
        chave = secrets.token_hex(32)
        try:
            with open(caminho, 'w') as f:
                f.write(chave)
            os.chmod(caminho, 0o600)
        except OSError:
            pass
        return chave


app.secret_key = _chave_sessao()
NA_RAILWAY = bool(os.getenv('RAILWAY_ENVIRONMENT'))
EM_PRODUCAO = NA_RAILWAY or os.getenv('FLASK_DEBUG') != '1'            # no Railway é sempre produção
COOKIE_SEGURO = EM_PRODUCAO and (NA_RAILWAY or os.getenv('INSECURE_COOKIES') != '1')
SESSAO_MAX_S = 12 * 3600                                               # limite absoluto de uma sessão
app.config.update(SESSION_COOKIE_NAME='__Host-envl' if COOKIE_SEGURO else 'envl_session',
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=COOKIE_SEGURO,
                  SESSION_COOKIE_PATH='/', PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
                  MAX_CONTENT_LENGTH=64 * 1024, SEND_FILE_MAX_AGE_DEFAULT=86400)
if EM_PRODUCAO:
    if not os.getenv('SESSION_SECRET'):
        app.logger.warning('SESSION_SECRET não definida: define uma chave fixa de 32+ caracteres no Railway.')
    elif len(os.getenv('SESSION_SECRET')) < 32:
        app.logger.warning('SESSION_SECRET demasiado curta (usa 32+ caracteres).')
    if ADMIN_PASSWORD and len(ADMIN_PASSWORD) < 12:
        app.logger.warning('ADMIN_PASSWORD fraca (usa 12+ caracteres).')
# A base de dados só sobrevive a um deploy se estiver num Volume (fora do Railway, o disco normal serve)
PERSISTENTE = (not NA_RAILWAY) or bool(_VOLUME and DB_PATH.startswith(_VOLUME)) or os.path.ismount(os.path.dirname(DB_PATH))
if not PERSISTENTE:
    app.logger.warning('SEM VOLUME: a base de dados está num disco temporário e PERDE-SE a cada deploy. '
                       'Cria um Volume no Railway (mount path /data).')
else:
    app.logger.info('Base de dados persistente em %s', DB_PATH)
if os.getenv('TRUST_PROXY') == '1' or os.getenv('RAILWAY_ENVIRONMENT'):          # atrás do proxy do Render/nginx: confia em 1 salto
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


EMAIL_STATUS = {'ultimo': 'Ainda sem envios desde o último arranque.', 'ok': None}


def enviar_email(assunto, corpo, responder_a=None):
    """Aviso interno em texto simples para CONTACT_EMAIL. Devolve (ok, mensagem). Resend primeiro; SMTP só se o plano o permitir."""
    assunto = re.sub(r'[\r\n]+', ' ', assunto)[:150]
    try:
        email_id = email_service.send(subject='[Envloument] ' + assunto, text=corpo, reply_to=responder_a)
        return True, 'Enviado via Resend (id %s) para %s' % (email_id, email_service.config.to)
    except EmailError as exc:
        erro = exc.detail
    if SMTP_USER and SMTP_PASS and CONTACT_EMAIL:
        try:
            m = EmailMessage()
            m['Subject'], m['From'], m['To'] = '[Envloument] ' + assunto, SMTP_USER, CONTACT_EMAIL
            if responder_a and email_valido(responder_a):
                m['Reply-To'] = responder_a
            m.set_content(corpo)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as srv:
                srv.starttls(); srv.login(SMTP_USER, SMTP_PASS); srv.send_message(m)
            return True, 'Enviado via SMTP para ' + CONTACT_EMAIL
        except Exception as e:
            erro += ' | SMTP falhou: %s (o Railway bloqueia SMTP nos planos Free/Hobby)' % e
    return False, erro


def notificar(assunto, corpo, responder_a=None):
    """Aviso por e-mail (e WhatsApp, se configurado) em segundo plano; regista o resultado para o /admin."""
    def _enviar():
        ok, msg = enviar_email(assunto, corpo, responder_a)
        EMAIL_STATUS.update(ultimo='%s UTC — %s' % (iso(agora())[:16].replace('T', ' '), msg), ok=ok)
        if not ok:
            print('E-mail NÃO enviado:', msg)
        if CALLMEBOT_KEY:
            try:
                q = urllib.parse.urlencode({'phone': '+' + WHATSAPP_NUM, 'apikey': CALLMEBOT_KEY, 'text': (assunto + '\n' + corpo)[:600]})
                urllib.request.urlopen('https://api.callmebot.com/whatsapp.php?' + q, timeout=15).read()
            except Exception as e:
                print('Falha ao enviar WhatsApp:', e)
    threading.Thread(target=_enviar, daemon=True).start()


# ───────────── defesa ativa: deteção, bloqueio e alerta de ataques ─────────────
BLOQUEIOS, _STRIKES, _ALERTADOS = {}, defaultdict(deque), {}
LIMITE_BLOQUEIO, DURACAO_BLOQUEIO = 10, 3600
IPS_CONFIANCA = {x.strip() for x in os.getenv('IP_CONFIANCA', '').split(',') if x.strip()}
RE_CAMINHO = re.compile(r'(\.\./|%2e%2e|/\.env|/\.git|wp-admin|wp-login|wp-content|xmlrpc|phpmyadmin|/vendor/|/cgi-bin|\.php|\.asp|\.jsp|/etc/passwd|/actuator|/server-status|/\.aws|/\.ssh|/backup|/console|/shell)', re.I)
RE_CARGA = re.compile(r"(union(\s|\+|/\*.*?\*/)+select|insert\s+into\s|drop\s+table|or\s+1\s*=\s*1|'\s*or\s*'?1|;\s*--|<\s*script|onerror\s*=|onload\s*=|javascript:|\$\{jndi:|/etc/passwd|\.\./\.\./|sleep\s*\(\s*\d|benchmark\s*\()", re.I)
RE_SCANNER = re.compile(r'sqlmap|nikto|nmap|masscan|acunetix|nessus|wpscan|dirbuster|gobuster|hydra|havij|zgrab|nuclei|openvas|burpcollab', re.I)


RE_MINIATURA = re.compile(r'/static/img/modelo-([a-z0-9_-]{2,30})\.jpg')
_MINI_BYTES = {}


def servir_miniatura(mid):
    """Miniatura exata do modelo, vinda do código (não depende de static/ estar atualizado no deploy)."""
    if mid not in _MINI_BYTES:
        _MINI_BYTES[mid] = base64.b64decode(MINIATURAS[mid])
    r = Response(_MINI_BYTES[mid], mimetype='image/jpeg')
    r.headers['Cache-Control'] = 'public, max-age=86400'
    return r


def _hosts_validos():
    return {h for h in (request.host, request.headers.get('Host'), request.headers.get('X-Forwarded-Host')) if h}


def origem_confiavel():
    """True se o pedido nasceu no próprio site (Sec-Fetch-Site, ou Origin/Referer). Defesa contra CSRF em ações por GET."""
    sfs = request.headers.get('Sec-Fetch-Site')
    if sfs:
        return sfs in ('same-origin', 'none')
    ref = request.headers.get('Origin') or request.headers.get('Referer')
    return bool(ref) and urllib.parse.urlsplit(ref).netloc in _hosts_validos()


def json_dict():
    """Corpo JSON como dict; qualquer outra coisa (lista, texto, vazio) vira {}."""
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def registar_incidente(tipo, detalhe='', peso=2):
    """Regista tentativa suspeita. Ao atingir o limite, bloqueia o IP e avisa por e-mail."""
    ip = request.remote_addr or '?'
    if ip in IPS_CONFIANCA:
        return
    t = time(); q = _STRIKES[ip]; q.append((t, peso))
    while q and t - q[0][0] > 3600:
        q.popleft()
    try:
        db().execute('INSERT INTO incidentes(ip,tipo,detalhe,criado_em) VALUES(?,?,?,?)',
                     (ip, tipo, re.sub(r'\s+', ' ', str(detalhe))[:300], iso(agora())))
        db().commit()
    except Exception:
        pass
    if sum(p for _, p in q) >= LIMITE_BLOQUEIO and ip not in BLOQUEIOS:
        BLOQUEIOS[ip] = t + DURACAO_BLOQUEIO
        if t - _ALERTADOS.get(ip, 0) > 900:
            _ALERTADOS[ip] = t
            notificar('ALERTA DE SEGURANÇA: IP bloqueado (%s)' % tipo,
                      'IP: %s\nTipo: %s\nDetalhe: %s\nPedido: %s %s\nUser-Agent: %s\nBloqueado por %d min.' % (
                          ip, tipo, str(detalhe)[:300], request.method, request.path[:200],
                          request.headers.get('User-Agent', '')[:200], DURACAO_BLOQUEIO // 60))


def _bloqueado():
    if request.path.startswith('/api/'):
        return jsonify(error='Acesso temporariamente bloqueado.'), 403
    return Response('Acesso temporariamente bloqueado.', 403, mimetype='text/plain')


def _valores_pedido():
    vals = [str(v) for v in request.args.values()] + [str(v) for v in request.form.values()]
    j = request.get_json(silent=True)
    if isinstance(j, dict):
        vals += [v for v in j.values() if isinstance(v, str)]
    return ' '.join(vals)[:4000]


@app.before_request
def guarda():
    ip = request.remote_addr or '?'
    if (EM_PRODUCAO and request.headers.get('X-Forwarded-Proto', '').lower() == 'http' and request.path != '/api/health'):
        return redirect(request.url.replace('http://', 'https://', 1), 308)      # HTTPS obrigatório (health check exceptuado)
    ate = BLOQUEIOS.get(ip)
    if ate:
        if time() < ate:
            return _bloqueado()
        BLOQUEIOS.pop(ip, None)
    if request.path.startswith('/static/'):
        mini = RE_MINIATURA.fullmatch(request.path)
        return servir_miniatura(mini.group(1)) if mini and mini.group(1) in MINIATURAS else None
    if request.path != '/api/health':
        excedeu, fila_g = limite('global', 300, 60)
        if excedeu:
            registar_incidente('flood', 'mais de 300 pedidos/min', 1)
            r = Response('Demasiados pedidos.', 429, mimetype='text/plain'); r.headers['Retry-After'] = '30'
            return r
        fila_g.append(time())
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and os.getenv('ORIGIN_CHECK', '1') != '0':
        origem = request.headers.get('Origin')
        if origem and (origem == 'null' or urllib.parse.urlsplit(origem).netloc not in _hosts_validos()):
            registar_incidente('csrf', 'Origin: ' + origem[:100], 3)
            return Response('Pedido recusado.', 403, mimetype='text/plain')
    if RE_SCANNER.search(request.headers.get('User-Agent', '')):
        registar_incidente('scanner', request.headers.get('User-Agent', ''), LIMITE_BLOQUEIO)
        return _bloqueado()
    if RE_CAMINHO.search(request.path):
        registar_incidente('sondagem', request.path, 4)
        return Response('Não encontrado.', 404, mimetype='text/plain')
    if request.method in ('POST', 'PUT') or request.args:
        m = RE_CARGA.search(_valores_pedido())
        if m:
            registar_incidente('injecao', m.group(0), 5)
            return Response('Pedido inválido.', 400, mimetype='text/plain')


@app.after_request
def comprimir(resp):
    """gzip para texto: páginas muito mais leves em redes móveis."""
    if (resp.direct_passthrough or resp.status_code != 200 or 'Content-Encoding' in resp.headers
            or 'gzip' not in request.headers.get('Accept-Encoding', '')):
        return resp
    if not (resp.mimetype.startswith('text/') or resp.mimetype in ('application/json', 'image/svg+xml')):
        return resp
    dados = resp.get_data()
    if len(dados) < 600:
        return resp
    resp.set_data(gzip.compress(dados, 6))
    resp.headers['Content-Encoding'] = 'gzip'
    resp.vary.add('Accept-Encoding')
    return resp


@app.after_request
def cabecalhos_seguranca(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    resp.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=(), usb=()')
    resp.headers.setdefault('Cross-Origin-Opener-Policy', 'same-origin')
    resp.headers.setdefault('Cross-Origin-Resource-Policy', 'same-origin')
    resp.headers.setdefault('X-Permitted-Cross-Domain-Policies', 'none')
    resp.headers.setdefault('Content-Security-Policy',
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
        "font-src 'self' data: https://fonts.gstatic.com https://cdnjs.cloudflare.com; img-src 'self' data:; connect-src 'self'; frame-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'" + ('; upgrade-insecure-requests' if request.is_secure else ''))
    if request.is_secure:
        resp.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    resp.headers['Server'] = 'Envloument'
    if request.path.startswith(('/demo', '/admin', '/api/')):
        resp.headers['Cache-Control'] = 'no-store, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        resp.vary.add('Cookie')
    return resp

SERVICOS = ['Criação de Sites', 'Loja Online', 'Gestão de Stock', 'Outros']

# Modelos mostrados no dashboard (preços em Kwanzas)
MODELOS = [
    dict(id='aurora', nome='Landing Page', tipo='Página única', preco=85000, prazo='3 a 5 dias',
         imagem='modelo-aurora.jpg',
         desc='Uma página única, moderna e de alto impacto para lançar um produto, serviço ou campanha e transformar visitantes em contactos.',
         extras=['Design 100% responsivo', 'Formulário de contacto', 'Botão de WhatsApp', 'Otimização SEO básica', 'Carregamento ultra-rápido']),
    dict(id='meridian', nome='Site Institucional', tipo='Multi-página', preco=180000, prazo='7 a 10 dias',
         imagem='modelo-meridian.jpg',
         desc='O cartão de visita digital da tua empresa: apresenta serviços, equipa e resultados com uma imagem profissional e de confiança.',
         extras=['Até 6 páginas', 'Secção de serviços e equipa', 'Formulário de propostas', 'Mapa de localização', 'Painel para editar textos']),
    dict(id='mercato', nome='Loja Online', tipo='E-commerce', preco=350000, prazo='12 a 15 dias',
         imagem='modelo-mercato.jpg',
         desc='Vende 24 horas por dia: catálogo de produtos, carrinho, checkout e gestão de encomendas, com preços em Kwanzas e pagamento na entrega.',
         extras=['Catálogo até 100 produtos', 'Carrinho e checkout', 'Painel de encomendas', 'Pesquisa e filtros', 'Integração com stock']),
]
MODELOS_POR_ID = {m['id']: m for m in MODELOS}


# ───────────────────────── base de dados ─────────────────────────
def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def fechar_db(_):
    conn = g.pop('db', None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS demo_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT NOT NULL,
            telefone TEXT NOT NULL,
            empresa TEXT,
            servico TEXT NOT NULL,
            usuario TEXT NOT NULL UNIQUE,
            senha_hash TEXT NOT NULL,
            criado_em TEXT NOT NULL,
            expira_em TEXT NOT NULL,
            ultimo_login TEXT
        );
        CREATE TABLE IF NOT EXISTS pedidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            demo_id INTEGER,
            tipo TEXT NOT NULL,
            assunto TEXT,
            mensagem TEXT,
            criado_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS incidentes (id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT, tipo TEXT, detalhe TEXT, criado_em TEXT);
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT NOT NULL,
            mensagem TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    ''')
    colunas = {r[1] for r in conn.execute('PRAGMA table_info(leads)')}
    for col in ('assunto', 'ip'):
        if col not in colunas:
            conn.execute("ALTER TABLE leads ADD COLUMN %s TEXT NOT NULL DEFAULT ''" % col)
    conn.commit(); conn.close()


# ───────────────────────── utilitários ─────────────────────────
def agora():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec='seconds')


def parse_iso(s):
    return datetime.fromisoformat(s)


def email_valido(e):
    return isinstance(e, str) and is_valid_email(e)


def telefone_valido(t):
    return bool(re.fullmatch(r'\+?[0-9][0-9 ()\-]{6,19}', t or ''))


def slug(texto):
    t = unicodedata.normalize('NFKD', texto or '').encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]', '', t.lower())


# Limitador simples em memória (MVP): n pedidos por janela, por IP
_hits = defaultdict(deque)


def limite(chave, maximo, janela_s, identificador=None):
    ident = identificador or request.remote_addr or '?'
    fila = _hits[(chave, ident)]
    t = time()
    while fila and t - fila[0] > janela_s:
        fila.popleft()
    if len(_hits) > 5000:                      # evita crescimento ilimitado da memória
        for k in [k for k, q in _hits.items() if not q or t - q[-1] > 3600]:
            del _hits[k]
    return len(fila) >= maximo, fila


def gerar_credenciais(nome=None):
    """Utilizador aleatório e palavra-passe com ~60 bits de entropia (12 símbolos)."""
    conn = db()
    for _ in range(30):
        usuario = 'demo_' + ''.join(secrets.choice('abcdefghjkmnpqrstuvwxyz23456789') for _ in range(6))
        if not conn.execute('SELECT 1 FROM demo_accounts WHERE usuario=?', (usuario,)).fetchone():
            break
    alfabeto = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # sem O/0/I/1 para evitar confusões
    grupos = [''.join(secrets.choice(alfabeto) for _ in range(4)) for _ in range(3)]
    return usuario, '-'.join(grupos)


def nova_senha():
    alfabeto = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return '-'.join(''.join(secrets.choice(alfabeto) for _ in range(4)) for _ in range(3))


def so_digitos(t):
    return re.sub(r'\D', '', t or '')


def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(24)
    return session['csrf']


def csrf_ok():
    return hmac.compare_digest(request.form.get('csrf', ''), session.get('csrf', '!'))


app.jinja_env.globals['csrf_token'] = csrf_token
_HASH_FALSO = generate_password_hash('x' * 12)   # iguala o tempo de resposta quando o utilizador não existe


def conta_atual():
    """Devolve a conta demo da sessão se ainda for válida; senão limpa a sessão."""
    did = session.get('demo_id')
    if not did:
        return None
    row = db().execute('SELECT * FROM demo_accounts WHERE id=?', (did,)).fetchone()
    if row is None or parse_iso(row['expira_em']) <= agora() or time() - session.get('t0', 0) > SESSAO_MAX_S:
        session.clear()
        return None
    return row


def demo_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        conta = conta_atual()
        if conta is None:
            if request.path.startswith('/api/'):
                return jsonify(error='A tua sessão de demonstração expirou. Entra novamente.'), 401
            return redirect(url_for('demo_login', expirado=1))
        g.conta = conta
        return f(*a, **kw)
    return wrapper


def fmt_kz(n):
    return '{:,}'.format(int(n)).replace(',', '.') + ' Kz'


app.jinja_env.filters['kz'] = fmt_kz


# ───────────────────────── páginas públicas ─────────────────────────
@app.get('/')
def home():
    r = Response(_index_servido(), mimetype='text/html')
    r.headers['Cache-Control'] = 'public, max-age=300'
    return r


_INDEX = {'mtime': 0, 'data': b''}


def _minificar(html):
    html = re.sub(r'<!--(?!\[).*?-->', '', html, flags=re.S)
    html = re.sub(r'(<style[^>]*>)(.*?)(</style>)', lambda m: m.group(1) + re.sub(r'/\*.*?\*/', '', m.group(2), flags=re.S) + m.group(3), html, flags=re.S)
    html = re.sub(r'(<script(?![^>]*\bsrc\b)[^>]*>)(.*?)(</script>)',
                  lambda m: m.group(1) + '\n'.join(l.strip() for l in m.group(2).split('\n') if l.strip() and not l.strip().startswith('//')) + m.group(3),
                  html, flags=re.S)
    return re.sub(r'\n\s*\n+', '\n', html)


def _index_servido():
    caminho = os.path.join(BASE_DIR, 'index.html')
    mt = os.path.getmtime(caminho)
    if _INDEX['mtime'] != mt:
        with open(caminho, encoding='utf-8') as f:
            _INDEX.update(mtime=mt, data=_minificar(f.read()).encode('utf-8'))
    return _INDEX['data']


@app.get('/api/health')
def health():
    try:
        db().execute('SELECT 1').fetchone()
        novo = lambda f, marca: os.path.isfile(os.path.join(BASE_DIR, 'templates', f)) and marca in open(os.path.join(BASE_DIR, 'templates', f), encoding='utf-8').read()
        return jsonify(ok=True, modelos=sum(1 for m in modelos_embutidos().values() if m.get('html')),
                       dashboard_novo=novo('dashboard.html', 'Demo Console'), miniaturas=len(MINIATURAS),
                       persistente=PERSISTENTE)
    except Exception:
        return jsonify(ok=False), 503


@app.get('/robots.txt')
def robots():
    return Response('User-agent: *\nDisallow: /demo/\nDisallow: /admin\nDisallow: /api/\n', mimetype='text/plain')


@app.get('/favicon.ico')
def favicon():
    return redirect(url_for('static', filename='img/logo.jpg'), 301)


_ERROS = {404: ('Página não encontrada', 'O endereço que abriste não existe ou foi movido.'),
          405: ('Pedido não permitido', 'Esta ação não é suportada neste endereço.'),
          413: ('Pedido demasiado grande', 'Reduz o tamanho da mensagem e tenta de novo.'),
          500: ('Erro interno', 'Algo correu mal do nosso lado. Tenta novamente dentro de instantes.')}


@app.errorhandler(404)
@app.errorhandler(405)
@app.errorhandler(413)
@app.errorhandler(500)
def erro_http(e):
    codigo = getattr(e, 'code', 500) or 500
    titulo, texto = _ERROS.get(codigo, _ERROS[500])
    if request.path.startswith('/api/'):
        return jsonify(error=titulo), codigo
    html = ('<!DOCTYPE html><html lang="pt"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex"><title>%s | Envloument</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;'
            'background:#0b1226;color:#e2e8f0;font-family:Inter,system-ui,sans-serif;text-align:center;padding:24px}h1{font-size:4rem;margin:0;'
            'background:linear-gradient(135deg,#60a5fa,#22d3ee);-webkit-background-clip:text;background-clip:text;color:transparent}p{color:#94a3b8}'
            'a{display:inline-block;margin-top:14px;background:#2563eb;color:#fff;padding:12px 26px;border-radius:99px;text-decoration:none;font-weight:600}'
            '</style></head><body><main><h1>%d</h1><h2>%s</h2><p>%s</p><a href="/">Voltar ao início</a></main></body></html>') % (titulo, codigo, titulo, texto)
    return Response(html, codigo, mimetype='text/html')


@app.post('/api/demo/solicitar')
def solicitar_demo():
    d = json_dict()
    if d.get('website'):                       # honeypot anti-bots
        registar_incidente('honeypot', 'campo oculto preenchido', 6)
        return jsonify(error='Pedido inválido.'), 400
    bloqueado, fila = limite('solicitar', 5, 3600)
    if bloqueado:
        return jsonify(error='Demasiados pedidos deste dispositivo. Tenta novamente daqui a pouco.'), 429

    campo = lambda k: sanitize_line(d.get(k)) if isinstance(d.get(k), str) else ''
    nome, email, telefone = campo('nome'), campo('email').lower(), campo('telefone')
    empresa, servico = campo('empresa')[:100], campo('servico')

    if len(nome) < 3 or len(nome) > 100: return jsonify(error='Indica o teu nome completo.'), 400
    if not email_valido(email) or len(email) > 160: return jsonify(error='Indica um e-mail válido.'), 400
    if not telefone_valido(telefone): return jsonify(error='Indica um número de WhatsApp/telefone válido (ex.: +244 900 000 000).'), 400
    if servico not in SERVICOS: return jsonify(error='Escolhe o serviço de interesse.'), 400

    usuario, senha = gerar_credenciais(nome)
    criado = agora(); expira = criado + timedelta(hours=DEMO_HORAS)
    db().execute(
        'INSERT INTO demo_accounts(nome,email,telefone,empresa,servico,usuario,senha_hash,criado_em,expira_em) VALUES(?,?,?,?,?,?,?,?,?)',
        (nome, email, telefone, empresa, servico, usuario, generate_password_hash(senha), iso(criado), iso(expira)))
    db().commit()
    fila.append(time())
    notificar('Nova demonstração: ' + nome,
              'Nome: %s\nE-mail: %s\nWhatsApp: %s (https://wa.me/%s)\nEmpresa: %s\nServiço: %s\nUtilizador demo: %s' % (
                  nome, email, telefone, so_digitos(telefone), empresa or '—', servico, usuario), email)
    return jsonify(
        message='Conta de demonstração criada com sucesso. As suas credenciais são válidas durante %d horas.' % DEMO_HORAS,
        usuario=usuario, senha=senha, expira_em=iso(expira), validade_horas=DEMO_HORAS), 201


@app.post('/api/contact')
@app.post('/api/contato')                 # nome antigo, mantido por compatibilidade
def contato():
    """Formulário de contacto: valida → guarda na base de dados → envia e-mail (Resend) para CONTACT_EMAIL."""
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify(ok=False, error='Pedido inválido: envia JSON (Content-Type: application/json).'), 400
    if d.get('website'):                  # honeypot anti-bots
        registar_incidente('honeypot', 'campo oculto preenchido', 6)
        return jsonify(ok=False, error='Pedido inválido.'), 400
    bloqueado, fila = limite('contato', 5, 3600)
    if bloqueado:
        resp = jsonify(ok=False, error='Demasiados pedidos deste dispositivo. Tenta novamente mais tarde.')
        resp.status_code = 429
        resp.headers['Retry-After'] = str(max(1, int(3600 - (time() - fila[0]))))
        return resp
    dados, erros = validate_contact(d)
    if erros:
        return jsonify(ok=False, error=next(iter(erros.values())), fields=erros), 422

    ip = request.remote_addr
    try:                                  # a mensagem fica sempre guardada, mesmo que o e-mail falhe
        db().execute('INSERT INTO leads(nome,email,assunto,mensagem,ip,created_at) VALUES(?,?,?,?,?,?)',
                     (dados.nome, dados.email, dados.assunto, dados.mensagem, ip or '', iso(agora())))
        db().commit()
    except sqlite3.Error:
        app.logger.exception('Contacto: falha ao guardar na base de dados')
        return jsonify(ok=False, error='Erro interno. Tenta novamente dentro de instantes.'), 500
    fila.append(time())

    subject, text, html = render_contact_email(dados, ip=ip)
    try:
        email_id = email_service.send(subject='[Envloument] ' + subject, text=text, html=html, reply_to=dados.email)
    except EmailError as exc:
        EMAIL_STATUS.update(ultimo='%s UTC — contacto: %s' % (iso(agora())[:16].replace('T', ' '), exc.detail), ok=False)
        app.logger.error('Contacto guardado mas o e-mail não foi enviado: %s', exc.detail)
        return jsonify(ok=False, saved=True, error=exc.public_message + ' A tua mensagem ficou registada; se preferires, fala connosco por WhatsApp.'), exc.status
    except Exception:
        app.logger.exception('Contacto: erro inesperado ao enviar o e-mail')
        return jsonify(ok=False, saved=True, error='Erro interno ao enviar a mensagem. A tua mensagem ficou registada.'), 500
    EMAIL_STATUS.update(ultimo='%s UTC — contacto enviado (id %s)' % (iso(agora())[:16].replace('T', ' '), email_id), ok=True)
    return jsonify(ok=True, message='Mensagem enviada com sucesso. Respondemos em menos de 24 horas.'), 201


_GUARDA_MODELO = ("<script>(function(){var t;function T(){var d=document.getElementById('__t');if(!d){d=document.createElement('div');d.id='__t';"
                  "d.style.cssText='position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:rgba(15,23,42,.92);color:#fff;font:600 12px system-ui,sans-serif;padding:9px 16px;border-radius:99px;z-index:99999;opacity:0;transition:opacity .25s;pointer-events:none;white-space:nowrap';"
                  "d.textContent='Pré-visualização: disponível no site final';document.body.appendChild(d)}d.style.opacity=1;clearTimeout(t);t=setTimeout(function(){d.style.opacity=0},1800)}"
                  "document.addEventListener('click',function(e){var a=e.target.closest('a');if(!a)return;e.preventDefault();var h=a.getAttribute('href')||'';"
                  "if(h.length>1&&h.charAt(0)==='#'){var el=document.getElementById(h.slice(1));if(el){el.scrollIntoView({behavior:'smooth'});return}}T()},true);"
                  "document.addEventListener('submit',function(e){e.preventDefault();T()},true)})();</script>")

_EMB = {'mtime': 0, 'data': {}}


def modelos_embutidos():
    """Modelos incluídos no index.html (fonte única; o site nunca depende da pasta modelos/)."""
    caminho = os.path.join(BASE_DIR, 'index.html')
    try:
        mt = os.path.getmtime(caminho)
        if _EMB['mtime'] != mt:
            with open(caminho, encoding='utf-8') as f:
                m = re.search(r'const MODELOS = (\[.*?\]);\n', f.read(), re.S)
            _EMB.update(mtime=mt, data={x['id']: x for x in json.loads(m.group(1))} if m else {})
    except (OSError, ValueError, KeyError, TypeError):
        app.logger.exception('Falha ao ler os modelos embutidos')
    return _EMB['data']


def catalogo_modelos():
    """Lista de modelos (sem o HTML) para o dashboard: os embutidos ou, em último caso, os 3 base."""
    emb = [{**{k: v for k, v in m.items() if k != 'html'}, 'imagem': 'modelo-%s.jpg' % m['id']}
           for m in modelos_embutidos().values() if m.get('html')]
    return emb or MODELOS


def html_modelo(mid):
    if not re.fullmatch(r'[a-z0-9_-]{2,30}', mid or ''):
        return None
    emb = modelos_embutidos().get(mid)
    if emb and emb.get('html'):
        return emb['html']
    caminho = os.path.join(BASE_DIR, 'modelos', mid + '.html')
    if os.path.isfile(caminho):
        with open(caminho, encoding='utf-8') as f:
            return f.read()
    return None


def resposta_modelo(mid, cache):
    html = html_modelo(mid)
    if html is None:
        abort(404)
    r = Response(html.replace('</body>', _GUARDA_MODELO + '</body>', 1), mimetype='text/html')
    r.headers['Cache-Control'] = cache
    return r


@app.get('/modelo/<mid>')
def modelo_publico(mid):
    """Modelos públicos para pré-visualização. Links e formulários ficam inertes (nunca saem do modelo)."""
    return resposta_modelo(mid, 'public, max-age=3600')


def _prompt_assistente():
    modelos = '; '.join('%s (%s, %s, %s)' % (m['nome'], m['tipo'], fmt_kz(m['preco']), m['prazo']) for m in MODELOS)
    return ('És o assistente virtual da Envloument, empresa de desenvolvimento web sediada em Luanda, Angola. '
            'Responde em português, a tratar por "tu", de forma profissional, clara e curta (máximo 90 palavras), sem listas longas. '
            'Serviços: sites e landing pages, lojas online, sistemas de gestão de stock, UX/UI e suporte técnico. '
            'Modelos prontos (pagamento único, em Kwanzas): ' + modelos + '. '
            'Existe uma área de demonstração gratuita com acesso de 24 horas (botão "Solicitar Demonstração"). '
            'Contactos: WhatsApp +244 952 393 828 e e-mail envlomenttechnologies@gmail.com; resposta em menos de 24 horas. '
            'Para orçamentos à medida, indica o formulário de contacto ou o WhatsApp. '
            'Não inventes preços, prazos, clientes ou funcionalidades que não constem acima. '
            'Se a pergunta não for sobre a Envloument, recusa com simpatia e volta ao tema.')


@app.post('/api/assistente')
def assistente():
    """Assistente com IA (opcional). Sem ANTHROPIC_API_KEY devolve 503 e o site usa as respostas locais."""
    d = json_dict()
    limpo = []
    for m in (d.get('messages') or [])[-8:]:
        if isinstance(m, dict) and m.get('role') in ('user', 'assistant') and isinstance(m.get('content'), str):
            t = m['content'].strip()[:500]
            if not t:
                continue
            if limpo and limpo[-1]['role'] == m['role']:
                limpo[-1]['content'] += '\n' + t
            elif limpo or m['role'] == 'user':
                limpo.append({'role': m['role'], 'content': t})
    if not limpo or limpo[-1]['role'] != 'user':
        return jsonify(error='Pergunta inválida.'), 400
    if not ANTHROPIC_KEY:
        return jsonify(fallback=True), 503
    bloqueado, fila = limite('chat', 20, 600)
    if bloqueado:
        return jsonify(error='Estás a enviar muitas perguntas seguidas. Tenta daqui a pouco.'), 429
    fila.append(time())
    try:
        req = urllib.request.Request('https://api.anthropic.com/v1/messages', method='POST',
                                     data=json.dumps({'model': ANTHROPIC_MODEL, 'max_tokens': 350,
                                                      'system': _prompt_assistente(), 'messages': limpo}).encode(),
                                     headers={'x-api-key': ANTHROPIC_KEY, 'anthropic-version': '2023-06-01',
                                              'content-type': 'application/json'})
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read())
        txt = ''.join(b.get('text', '') for b in out.get('content', []) if b.get('type') == 'text').strip()[:900]
        if not txt:
            raise ValueError('resposta vazia')
        return jsonify(reply=txt)
    except Exception as e:
        print('Falha no assistente IA:', e)
        return jsonify(fallback=True), 502


# ───────────────────────── área demo ─────────────────────────
@app.route('/demo/login', methods=['GET', 'POST'])
def demo_login():
    if request.method == 'GET':
        if conta_atual():
            return redirect(url_for('dashboard'))
        aviso = 'A tua demonstração expirou. Solicita uma nova para continuar.' if request.args.get('expirado') else ''
        return render_template('demo_login.html', erro='', aviso=aviso,
                               usuario=(request.args.get('u') or '')[:40])

    usuario = (request.form.get('usuario') or '').strip().lower()[:40]
    senha = (request.form.get('senha') or '')[:128]
    if not csrf_ok():
        return render_template('demo_login.html', aviso='', usuario='', erro='Sessão inválida. Atualiza a página e tenta novamente.'), 400
    b_ip, f_ip = limite('login', 10, 600)
    b_us, f_us = limite('login_u', 5, 900, identificador=usuario or '-')
    if b_ip or b_us:
        registar_incidente('forca_bruta', 'login: ' + usuario, 3)
        return render_template('demo_login.html', aviso='', usuario=usuario,
                               erro='Demasiadas tentativas. Aguarda alguns minutos e tenta de novo.'), 429
    row = db().execute('SELECT * FROM demo_accounts WHERE usuario=?', (usuario,)).fetchone()
    ok = check_password_hash(row['senha_hash'] if row else _HASH_FALSO, senha) and row is not None
    if not ok:
        f_ip.append(time()); f_us.append(time())
        return render_template('demo_login.html', aviso='', usuario=usuario,
                               erro='Credenciais inválidas.'), 401
    if parse_iso(row['expira_em']) <= agora():
        return render_template('demo_login.html', aviso='', usuario='',
                               erro='Este acesso expirou. Solicita uma nova demonstração.'), 403
    session.clear(); session.permanent = True
    session['demo_id'] = row['id']; session['t0'] = int(time())
    db().execute('UPDATE demo_accounts SET ultimo_login=? WHERE id=?', (iso(agora()), row['id']))
    db().commit()
    return redirect(url_for('dashboard'))


@app.route('/demo/recuperar', methods=['GET', 'POST'])
def demo_recuperar():
    """Esqueceste a palavra-passe? As palavras-passe só existem como hash (não se podem 'revelar'),
    por isso, depois de confirmar utilizador + e-mail + telefone registados, geramos uma nova."""
    def pagina(erro='', nova=None, codigo=200):
        return render_template('demo_recuperar.html', erro=erro, nova=nova), codigo
    if request.method == 'GET':
        return pagina()
    if not csrf_ok():
        return pagina('Sessão inválida. Atualiza a página e tenta novamente.', codigo=400)
    usuario = (request.form.get('usuario') or '').strip().lower()[:40]
    email = (request.form.get('email') or '').strip().lower()[:160]
    tel = so_digitos(request.form.get('telefone'))
    b1, f1 = limite('rec', 8, 900)
    b2, f2 = limite('rec_u', 4, 900, identificador=usuario or '-')
    if b1 or b2:
        return pagina('Demasiadas tentativas. Aguarda alguns minutos.', codigo=429)
    row = db().execute('SELECT * FROM demo_accounts WHERE usuario=?', (usuario,)).fetchone()
    ok = (row is not None and len(tel) >= 9 and hmac.compare_digest(row['email'].lower(), email)
          and so_digitos(row['telefone'])[-9:] == tel[-9:])
    if not ok:
        f1.append(time()); f2.append(time())
        return pagina('Os dados não coincidem com nenhuma demonstração.', codigo=401)
    if parse_iso(row['expira_em']) <= agora():
        return pagina('Este acesso expirou. Solicita uma nova demonstração.', codigo=403)
    senha = nova_senha()
    db().execute('UPDATE demo_accounts SET senha_hash=? WHERE id=?', (generate_password_hash(senha), row['id']))
    db().commit()
    return pagina(nova=dict(usuario=row['usuario'], senha=senha, expira=row['expira_em']))


@app.post('/demo/logout')
def demo_logout():
    if csrf_ok():
        session.clear()
    return redirect(url_for('demo_login'))


@app.get('/demo/dashboard')
@demo_required
def dashboard():
    c = g.conta
    return render_template('dashboard.html', conta=c, modelos=catalogo_modelos(), servicos=SERVICOS, demo_horas=DEMO_HORAS,
                           expira_em=c['expira_em'], primeiro_nome=c['nome'].split()[0])


@app.get('/demo/modelo/<mid>')
@demo_required
def modelo_ao_vivo(mid):
    return resposta_modelo(mid, 'no-store, max-age=0')


@app.post('/api/demo/pedido')
@demo_required
def novo_pedido():
    d = json_dict()
    tipo = d.get('tipo')
    if tipo not in ('modelo', 'projeto', 'stock', 'contacto'):
        return jsonify(error='Pedido inválido.'), 400
    bloqueado, fila = limite('pedido', 20, 3600)
    if bloqueado:
        return jsonify(error='Muitos pedidos seguidos. Tenta mais tarde.'), 429

    txt = lambda k, n: sanitize_line(d.get(k))[:n] if isinstance(d.get(k), str) else ''
    assunto, mensagem = '', (sanitize_text(d.get('mensagem'))[:1500] if isinstance(d.get('mensagem'), str) else '')
    if tipo == 'modelo':
        m = next((x for x in catalogo_modelos() if x['id'] == d.get('modelo')), None)
        if not m: return jsonify(error='Modelo inválido.'), 400
        assunto = '%s (%s)' % (m['nome'], fmt_kz(m['preco']))
    elif tipo == 'projeto':
        tp, orc, prazo = txt('tipo_projeto', 60), txt('orcamento', 40), txt('prazo', 40)
        if not tp or len(mensagem) < 10:
            return jsonify(error='Escolhe o tipo de projeto e descreve a tua ideia (mín. 10 caracteres).'), 400
        assunto = tp
        mensagem = 'Orçamento: %s | Prazo: %s\n%s' % (orc or '—', prazo or '—', mensagem)
    elif tipo == 'stock':
        assunto = 'Sistema de Gestão de Stock'
    else:
        assunto = txt('assunto', 80) or 'Contacto'
        if len(mensagem) < 5:
            return jsonify(error='Escreve a tua mensagem.'), 400

    db().execute('INSERT INTO pedidos(demo_id,tipo,assunto,mensagem,criado_em) VALUES(?,?,?,?,?)',
                 (g.conta['id'], tipo, assunto, mensagem, iso(agora())))
    db().commit(); fila.append(time())
    c = g.conta
    notificar('Pedido na demo (%s): %s' % (tipo, assunto or tipo),
              'Cliente: %s\nE-mail: %s\nWhatsApp: %s (https://wa.me/%s)\nTipo: %s\nAssunto: %s\n\n%s' % (
                  c['nome'], c['email'], c['telefone'], so_digitos(c['telefone']), tipo, assunto, mensagem), c['email'])
    return jsonify(message='Pedido enviado! A nossa equipa vai contactar-te por WhatsApp ou e-mail em menos de 24 horas.'), 201


# ───────────────────────── admin (opcional) ─────────────────────────
def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not ADMIN_PASSWORD or (ADMIN_IPS and (request.remote_addr or '') not in ADMIN_IPS):
            abort(404)
        au = request.authorization
        if not au or au.username != 'admin' or not hmac.compare_digest(au.password or '', ADMIN_PASSWORD):
            if au:
                registar_incidente('admin_auth', 'tentativa de acesso ao /admin', 3)
            return Response('Autenticação necessária', 401, {'WWW-Authenticate': 'Basic realm="Envloument admin"'})
        return f(*a, **kw)
    return wrapper


@app.get('/admin')
@admin_required
def admin():
    contas = db().execute('SELECT * FROM demo_accounts ORDER BY id DESC LIMIT 200').fetchall()
    pedidos = db().execute('''SELECT p.*, a.nome, a.email, a.telefone FROM pedidos p
                              LEFT JOIN demo_accounts a ON a.id=p.demo_id ORDER BY p.id DESC LIMIT 200''').fetchall()
    leads = db().execute('SELECT * FROM leads ORDER BY id DESC LIMIT 200').fetchall()
    incidentes = db().execute('SELECT * FROM incidentes ORDER BY id DESC LIMIT 100').fetchall()
    bloqueados = [(ip, int((t - time()) / 60) + 1) for ip, t in BLOQUEIOS.items() if t > time()]
    return render_template('admin.html', contas=contas, pedidos=pedidos, leads=leads, agora=iso(agora()),
                           incidentes=incidentes, bloqueados=bloqueados, email_status=EMAIL_STATUS,
                           email_cfg=email_service.configured or bool(SMTP_USER and SMTP_PASS and CONTACT_EMAIL),
                           email_problemas=email_service.problems(), teste=request.args.get('m', ''),
                           persistente=PERSISTENTE, n_backups=len(listar_backups()))


@app.get('/admin/teste-email')
@admin_required
def admin_teste_email():
    if not origem_confiavel():
        registar_incidente('csrf', 'admin/teste-email de origem externa', 3)
        abort(403)
    ok, msg = enviar_email('Teste de e-mail', 'Se estás a ler isto, o envio de e-mails da Envloument está a funcionar.')
    EMAIL_STATUS.update(ultimo='%s UTC — %s' % (iso(agora())[:16].replace('T', ' '), msg), ok=ok)
    return redirect(url_for('admin', m=('OK: ' if ok else 'FALHOU: ') + msg[:300]))


@app.get('/admin/desbloquear')
@admin_required
def admin_desbloquear():
    if not origem_confiavel():
        registar_incidente('csrf', 'admin/desbloquear de origem externa', 3)
        abort(403)
    ip = request.args.get('ip', '')
    BLOQUEIOS.pop(ip, None); _STRIKES.pop(ip, None)
    return redirect(url_for('admin'))


@app.get('/admin/export.csv')
@admin_required
def admin_csv():
    linhas = db().execute('SELECT id,nome,email,telefone,empresa,servico,usuario,criado_em,expira_em,ultimo_login FROM demo_accounts ORDER BY id DESC').fetchall()
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(['id', 'nome', 'email', 'telefone', 'empresa', 'servico', 'usuario', 'criado_em', 'expira_em', 'ultimo_login'])
    for r in linhas:
        w.writerow([('\'' + str(v) if isinstance(v, str) and v[:1] in '=+-@' else v) for v in tuple(r)])
    return Response('\ufeff' + buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=demonstracoes.csv'})



# ───────────── backups da base de dados ─────────────
BACKUP_DIR = os.path.join(os.path.dirname(DB_PATH), 'backups')


def fazer_backup(destino=None):
    """Cópia consistente da base de dados (API de backup do SQLite, segura mesmo com a app a escrever)."""
    destino = destino or os.path.join(BACKUP_DIR, 'envloument-%s.db' % agora().strftime('%Y%m%d-%H%M'))
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    origem, copia = sqlite3.connect(DB_PATH), sqlite3.connect(destino)
    try:
        origem.backup(copia)
    finally:
        copia.close(); origem.close()
    return destino


def listar_backups():
    return sorted(glob.glob(os.path.join(BACKUP_DIR, 'envloument-*.db')))


def _ciclo_backups(manter=7):
    """Um backup por dia dentro do Volume; guarda os últimos 7."""
    while True:
        try:
            ficheiros = listar_backups()
            if not ficheiros or time() - os.path.getmtime(ficheiros[-1]) > 20 * 3600:
                app.logger.info('Backup diário criado: %s', fazer_backup())
                for velho in listar_backups()[:-manter]:
                    os.remove(velho)
        except Exception:
            app.logger.exception('Falha no backup automático')
        sleep(3600)


@app.get('/admin/copia-seguranca.db')
@admin_required
def admin_backup():
    """Descarrega uma cópia completa da base de dados (contém dados pessoais: guarda-a em local seguro)."""
    with tempfile.TemporaryDirectory() as pasta:
        with open(fazer_backup(os.path.join(pasta, 'backup.db')), 'rb') as f:
            dados = f.read()
    return Response(dados, mimetype='application/octet-stream',
                    headers={'Content-Disposition': 'attachment; filename=envloument-backup-%s.db' % agora().strftime('%Y%m%d')})


init_db()
if EM_PRODUCAO and PERSISTENTE and os.getenv('BACKUPS', '1') != '0':
    threading.Thread(target=_ciclo_backups, daemon=True).start()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=os.getenv("FLASK_DEBUG") == "1" and not NA_RAILWAY
    )
