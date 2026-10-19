# Relatório de segurança — Envloument

Auditoria da aplicação Flask (Railway). Cobertura de testes: `tests/test_security.py` + `tests/test_contact.py` + `tests/test_site.py` (49 testes, todos a passar).

## Verificado e correto (sem alterações necessárias)
| Controlo | Estado |
|---|---|
| SQL Injection | Todas as queries usam parâmetros (`?`). A única interpolação é o `ALTER TABLE` com nomes de coluna fixos no código. |
| XSS | Jinja com auto-escape, sem `|safe`; no JavaScript, dados de utilizador entram por `textContent`/`esc()`. |
| Passwords | Hash scrypt (Werkzeug), comparação em tempo constante, resposta idêntica para utilizador inexistente (hash falso). |
| Uploads | Não existe nenhum endpoint de upload; corpo máximo 64 KB. |
| Segredos | Nenhuma chave no código; `.env`, `*.db`, `.session_key` no `.gitignore`; só `/static` é servido. |
| Rotas protegidas | `/demo/*` e `/api/demo/pedido` exigem sessão válida; `/admin*` devolve 404 sem `ADMIN_PASSWORD`. |

## Vulnerabilidades encontradas e corrigidas
| # | Descrição | Risco | Correção |
|---|---|---|---|
| S1 | Ações do `/admin` por GET (desbloquear IP, e-mail de teste) sujeitas a CSRF com autenticação Basic | **Alto** | `origem_confiavel()` (Sec-Fetch-Site / Origin / Referer) |
| S2 | Sem verificação de `Origin` em pedidos POST | Médio | Rejeição 403 + incidente quando o Origin não é o próprio host |
| S3 | Sessão sem validade própria; cookie sem prefixo `__Host-` | Médio | Sessão de 8 h (renovável) com limite absoluto de 12 h; `__Host-envl`; `Secure` forçado no Railway |
| S4 | JSON que não é objeto (`[1]`) causava erro 500 | Médio | `json_dict()` → 400 |
| S5 | `solicitar`/`pedido` guardavam texto sem sanitizar; regex de e-mail fraca | Médio | `sanitize_line/sanitize_text/is_valid_email` |
| S6 | Sem limite global de pedidos por IP | Médio | 300 pedidos/min por IP (429 + Retry-After); `/api/health` e `/static` isentos |
| S7 | `Cache-Control: no-store` só em parte das rotas sensíveis | Médio | Estendido a `/demo`, `/admin` e todo o `/api/` (+ `Pragma`, `Vary: Cookie`) |
| S8 | Cabeçalhos em falta | Baixo | COOP, CORP, X-Permitted-Cross-Domain-Policies, `upgrade-insecure-requests` (em HTTPS) |
| S9 | Sem redirecionamento HTTP→HTTPS na aplicação | Médio | 308 quando `X-Forwarded-Proto=http` (exceto health check) |
| S10 | `FLASK_DEBUG`/`INSECURE_COOKIES` esquecidos no Railway enfraqueciam os cookies | Médio | `RAILWAY_ENVIRONMENT` força produção e cookies seguros |
| S11 | `/admin` sem restrição de IP e sem aviso de palavra-passe fraca | Baixo | `ADMIN_IPS` opcional; avisos no arranque |
| S12 | Modelos dependiam de ficheiros (404 em produção) | Funcional | HTML dos modelos embutido no `index.html`; `/api/health` mostra `modelos: 9` |
| — | Código morto | Baixo | Import `send_from_directory` removido; `.gitignore` reforçado |

Já existentes e mantidos: CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, HSTS (em HTTPS), CSRF em login/recuperação/logout, rate limiting de login/contacto/demo/assistente/recuperação, bloqueio automático de IPs hostis com alerta por e-mail, honeypots, erros 500 genéricos.

## Requer intervenção manual
| Item | Risco | O que fazer |
|---|---|---|
| CSP com `'unsafe-inline'` em `script-src` | **Médio** | O site usa scripts e handlers inline. Remover exige refatorar para ficheiros JS externos + nonces (muda a estrutura do front-end). Mitigado por escape rigoroso e sanitização. |
| Dependências | Médio | Não foi possível consultar bases de vulnerabilidades sem rede. Corre `pip-audit -r requirements.txt` e ativa o Dependabot no GitHub. Versões fixadas: Flask 3.1.3, Werkzeug 3.1.7, gunicorn 23.0.0 — confirma as mais recentes. |
| Variáveis no Railway | Alto se em falta | `SESSION_SECRET` (32+), `ADMIN_PASSWORD` (12+), `RESEND_API_KEY`; marca-as como *sealed*. |
| Contas | Alto se em falta | 2FA no GitHub, Railway e Resend; repositório **privado**; rodar qualquer chave que alguma vez tenha sido enviada para o Git. |
| Rate limiting em memória | Baixo | Válido com 1 worker (configuração atual). Com vários workers/instâncias, migrar para Redis. |
| Base de dados SQLite | Baixo | Volume em `/data` e backups regulares (regra 3-2-1). Considerar PostgreSQL se o volume crescer. |
| RGPD/Lei de proteção de dados | Médio | A política de privacidade do site é um modelo: revê com um advogado e define prazos de retenção para leads e contas demo. |

## Recomendações para produção
1. Domínio próprio com HTTPS e (após validar) HSTS com `includeSubDomains`; só depois ponderar `preload`.
2. `ADMIN_IPS` com o(s) teu(s) IP(s) fixo(s) e uma palavra-passe longa no `/admin`.
3. Monitorizar os alertas de e-mail "ALERTA DE SEGURANÇA" e rever a tabela de incidentes no `/admin`.
4. Domínio verificado no Resend (SPF/DKIM) para que os e-mails não caiam em spam.
5. Executar um pentest autorizado antes de tratar dados sensíveis ou pagamentos.
6. Se usares validação de hosts (TrustedHost) no futuro, permite `healthcheck.railway.app`, senão o health check do Railway falha.
