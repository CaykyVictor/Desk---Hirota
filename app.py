"""Hirota Desk – backend: WhatsApp (Cloud API) + e-mail + painel da TI.
Flask + SQLite. Configuração por variáveis de ambiente (veja .env.example)."""
import csv, hashlib, hmac, json, logging, os, smtplib, sqlite3, threading, time
from email.message import EmailMessage
from functools import wraps

import requests
from flask import Flask, Response, abort, jsonify, request, send_from_directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("desk")
app = Flask(__name__)
E = lambda k, d="": os.environ.get(k, d)
now = lambda: int(time.time() * 1000)

CATS = ["Rede", "Software", "Hardware", "Balança", "Telefonia fixa",
        "Celular/coletor", "Cabos", "Fontes/energia", "Outros"]
URG = {"1": ("Crítica", "Parou a operação (PDV parado ou loja sem internet)"),
       "2": ("Alta", "Atrapalha muito, mas dá para trabalhar"),
       "3": ("Média", "Não é urgente"),
       "4": ("Baixa", "Solicitação ou melhoria")}
SLA = {"Crítica": 1, "Alta": 4, "Média": 24, "Baixa": 72}   # horas corridas
STATUS = ["Aberto", "Triagem", "Em atendimento", "Aguardando peça/fornecedor", "Resolvido", "Fechado"]
CAT_MENU = "\n".join(f"{i} - {c}" for i, c in enumerate(CATS, 1))
URG_MENU = "Qual a urgência?\n" + "\n".join(f"{k} - {v[1]}" for k, v in URG.items())
FLOW = ("cat", "urg", "unit", "desc")
SIM, MAILS = [], []          # saídas simuladas (sem credenciais) – usadas nos testes

# ---------------------------------------------------------------- banco
SCHEMA = """
CREATE TABLE IF NOT EXISTS units(code TEXT PRIMARY KEY, name TEXT, format TEXT);
CREATE TABLE IF NOT EXISTS phones(phone TEXT PRIMARY KEY, unit_code TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, unit_code TEXT, unit_name TEXT,
  format TEXT, phone TEXT, requester TEXT, cat TEXT, prio TEXT, description TEXT, status TEXT,
  tech TEXT DEFAULT '', solution TEXT DEFAULT '', part TEXT DEFAULT '', created INTEGER, due INTEGER, resolved INTEGER);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, ts INTEGER,
  direction TEXT, author TEXT, text TEXT, media TEXT, delivery TEXT);
CREATE TABLE IF NOT EXISTS sessions(phone TEXT PRIMARY KEY, state TEXT, data TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS seen(wa_id TEXT PRIMARY KEY);
"""

def conn():
    c = sqlite3.connect(E("DB_PATH", "desk.db"), timeout=10)
    c.row_factory = sqlite3.Row
    return c

def q(sql, a=(), one=False):
    c = conn()
    try:
        rows = c.execute(sql, a).fetchall()
    finally:
        c.close()
    return (rows[0] if rows else None) if one else rows

def x(sql, a=()):
    c = conn()
    try:
        cur = c.execute(sql, a); c.commit(); return cur.lastrowid
    finally:
        c.close()

def init_db():
    c = conn(); c.executescript(SCHEMA); c.commit(); c.close()
    os.makedirs(E("UPLOADS", "uploads"), exist_ok=True)

def lab(tid): return f"CH-{int(tid):04d}"

def get_session(phone):
    r = q("SELECT * FROM sessions WHERE phone=?", (phone,), True)
    return {"state": r["state"], "data": json.loads(r["data"]), "ts": r["ts"]} if r else {"state": "idle", "data": {}, "ts": 0}

def set_session(phone, state, data=None):
    x("INSERT INTO sessions(phone,state,data,ts) VALUES(?,?,?,?) ON CONFLICT(phone) DO UPDATE SET "
      "state=excluded.state,data=excluded.data,ts=excluded.ts", (phone, state, json.dumps(data or {}), now()))

def find_phone(phone):
    """Casa o telefone exato ou pelos 8 últimos dígitos (cobre a variação do 9º dígito no Brasil)."""
    return (q("SELECT * FROM phones WHERE phone=?", (phone,), True)
            or q("SELECT * FROM phones WHERE substr(phone,-8)=?", (phone[-8:],), True))

