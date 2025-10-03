import os, asyncio, time, logging
from typing import Dict, Tuple, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import discord
from discord.ext import commands
from discord.utils import utcnow

from apps.rhid_runner.automator import run_rhid_punch

# -----------------------------------------------------------------------------
# Setup
# -----------------------------------------------------------------------------
load_dotenv()

LOGLEVEL = os.environ.get("LOGLEVEL", "INFO").upper()
logging.basicConfig(level=LOGLEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("discord")

DISCORD_BOT_TOKEN   = os.environ.get("DISCORD_BOT_TOKEN")
ALLOWED_GUILD_ID    = os.environ.get("ALLOWED_GUILD_ID")
RATE_LIMIT_SECONDS  = int(os.environ.get("RATE_LIMIT_SECONDS", "60"))
APP_TZ              = os.environ.get("APP_TZ", "America/Sao_Paulo")  # <<<<<< Timezone da aplicação
TZ                  = ZoneInfo(APP_TZ)

if not DISCORD_BOT_TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN ausente no .env")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

recent: Dict[int, float] = {}        # anti-spam (por usuário)
automation_lock = asyncio.Lock()     # serializar execuções do automator

# -----------------------------------------------------------------------------
# Funções auxiliares de data/hora
# -----------------------------------------------------------------------------
def _fmt_dt_local(dt_aware_utc: datetime) -> str:
    """
    Recebe datetime aware em UTC e devolve string no fuso da app (TZ).
    Formato: 03/10/2025 11:22:33 BRT
    """
    return dt_aware_utc.astimezone(TZ).strftime("%d/%m/%Y %H:%M:%S %Z")

def _parse_ts_iso_to_utc(ts: str) -> Optional[datetime]:
    """
    Tenta parsear um ISO8601, aceitando 'Z'. Retorna aware em UTC.
    """
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("UTC"))
    except Exception:
        return None

def _try_parse_card_hora_to_utc(hora_str: str) -> Optional[datetime]:
    """
    Para payloads 'card' onde hora veio como '03/10/2025 14:14:54 UTC'.
    Converte para aware UTC se possível.
    """
    try:
        # dia/mês/ano HH:MM:SS UTC
        dt_naive = datetime.strptime(hora_str.replace(" UTC", ""), "%d/%m/%Y %H:%M:%S")
        return dt_naive.replace(tzinfo=ZoneInfo("UTC"))
    except Exception:
        return None

# -----------------------------------------------------------------------------
# Builders de embed bonitões
# -----------------------------------------------------------------------------
def _embed_from_embeds_dict(payload: dict) -> Tuple[Optional[str], discord.Embed, Optional[discord.ui.View]]:
    """
    Converte payload {'content','embeds':[...],'maps_url':...} em (content, embed, view)
    com campos padronizados e ícones. Se houver timestamp ISO no embed, também exibimos
    um campo '🕒 Horário' formatado em TZ.
    """
    ed = (payload.get("embeds") or [{}])[0]
    color_int = ed.get("color") or 0x2E86D9

    embed = discord.Embed(
        title="✅ RHID",
        description="Rotina de marcação de ponto",
        color=color_int,
        timestamp=utcnow(),  # timestamp do Discord (o cliente converte para o fuso do usuário)
    )

    # Se tiver 'timestamp' (ISO), mostramos "🕒 Horário" na TZ da aplicação
    ts_iso = ed.get("timestamp")
    if ts_iso:
        dt_utc = _parse_ts_iso_to_utc(ts_iso)
        if dt_utc:
            embed.add_field(name="🕒 Horário", value=_fmt_dt_local(dt_utc), inline=True)

    # Reaproveita os campos existentes, mas com apelidos/ícones quando possível
    fields = ed.get("fields") or []
    aliases = {
        "Horário": "🕒 Horário",
        "Modo": "⚙️ Modo",
        "E-mail": "📧 E-mail",
        "Localização": "📍 Localização",
        "Etapas": "🧭 Etapas",
        "Resultado": "✅ Resultado",
        "Duração": "⏱️ Duração",
    }
    for f in fields:
        name = aliases.get(f.get("name", ""), f.get("name", "-"))
        embed.add_field(
            name=name,
            value=f.get("value", "—"),
            inline=bool(f.get("inline", False)),
        )

    footer_data = ed.get("footer") or {}
    if footer_data:
        txt = footer_data.get("text") or ""
        embed.set_footer(text=txt)

    # Botão "Abrir no Maps"
    view = None
    maps_url = payload.get("maps_url")
    if maps_url:
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(style=discord.ButtonStyle.link, label="Abrir no Maps", url=maps_url)
        )

    return payload.get("content") or None, embed, view


