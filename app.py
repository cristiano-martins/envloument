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
import hmac
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
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'envloument.db')
DEMO_HORAS = int(os.getenv('DEMO_HORAS', '24'))
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.getenv('SESSION_SECRET') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  MAX_CONTENT_LENGTH=64 * 1024)

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
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def fechar_db(_):
    conn = g.pop('db', None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
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


def limite(chave, maximo, janela_s):
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '?').split(',')[0].strip()
    fila = _hits[(chave, ip)]
    t = time()
    while fila and t - fila[0] > janela_s:
        fila.popleft()
    return len(fila) >= maximo, fila


def gerar_credenciais(nome):
    conn = db()
    base = 'demo_' + (slug(nome.split()[0])[:10] or 'cliente')
    for _ in range(30):
        usuario = base + ''.join(secrets.choice('0123456789') for _ in range(2))
        if not conn.execute('SELECT 1 FROM demo_accounts WHERE usuario=?', (usuario,)).fetchone():
            break
    else:
        usuario = base + secrets.token_hex(3)
    alfabeto = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # sem O/0/I/1 para evitar confusões
    senha = 'ENV%d-%s' % (agora().year, ''.join(secrets.choice(alfabeto) for _ in range(5)))
    return usuario, senha


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
    return send_from_directory(BASE_DIR, 'index.html')


@app.get('/api/health')
def health():
    return jsonify(ok=True)


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
    return jsonify(
        message='Conta de demonstração criada com sucesso. As suas credenciais são válidas durante %d horas.' % DEMO_HORAS,
        usuario=usuario, senha=senha, expira_em=iso(expira), validade_horas=DEMO_HORAS), 201


@app.post('/api/contato')
def contato():
    """Formulário de contacto da página inicial."""
    d = request.get_json(silent=True) or {}
    nome = (d.get('nome') or '').strip()[:80]
    email = (d.get('email') or '').strip()[:120]
    msg = (d.get('mensagem') or '').strip()[:1500]
    if len(nome) < 2 or not email_valido(email) or len(msg) < 10:
        return jsonify(error='Dados inválidos.'), 400
    db().execute('INSERT INTO leads(nome,email,mensagem,created_at) VALUES(?,?,?,?)', (nome, email, msg, iso(agora())))
    db().commit()
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

    usuario = (request.form.get('usuario') or '').strip().lower()
    senha = request.form.get('senha') or ''
    bloqueado, fila = limite('login', 10, 600)
    if bloqueado:
        return render_template('demo_login.html', aviso='', usuario=usuario,
                               erro='Demasiadas tentativas. Aguarda alguns minutos e tenta de novo.'), 429
    row = db().execute('SELECT * FROM demo_accounts WHERE usuario=?', (usuario,)).fetchone()
    if row is None or not check_password_hash(row['senha_hash'], senha):
        fila.append(time())
        return render_template('demo_login.html', aviso='', usuario=usuario,
                               erro='Utilizador ou palavra-passe incorretos.'), 401
    if parse_iso(row['expira_em']) <= agora():
        return render_template('demo_login.html', aviso='', usuario=usuario,
                               erro='Estas credenciais já expiraram (validade de 24 horas). Solicita uma nova demonstração.'), 403
    session.clear(); session['demo_id'] = row['id']
    db().execute('UPDATE demo_accounts SET ultimo_login=? WHERE id=?', (iso(agora()), row['id']))
    db().commit()
    return redirect(url_for('dashboard'))


@app.post('/demo/logout')
def demo_logout():
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
    return render_template('admin.html', contas=contas, pedidos=pedidos, agora=iso(agora()))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000), debug=os.getenv('FLASK_DEBUG') == '1')