# ---------------------------------------------------------------- saídas
def send_wa(to, text):
    if not E("WA_TOKEN"):
        SIM.append((to, text)); log.info("[WhatsApp simulado] -> %s: %s", to, text[:80]); return "simulado"
    try:
        r = requests.post(f"https://graph.facebook.com/{E('GRAPH_VERSION', 'v21.0')}/{E('WA_PHONE_ID')}/messages",
                          headers={"Authorization": "Bearer " + E("WA_TOKEN")}, timeout=10,
                          json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}})
        if r.ok: return "enviado"
        if "131047" in r.text: return "erro: fora da janela de 24h (exige modelo aprovado)"
        return f"erro {r.status_code}: {r.text[:200]}"
    except requests.RequestException as e:
        return f"erro: {e}"

def send_mail(subject, body):
    """Envia e-mail à TI. Tenta até 3 vezes (0 s, 5 s, 30 s) antes de desistir."""
    to = [a.strip() for a in E("TI_EMAILS").split(",") if a.strip()]
    if not (E("SMTP_HOST") and to):
        MAILS.append((subject, body)); log.info("[e-mail simulado] %s", subject); return True
    m = EmailMessage(); m["Subject"] = subject; m["From"] = E("SMTP_FROM", E("SMTP_USER")); m["To"] = ", ".join(to)
    m.set_content(body)
    for i, wait in enumerate((0, 5, 30), 1):
        if wait: time.sleep(wait)
        try:
            port = int(E("SMTP_PORT", "587"))
            with (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)(E("SMTP_HOST"), port, timeout=15) as s:
                if port != 465: s.starttls()
                if E("SMTP_USER"): s.login(E("SMTP_USER"), E("SMTP_PASS"))
                s.send_message(m)
            return True
        except Exception:
            log.exception("falha ao enviar e-mail (tentativa %d/3)", i)
    return False

def bg(fn, *a):
    fn(*a) if app.config.get("SYNC") else threading.Thread(target=fn, args=a, daemon=True).start()

def store(tid, direction, author, text, media=None, delivery=None):
    return x("INSERT INTO messages(ticket_id,ts,direction,author,text,media,delivery) VALUES(?,?,?,?,?,?,?)",
             (tid, now(), direction, author, text, media, delivery))

def say(t, text, author="Robô"):
    """Envia ao funcionário pelo WhatsApp e registra na conversa do chamado."""
    d = send_wa(t["phone"], text) if t["phone"] else "sem WhatsApp"
    store(t["id"], "out", author, text, None, d); return d

def save_media(mid):
    if not E("WA_TOKEN"): return f"sim-{mid}"
    try:
        h = {"Authorization": "Bearer " + E("WA_TOKEN")}
        info = requests.get(f"https://graph.facebook.com/{E('GRAPH_VERSION', 'v21.0')}/{mid}", headers=h, timeout=10).json()
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "application/pdf": ".pdf", "video/mp4": ".mp4"}.get(info.get("mime_type"), "")
        name = mid + ext
        with open(os.path.join(E("UPLOADS", "uploads"), name), "wb") as f:
            f.write(requests.get(info["url"], headers=h, timeout=30).content)
        return name
    except Exception:
        log.exception("falha ao baixar mídia"); return None

def link(tid): return f"{E('PANEL_URL', '(defina PANEL_URL)')}#{tid}"

# ---------------------------------------------------------------- robô
def start(phone):
    set_session(phone, "cat")
    return send_wa(phone, "Olá! Sou o assistente de TI da Hirota. 🛠️\nQual o tipo de problema?\n" + CAT_MENU)

def unit_label(code):
    u = q("SELECT * FROM units WHERE code=?", (code,), True)
    return f"{code} – {u['name']}" if u else code

