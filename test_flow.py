"""Teste ponta a ponta do fluxo (sem WhatsApp/SMTP reais). Rode: python test_flow.py"""
import base64, os, tempfile
d = tempfile.mkdtemp()
os.environ.update(DB_PATH=d + "/t.db", UPLOADS=d + "/up", PANEL_PASS="x", VERIFY_TOKEN="vt")
import app as A
A.app.config["SYNC"] = True
c = A.app.test_client()
H = {"Authorization": "Basic " + base64.b64encode(b"admin:x").decode()}
A.x("INSERT INTO units VALUES('U001','Hirota Super Centro','Super')")
A.x("INSERT INTO phones VALUES('5511911112222','U001','Maria')")
n = [0]
def wa(frm, body=None, kind="text", **extra):
    n[0] += 1; m = {"id": f"w{n[0]}", "from": frm, "type": kind}
    m.update({"text": {"body": body}} if kind == "text" else extra)
    return c.post("/webhook", json={"entry": [{"changes": [{"value": {"contacts": [{"wa_id": frm, "profile": {"name": "Zé"}}], "messages": [m]}}]}]})
last = lambda: A.SIM[-1][1]

assert c.get("/webhook?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=123").data == b"123"
assert c.get("/webhook?hub.mode=subscribe&hub.verify_token=zz&hub.challenge=1").status_code == 403
assert c.get("/api/tickets").status_code == 401

# loja desconhecida: menu -> categoria -> urgência -> código -> descrição
P = "5511999990000"
wa(P, "oi"); assert "Qual o tipo" in last()
wa(P, "4"); assert "urgência" in last()
wa(P, "1"); assert "código da sua loja" in last()
wa(P, "U999"); assert "Não encontrei" in last()
wa(P, "u001"); assert "Hirota Super Centro" in last()
wa(P, "Balança do açougue não liga")
assert "CH-0001" in last() and "Prazo de atendimento: 1 h" in last()
assert A.MAILS[-1][0].startswith("[Crítica] Novo chamado CH-0001")
t = A.q("SELECT * FROM tickets", one=True); assert t["cat"] == "Balança" and t["prio"] == "Crítica" and t["format"] == "Super"

# mensagem livre vai para o chamado aberto; foto também; reentrega é ignorada
wa(P, "ela caiu da bancada"); assert "adicionada ao chamado" not in last()
wa(P, kind="image", image={"id": "IMG1", "caption": "foto"})
r = c.get("/api/tickets/1", headers=H).get_json()
assert [m["direction"] for m in r["messages"]].count("in") == 3
m0 = {"id": "dup", "from": P, "type": "text", "text": {"body": "x"}}
body = {"entry": [{"changes": [{"value": {"messages": [m0]}}]}]}
c.post("/webhook", json=body); c.post("/webhook", json=body)
assert sum(1 for m in c.get("/api/tickets/1", headers=H).get_json()["messages"] if m["text"] == "x") == 1

# TI responde pelo painel -> volta ao WhatsApp
r = c.post("/api/tickets/1/messages", headers=H, json={"text": "Vou enviar um técnico"}).get_json()
assert r["delivery"] == "simulado" and A.SIM[-1] == (P, "Vou enviar um técnico")
c.patch("/api/tickets/1", headers=H, json={"status": "Em atendimento", "tech": "Carlos"})
assert "Em atendimento" in last()

# resolvido -> confirmação da loja
c.patch("/api/tickets/1", headers=H, json={"status": "Resolvido", "solution": "Trocada a fonte"})
assert "Trocada a fonte" in last() and "*1*" in last()
wa(P, "2"); assert A.q("SELECT status FROM tickets", one=True)[0] == "Aberto"
c.patch("/api/tickets/1", headers=H, json={"status": "Resolvido"}); wa(P, "1")
assert A.q("SELECT status FROM tickets", one=True)[0] == "Fechado"

# telefone cadastrado (inclusive sem o 9º dígito) pula a pergunta da loja
Q = "551111112222"
wa(Q, "menu"); wa(Q, "1"); wa(Q, "2"); assert "Hirota Super Centro" in last()
wa(Q, "Sem internet na loja")
t2 = A.q("SELECT * FROM tickets WHERE id=2", one=True); assert t2["unit_code"] == "U001" and t2["prio"] == "Alta" and t2["requester"] == "Maria"
lst = c.get("/api/tickets", headers=H).get_json(); assert len(lst) == 2 and lst[0]["last_dir"] == "out"
# chamado criado pelo painel (sem WhatsApp) também dispara e-mail
n0 = len(A.MAILS)
r = c.post("/api/tickets", headers=H, json={"unit": "u001", "cat": "Rede", "prio": "Crítica", "desc": "PDV 2 sem rede", "requester": "Ana"})
assert r.status_code == 201 and len(A.MAILS) == n0 + 1 and "Painel (admin)" in A.MAILS[-1][1] and A.MAILS[-1][0].startswith("[Crítica]")
tid = r.get_json()["id"]
assert c.patch(f"/api/tickets/{tid}", headers=H, json={"status": "Resolvido"}).status_code == 200      # sem telefone: não quebra
assert c.post("/api/tickets", headers=H, json={"unit": "", "cat": "Rede", "prio": "Alta", "desc": "x"}).status_code == 400
print("OK – todos os testes passaram")
