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
    PORT / FLASK_DEBUG
"""
import gzip
import hmac
import smtplib
import threading
import urllib.parse
import urllib.request
from email.message import EmailMessage
import csv
import io
import os
import re
import secrets
import sqlite3
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps
from time import time

from flask import (Flask, Response, abort, g, jsonify, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_VOLUME = os.getenv('RAILWAY_VOLUME_MOUNT_PATH')   # definido automaticamente pelo Railway quando há Volume
DB_PATH = os.getenv('DATABASE_PATH') or os.path.join(_VOLUME or BASE_DIR, 'envloument.db')
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
DEMO_HORAS = int(os.getenv('DEMO_HORAS', '24'))
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')

NOTIFY_EMAIL = os.getenv('NOTIFY_EMAIL', 'envlomenttechnologies@gmail.com')
WHATSAPP_NUM = os.getenv('WHATSAPP_NUM', '244952393828')
SMTP_HOST = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER', '')            # ex.: envlomenttechnologies@gmail.com
SMTP_PASS = os.getenv('SMTP_PASS', '')            # palavra-passe de aplicação do Gmail
CALLMEBOT_KEY = os.getenv('CALLMEBOT_APIKEY', '') # opcional: avisos automáticos no WhatsApp

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
EM_PRODUCAO = os.getenv('FLASK_DEBUG') != '1'
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=EM_PRODUCAO and os.getenv('INSECURE_COOKIES') != '1',
                  MAX_CONTENT_LENGTH=64 * 1024, SEND_FILE_MAX_AGE_DEFAULT=86400)
if os.getenv('TRUST_PROXY') == '1' or os.getenv('RAILWAY_ENVIRONMENT'):          # atrás do proxy do Render/nginx: confia em 1 salto
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def notificar(assunto, corpo, responder_a=None):
    """Envia aviso por e-mail (SMTP) e, se configurado, por WhatsApp (CallMeBot). Em segundo plano."""
    assunto = re.sub(r'[\r\n]+', ' ', assunto)[:150]

    def _enviar():
        if SMTP_USER and SMTP_PASS:
            try:
                m = EmailMessage()
                m['Subject'], m['From'], m['To'] = '[Envloument] ' + assunto, SMTP_USER, NOTIFY_EMAIL
                if responder_a and email_valido(responder_a):
                    m['Reply-To'] = responder_a
                m.set_content(corpo)
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as srv:
                    srv.starttls(); srv.login(SMTP_USER, SMTP_PASS); srv.send_message(m)
            except Exception as e:
                print('Falha ao enviar e-mail:', e)
        if CALLMEBOT_KEY:
            try:
                q = urllib.parse.urlencode({'phone': '+' + WHATSAPP_NUM, 'apikey': CALLMEBOT_KEY,
                                            'text': assunto + '\n' + corpo[:600]})
                urllib.request.urlopen('https://api.callmebot.com/whatsapp.php?' + q, timeout=15).read()
            except Exception as e:
                print('Falha ao enviar WhatsApp:', e)
    threading.Thread(target=_enviar, daemon=True).start()


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
    resp.headers['Vary'] = 'Accept-Encoding'
    return resp


@app.after_request
def cabecalhos_seguranca(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    resp.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    if request.path.startswith(('/demo', '/admin', '/api/demo')):
        resp.headers['Cache-Control'] = 'no-store'
    return resp

SERVICOS = ['Criação de Sites', 'Loja Online', 'Gestão de Stock', 'Outros']

# Modelos mostrados no dashboard (preços em Kwanzas)
MODELOS = [
    dict(id='aurora', nome='Landing Page', tipo='Página única', preco=85000, prazo='3 a 5 dias',
         imagem='modelo-landing.jpg',
         desc='Uma página única, moderna e de alto impacto para lançar um produto, serviço ou campanha e transformar visitantes em contactos.',
         extras=['Design 100% responsivo', 'Formulário de contacto', 'Botão de WhatsApp', 'Otimização SEO básica', 'Carregamento ultra-rápido']),
    dict(id='meridian', nome='Site Institucional', tipo='Multi-página', preco=180000, prazo='7 a 10 dias',
         imagem='modelo-institucional.jpg',
         desc='O cartão de visita digital da tua empresa: apresenta serviços, equipa e resultados com uma imagem profissional e de confiança.',
         extras=['Até 6 páginas', 'Secção de serviços e equipa', 'Formulário de propostas', 'Mapa de localização', 'Painel para editar textos']),
    dict(id='mercato', nome='Loja Online', tipo='E-commerce', preco=350000, prazo='12 a 15 dias',
         imagem='modelo-loja.jpg',
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
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT NOT NULL,
            mensagem TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    ''')
    conn.commit(); conn.close()


# ───────────────────────── utilitários ─────────────────────────
def agora():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat(timespec='seconds')


def parse_iso(s):
    return datetime.fromisoformat(s)


def email_valido(e):
    return bool(re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', e or ''))


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
    if row is None or parse_iso(row['expira_em']) <= agora():
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
    with open(os.path.join(BASE_DIR, 'index.html'), 'rb') as f:
        r = Response(f.read(), mimetype='text/html')
    r.headers['Cache-Control'] = 'public, max-age=300'
    return r


@app.get('/api/health')
def health():
    try:
        db().execute('SELECT 1').fetchone()
        return jsonify(ok=True)
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
    d = request.get_json(silent=True) or {}
    if d.get('website'):                       # honeypot anti-bots
        return jsonify(error='Pedido inválido.'), 400
    bloqueado, fila = limite('solicitar', 5, 3600)
    if bloqueado:
        return jsonify(error='Demasiados pedidos deste dispositivo. Tenta novamente daqui a pouco.'), 429

    nome = (d.get('nome') or '').strip()
    email = (d.get('email') or '').strip().lower()
    telefone = (d.get('telefone') or '').strip()
    empresa = (d.get('empresa') or '').strip()[:100]
    servico = (d.get('servico') or '').strip()

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


@app.post('/api/contato')
def contato():
    """Formulário de contacto da página inicial."""
    d = request.get_json(silent=True) or {}
    if d.get('website'):
        return jsonify(error='Pedido inválido.'), 400
    bloqueado, fila = limite('contato', 5, 3600)
    if bloqueado:
        return jsonify(error='Demasiados pedidos deste dispositivo. Tenta novamente mais tarde.'), 429
    nome = (d.get('nome') or '').strip()[:80]
    email = (d.get('email') or '').strip()[:120]
    msg = (d.get('mensagem') or '').strip()[:1500]
    if len(nome) < 2 or not email_valido(email) or len(msg) < 10:
        return jsonify(error='Dados inválidos.'), 400
    db().execute('INSERT INTO leads(nome,email,mensagem,created_at) VALUES(?,?,?,?)', (nome, email, msg, iso(agora())))
    db().commit(); fila.append(time())
    notificar('Mensagem de contacto: ' + nome, 'Nome: %s\nE-mail: %s\n\n%s' % (nome, email, msg), email)
    return jsonify(message='Pedido recebido.'), 201


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
    session.clear(); session['demo_id'] = row['id']
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
    return render_template('dashboard.html', conta=c, modelos=MODELOS, servicos=SERVICOS,
                           expira_em=c['expira_em'], primeiro_nome=c['nome'].split()[0])


@app.get('/demo/modelo/<mid>')
@demo_required
def modelo_ao_vivo(mid):
    if mid not in MODELOS_POR_ID:
        abort(404)
    return send_from_directory(os.path.join(BASE_DIR, 'modelos'), mid + '.html')


@app.post('/api/demo/pedido')
@demo_required
def novo_pedido():
    d = request.get_json(silent=True) or {}
    tipo = d.get('tipo')
    if tipo not in ('modelo', 'projeto', 'stock', 'contacto'):
        return jsonify(error='Pedido inválido.'), 400
    bloqueado, fila = limite('pedido', 20, 3600)
    if bloqueado:
        return jsonify(error='Muitos pedidos seguidos. Tenta mais tarde.'), 429

    assunto, mensagem = '', (d.get('mensagem') or '').strip()[:1500]
    if tipo == 'modelo':
        m = MODELOS_POR_ID.get(d.get('modelo'))
        if not m: return jsonify(error='Modelo inválido.'), 400
        assunto = '%s (%s)' % (m['nome'], fmt_kz(m['preco']))
    elif tipo == 'projeto':
        tp = (d.get('tipo_projeto') or '').strip()[:60]
        orc = (d.get('orcamento') or '').strip()[:40]
        prazo = (d.get('prazo') or '').strip()[:40]
        if not tp or len(mensagem) < 10:
            return jsonify(error='Escolhe o tipo de projeto e descreve a tua ideia (mín. 10 caracteres).'), 400
        assunto = tp
        mensagem = 'Orçamento: %s | Prazo: %s\n%s' % (orc or '—', prazo or '—', mensagem)
    elif tipo == 'stock':
        assunto = 'Sistema de Gestão de Stock'
    else:
        assunto = (d.get('assunto') or 'Contacto').strip()[:80]
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
        if not ADMIN_PASSWORD:
            abort(404)
        au = request.authorization
        if not au or au.username != 'admin' or not hmac.compare_digest(au.password or '', ADMIN_PASSWORD):
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
    return render_template('admin.html', contas=contas, pedidos=pedidos, leads=leads, agora=iso(agora()))


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


init_db()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=os.getenv("FLASK_DEBUG") == "1"
    )