def open_ticket(phone, who, code, cat, prio, desc, media=(), source="WhatsApp"):
    """Cria o chamado (qualquer origem) e SEMPRE dispara o e-mail para a TI."""
    u = q("SELECT * FROM units WHERE code=?", (code,), True); t0 = now()
    tid = x("INSERT INTO tickets(unit_code,unit_name,format,phone,requester,cat,prio,description,status,created,due) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (code, u["name"] if u else "", u["format"] if u else "", phone, who, cat, prio, desc, "Aberto",
             t0, t0 + SLA[prio] * 3600000))
    store(tid, "in", who, desc)
    for f in media: store(tid, "in", who, "[foto]", f)
    bg(send_mail, f"[{prio}] Novo chamado {lab(tid)} – {unit_label(code)} – {cat}",
       f"Origem: {source}\nUnidade: {unit_label(code)} ({u['format'] if u else '-'})\nSolicitante: {who} ({phone or 'sem telefone'})\n"
       f"Categoria: {cat}\nPrioridade: {prio} (prazo {SLA[prio]} h)\n\n{desc}\n\nAtender: {link(tid)}")
    return q("SELECT * FROM tickets WHERE id=?", (tid,), True)

def ack(t):
    return (f"✅ Chamado *{lab(t['id'])}* aberto ({t['cat']}, prioridade {t['prio']}).\nPrazo de atendimento: {SLA[t['prio']]} h.\n"
            "Pode enviar fotos ou mais detalhes por aqui; a TI responde nesta conversa.\nPara abrir outro chamado, envie *menu*.")

def create(phone, name, d, desc):
    p = find_phone(phone); who = (p["name"] if p and p["name"] else name) or phone
    t = open_ticket(phone, who, d["unit"], d["cat"], d["prio"], desc, d.get("media", []))
    set_session(phone, "idle"); say(t, ack(t))

def append(t, name, text, kind, media):
    store(t["id"], "in", t["requester"] or name, text or f"[{kind}]", media)
    bg(send_mail, f"Nova mensagem no chamado {lab(t['id'])} – {unit_label(t['unit_code'])}",
       f"{t['requester']}: {text or '[' + kind + ']'}\n\nResponder: {link(t['id'])}")
    recent = q("SELECT 1 FROM messages WHERE ticket_id=? AND direction='out' AND ts>?", (t["id"], now() - 300000), True)
    if not recent:
        say(t, f"Mensagem adicionada ao chamado {lab(t['id'])}. A TI responde por aqui. Para abrir outro chamado, envie *menu*.")

def confirm(phone, d, low):
    t = q("SELECT * FROM tickets WHERE id=?", (d.get("ticket"),), True)
    if not t: set_session(phone, "idle"); return start(phone)
    if low == "1":
        x("UPDATE tickets SET status='Fechado' WHERE id=?", (t["id"],)); set_session(phone, "idle")
        say(t, f"Obrigado! Chamado {lab(t['id'])} encerrado. 👍")
        bg(send_mail, f"Chamado {lab(t['id'])} fechado pela loja", f"{t['requester']} confirmou a solução.")
    elif low == "2":
        x("UPDATE tickets SET status='Aberto', resolved=NULL, due=? WHERE id=?", (now() + SLA[t["prio"]] * 3600000, t["id"]))
        set_session(phone, "idle")
        say(t, f"Entendido, reabri o chamado {lab(t['id'])}. A TI vai dar continuidade.")
        bg(send_mail, f"[REABERTO] Chamado {lab(t['id'])} – {unit_label(t['unit_code'])}",
           f"A loja informou que o problema continua.\n\nAtender: {link(t['id'])}")
    else:
        say(t, f"O chamado {lab(t['id'])} foi marcado como resolvido.\nResponda *1* se o problema foi resolvido ou *2* se continua.")

