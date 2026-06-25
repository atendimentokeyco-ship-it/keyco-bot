import os
import json
import urllib.request
import urllib.parse
import urllib.error
import base64
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# ── CONFIG ──
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
DATA_FILE = "keyco_data.json"

# ── DATA ──
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"pagar": [], "receber": [], "orcamentos": [], "counter": {"pag": 0, "cob": 0, "orc": 0}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def next_id(data, prefix):
    key = {"PAG": "pag", "COB": "cob", "ORC": "orc"}[prefix]
    data["counter"][key] += 1
    return f"{prefix}-{data['counter'][key]:03d}"

def fmt_brl(valor):
    try:
        v = float(valor)
        return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except:
        return f"R$ {valor}"

def days_until(venc_str):
    try:
        d, m, y = venc_str.split("/")
        venc = datetime(int(y), int(m), int(d))
        hoje = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return (venc - hoje).days
    except:
        return 999

def status_venc(days):
    if days < 0: return "🔴 Vencido"
    elif days == 0: return "🔴 Vence HOJE"
    elif days <= 3: return "🟠 Em breve"
    elif days <= 7: return "🟡 Esta semana"
    else: return "🟢 Em dia"

# ── HTTP HELPERS ──
def http_post(url, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

def http_get(url):
    with urllib.request.urlopen(url, timeout=15) as resp:
        return resp.read()

# ── TELEGRAM ──
def send_message(chat_id, text):
    try:
        http_post(f"{TELEGRAM_API}/sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        })
    except Exception as e:
        print(f"Erro send_message: {e}")

# ── CLAUDE ──
def ask_claude(messages, system=""):
    if not ANTHROPIC_KEY:
        return None
    try:
        resp = http_post("https://api.anthropic.com/v1/messages", {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 400,
            "system": system,
            "messages": messages
        })
        # Need anthropic-version header
        return None
    except:
        return None

def call_anthropic(payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01"
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
        return data.get("content", [{}])[0].get("text", "")

def interpret_message(text):
    if not ANTHROPIC_KEY:
        return {"intencao": "menu", "dados": {}}
    system = """Interprete a mensagem do usuário e retorne JSON com a intenção.
Intenções: menu, listar_pagar, listar_receber, listar_orcamentos, pagar_hoje, pagar_semana, nova_conta_pagar, nova_conta_receber, novo_orcamento, marcar_pago, marcar_recebido, aprovar_orcamento, recusar_orcamento, resumo
Retorne SOMENTE JSON: {"intencao": "...", "dados": {"nome": "...", "id": "..."}}
Exemplos:
"paguei a Udinese" → {"intencao": "marcar_pago", "dados": {"nome": "Udinese"}}
"o que vence hoje" → {"intencao": "pagar_hoje", "dados": {}}
"quero lançar conta" → {"intencao": "nova_conta_pagar", "dados": {}}
"cliente João pagou" → {"intencao": "marcar_recebido", "dados": {"nome": "João"}}"""
    try:
        result = call_anthropic({"model": "claude-sonnet-4-20250514", "max_tokens": 200, "system": system, "messages": [{"role": "user", "content": text}]})
        result = result.replace("```json", "").replace("```", "").strip()
        return json.loads(result)
    except:
        return {"intencao": "menu", "dados": {}}

def read_boleto_image(file_id):
    if not ANTHROPIC_KEY:
        return None
    try:
        # Get file path from Telegram
        file_info = http_get(f"{TELEGRAM_API}/getFile?file_id={file_id}")
        file_data = json.loads(file_info)
        file_path = file_data.get("result", {}).get("file_path", "")
        if not file_path:
            return None
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        file_bytes = http_get(file_url)
        b64 = base64.b64encode(file_bytes).decode()
        media_type = "image/jpeg"
        if file_path.endswith(".png"): media_type = "image/png"
        elif file_path.endswith(".pdf"): media_type = "application/pdf"
        if media_type == "application/pdf":
            content = [
                {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": "Boleto/fatura. Extraia fornecedor, valor, vencimento. Responda SOMENTE JSON: {\"fornecedor\":\"...\",\"valor\":0.00,\"vencimento\":\"dd/mm/aaaa\"}"}
            ]
        else:
            content = [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": "Boleto/fatura. Extraia fornecedor, valor, vencimento. Responda SOMENTE JSON: {\"fornecedor\":\"...\",\"valor\":0.00,\"vencimento\":\"dd/mm/aaaa\"}"}
            ]
        result = call_anthropic({"model": "claude-sonnet-4-20250514", "max_tokens": 200, "messages": [{"role": "user", "content": content}]})
        result = result.replace("```json", "").replace("```", "").strip()
        return json.loads(result)
    except Exception as e:
        print(f"Erro read_boleto: {e}")
        return None

# ── STATE ──
user_states = {}

def handle_message(chat_id, text=None, photo=None, document=None):
    data = load_data()
    state = user_states.get(chat_id, {})

    # FOTO/DOCUMENTO
    if photo or document:
        file_id = photo[-1]["file_id"] if photo else document["file_id"]
        send_message(chat_id, "⏳ Lendo o documento com IA...")
        resultado = read_boleto_image(file_id)
        if resultado:
            user_states[chat_id] = {"step": "confirmar_boleto", "temp": resultado, "tipo": state.get("tipo_lancamento", "pagar")}
            tipo = "pagar" if state.get("tipo_lancamento", "pagar") == "pagar" else "receber"
            send_message(chat_id, f"✅ *Dados extraídos:*\n\n🏢 {resultado.get('fornecedor','—')}\n💰 {fmt_brl(resultado.get('valor',0))}\n📅 {resultado.get('vencimento','—')}\n\nConfirma como conta a {tipo}? Digite *sim* ou *não*")
        else:
            send_message(chat_id, "❌ Não consegui ler. Digite manualmente:\n*Fornecedor, Valor, dd/mm/aaaa*\nEx: `Udinese, 3200, 25/06/2025`")
        return

    if not text:
        return
    tl = text.lower().strip()

    # ESTADOS
    if state.get("step") == "confirmar_boleto":
        if "sim" in tl:
            temp = state["temp"]
            tipo = state.get("tipo", "pagar")
            if tipo == "pagar":
                item = {"id": next_id(data, "PAG"), "fornecedor": temp.get("fornecedor","Sem nome"), "valor": float(temp.get("valor",0)), "vencimento": temp.get("vencimento",""), "status": "aberto", "tipo": "Boleto"}
                data["pagar"].append(item)
                save_data(data)
                user_states[chat_id] = {}
                send_message(chat_id, f"✅ *{item['id']}* lançado!\n🏢 {item['fornecedor']}\n💰 {fmt_brl(item['valor'])}\n📅 {item['vencimento']}")
            else:
                item = {"id": next_id(data, "COB"), "cliente": temp.get("fornecedor","Sem nome"), "valor": float(temp.get("valor",0)), "vencimento": temp.get("vencimento",""), "status": "aberto", "nf": ""}
                data["receber"].append(item)
                save_data(data)
                user_states[chat_id] = {}
                send_message(chat_id, f"✅ *{item['id']}* lançado!\n👤 {item['cliente']}\n💰 {fmt_brl(item['valor'])}\n📅 {item['vencimento']}")
        else:
            user_states[chat_id] = {}
            send_message(chat_id, "❌ Cancelado.")
        return

    if state.get("step") == "nova_pagar_manual":
        parts = [p.strip() for p in text.split(",")]
        if len(parts) >= 3:
            try:
                val = float(parts[1].replace("R$","").replace(".","").replace(",",".").strip())
                item = {"id": next_id(data,"PAG"), "fornecedor": parts[0], "valor": val, "vencimento": parts[2].strip(), "status": "aberto", "tipo": "Manual"}
                data["pagar"].append(item)
                save_data(data)
                user_states[chat_id] = {}
                send_message(chat_id, f"✅ *{item['id']}* lançado!\n🏢 {item['fornecedor']}\n💰 {fmt_brl(item['valor'])}\n📅 {item['vencimento']}")
            except:
                send_message(chat_id, "❌ Valor inválido. Use: *Fornecedor, 3200, 25/06/2025*")
        else:
            send_message(chat_id, "❌ Formato: *Fornecedor, Valor, dd/mm/aaaa*")
        return

    if state.get("step") == "nova_receber_manual":
        parts = [p.strip() for p in text.split(",")]
        if len(parts) >= 3:
            try:
                val = float(parts[1].replace("R$","").replace(".","").replace(",",".").strip())
                item = {"id": next_id(data,"COB"), "cliente": parts[0], "valor": val, "vencimento": parts[2].strip(), "status": "aberto", "nf": ""}
                data["receber"].append(item)
                save_data(data)
                user_states[chat_id] = {}
                send_message(chat_id, f"✅ *{item['id']}* lançado!\n👤 {item['cliente']}\n💰 {fmt_brl(item['valor'])}\n📅 {item['vencimento']}")
            except:
                send_message(chat_id, "❌ Valor inválido.")
        else:
            send_message(chat_id, "❌ Formato: *Cliente, Valor, dd/mm/aaaa*")
        return

    if state.get("step") == "novo_orc_manual":
        parts = [p.strip() for p in text.split(",")]
        if len(parts) >= 3:
            try:
                val = float(parts[2].replace("R$","").replace(".","").replace(",",".").strip())
                item = {"id": next_id(data,"ORC"), "cliente": parts[0], "descricao": parts[1], "valor": val, "status": "pendente", "data": datetime.now().strftime("%d/%m/%Y")}
                data["orcamentos"].append(item)
                save_data(data)
                user_states[chat_id] = {}
                send_message(chat_id, f"✅ *{item['id']}* criado!\n👤 {item['cliente']}\n📋 {item['descricao']}\n💰 {fmt_brl(item['valor'])}")
            except:
                send_message(chat_id, "❌ Valor inválido.")
        else:
            send_message(chat_id, "❌ Formato: *Cliente, Descrição, Valor*")
        return

    # COMANDOS DIRETOS
    if tl in ["/start", "/menu", "menu", "ajuda", "/ajuda"]:
        send_message(chat_id, """🏢 *Keycomerce — Financeiro*

💸 *A pagar*
• `pagar` — ver todas
• `hoje` — vence hoje
• `semana` — próximos 7 dias
• `paguei [nome]` — marcar pago

📥 *A receber*
• `receber` — ver todas
• `recebido [nome]` — marcar recebido

📋 *Orçamentos*
• `orçamentos` — ver todos
• `aprovar ORC-001` — aprovar
• `recusar ORC-001` — recusar

➕ *Lançar*
• `nova conta` — conta a pagar
• `nova cobrança` — conta a receber
• `novo orçamento` — criar orçamento
• 📷 Foto ou PDF do boleto

📊 `resumo` — visão geral""")
        return

    if tl == "resumo":
        pag = [p for p in data["pagar"] if p["status"]=="aberto"]
        rec = [r for r in data["receber"] if r["status"]=="aberto"]
        orc = [o for o in data["orcamentos"] if o["status"]=="pendente"]
        venc = [p for p in pag if days_until(p["vencimento"])<0]
        hj = [p for p in pag if days_until(p["vencimento"])==0]
        send_message(chat_id, f"""📊 *Resumo Financeiro*

💸 *A pagar:* {fmt_brl(sum(p['valor'] for p in pag))}
  🔴 Vencidas: {len(venc)} | Hoje: {len(hj)}

💰 *A receber:* {fmt_brl(sum(r['valor'] for r in rec))}
  📋 {len(rec)} cobrança(s)

📋 *Orçamentos pendentes:* {len(orc)}""")
        return

    if tl in ["pagar", "contas a pagar", "a pagar"]:
        abertos = sorted([p for p in data["pagar"] if p["status"]=="aberto"], key=lambda x: days_until(x["vencimento"]))
        if not abertos:
            send_message(chat_id, "✅ Nenhuma conta a pagar em aberto.")
            return
        linhas = ["💸 *Contas a pagar*\n"]
        for p in abertos:
            d = days_until(p["vencimento"])
            linhas.append(f"{status_venc(d)} *{p['id']}*\n🏢 {p['fornecedor']}\n💰 {fmt_brl(p['valor'])} · {p['vencimento']}\n")
        linhas.append(f"*Total: {fmt_brl(sum(p['valor'] for p in abertos))}*")
        send_message(chat_id, "\n".join(linhas))
        return

    if tl in ["hoje", "vence hoje"]:
        items = [p for p in data["pagar"] if p["status"]=="aberto" and days_until(p["vencimento"])<=0]
        if not items:
            send_message(chat_id, "✅ Nada vencendo hoje.")
            return
        linhas = ["🔴 *Vence hoje / Vencidas*\n"]
        for p in items:
            linhas.append(f"*{p['id']}* — {p['fornecedor']}\n💰 {fmt_brl(p['valor'])} · {p['vencimento']}\n")
        send_message(chat_id, "\n".join(linhas))
        return

    if tl in ["semana", "vence semana", "essa semana"]:
        items = [p for p in data["pagar"] if p["status"]=="aberto" and 0<=days_until(p["vencimento"])<=7]
        if not items:
            send_message(chat_id, "✅ Nada vencendo essa semana.")
            return
        linhas = ["🟡 *Vence esta semana*\n"]
        for p in items:
            d = days_until(p["vencimento"])
            linhas.append(f"{status_venc(d)} *{p['id']}* — {p['fornecedor']}\n💰 {fmt_brl(p['valor'])} · {p['vencimento']}\n")
        send_message(chat_id, "\n".join(linhas))
        return

    if tl in ["receber", "a receber", "contas a receber"]:
        abertos = sorted([r for r in data["receber"] if r["status"]=="aberto"], key=lambda x: days_until(x["vencimento"]))
        if not abertos:
            send_message(chat_id, "✅ Nenhuma cobrança em aberto.")
            return
        linhas = ["📥 *Contas a receber*\n"]
        for r in abertos:
            d = days_until(r["vencimento"])
            linhas.append(f"{status_venc(d)} *{r['id']}*\n👤 {r['cliente']}\n💰 {fmt_brl(r['valor'])} · {r['vencimento']}\n")
        linhas.append(f"*Total: {fmt_brl(sum(r['valor'] for r in abertos))}*")
        send_message(chat_id, "\n".join(linhas))
        return

    if tl in ["orçamentos", "orcamentos"]:
        items = data["orcamentos"][-10:]
        if not items:
            send_message(chat_id, "📋 Nenhum orçamento cadastrado.")
            return
        emoji = {"pendente":"🟡","aprovado":"🟢","recusado":"🔴","enviado":"🔵"}
        linhas = ["📋 *Orçamentos*\n"]
        for o in items:
            linhas.append(f"{emoji.get(o['status'],'⚪')} *{o['id']}* — {o['cliente']}\n📋 {o['descricao']} · {fmt_brl(o['valor'])}\n")
        send_message(chat_id, "\n".join(linhas))
        return

    if tl in ["nova conta", "nova conta a pagar", "lançar conta"]:
        user_states[chat_id] = {"step": "nova_pagar_manual", "tipo_lancamento": "pagar"}
        send_message(chat_id, "💸 *Nova conta a pagar*\n\nMande 📷 foto/PDF do boleto\nOu digite: *Fornecedor, Valor, dd/mm/aaaa*\nEx: `Udinese perfis, 3200, 25/06/2025`")
        return

    if tl in ["nova cobrança", "nova cobranca", "nova conta a receber"]:
        user_states[chat_id] = {"step": "nova_receber_manual", "tipo_lancamento": "receber"}
        send_message(chat_id, "📥 *Nova cobrança*\nFormato: *Cliente, Valor, dd/mm/aaaa*\nEx: `Esquadrias João, 1640, 25/06/2025`")
        return

    if tl in ["novo orçamento", "novo orcamento"]:
        user_states[chat_id] = {"step": "novo_orc_manual"}
        send_message(chat_id, "📋 *Novo orçamento*\nFormato: *Cliente, Descrição, Valor*\nEx: `Esquadrias João, 10 telas Udinese, 890`")
        return

    # MARCAR PAGO
    if tl.startswith("paguei") or tl.startswith("pago"):
        nome = tl.replace("paguei","").replace("pago","").strip()
        encontrados = [p for p in data["pagar"] if p["status"]=="aberto" and nome in p["fornecedor"].lower()]
        if not encontrados:
            send_message(chat_id, f"❌ Nenhuma conta aberta com *{nome}*.\nDigite `pagar` para ver todas.")
        elif len(encontrados) == 1:
            encontrados[0]["status"] = "pago"
            encontrados[0]["data_pagamento"] = datetime.now().strftime("%d/%m/%Y")
            save_data(data)
            send_message(chat_id, f"✅ *{encontrados[0]['id']}* marcado como pago!\n🏢 {encontrados[0]['fornecedor']} — {fmt_brl(encontrados[0]['valor'])}")
        else:
            linhas = [f"Encontrei {len(encontrados)} contas:\n"]
            for p in encontrados:
                linhas.append(f"• *{p['id']}* — {p['fornecedor']} · {fmt_brl(p['valor'])} · {p['vencimento']}")
            linhas.append("\nQual? Digite o ID. Ex: *PAG-001*")
            send_message(chat_id, "\n".join(linhas))
        return

    # MARCAR RECEBIDO
    if tl.startswith("recebido") or tl.startswith("recebi"):
        nome = tl.replace("recebido","").replace("recebi","").strip()
        encontrados = [r for r in data["receber"] if r["status"]=="aberto" and nome in r["cliente"].lower()]
        if not encontrados:
            send_message(chat_id, f"❌ Nenhuma cobrança aberta com *{nome}*.\nDigite `receber` para ver todas.")
        elif len(encontrados) == 1:
            encontrados[0]["status"] = "pago"
            encontrados[0]["data_pagamento"] = datetime.now().strftime("%d/%m/%Y")
            save_data(data)
            send_message(chat_id, f"✅ *{encontrados[0]['id']}* marcado como recebido!\n👤 {encontrados[0]['cliente']} — {fmt_brl(encontrados[0]['valor'])}")
        else:
            linhas = [f"Encontrei {len(encontrados)} cobranças:\n"]
            for r in encontrados:
                linhas.append(f"• *{r['id']}* — {r['cliente']} · {fmt_brl(r['valor'])}")
            linhas.append("\nQual? Digite o ID.")
            send_message(chat_id, "\n".join(linhas))
        return

    # APROVAR/RECUSAR
    if tl.startswith("aprovar"):
        orc_id = text.upper().replace("APROVAR","").strip()
        orc = next((o for o in data["orcamentos"] if o["id"]==orc_id), None)
        if orc:
            orc["status"] = "aprovado"
            save_data(data)
            send_message(chat_id, f"✅ *{orc['id']}* aprovado!\n👤 {orc['cliente']} — {fmt_brl(orc['valor'])}")
        else:
            send_message(chat_id, f"❌ Orçamento *{orc_id}* não encontrado.")
        return

    if tl.startswith("recusar"):
        orc_id = text.upper().replace("RECUSAR","").strip()
        orc = next((o for o in data["orcamentos"] if o["id"]==orc_id), None)
        if orc:
            orc["status"] = "recusado"
            save_data(data)
            send_message(chat_id, f"❌ *{orc['id']}* recusado.")
        else:
            send_message(chat_id, f"❌ Orçamento *{orc_id}* não encontrado.")
        return

    # ID DIRETO
    if text.upper().startswith("PAG-"):
        item = next((p for p in data["pagar"] if p["id"]==text.upper()), None)
        if item and item["status"]=="aberto":
            item["status"] = "pago"
            item["data_pagamento"] = datetime.now().strftime("%d/%m/%Y")
            save_data(data)
            send_message(chat_id, f"✅ *{item['id']}* marcado como pago!")
        return

    if text.upper().startswith("COB-"):
        item = next((r for r in data["receber"] if r["id"]==text.upper()), None)
        if item and item["status"]=="aberto":
            item["status"] = "pago"
            item["data_pagamento"] = datetime.now().strftime("%d/%m/%Y")
            save_data(data)
            send_message(chat_id, f"✅ *{item['id']}* marcado como recebido!")
        return

    # IA FALLBACK
    intencao_data = interpret_message(text)
    intencao = intencao_data.get("intencao", "menu")
    if intencao != "menu":
        handle_message_by_intent(chat_id, intencao, intencao_data.get("dados",{}), data)
    else:
        send_message(chat_id, "🤔 Não entendi. Digite `menu` para ver as opções.")

def handle_message_by_intent(chat_id, intencao, dados, data):
    if intencao == "marcar_pago":
        nome = dados.get("nome","").lower()
        encontrados = [p for p in data["pagar"] if p["status"]=="aberto" and nome in p["fornecedor"].lower()]
        if encontrados:
            encontrados[0]["status"] = "pago"
            encontrados[0]["data_pagamento"] = datetime.now().strftime("%d/%m/%Y")
            save_data(data)
            send_message(chat_id, f"✅ *{encontrados[0]['id']}* marcado como pago!\n🏢 {encontrados[0]['fornecedor']}")
        else:
            send_message(chat_id, f"❌ Nenhuma conta aberta com *{dados.get('nome','')}*.")
    elif intencao == "marcar_recebido":
        nome = dados.get("nome","").lower()
        encontrados = [r for r in data["receber"] if r["status"]=="aberto" and nome in r["cliente"].lower()]
        if encontrados:
            encontrados[0]["status"] = "pago"
            encontrados[0]["data_pagamento"] = datetime.now().strftime("%d/%m/%Y")
            save_data(data)
            send_message(chat_id, f"✅ *{encontrados[0]['id']}* marcado como recebido!")
        else:
            send_message(chat_id, f"❌ Nenhuma cobrança aberta com *{dados.get('nome','')}*.")
    elif intencao == "nova_conta_pagar":
        user_states[chat_id] = {"step": "nova_pagar_manual"}
        send_message(chat_id, "💸 *Nova conta a pagar*\nFormato: *Fornecedor, Valor, dd/mm/aaaa*")
    elif intencao == "nova_conta_receber":
        user_states[chat_id] = {"step": "nova_receber_manual"}
        send_message(chat_id, "📥 *Nova cobrança*\nFormato: *Cliente, Valor, dd/mm/aaaa*")
    elif intencao == "resumo":
        handle_message(chat_id, text="resumo")
    else:
        send_message(chat_id, "🤔 Não entendi. Digite `menu` para ver as opções.")

# ── WEBHOOK SERVER ──
class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/webhook":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            try:
                update = json.loads(body)
                message = update.get("message", {})
                chat_id = message.get("chat", {}).get("id")
                if chat_id:
                    text = message.get("text")
                    photo = message.get("photo")
                    document = message.get("document")
                    threading.Thread(target=handle_message, args=(chat_id, text, photo, document)).start()
            except Exception as e:
                print(f"Erro webhook: {e}")
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Keycomerce Bot OK")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Keycomerce Bot rodando OK")

    def log_message(self, format, *args):
        pass

def setup_webhook():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if domain:
        url = f"https://{domain}/webhook"
        try:
            http_post(f"{TELEGRAM_API}/setWebhook", {"url": url})
            print(f"Webhook: {url}")
        except Exception as e:
            print(f"Erro webhook setup: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    setup_webhook()
    print(f"Bot rodando na porta {port}")
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    server.serve_forever()
