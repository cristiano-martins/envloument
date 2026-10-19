# Envloument — MVP "Solicitar Demonstração"

## Como executar
    pip install -r requirements.txt
    python app.py          # abre http://127.0.0.1:5000

## Fluxo
1. Site (`/`) → botão **Solicitar Demonstração** → formulário.
2. O servidor gera utilizador + palavra-passe temporários (validade 24 h) e mostra-os no ecrã.
3. `/demo/login` → introduz as credenciais → `/demo/dashboard`.
4. Dashboard: Início · Modelos de Sites · Serviços · Gestão de Stock · Solicitar Projeto · Contacto.

## Ver os pedidos recebidos (opcional)
    ADMIN_PASSWORD=uma-senha-forte python app.py     # depois abre /admin (utilizador: admin)

## Estrutura
    app.py               backend Flask (SQLite: envloument.db, criado automaticamente)
    index.html           página inicial (HTML + CSS + JS num só ficheiro)
    templates/           demo_login.html · dashboard.html · admin.html
    static/css/demo.css  estilos da área demo
    static/img/          fundo.jpg (imagem do fundo da página), logo e pré-visualizações dos modelos
    modelos/             mini-sites (demonstração ao vivo de cada modelo)

## Notas
- Define SESSION_SECRET em produção (senão as sessões reiniciam a cada arranque).
- As palavras-passe temporárias ficam guardadas só com hash; a validade (DEMO_HORAS) é verificada no servidor a cada pedido.

## Deploy no Railway
1. Sobe esta pasta para um repositório GitHub → Railway → **New Project → Deploy from GitHub repo**.
2. **Volume (obrigatório para não perder dados):** no serviço → *Settings → Volumes → Add Volume* → mount path `/data`. A app deteta-o sozinha (`RAILWAY_VOLUME_MOUNT_PATH`).
3. **Variables:** `SESSION_SECRET` (texto aleatório longo), `ADMIN_PASSWORD` (para `/admin`), e `SMTP_USER` / `SMTP_PASS` (ver abaixo).
4. *Settings → Networking → Generate Domain* (ou domínio próprio). Health check: `/api/health`.
- Corre com 1 worker + 8 threads (limites de tentativas consistentes e SQLite seguro).
- Local: `FLASK_DEBUG=1 python app.py` → http://127.0.0.1:5000

## E-mails via Resend (formulário de contacto)
O Railway **bloqueia SMTP (Gmail) nos planos Free/Trial/Hobby**; por isso o site usa a API HTTPS do **Resend**.
Variáveis (Railway → Variables; modelo em `.env.example`):

| Variável | Para quê |
|---|---|
| `RESEND_API_KEY` | chave da API (resend.com → API Keys) |
| `CONTACT_EMAIL` | e-mail que recebe as mensagens |
| `RESEND_FROM_EMAIL` | remetente; em testes `onboarding@resend.dev`, em produção um endereço de um domínio verificado |
| `RESEND_FROM_NAME` | nome do remetente (opcional) |

Nota: no modo de teste (`onboarding@resend.dev`) o Resend só entrega para o e-mail da conta Resend; para outros destinatários verifica um domínio.
**Rota:** `POST /api/contact` (JSON: `nome`, `email`, `assunto`, `mensagem`; `/api/contato` continua a funcionar). Respostas: `201` ok · `400` JSON inválido · `422` campos inválidos (`fields`) · `429` demasiados pedidos (`Retry-After`) · `502/503/504` falha do e-mail (a mensagem fica guardada e visível em `/admin`).
Ficheiros (todos ao lado do `app.py`): `email_service.py` (cliente Resend) · `contact_validation.py` (validação/sanitização) · `email_templates.py` (e-mail HTML+texto). Testes: `python -m unittest discover -s tests -v`.
Diagnóstico: `/admin` → **Estado do e-mail** → *Enviar e-mail de teste* (mostra as variáveis em falta ou o erro exato do Resend).
(Opcional) WhatsApp automático via CallMeBot: `CALLMEBOT_APIKEY`. SMTP só em planos que o permitam: `SMTP_USER` + `SMTP_PASS`.

## Defesa ativa
O servidor regista e bloqueia (1 h) IPs que sondam caminhos (`/.env`, `/wp-admin`…), enviam injeção SQL/XSS, usam scanners (sqlmap, nikto…), preenchem honeypots ou forçam logins/admin, e **avisa por e-mail**. Vê e desbloqueia IPs em `/admin`. Para nunca te bloqueares a ti: `IP_CONFIANCA=o.teu.ip`.
O F12/Ctrl+U bloqueado no navegador é só dissuasão (qualquer pessoa o contorna); a proteção real é do servidor — o que alguém altere no navegador nunca altera o site nem a base de dados. O código é servido minificado e sem comentários; **mantém o repositório GitHub privado**.

## Assistente com IA (opcional)
Funciona já com respostas locais e perguntas sugeridas. Para IA real: `ANTHROPIC_API_KEY` (e opcionalmente `ANTHROPIC_MODEL`) no Railway; o limite é 20 perguntas/10 min por IP e respostas até 350 tokens.

## Fotos e redes da liderança
Fotos: coloca JPGs quadrados em `static/img/equipa/` (`wilson.jpg`, `cristiano.jpg`, `jeremias.jpg`, `patricia.jpg`, `jony.jpg`). Redes: edita o objeto `REDES` no fim do `index.html` (links LinkedIn/GitHub; só aparecem os preenchidos).

## Credenciais da demo
- Ao pedir a demonstração o ficheiro `envloument-credenciais.txt` é descarregado automaticamente (e há botão para repetir).
- Esqueceu-se da palavra-passe? `/demo/recuperar` (utilizador + e-mail + telefone do pedido) gera uma nova. As palavras-passe só existem como hash, por isso não podem ser "reveladas".

## Modelos de sites
Cada modelo é um ficheiro em `modelos/<id>.html` e está **embutido no `index.html`** (por isso o site não precisa de mais ficheiros no deploy). Depois de editar um modelo: `python embed_modelos.py`.