def handle(phone, name, m):
    text = (m.get("text") or "").strip(); low = text.lower()
    s = get_session(phone); st, d = s["state"], s["data"]
    if st in FLOW and now() - s["ts"] > 30 * 60000: st, d = "idle", {}      # fluxo abandonado
    if low in ("menu", "novo", "0"): return start(phone)
    if st == "confirm": return confirm(phone, d, low)
    media = save_media(m["media_id"]) if m.get("media_id") else None
    if st == "cat":
        if low.isdigit() and 1 <= int(low) <= len(CATS):
            d["cat"] = CATS[int(low) - 1]; set_session(phone, "urg", d); return send_wa(phone, URG_MENU)
        return send_wa(phone, f"Escolha um número de 1 a {len(CATS)}.\n{CAT_MENU}")
    if st == "urg":
        if low not in URG: return send_wa(phone, "Responda 1, 2, 3 ou 4.\n" + URG_MENU)
        d["prio"] = URG[low][0]; p = find_phone(phone)
        if p and p["unit_code"]:
            d["unit"] = p["unit_code"]; set_session(phone, "desc", d)
            return send_wa(phone, f"Loja: {unit_label(p['unit_code'])}.\nAgora descreva o problema (pode enviar foto com legenda).")
        set_session(phone, "unit", d); return send_wa(phone, "Qual o código da sua loja? (ex.: U001)")
    if st == "unit":
        code = text.upper()
        if q("SELECT 1 FROM units LIMIT 1") and not q("SELECT 1 FROM units WHERE code=?", (code,), True):
            return send_wa(phone, "Não encontrei esse código. Confira e envie de novo (ex.: U001).")
        d["unit"] = code; set_session(phone, "desc", d)
        return send_wa(phone, f"Loja: {unit_label(code)}.\nAgora descreva o problema (pode enviar foto com legenda).")
    if st == "desc":
        if media: d.setdefault("media", []).append(media)
        if not text:
            set_session(phone, "desc", d); return send_wa(phone, "Recebi o arquivo. Agora descreva o problema em texto, por favor.")
        return create(phone, name, d, text)
    t = q("SELECT * FROM tickets WHERE phone=? ORDER BY id DESC LIMIT 1", (phone,), True)   # idle
    if t and t["status"] not in ("Resolvido", "Fechado"): return append(t, name, text, m.get("kind", ""), media)
    if t and t["status"] == "Resolvido":
        set_session(phone, "confirm", {"ticket": t["id"]}); return confirm(phone, {"ticket": t["id"]}, "")
    return start(phone)

# ---------------------------------------------------------------- webhook Meta
@app.get("/webhook")
def verify():
    if request.args.get("hub.mode") == "subscribe" and E("VERIFY_TOKEN") and request.args.get("hub.verify_token") == E("VERIFY_TOKEN"):
        return request.args.get("hub.challenge", ""), 200
    abort(403)

@app.post("/webhook")
def receive():
    if E("APP_SECRET"):
        sig = "sha256=" + hmac.new(E("APP_SECRET").encode(), request.get_data(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, request.headers.get("X-Hub-Signature-256", "")): abort(403)
    for en in (request.get_json(silent=True) or {}).get("entry", []):
        for ch in en.get("changes", []):
            v = ch.get("value", {}); names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in v.get("contacts", [])}
            for msg in v.get("messages", []):
                try: x("INSERT INTO seen(wa_id) VALUES(?)", (msg["id"],))
                except sqlite3.IntegrityError: continue                      # reentrega da Meta
                kind = msg.get("type", "")
                body = msg.get("text", {}).get("body", "") if kind == "text" else ""
                media_id = None
                if kind in ("image", "document", "video", "audio"):
                    o = msg.get(kind, {}); media_id, body = o.get("id"), o.get("caption", "")
                elif kind == "button": body = msg.get("button", {}).get("text", "")
                elif kind == "interactive": body = str(msg["interactive"].get("button_reply", msg["interactive"].get("list_reply", {})).get("title", ""))
                elif kind != "text": body = f"[{kind} não suportado]"
                ph = msg["from"]
                bg(handle, ph, names.get(ph, ""), {"text": body, "media_id": media_id, "kind": kind})
    return "ok", 200

# ---------------------------------------------------------------- painel / API da TI
def eq(a, b): return hmac.compare_digest((a or "").encode(), (b or "").encode())

def auth(f):
    @wraps(f)
    def w(*a, **k):
        if not E("PANEL_PASS"): abort(503, "Defina PANEL_PASS")
        au = request.authorization
        if not (au and eq(au.username, E("PANEL_USER", "admin")) and eq(au.password, E("PANEL_PASS"))):
            return Response("Login necessário", 401, {"WWW-Authenticate": 'Basic realm="Hirota Desk"'})
        return f(*a, **k)
    return w

def tj(r):
    d = dict(r); d["label"] = lab(d["id"]); return d

@app.get("/")
@app.get("/painel")
@auth
def painel(): return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "panel.html")

@app.get("/media/<path:name>")
@auth
def media(name): return send_from_directory(E("UPLOADS", "uploads"), name)

@app.get("/api/tickets")
@auth
def tickets():
    st = request.args.get("status")
    rows = q("SELECT t.*, (SELECT direction FROM messages m WHERE m.ticket_id=t.id ORDER BY m.id DESC LIMIT 1) AS last_dir "
             "FROM tickets t" + (" WHERE status=?" if st else "") + " ORDER BY t.id DESC LIMIT 300", (st,) if st else ())
    return jsonify([tj(r) for r in rows])

