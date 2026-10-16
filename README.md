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

## Notificações (e-mail + WhatsApp)
Cada demonstração, mensagem de contacto e pedido na área demo gera um aviso para `envlomenttechnologies@gmail.com`.
1. Gmail → Conta Google → Segurança → Verificação em 2 passos → **Palavras-passe de aplicação** → cria uma.
2. No Render define `SMTP_USER=envlomenttechnologies@gmail.com` e `SMTP_PASS=<palavra-passe de aplicação>`.
3. (Opcional) WhatsApp automático para +244 952 393 828 via CallMeBot: envia "I allow callmebot to send me messages" para +34 644 51 95 23, recebes a chave e defines `CALLMEBOT_APIKEY`.
Sem estas variáveis tudo continua a funcionar; os pedidos ficam em `/admin` (com ligação direta ao WhatsApp do cliente).

## Credenciais da demo
- Ao pedir a demonstração o ficheiro `envloument-credenciais.txt` é descarregado automaticamente (e há botão para repetir).
- Esqueceu-se da palavra-passe? `/demo/recuperar` (utilizador + e-mail + telefone do pedido) gera uma nova. As palavras-passe só existem como hash, por isso não podem ser "reveladas".
