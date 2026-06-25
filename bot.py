import os
import json
import asyncio
from datetime import datetime, timedelta
import aiohttp
from aiohttp import web

# ── CONFIG ──
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "SEU_TOKEN_AQUI")
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
        return f"R$ {float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
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
    if days < 0:
        return "🔴 Vencido"
    elif days == 0:
        return "🔴 Vence HOJE"
    elif days <= 3:
        return "🟠 Vence em breve"
    elif days <= 7:
        return "🟡 Esta semana"
    else:
        return "🟢 Em dia"

# ── TELEGRAM ──
async def send_message(chat_id, text, parse_mode="Markdown"):
    async with aiohttp.ClientSession() as session:
        await session.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode
        })

async def send_photo_request(chat_id):
    await send_message(chat_id, "📷 Me manda a foto ou PDF do boleto/fatura:")

# ── AI ──
async def ask_claude(messages, system=""):
    if not ANTHROPIC_KEY:
        return None
    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 500, "system": system, "messages": messages}
        )
        data = await resp.json()
        return data.get("content", [{}])[0].get("text", "")

async def read_boleto_image(file_id):
    """Download image from Telegram and send to Claude"""
    async with aiohttp.ClientSession() as session:
        # Get file path
        resp = await session.get(f"{TELEGRAM_API}/getFile?file_id={file_id}")
        file_data = await resp.json()
        file_path = file_data.get("result", {}).get("file_path", "")
        if not file_path:
            return None
        # Download file
        file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        resp2 = await session.get(file_url)
        file_bytes = await resp2.read()
        import base64
        b64 = base64.b64encode(file_bytes).decode()
        # Detect type
        media_type = "image/jpeg"
        if file_path.endswith(".png"):
            media_type = "image/png"
        elif file_path.endswith(".pdf"):
            media_type = "application/pdf"
        # Ask Claude
        if not ANTHROPIC_KEY:
            return None
        if media_type == "application/pdf":
            content = [
                {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": "Boleto ou fatura. Extraia: fornecedor/beneficiário, valor total, data de vencimento. Responda SOMENTE JSON: {\"fornecedor\":\"...\",\"valor\":0.00,\"vencimento\":\"dd/mm/aaaa\"}"}
            ]
        else:
            content = [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": "Boleto ou fatura. Extraia: fornecedor/beneficiário, valor total, data de vencimento. Responda SOMENTE JSON sem markdown: {\"fornecedor\":\"...\",\"valor\":0.00,\"vencimento\":\"dd/mm/aaaa\"}"}
            ]
        resp3 = await session.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 200, "messages": [{"role": "user", "content": content}]}
        )
        data = await resp3.json()
        text = data.get("content", [{}])[0].get("text", "")
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)

async def interpret_message(text):
    """Use Claude to understand what the user wants"""
    system = """Você é o assistente financeiro da Keyco. Interprete a mensagem e retorne JSON com a intenção do usuário.

Intenções possíveis:
- "menu" → ver menu principal
- "listar_pagar" → ver contas a pagar
- "listar_receber" → ver contas a receber  
- "listar_orcamentos" → ver orçamentos
- "pagar_hoje" → ver o que vence hoje
- "pagar_semana" → ver o que vence esta semana
- "nova_conta_pagar" → lançar nova conta a pagar
- "nova_conta_receber" → lançar nova conta a receber
- "novo_orcamento" → criar novo orçamento
- "marcar_pago" → marcar conta como paga (extrair nome/id se mencionado)
- "marcar_recebido" → marcar como recebido
- "aprovar_orcamento" → aprovar orçamento
- "recusar_orcamento" → recusar orçamento
- "resumo" → resumo geral financeiro
- "ajuda" → mostrar ajuda

Retorne SOMENTE JSON: {"intencao": "...", "dados": {...}}
Exemplos:
"paguei a Udinese" → {"intencao": "marcar_pago", "dados": {"nome": "Udinese"}}
"quero lançar uma conta" → {"intencao": "nova_conta_pagar", "dados": {}}
"o que vence hoje" → {"intencao": "pagar_hoje", "dados": {}}
"cliente X pagou" → {"intencao": "marcar_recebido", "dados": {"nome": "X"}}"""

    result = await ask_claude([{"role": "user", "content": text}], system)
    if not result:
        return {"intencao": "menu", "dados": {}}
    try:
        result = result.replace("```json", "").replace("```", "").strip()
        return json.loads(result)
    except:
        return {"intencao": "menu", "dados": {}}