@app.post("/api/tickets")
@auth
def create_api():
    b = request.get_json(silent=True) or {}
    unit, cat, prio, desc = (b.get("unit") or "").strip().upper(), b.get("cat"), b.get("prio"), (b.get("desc") or "").strip()
    if not unit or cat not in CATS or prio not in SLA or not desc: abort(400)
    ph = "".join(c for c in str(b.get("phone", "")) if c.isdigit()); user = request.authorization.username
    t = open_ticket(ph, (b.get("requester") or user).strip(), unit, cat, prio, desc, (), f"Painel ({user})")
    if ph: say(t, ack(t))
    return jsonify(tj(t)), 201

@app.get("/api/tickets/<int:tid>")
@auth
def ticket(tid):
    t = q("SELECT * FROM tickets WHERE id=?", (tid,), True) or abort(404)
    return jsonify({"ticket": tj(t), "messages": [dict(r) for r in q("SELECT * FROM messages WHERE ticket_id=? ORDER BY id", (tid,))]})

@app.post("/api/tickets/<int:tid>/messages")
@auth
def reply(tid):
    t = q("SELECT * FROM tickets WHERE id=?", (tid,), True) or abort(404)
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text: abort(400)
    return jsonify({"delivery": say(t, text, author=request.authorization.username)})

@app.patch("/api/tickets/<int:tid>")
@auth
def update(tid):
    t = q("SELECT * FROM tickets WHERE id=?", (tid,), True) or abort(404)
    b = request.get_json(silent=True) or {}
    new = {k: str(b[k]).strip() for k in ("status", "tech", "solution", "part") if k in b}
    if new.get("status", t["status"]) not in STATUS: abort(400)
    st = new.get("status", t["status"])
    new["resolved"] = (t["resolved"] or now()) if st in ("Resolvido", "Fechado") else None
    x("UPDATE tickets SET " + ",".join(f"{k}=?" for k in new) + " WHERE id=?", (*new.values(), tid))
    if st != t["status"]:
        if st == "Resolvido":
            if t["phone"]: set_session(t["phone"], "confirm", {"ticket": tid})
            msg = (f"✅ O chamado {lab(tid)} foi marcado como resolvido.\n" + (f"Solução: {new.get('solution') or t['solution']}\n" if (new.get("solution") or t["solution"]) else "")
                   + "Responda *1* se o problema foi resolvido ou *2* se continua.")
        elif st == "Fechado": msg = f"Chamado {lab(tid)} encerrado."
        else: msg = f"Atualização do chamado {lab(tid)}: agora está *{st}*."
        say(t, msg)
    return jsonify(tj(q("SELECT * FROM tickets WHERE id=?", (tid,), True)))

# ---------------------------------------------------------------- comandos de cadastro
@app.cli.command("test-email")
def test_email():
    """flask --app app test-email  – envia um e-mail de teste para TI_EMAILS"""
    import click
    click.echo("enviado" if send_mail("Teste – Hirota Desk", "Se você recebeu este e-mail, o SMTP está configurado.") else "FALHOU (veja o log)")

@app.cli.command("import-units")
def import_units():
    """flask --app app import-units unidades.csv  (colunas: codigo,nome,formato)"""
    import click, sys
    path = sys.argv[-1]
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            x("INSERT INTO units(code,name,format) VALUES(?,?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name,format=excluded.format",
              (r["codigo"].strip().upper(), r["nome"].strip(), r["formato"].strip()))
    click.echo("unidades importadas")

@app.cli.command("import-phones")
def import_phones():
    """flask --app app import-phones telefones.csv  (colunas: telefone,codigo,nome) – telefone com DDI+DDD, só números"""
    import click, sys
    with open(sys.argv[-1], encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            x("INSERT INTO phones(phone,unit_code,name) VALUES(?,?,?) ON CONFLICT(phone) DO UPDATE SET unit_code=excluded.unit_code,name=excluded.name",
              ("".join(c for c in r["telefone"] if c.isdigit()), r["codigo"].strip().upper(), r["nome"].strip()))
    click.echo("telefones importados")

init_db()
if __name__ == "__main__":
    app.run(port=int(E("PORT", "8000")))
