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