# ── HANDLERS ──
user_states = {}  # chat_id → {step, temp_data}

async def handle_message(chat_id, text=None, photo=None, document=None):
    data = load_data()
    state = user_states.get(chat_id, {})

    # ── FOTO / DOCUMENTO ──
    if photo or document:
        file_id = photo[-1]["file_id"] if photo else document["file_id"]
        await send_message(chat_id, "⏳ Lendo o documento com IA...")
        try:
            resultado = await read_boleto_image(file_id)
            if resultado:
                user_states[chat_id] = {
                    "step": "confirmar_boleto",
                    "temp": resultado,
                    "tipo": state.get("tipo_lancamento", "pagar")
                }
                tipo = "pagar" if state.get("tipo_lancamento", "pagar") == "pagar" else "receber"
                msg = f"""✅ *Dados extraídos:*

🏢 Fornecedor: *{resultado.get('fornecedor', '—')}*
💰 Valor: *{fmt_brl(resultado.get('valor', 0))}*
📅 Vencimento: *{resultado.get('vencimento', '—')}*

Confirma o lançamento como conta a {tipo}?
Digite *sim* para confirmar ou *não* para cancelar."""
                await send_message(chat_id, msg)
            else:
                await send_message(chat_id, "❌ Não consegui ler o documento. Digite os dados manualmente:\n\nFormato: *Fornecedor, Valor, Vencimento*\nEx: Udinese, 3200, 25/06/2025")
        except Exception as e:
            await send_message(chat_id, "❌ Erro ao ler. Digite manualmente:\nFormato: *Fornecedor, Valor, dd/mm/aaaa*")
        return

    if not text:
        return

    text_lower = text.lower().strip()

    # ── ESTADOS DE CONVERSA ──
    if state.get("step") == "confirmar_boleto":
        if "sim" in text_lower:
            temp = state["temp"]
            tipo = state.get("tipo", "pagar")
            if tipo == "pagar":
                item = {
                    "id": next_id(data, "PAG"),
                    "fornecedor": temp.get("fornecedor", "Sem nome"),
                    "valor": float(temp.get("valor", 0)),
                    "vencimento": temp.get("vencimento", ""),
                    "status": "aberto",
                    "tipo": "Boleto"
                }
                data["pagar"].append(item)
                save_data(data)
                user_states[chat_id] = {}
                await send_message(chat_id, f"✅ *{item['id']}* lançado!\n\n🏢 {item['fornecedor']}\n💰 {fmt_brl(item['valor'])}\n📅 Venc. {item['vencimento']}")
            else:
                item = {
                    "id": next_id(data, "COB"),
                    "cliente": temp.get("fornecedor", "Sem nome"),
                    "valor": float(temp.get("valor", 0)),
                    "vencimento": temp.get("vencimento", ""),
                    "status": "aberto",
                    "tipo": "Boleto",
                    "nf": ""
                }
                data["receber"].append(item)
                save_data(data)
                user_states[chat_id] = {}
                await send_message(chat_id, f"✅ *{item['id']}* lançado!\n\n👤 {item['cliente']}\n💰 {fmt_brl(item['valor'])}\n📅 Venc. {item['vencimento']}")
        else:
            user_states[chat_id] = {}
            await send_message(chat_id, "❌ Cancelado. Digite /menu para voltar.")
        return

    if state.get("step") == "nova_conta_pagar_manual":
        parts = [p.strip() for p in text.split(",")]
        if len(parts) >= 3:
            item = {
                "id": next_id(data, "PAG"),
                "fornecedor": parts[0],
                "valor": float(parts[1].replace("R$", "").replace(".", "").replace(",", ".").strip()),
                "vencimento": parts[2].strip(),
                "status": "aberto",
                "tipo": "Manual"
            }
            data["pagar"].append(item)
            save_data(data)
            user_states[chat_id] = {}
            await send_message(chat_id, f"✅ *{item['id']}* lançado!\n\n🏢 {item['fornecedor']}\n💰 {fmt_brl(item['valor'])}\n📅 Venc. {item['vencimento']}")
        else:
            await send_message(chat_id, "❌ Formato inválido. Use:\n*Fornecedor, Valor, dd/mm/aaaa*\nEx: Udinese, 3200, 25/06/2025")
        return

    if state.get("step") == "nova_conta_receber_manual":
        parts = [p.strip() for p in text.split(",")]
        if len(parts) >= 3:
            item = {
                "id": next_id(data, "COB"),
                "cliente": parts[0],
                "valor": float(parts[1].replace("R$", "").replace(".", "").replace(",", ".").strip()),
                "vencimento": parts[2].strip(),
                "status": "aberto",
                "tipo": "Manual",
                "nf": ""
            }
            data["receber"].append(item)
            save_data(data)
            user_states[chat_id] = {}
            await send_message(chat_id, f"✅ *{item['id']}* lançado!\n\n👤 {item['cliente']}\n💰 {fmt_brl(item['valor'])}\n📅 Venc. {item['vencimento']}")
        else:
            await send_message(chat_id, "❌ Formato inválido. Use:\n*Cliente, Valor, dd/mm/aaaa*\nEx: Esquadrias João, 1640, 25/06/2025")
        return

    if state.get("step") == "novo_orcamento_manual":
        parts = [p.strip() for p in text.split(",")]
        if len(parts) >= 3:
            item = {
                "id": next_id(data, "ORC"),
                "cliente": parts[0],
                "descricao": parts[1],
                "valor": float(parts[2].replace("R$", "").replace(".", "").replace(",", ".").strip()),
                "status": "pendente",
                "data": datetime.now().strftime("%d/%m/%Y"),
                "obs": parts[3] if len(parts) > 3 else ""
            }
            data["orcamentos"].append(item)
            save_data(data)
            user_states[chat_id] = {}
            await send_message(chat_id, f"✅ *{item['id']}* criado!\n\n👤 {item['cliente']}\n📋 {item['descricao']}\n💰 {fmt_brl(item['valor'])}\n📅 {item['data']}")
        else:
            await send_message(chat_id, "❌ Formato inválido. Use:\n*Cliente, Descrição, Valor*\nEx: Esquadrias João, 10 telas Udinese branco, 890")
        return

    # ── INTERPRETAR MENSAGEM ──
    intencao_data = await interpret_message(text)
    intencao = intencao_data.get("intencao", "menu")
    dados = intencao_data.get("dados", {})

    # ── MENU ──
    if intencao in ["menu", "ajuda"] or text_lower in ["/start", "/menu"]:
        msg = """🏢 *Keycomerce — Financeiro*

O que você quer fazer?

💰 *Contas a pagar*
• `pagar` — ver todas
• `vence hoje` — urgentes
• `vence semana` — próximos 7 dias
• `paguei [nome]` — marcar como pago

📥 *Contas a receber*
• `receber` — ver todas
• `recebido [nome]` — marcar como recebido

📋 *Orçamentos*
• `orçamentos` — ver todos
• `aprovar [id]` — aprovar orçamento
• `recusar [id]` — recusar orçamento

➕ *Lançar novo*
• `nova conta` — lançar conta a pagar
• `nova cobrança` — lançar conta a receber
• `novo orçamento` — criar orçamento
• Mande uma 📷 *foto ou PDF* do boleto

📊 `resumo` — visão geral"""
        await send_message(chat_id, msg)
        return

    # ── RESUMO ──
    if intencao == "resumo":
        pagar_aberto = [p for p in data["pagar"] if p["status"] == "aberto"]
        receber_aberto = [r for r in data["receber"] if r["status"] == "aberto"]
        orc_pendente = [o for o in data["orcamentos"] if o["status"] == "pendente"]
        vencido = [p for p in pagar_aberto if days_until(p["vencimento"]) < 0]
        hoje = [p for p in pagar_aberto if days_until(p["vencimento"]) == 0]
        semana = [p for p in pagar_aberto if 1 <= days_until(p["vencimento"]) <= 7]
        total_pagar = sum(p["valor"] for p in pagar_aberto)
        total_receber = sum(r["valor"] for r in receber_aberto)

        msg = f"""📊 *Resumo Financeiro — Keyco*

💸 *A pagar em aberto:* {fmt_brl(total_pagar)}
  🔴 Vencidas: {len(vencido)} conta(s)
  🔴 Vencem hoje: {len(hoje)} conta(s)
  🟡 Esta semana: {len(semana)} conta(s)

💰 *A receber em aberto:* {fmt_brl(total_receber)}
  📋 {len(receber_aberto)} cobrança(s) pendente(s)

📋 *Orçamentos pendentes:* {len(orc_pendente)}"""
        await send_message(chat_id, msg)
        return

    # ── LISTAR PAGAR ──
    if intencao in ["listar_pagar", "pagar_hoje", "pagar_semana"]:
        abertos = [p for p in data["pagar"] if p["status"] == "aberto"]
        if intencao == "pagar_hoje":
            items = [p for p in abertos if days_until(p["vencimento"]) <= 0]
            titulo = "🔴 *Vence hoje / Vencidas*"
        elif intencao == "pagar_semana":
            items = [p for p in abertos if 0 <= days_until(p["vencimento"]) <= 7]
            titulo = "🟡 *Vencem esta semana*"
        else:
            items = sorted(abertos, key=lambda x: days_until(x["vencimento"]))
            titulo = "💸 *Contas a pagar — em aberto*"

        if not items:
            await send_message(chat_id, f"{titulo}\n\n✅ Nenhuma conta nesta categoria.")
            return

        linhas = [titulo, ""]
        for p in items:
            d = days_until(p["vencimento"])
            st = status_venc(d)
            linhas.append(f"{st} *{p['id']}*")
            linhas.append(f"🏢 {p['fornecedor']}")
            linhas.append(f"💰 {fmt_brl(p['valor'])} · 📅 {p['vencimento']}")
            linhas.append("")

        total = sum(p["valor"] for p in items)
        linhas.append(f"*Total: {fmt_brl(total)}*")
        await send_message(chat_id, "\n".join(linhas))
        return

    # ── LISTAR RECEBER ──
    if intencao == "listar_receber":
        abertos = [r for r in data["receber"] if r["status"] == "aberto"]
        if not abertos:
            await send_message(chat_id, "📥 *Contas a receber*\n\n✅ Nenhuma cobrança em aberto.")
            return
        linhas = ["📥 *Contas a receber — em aberto*", ""]
        for r in sorted(abertos, key=lambda x: days_until(x["vencimento"])):
            d = days_until(r["vencimento"])
            st = status_venc(d)
            linhas.append(f"{st} *{r['id']}*")
            linhas.append(f"👤 {r['cliente']}")
            linhas.append(f"💰 {fmt_brl(r['valor'])} · 📅 {r['vencimento']}")
            linhas.append("")
        total = sum(r["valor"] for r in abertos)
        linhas.append(f"*Total a receber: {fmt_brl(total)}*")
        await send_message(chat_id, "\n".join(linhas))
        return

    # ── LISTAR ORÇAMENTOS ──
    if intencao == "listar_orcamentos":
        items = data["orcamentos"]
        if not items:
            await send_message(chat_id, "📋 *Orçamentos*\n\nNenhum orçamento cadastrado.")
            return
        linhas = ["📋 *Orçamentos*", ""]
        status_emoji = {"pendente": "🟡", "aprovado": "🟢", "recusado": "🔴", "enviado": "🔵"}
        for o in items[-10:]:
            emoji = status_emoji.get(o["status"], "⚪")
            linhas.append(f"{emoji} *{o['id']}* — {o['cliente']}")
            linhas.append(f"📋 {o['descricao']} · 💰 {fmt_brl(o['valor'])}")
            linhas.append(f"Status: {o['status'].capitalize()} · {o['data']}")
            linhas.append("")
        await send_message(chat_id, "\n".join(linhas))
        return

    # ── MARCAR PAGO ──
    if intencao == "marcar_pago":
        nome = dados.get("nome", "").lower()
        encontrados = [p for p in data["pagar"] if p["status"] == "aberto" and nome in p["fornecedor"].lower()]
        if not encontrados:
            await send_message(chat_id, f"❌ Não encontrei conta aberta com *{dados.get('nome', '')}*.\n\nDigite `pagar` para ver todas as contas em aberto.")
        elif len(encontrados) == 1:
            encontrados[0]["status"] = "pago"
            encontrados[0]["data_pagamento"] = datetime.now().strftime("%d/%m/%Y")
            save_data(data)
            await send_message(chat_id, f"✅ *{encontrados[0]['id']}* marcado como pago!\n\n🏢 {encontrados[0]['fornecedor']}\n💰 {fmt_brl(encontrados[0]['valor'])}")
        else:
            linhas = [f"Encontrei {len(encontrados)} contas para *{dados.get('nome', '')}*:", ""]
            for p in encontrados:
                linhas.append(f"• *{p['id']}* — {fmt_brl(p['valor'])} · venc. {p['vencimento']}")
            linhas.append("\nQual delas? Digite o ID. Ex: *PAG-001*")
            user_states[chat_id] = {"step": "confirmar_pago_id"}
            await send_message(chat_id, "\n".join(linhas))
        return

    # ── MARCAR RECEBIDO ──
    if intencao == "marcar_recebido":
        nome = dados.get("nome", "").lower()
        encontrados = [r for r in data["receber"] if r["status"] == "aberto" and nome in r["cliente"].lower()]
        if not encontrados:
            await send_message(chat_id, f"❌ Não encontrei cobrança aberta para *{dados.get('nome', '')}*.\n\nDigite `receber` para ver todas.")
        elif len(encontrados) == 1:
            encontrados[0]["status"] = "pago"
            encontrados[0]["data_pagamento"] = datetime.now().strftime("%d/%m/%Y")
            save_data(data)
            await send_message(chat_id, f"✅ *{encontrados[0]['id']}* marcado como recebido!\n\n👤 {encontrados[0]['cliente']}\n💰 {fmt_brl(encontrados[0]['valor'])}")
        else:
            linhas = [f"Encontrei {len(encontrados)} cobranças para *{dados.get('nome', '')}*:", ""]
            for r in encontrados:
                linhas.append(f"• *{r['id']}* — {fmt_brl(r['valor'])} · venc. {r['vencimento']}")
            linhas.append("\nQual delas? Digite o ID.")
            await send_message(chat_id, "\n".join(linhas))
        return

    # ── APROVAR/RECUSAR ORÇAMENTO ──
    if intencao in ["aprovar_orcamento", "recusar_orcamento"]:
        orc_id = dados.get("id", text.upper().strip())
        orc = next((o for o in data["orcamentos"] if o["id"] == orc_id), None)
        if not orc:
            await send_message(chat_id, f"❌ Orçamento *{orc_id}* não encontrado.")
            return
        novo_status = "aprovado" if intencao == "aprovar_orcamento" else "recusado"
        orc["status"] = novo_status
        save_data(data)
        emoji = "✅" if novo_status == "aprovado" else "❌"
        await send_message(chat_id, f"{emoji} Orçamento *{orc['id']}* {novo_status}!\n\n👤 {orc['cliente']}\n💰 {fmt_brl(orc['valor'])}")
        return

    # ── NOVA CONTA PAGAR ──
    if intencao == "nova_conta_pagar":
        user_states[chat_id] = {"step": "nova_conta_pagar_manual", "tipo_lancamento": "pagar"}
        await send_message(chat_id, "💸 *Nova conta a pagar*\n\nVocê pode:\n📷 Mandar foto ou PDF do boleto\n\nOu digitar no formato:\n*Fornecedor, Valor, dd/mm/aaaa*\n\nEx: `Udinese perfis, 3200, 25/06/2025`")
        return

    # ── NOVA CONTA RECEBER ──
    if intencao == "nova_conta_receber":
        user_states[chat_id] = {"step": "nova_conta_receber_manual", "tipo_lancamento": "receber"}
        await send_message(chat_id, "📥 *Nova cobrança*\n\nDigite no formato:\n*Cliente, Valor, dd/mm/aaaa*\n\nEx: `Esquadrias João, 1640, 25/06/2025`")
        return

    # ── NOVO ORÇAMENTO ──
    if intencao == "novo_orcamento":
        user_states[chat_id] = {"step": "novo_orcamento_manual"}
        await send_message(chat_id, "📋 *Novo orçamento*\n\nDigite no formato:\n*Cliente, Descrição, Valor*\n\nEx: `Esquadrias João, 10 telas Udinese branco, 890`")
        return

    # ── FALLBACK ──
    await send_message(chat_id, "🤔 Não entendi. Digite /menu para ver o que posso fazer por você.")


# ── WEBHOOK SERVER ──
async def webhook_handler(request):
    try:
        update = await request.json()
        message = update.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        if not chat_id:
            return web.Response(text="ok")
        text = message.get("text")
        photo = message.get("photo")
        document = message.get("document")
        await handle_message(chat_id, text=text, photo=photo, document=document)
    except Exception as e:
        print(f"Erro: {e}")
    return web.Response(text="ok")

async def health(request):
    return web.Response(text="Keycomerce Bot rodando ✅")

async def setup_webhook(app):
    base_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if base_url:
        webhook_url = f"https://{base_url}/webhook"
        async with aiohttp.ClientSession() as session:
            await session.post(f"{TELEGRAM_API}/setWebhook", json={"url": webhook_url})
            print(f"Webhook configurado: {webhook_url}")

app = web.Application()
app.router.add_post("/webhook", webhook_handler)
app.router.add_get("/", health)
app.on_startup.append(setup_webhook)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    web.run_app(app, port=port)
