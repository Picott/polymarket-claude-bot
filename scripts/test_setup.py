"""
Verifica que el entorno está correctamente configurado.
Uso: python scripts/test_setup.py
"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(dotenv_path=ROOT / "config" / ".env")

PASS = "  [OK]"
FAIL = "  [FAIL]"

def check(label, condition, hint=""):
    icon = PASS if condition else FAIL
    print(f"{icon}  {label}")
    if not condition and hint:
        print(f"       → {hint}")
    return condition

ok = True

print("\n── Verificando configuración de polymarket-claude-bot ──\n")

# 1. .env existe
ok &= check(
    "config/.env encontrado",
    (ROOT / "config" / ".env").exists(),
    "Crea el archivo: copy config\\.env.example config\\.env"
)

# 2. ANTHROPIC_API_KEY
key = os.getenv("ANTHROPIC_API_KEY", "")
ok &= check(
    "ANTHROPIC_API_KEY configurada",
    key.startswith("sk-ant-") and len(key) > 20,
    "Edita config\\.env y pon tu clave de https://console.anthropic.com"
)

# 3. Anthropic SDK importable
try:
    import anthropic
    ok &= check("anthropic SDK instalado", True)
except ImportError:
    ok &= check("anthropic SDK instalado", False, "pip install -r requirements.txt")

# 4. Flask importable
try:
    import flask
    ok &= check("flask instalado", True)
except ImportError:
    ok &= check("flask instalado", False, "pip install -r requirements.txt")

# 5. Conexión a la API de Anthropic
if key.startswith("sk-ant-"):
    try:
        import anthropic as ant
        client = ant.Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "di solo: OK"}]
        )
        ok &= check("Conexión a Anthropic API", True)
    except Exception as e:
        ok &= check("Conexión a Anthropic API", False, str(e))

# 6. Directorio logs/
ok &= check(
    "Directorio logs/ existe",
    (ROOT / "logs").is_dir(),
    "Crea la carpeta: mkdir logs"
)

print()
if ok:
    print("  Todo listo. Puedes lanzar el bot:\n")
    print("    python scripts/run_bot.py --mode paper --once")
    print("    python dashboard/server.py\n")
else:
    print("  Corrige los errores anteriores antes de continuar.\n")

sys.exit(0 if ok else 1)
