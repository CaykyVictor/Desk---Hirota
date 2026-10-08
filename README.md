# Hirota Desk – backend (WhatsApp + e-mail + painel da TI)

Funcionários abrem chamados pelo WhatsApp; a TI recebe por **e-mail** e no **painel web** (`/painel`) e responde pelo painel, com a resposta voltando ao WhatsApp.

## Fluxo
1. Funcionário envia mensagem ao número da TI → robô mostra o menu de categorias (Rede, Software, Hardware, Balança…).
2. Escolhe a urgência (1 Crítica · 2 Alta · 3 Média · 4 Baixa) → informa o código da loja (pulado se o telefone estiver cadastrado) → descreve o problema (pode mandar foto).
3. O sistema cria o chamado `CH-0001`, calcula o prazo (SLA 1 h / 4 h / 24 h / 72 h, horas corridas) e **envia e-mail à TI**. Chamados abertos pelo botão **+ Novo chamado** do painel também disparam o e-mail.
4. Novas mensagens do funcionário entram no chamado aberto e geram e-mail. A TI responde no painel → vai por WhatsApp.
5. Ao marcar **Resolvido**, o funcionário recebe "1 = resolvido, 2 = continua": 1 fecha o chamado, 2 reabre.
6. Palavras-chave: `menu`, `novo` ou `0` reiniciam o robô a qualquer momento.

## Notificações por e-mail para a TI
Enviadas para todos os endereços de `TI_EMAILS` quando: **(a)** chega um chamado novo (WhatsApp ou painel), **(b)** o funcionário escreve num chamado aberto, **(c)** a loja reabre um chamado, **(d)** a loja confirma o fechamento. O assunto traz a prioridade, ex.: `[Crítica] Novo chamado CH-0012 – U045 – Hirota Express Centro – Rede`, e o corpo traz o link para atender.
- Configure `SMTP_HOST`, `SMTP_PORT` (587 com STARTTLS ou 465 com SSL), `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` e `TI_EMAILS` no `.env`.
- **Teste antes de usar:** `flask --app app test-email`.
- Em caso de falha o envio é repetido até 3 vezes (imediato, 5 s e 30 s); se o servidor reiniciar nesse intervalo, o aviso se perde. O chamado continua salvo e visível no painel.
- Provedores como Gmail e Microsoft 365 podem exigir senha de aplicativo ou habilitar SMTP autenticado; confira a regra atual do seu provedor.
- O protótipo web publicado no claude.ai **não envia e-mail**; a notificação só funciona neste back-end.

## Rodar localmente (sem WhatsApp/e-mail reais)
```bash
pip install -r requirements.txt
python test_flow.py                      # teste ponta a ponta
PANEL_PASS=senha python app.py           # painel em http://localhost:8000/painel (usuário: admin)
```
Sem `WA_TOKEN`/`SMTP_HOST` o sistema roda em modo simulado (grava em log em vez de enviar).

## Cadastros (opcional, mas recomendado)
```bash
# unidades.csv: codigo,nome,formato      ex.: U001,Hirota Super Centro,Super
flask --app app import-units unidades.csv
# telefones.csv: telefone,codigo,nome    ex.: 5511911112222,U001,Maria  (DDI+DDD, só números)
flask --app app import-phones telefones.csv
```

## Colocar no ar
1. **Servidor com HTTPS público** (a Meta só chama webhooks HTTPS): VPS, Render, Railway etc.
   `gunicorn -w 1 -b 0.0.0.0:8000 app:app` atrás de Nginx/Caddy. Use **1 worker** (SQLite) ou migre para PostgreSQL.
2. Copie `.env.example` para `.env` e preencha (exporte as variáveis ou use o gerenciador do seu servidor).
3. **Meta for Developers**: crie um app com o produto WhatsApp, cadastre um número comercial dedicado, gere um token permanente (usuário de sistema) e anote *Phone number ID* e *App secret*.
4. Em WhatsApp → Configuração → Webhook: URL `https://SEU-DOMINIO/webhook`, token = `VERIFY_TOKEN`, assine o campo `messages`.
5. Faça backup do `desk.db` e da pasta `uploads/` periodicamente.

## Limitações conhecidas
- **Janela de 24 h do WhatsApp**: só é possível responder texto livre até 24 h após a última mensagem do funcionário. Fora disso o painel mostra "não entregue"; seria preciso cadastrar *modelos de mensagem* aprovados pela Meta (não implementado).
- Painel com um único login (HTTP Basic), sem perfis. Atualização por consulta a cada 8 s.
- E-mail só sai para a TI (não há e-mail para o funcionário).
- Não há escalonamento automático ao estourar o SLA, nem relatórios além do painel.
- Mídia (fotos) é baixada na hora do recebimento; áudios e vídeos são salvos mas não tocados no painel.
- Custos e regras do WhatsApp Business mudam; confirme a tabela atual de preços da Meta ou do provedor.