def _embed_from_card_dict(card: dict) -> Tuple[Optional[str], discord.Embed, Optional[discord.ui.View]]:
    """
    Converte dict 'card' (retornado pelo automator) em Embed estilizado.
    Campos esperados: hora (string) e/ou hora_iso (ISO), modo, email, lat, lon, etapas, resultado, duracao, trigger, maps_url.
    """
    color = 0x4A90E2
    embed = discord.Embed(
        title="✅ RHID",
        description="Rotina de marcação de ponto",
        color=color,
        timestamp=utcnow(),
    )

    # --- Horário em Brasília (ou TZ escolhida)
    # Preferência: hora_iso -> timestamp (ISO) -> tenta parsear 'hora' com 'UTC' -> usa agora
    dt_utc = None
    if isinstance(card.get("hora_iso"), str):
        dt_utc = _parse_ts_iso_to_utc(card["hora_iso"])
    if not dt_utc and isinstance(card.get("timestamp"), str):
        dt_utc = _parse_ts_iso_to_utc(card["timestamp"])
    if not dt_utc and isinstance(card.get("hora"), str):
        dt_utc = _try_parse_card_hora_to_utc(card["hora"])
    if not dt_utc:
        dt_utc = utcnow()

    embed.add_field(name="🕒 Horário", value=_fmt_dt_local(dt_utc), inline=True)

    # --- Modo / E-mail
    embed.add_field(name="⚙️ Modo", value=card.get("modo", "-"), inline=True)
    embed.add_field(name="📧 E-mail", value=card.get("email", "-"), inline=False)

    # --- Localização
    lat = card.get("lat")
    lon = card.get("lon")
    maps_url = card.get("maps_url")
    if lat is not None and lon is not None:
        loc_txt = f"{lat}, {lon} (Empresa)"
        if maps_url:
            loc_txt += f" • [Abrir no Maps]({maps_url})"
        embed.add_field(name="📍 Localização", value=loc_txt, inline=False)

    # --- Etapas / Resultado / Duração
    if card.get("etapas"):
        embed.add_field(name="🧭 Etapas", value=card["etapas"], inline=False)
    if card.get("resultado"):
        embed.add_field(name="✅ Resultado", value=card["resultado"], inline=False)
    dur = card.get("duracao")
    if isinstance(dur, (int, float)):
        embed.add_field(name="⏱️ Duração", value=f"{dur:.1f}s", inline=True)

    trig = card.get("trigger", "button")
    embed.set_footer(text=f"Trigger: {trig}")

    # Botão de Maps (se houver)
    view = None
    if maps_url:
        view = discord.ui.View()
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Abrir no Maps", url=maps_url))

    return None, embed, view


def build_embed_from_result(result) -> Tuple[Optional[str], discord.Embed, Optional[discord.ui.View]]:
    """
    Aceita:
      - dict com chave "embeds" (estilo discord-webhook)  -> _embed_from_embeds_dict
      - dict "card" simplificado                          -> _embed_from_card_dict
      - string (fallback)                                 -> descrição simples
    """
    if isinstance(result, dict):
        if "embeds" in result:
            return _embed_from_embeds_dict(result)
        card_keys = {"hora", "hora_iso", "timestamp", "modo", "email", "lat", "lon", "etapas", "resultado", "duracao", "trigger", "maps_url"}
        if any(k in result for k in card_keys):
            return _embed_from_card_dict(result)

    # fallback: string
    embed = discord.Embed(
        title="✅ RHID",
        description=str(result),
        color=0x2E86D9,
        timestamp=utcnow(),
    )
    return None, embed, None

# -----------------------------------------------------------------------------
# Execução do automator (thread pool)
# -----------------------------------------------------------------------------
async def run_rhid_punch_async(trigger: str, who: discord.abc.User):
    loop = asyncio.get_running_loop()
    def _run_blocking():
        return run_rhid_punch(trigger=trigger, discord_user={
            "id": str(who.id),
            "username": f"{who.name}#{who.discriminator}",
        })
    return await loop.run_in_executor(None, _run_blocking)

# -----------------------------------------------------------------------------
# UI: Botão "Bater Ponto"
# -----------------------------------------------------------------------------
class PunchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Bater Ponto", style=discord.ButtonStyle.success, custom_id="punch_button")
    async def punch(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        now = time.time()
        last = recent.get(user_id, 0)
        if now - last < RATE_LIMIT_SECONDS:
            await interaction.response.send_message(
                f"Aguarde {int(RATE_LIMIT_SECONDS - (now - last))}s para tentar novamente.",
                ephemeral=True
            )
            return
        recent[user_id] = now

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            async with automation_lock:
                result = await run_rhid_punch_async(trigger="button", who=interaction.user)

            content, embed, view = build_embed_from_result(result)
            kwargs = {"content": content, "embed": embed, "ephemeral": True}
            if view is not None:
                kwargs["view"] = view
            await interaction.followup.send(**kwargs)

        except Exception as e:
            log.exception("Falha no punch (botão)")
            await interaction.followup.send(f"❌ Erro: {e}", ephemeral=True)

# -----------------------------------------------------------------------------
# Eventos & Slash Commands
# -----------------------------------------------------------------------------
@bot.event
async def on_ready():
    log.info("Bot pronto como %s (id=%s)", bot.user, bot.user.id)
    try:
        if ALLOWED_GUILD_ID:
            guild = discord.Object(id=int(ALLOWED_GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            log.info("Slash commands sincronizados para guild %s", ALLOWED_GUILD_ID)
        else:
            await bot.tree.sync()
            log.info("Slash commands sincronizados globalmente")
    except Exception:
        log.exception("Falha ao sincronizar slash commands")

@bot.tree.command(name="postarponto", description="Publica o botão para bater ponto")
async def postarponto(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use em um servidor (não em DM).", ephemeral=True)
        return
    view = PunchView()
    embed = discord.Embed(
        title="Registro de Ponto RHID",
        description="Clique no botão abaixo para registrar o ponto.",
        color=0x00A86B
    )
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="baterponto", description="Executa imediatamente a marcação de ponto")
async def baterponto(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        async with automation_lock:
            result = await run_rhid_punch_async(trigger="slash", who=interaction.user)

        content, embed, view = build_embed_from_result(result)
        kwargs = {"content": content, "embed": embed, "ephemeral": True}
        if view is not None:
            kwargs["view"] = view
        await interaction.followup.send(**kwargs)

    except Exception as e:
        log.exception("Erro no /baterponto")
        await interaction.followup.send(f"❌ Erro: {e}", ephemeral=True)

# -----------------------------------------------------------------------------
# Run
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
