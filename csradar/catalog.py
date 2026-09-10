"""Descoberta de jogos instalados, em toda loja que guarde o inventario em
formato legivel.

Cada loja resolveu o problema de um jeito. O que da para ler sem engenharia
reversa:

  steam       steamapps/libraryfolders.vdf + appmanifest_*.acf   (KeyValues)
  epic        ProgramData/Epic/.../Manifests/*.item              (JSON)
  gog         registro HKLM\\SOFTWARE\\GOG.com\\Games            (registro)
  xbox        .GamingRoot na raiz dos discos + XboxGames/        (binario+dir)
  battlenet   %APPDATA%/Battle.net/Battle.net.config             (JSON)
  riot        ProgramData/Riot Games/RiotClientInstalls.json     (JSON)
  ea          Origin LocalContent/**/*.mfst + EA Desktop         (texto/dir)
  ubisoft     registro HKLM\\SOFTWARE\\Ubisoft\\Launcher\\Installs
  rockstar    registro HKLM\\SOFTWARE\\Rockstar Games
  amazon      %APPDATA%/Amazon Games/.../GameInstallInfo.sqlite  (SQLite)
  itch        %APPDATA%/itch/db/butler.db                        (SQLite)
  legendary   ~/.config/legendary/installed.json (Epic alternativo, Linux)
  lutris      ~/.local/share/lutris/pga.db                       (SQLite)
  uninstall   chaves Uninstall do Windows - a rede de seguranca que pega
              tudo que tem desinstalador e nao expoe manifesto proprio

Cada scanner recebe os caminhos por parametro para os testes rodarem contra
fixtures, e nenhum deles abre processo nem toca em jogo rodando.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import struct
import sys
from pathlib import Path

from .games.base import InstalledGame
from .games.registry import identify

# --------------------------------------------------------------- parser VDF

_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text: str) -> dict:
    """Parser minimo do formato KeyValues da Valve.

    Cobre o que os manifestos usam: chaves entre aspas, blocos entre chaves,
    comentarios com //. Nao cobre condicionais ([$WIN32]) nem includes, que
    nao aparecem em appmanifest/libraryfolders.
    """
    lines = []
    for line in text.splitlines():
        stripped = line.split("//")[0] if not line.strip().startswith('"') else line
        lines.append(stripped)
    cleaned = "\n".join(lines)

    tokens = []
    for m in _VDF_TOKEN.finditer(cleaned):
        tokens.append(m.group(1) if m.group(1) is not None else m.group(2))

    root: dict = {}
    stack = [root]
    pending_key = None
    for tok in tokens:
        if tok == "{":
            if pending_key is None:
                continue
            node: dict = {}
            stack[-1][pending_key] = node
            stack.append(node)
            pending_key = None
        elif tok == "}":
            if len(stack) > 1:
                stack.pop()
            pending_key = None
        elif pending_key is None:
            pending_key = tok.replace('\\"', '"')
        else:
            stack[-1][pending_key] = tok.replace("\\\\", "\\").replace('\\"', '"')
            pending_key = None
    return root


def _env(name: str, fallback: str = "") -> str:
    return os.environ.get(name, fallback)


def _make(name, launcher, install_dir="", app_id="", size=0) -> InstalledGame:
    return InstalledGame(
        name=str(name), launcher=launcher, install_dir=str(install_dir or ""),
        app_id=str(app_id or ""), size_bytes=int(size or 0),
        profile=identify(str(name), app_id if str(app_id).isdigit() else None),
    )


def _sqlite_rows(path, query: str) -> list:
    """Le um SQLite de terceiro em modo somente leitura.

    Somente leitura importa: o launcher pode estar aberto com o banco travado,
    e nao temos nada que justifique escrever no inventario de ninguem.
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        uri = f"file:{p.as_posix()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return []
    try:
        return conn.execute(query).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()


# ------------------------------------------------------------------- Steam


def steam_install_dir() -> Path | None:
    for env in ("CSRADAR_STEAM_PATH", "STEAM_PATH"):
        value = os.environ.get(env)
        if value and Path(value).exists():
            return Path(value)
    try:
        import winreg

        for hive, key in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            path = Path(winreg.QueryValueEx(k, name)[0])
                            if path.exists():
                                return path
                        except FileNotFoundError:
                            continue
            except OSError:
                continue
    except ImportError:
        pass

    home = Path.home()
    for candidate in (
        r"C:\Program Files (x86)\Steam",
        r"C:\Program Files\Steam",
        home / ".steam" / "steam",                          # Linux
        home / ".local" / "share" / "Steam",                # Linux
        home / "Library" / "Application Support" / "Steam",  # macOS
    ):
        if Path(candidate).exists():
            return Path(candidate)
    return None


def steam_libraries(steam_dir) -> list:
    """Todas as bibliotecas Steam, incluindo as em outros discos."""
    steam_dir = Path(steam_dir)
    libs = [steam_dir / "steamapps"]
    vdf = steam_dir / "steamapps" / "libraryfolders.vdf"
    if not vdf.exists():
        return [p for p in libs if p.exists()]
    data = parse_vdf(vdf.read_text(encoding="utf-8", errors="replace"))
    folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
    for value in folders.values():
        path = value.get("path") if isinstance(value, dict) else value
        if not path:
            continue
        candidate = Path(path) / "steamapps"
        if candidate not in libs:
            libs.append(candidate)
    return [p for p in libs if p.exists()]


def scan_steam(steam_dir=None) -> list:
    steam_dir = Path(steam_dir) if steam_dir else steam_install_dir()
    if not steam_dir or not Path(steam_dir).exists():
        return []
    found = []
    for lib in steam_libraries(steam_dir):
        for manifest in sorted(lib.glob("appmanifest_*.acf")):
            try:
                data = parse_vdf(manifest.read_text(encoding="utf-8",
                                                    errors="replace"))
            except OSError:
                continue
            app = data.get("AppState") or {}
            appid = app.get("appid", "")
            if not appid:
                continue
            install_dir = app.get("installdir", "")
            full = lib / "common" / install_dir if install_dir else lib
            found.append(_make(app.get("name") or f"appid {appid}", "steam",
                               full, appid, app.get("SizeOnDisk", 0)))
    return found


# -------------------------------------------------------------------- Epic


def scan_epic(manifest_dir=None) -> list:
    path = Path(manifest_dir) if manifest_dir else (
        Path(_env("PROGRAMDATA", r"C:\ProgramData"))
        / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    )
    if not path.exists():
        return []
    found = []
    for item in sorted(path.glob("*.item")):
        try:
            data = json.loads(item.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        name = data.get("DisplayName") or data.get("AppName")
        if not name:
            continue
        found.append(_make(name, "epic", data.get("InstallLocation", ""),
                           data.get("AppName", ""), data.get("InstallSize", 0)))
    return found


def scan_legendary(path=None) -> list:
    """Legendary/Heroic: cliente Epic alternativo, comum no Linux."""
    p = Path(path) if path else Path.home() / ".config" / "legendary" / "installed.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for app, info in (data or {}).items():
        if not isinstance(info, dict):
            continue
        out.append(_make(info.get("title") or app, "epic",
                         info.get("install_path", ""), app,
                         info.get("install_size", 0)))
    return out


# --------------------------------------------------------------------- GOG


def scan_gog(galaxy_db=None) -> list:
    """GOG Galaxy: registro no Windows, banco do Galaxy como reforco."""
    found = []
    try:
        import winreg

        for hive, key in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\Games"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\GOG.com\Games"),
        ):
            try:
                root = winreg.OpenKey(hive, key)
            except OSError:
                continue
            with root:
                for i in range(_subkey_count(winreg, root)):
                    try:
                        sub = winreg.EnumKey(root, i)
                        with winreg.OpenKey(root, sub) as node:
                            name = _reg_value(winreg, node, "gameName")
                            path = _reg_value(winreg, node, "path") or ""
                            if name:
                                found.append(_make(name, "gog", path, sub))
                    except OSError:
                        continue
    except ImportError:
        pass

    db = Path(galaxy_db) if galaxy_db else (
        Path(_env("PROGRAMDATA", r"C:\ProgramData"))
        / "GOG.com" / "Galaxy" / "storage" / "galaxy-2.0.db"
    )
    rows = _sqlite_rows(
        db,
        "SELECT productId, installationPath FROM InstalledBaseProducts",
    )
    for product_id, install_path in rows:
        if any(g.app_id == str(product_id) for g in found):
            continue
        name = Path(str(install_path or "")).name or f"gog {product_id}"
        found.append(_make(name, "gog", install_path, product_id))
    return found


# -------------------------------------------------------------- Xbox / MS


def _gaming_root_targets(drives=None) -> list:
    """Le o arquivo .GamingRoot na raiz de cada disco.

    O Xbox app grava um arquivo binario minusculo apontando para a pasta onde
    os jogos daquele disco foram instalados. O formato e: 4 bytes de assinatura
    'RGBX', 4 bytes, e o caminho em UTF-16LE terminado em nulo.
    """
    if drives is None:
        drives = [f"{c}:\\" for c in "CDEFGHIJKL"] if sys.platform == "win32" else []
    targets = []
    for drive in drives:
        marker = Path(drive) / ".GamingRoot"
        if not marker.exists():
            continue
        try:
            raw = marker.read_bytes()
        except OSError:
            continue
        if len(raw) < 10:
            continue
        try:
            text = raw[8:].decode("utf-16-le", errors="ignore").split("\x00")[0]
        except (UnicodeDecodeError, struct.error):
            continue
        if text:
            targets.append(Path(drive) / text.strip("\\/"))
    return targets


def scan_xbox(roots=None) -> list:
    """Jogos do Xbox app / Game Pass PC."""
    candidates = list(roots) if roots else _gaming_root_targets()
    if not roots:
        for extra in (r"C:\XboxGames", Path(_env("PROGRAMFILES", "")) / "WindowsApps"):
            if extra and Path(extra).exists():
                candidates.append(Path(extra))
    found = []
    for root in candidates:
        root = Path(root)
        if not root.exists():
            continue
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
        for child in children:
            name = _xbox_display_name(child) or child.name
            found.append(_make(name, "xbox", child))
    return found


def _xbox_display_name(folder: Path) -> str:
    """O nome bonito vem do AppxManifest, quando ele existe e e legivel."""
    for candidate in (folder / "Content" / "AppxManifest.xml",
                      folder / "AppxManifest.xml"):
        if not candidate.exists():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r"<DisplayName>([^<]+)</DisplayName>", text)
        if m and not m.group(1).startswith("ms-resource"):
            return m.group(1).strip()
    return ""


# --------------------------------------------------------------- Battle.net


def scan_battlenet(config_path=None) -> list:
    """O Battle.net guarda a lista em JSON, nao no product.db binario."""
    p = Path(config_path) if config_path else (
        Path(_env("APPDATA", "")) / "Battle.net" / "Battle.net.config"
    )
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    games = (data.get("Games") or {})
    out = []
    for key, info in games.items():
        if not isinstance(info, dict) or key in ("battle_net", "agent"):
            continue
        path = info.get("LastPlayedPath") or info.get("Path") or ""
        name = _BNET_NAMES.get(key, key.replace("_", " ").title())
        out.append(_make(name, "battlenet", Path(path).parent if path else "", key))
    return out


_BNET_NAMES = {
    "prometheus": "Overwatch 2",
    "wow": "World of Warcraft",
    "s2": "StarCraft II",
    "s1": "StarCraft: Brood War",
    "w3": "Warcraft III: Reforged",
    "hs_beta": "Hearthstone",
    "diablo3": "Diablo III",
    "fenris": "Diablo IV",
    "odin": "Call of Duty: Modern Warfare",
    "lazarus": "Call of Duty: Modern Warfare II",
    "zeus": "Call of Duty: Black Ops 6",
    "hero": "Heroes of the Storm",
    "viper": "Call of Duty: Black Ops 4",
}


# -------------------------------------------------------------------- Riot


def scan_riot(installs_path=None) -> list:
    p = Path(installs_path) if installs_path else (
        Path(_env("PROGRAMDATA", r"C:\ProgramData"))
        / "Riot Games" / "RiotClientInstalls.json"
    )
    out = []
    seen = set()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            data = {}
        for key, value in (data.get("associated_client") or {}).items():
            folder = Path(key)
            name = folder.name or str(value)
            if name and name not in seen:
                seen.add(name)
                out.append(_make(name, "riot", folder))
    # o instalador padrao cria C:\Riot Games\<Jogo>
    root = Path(r"C:\Riot Games")
    if root.exists():
        try:
            for child in sorted(p for p in root.iterdir() if p.is_dir()):
                if child.name in seen or child.name == "Riot Client":
                    continue
                seen.add(child.name)
                out.append(_make(child.name, "riot", child))
        except OSError:
            pass
    return out


# ---------------------------------------------------------------- EA / EA app


def scan_ea(origin_dir=None, ea_dir=None) -> list:
    """Origin grava .mfst por jogo; o EA app usa pastas em InstallData."""
    out = []
    origin = Path(origin_dir) if origin_dir else (
        Path(_env("PROGRAMDATA", r"C:\ProgramData")) / "Origin" / "LocalContent"
    )
    if origin.exists():
        try:
            for child in sorted(p for p in origin.iterdir() if p.is_dir()):
                out.append(_make(child.name, "ea", child))
        except OSError:
            pass

    ea = Path(ea_dir) if ea_dir else (
        Path(_env("PROGRAMDATA", r"C:\ProgramData"))
        / "EA Desktop" / "InstallData"
    )
    if ea.exists():
        try:
            for child in sorted(p for p in ea.iterdir() if p.is_dir()):
                if not any(g.name == child.name for g in out):
                    out.append(_make(child.name, "ea", child))
        except OSError:
            pass
    return out


# ---------------------------------------------------------------- Ubisoft


def scan_ubisoft() -> list:
    try:
        import winreg
    except ImportError:
        return []
    out = []
    key = r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key)
    except OSError:
        return []
    with root:
        for i in range(_subkey_count(winreg, root)):
            try:
                sub = winreg.EnumKey(root, i)
                with winreg.OpenKey(root, sub) as node:
                    path = _reg_value(winreg, node, "InstallDir") or ""
                    name = Path(path.rstrip("/\\")).name or f"ubisoft {sub}"
                    out.append(_make(name, "ubisoft", path, sub))
            except OSError:
                continue
    return out


def scan_rockstar() -> list:
    try:
        import winreg
    except ImportError:
        return []
    out = []
    for key in (r"SOFTWARE\WOW6432Node\Rockstar Games",
                r"SOFTWARE\Rockstar Games"):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key)
        except OSError:
            continue
        with root:
            for i in range(_subkey_count(winreg, root)):
                try:
                    sub = winreg.EnumKey(root, i)
                    with winreg.OpenKey(root, sub) as node:
                        path = (_reg_value(winreg, node, "InstallFolder")
                                or _reg_value(winreg, node, "InstallLocation") or "")
                        if path:
                            out.append(_make(sub, "rockstar", path))
                except OSError:
                    continue
    return out


# ------------------------------------------------------------ Amazon / itch


def scan_amazon(db_path=None) -> list:
    p = Path(db_path) if db_path else (
        Path(_env("APPDATA", "")) / "Amazon Games" / "Data" / "Games" / "Sql"
        / "GameInstallInfo.sqlite"
    )
    rows = _sqlite_rows(p, "SELECT ProductTitle, InstallDirectory, Id "
                           "FROM DbSet WHERE Installed = 1")
    return [_make(title, "amazon", path, pid) for title, path, pid in rows]


def scan_itch(db_path=None) -> list:
    p = Path(db_path) if db_path else (
        Path(_env("APPDATA", "")) / "itch" / "db" / "butler.db"
    )
    rows = _sqlite_rows(p, "SELECT title, verdict FROM games")
    out = []
    for title, verdict in rows:
        path = ""
        if verdict:
            try:
                path = json.loads(verdict).get("basePath", "")
            except (json.JSONDecodeError, TypeError):
                path = ""
        out.append(_make(title, "itch", path))
    return out


def scan_lutris(db_path=None) -> list:
    """Lutris: gerenciador do Linux que agrega varias lojas."""
    p = Path(db_path) if db_path else (
        Path.home() / ".local" / "share" / "lutris" / "pga.db"
    )
    rows = _sqlite_rows(p, "SELECT name, directory, runner FROM games "
                           "WHERE installed = 1")
    return [_make(name, f"lutris:{runner or '?'}", directory)
            for name, directory, runner in rows]


# --------------------------------------------------------- registro Windows


UNINSTALL_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)

_LAUNCHER_HINTS = (
    ("battle.net", "battlenet"),
    ("blizzard", "battlenet"),
    ("riot", "riot"),
    ("ubisoft", "ubisoft"),
    ("uplay", "ubisoft"),
    ("electronic arts", "ea"),
    ("ea desktop", "ea"),
    ("origin", "ea"),
    ("rockstar", "rockstar"),
    ("epic games", "epic"),
    ("gog", "gog"),
    ("xbox", "xbox"),
    ("microsoft.", "xbox"),
    ("valve", "steam"),
    ("amazon games", "amazon"),
    ("itch.io", "itch"),
    ("wargaming", "wargaming"),
    ("mihoyo", "hoyoplay"),
    ("hoyoverse", "hoyoplay"),
    ("hoyoplay", "hoyoplay"),
    ("cognosphere", "hoyoplay"),
    ("nexon", "nexon"),
    ("ncsoft", "ncsoft"),
    ("garena", "garena"),
    ("netease", "netease"),
    ("jagex", "jagex"),
    ("oculus", "oculus"),
    ("meta platforms", "oculus"),
    ("viveport", "viveport"),
    ("roblox", "roblox"),
    ("mojang", "minecraft"),
    ("battlestate", "standalone"),
    ("krafton", "standalone"),
    ("cfx.re", "standalone"),
    ("paradox", "paradox"),
    ("square enix", "square_enix"),
    ("bandai namco", "standalone"),
    ("chess.com", "standalone"),
)


def scan_registry(hives=None) -> list:
    """Rede de seguranca: pega o que nao expoe manifesto proprio."""
    try:
        import winreg
    except ImportError:
        return []

    hives = hives or (
        (winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEYS[0]),
        (winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEYS[1]),
        (winreg.HKEY_CURRENT_USER, UNINSTALL_KEYS[0]),
    )
    found = []
    seen = set()
    for hive, key in hives:
        try:
            root = winreg.OpenKey(hive, key)
        except OSError:
            continue
        with root:
            for i in range(_subkey_count(winreg, root)):
                try:
                    sub = winreg.EnumKey(root, i)
                    with winreg.OpenKey(root, sub) as node:
                        name = _reg_value(winreg, node, "DisplayName")
                        if not name or name in seen:
                            continue
                        profile = identify(name)
                        publisher = (_reg_value(winreg, node, "Publisher") or "").lower()
                        launcher = _guess_launcher(name, publisher)
                        # sem perfil conhecido e sem pista de launcher de jogo,
                        # e provavelmente um driver ou um utilitario
                        if profile is None and launcher is None:
                            continue
                        seen.add(name)
                        size = int(_reg_value(winreg, node, "EstimatedSize") or 0) * 1024
                        found.append(_make(
                            name, launcher or "standalone",
                            _reg_value(winreg, node, "InstallLocation") or "",
                            sub, size))
                except OSError:
                    continue
    return found


def _subkey_count(winreg, key) -> int:
    try:
        return winreg.QueryInfoKey(key)[0]
    except OSError:
        return 0


def _reg_value(winreg, key, name):
    try:
        return winreg.QueryValueEx(key, name)[0]
    except (OSError, FileNotFoundError):
        return None


def _guess_launcher(name: str, publisher: str) -> str | None:
    blob = f"{name} {publisher}".lower()
    for hint, launcher in _LAUNCHER_HINTS:
        if hint in blob:
            return launcher
    return None


# ---------------------------------------------------------- pastas conhecidas


KNOWN_DIRS = (
    (r"C:\Program Files (x86)\Ubisoft\Ubisoft Game Launcher\games", "ubisoft"),
    (r"C:\Program Files\EA Games", "ea"),
    (r"C:\Program Files (x86)\EA Games", "ea"),
    (r"C:\Program Files\Epic Games", "epic"),
    (r"C:\Battlestate Games", "standalone"),
    (r"C:\Riot Games", "riot"),
    (r"C:\Program Files\HoYoPlay\games", "hoyoplay"),
    (r"C:\Program Files\miHoYo", "hoyoplay"),
    (r"C:\Program Files\Nexon", "nexon"),
    (r"C:\Nexon", "nexon"),
    (r"C:\Program Files (x86)\NCSOFT", "ncsoft"),
    (r"C:\Program Files (x86)\Garena\Games", "garena"),
    (r"C:\Program Files\NetEase", "netease"),
    (r"C:\Games", "standalone"),
    (r"%PROGRAMFILES%\Oculus\Software\Software", "oculus"),
    (r"%LOCALAPPDATA%\Programs", "standalone"),
)


def scan_known_dirs(dirs=None) -> list:
    found = []
    for raw, launcher in (dirs or KNOWN_DIRS):
        root = Path(os.path.expandvars(str(raw)))
        if not root.exists() or not root.is_dir():
            continue
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
        for child in children:
            # nas pastas genericas so listamos o que o catalogo reconhece,
            # senao o resultado vira uma lista de aplicativos aleatorios
            profile = identify(child.name)
            if profile is None and "%" in str(raw):
                continue
            found.append(_make(child.name, launcher, child))
    return found


# ------------------------------------------------------------------- Heroic


def scan_heroic(config_dir=None) -> list:
    """Heroic: cliente alternativo de Epic, GOG e Amazon (Linux e Windows).

    Guarda um `installed.json` por loja. Sao arquivos diferentes com formatos
    diferentes, o que e o normal aqui: cada um foi escrito por um time.
    """
    root = Path(config_dir) if config_dir else (
        Path(_env("APPDATA", "")) / "heroic" if os.name == "nt"
        else Path.home() / ".config" / "heroic"
    )
    if not root.exists():
        return []
    out = []

    epic = root / "legendaryConfig" / "legendary" / "installed.json"
    out.extend(scan_legendary(epic) if epic.exists() else [])

    for store, launcher in (("gog_store", "gog"), ("nile_config", "amazon")):
        p = root / store / "installed.json"
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        items = data.get("installed") if isinstance(data, dict) else data
        for info in items or []:
            if not isinstance(info, dict):
                continue
            name = (info.get("title") or info.get("appName")
                    or info.get("id") or "")
            if name:
                out.append(_make(name, launcher,
                                 info.get("install_path", ""),
                                 info.get("appName") or info.get("id", "")))
    return out


# --------------------------------------------------------------- Meta / VR


def scan_oculus(manifest_dir=None, software_dir=None) -> list:
    """Meta (Oculus): um manifesto JSON por titulo instalado.

    O nome canonico vem em formato de pacote (`hyperbolic-magnetism-beat-saber`)
    quando o manifesto nao traz o nome de exibicao; a pasta em Software/ e
    melhor. Vale para Pavlov, Beat Saber, Onward e o resto do catalogo VR.
    """
    base = Path(_env("PROGRAMDATA", r"C:\ProgramData")) / "Oculus"
    mdir = Path(manifest_dir) if manifest_dir else base / "Manifests"
    out = []
    seen = set()
    if mdir.exists():
        for item in sorted(mdir.glob("*.json")):
            if item.name.endswith("_assets.json"):
                continue
            try:
                data = json.loads(item.read_text(encoding="utf-8",
                                                 errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            name = (data.get("displayName") or data.get("canonicalName")
                    or item.stem)
            if name in seen:
                continue
            seen.add(name)
            out.append(_make(name, "oculus", data.get("libraryPath", ""),
                             data.get("appId", "")))

    sdir = Path(software_dir) if software_dir else (
        Path(_env("PROGRAMFILES", r"C:\Program Files")) / "Oculus" / "Software"
        / "Software")
    if sdir.exists():
        try:
            for child in sorted(p for p in sdir.iterdir() if p.is_dir()):
                pretty = child.name.replace("-", " ").title()
                if pretty in seen:
                    continue
                seen.add(pretty)
                out.append(_make(pretty, "oculus", child))
        except OSError:
            pass
    return out


# ---------------------------------------------------------------- Wargaming


def scan_wargaming(prefs_path=None) -> list:
    """Wargaming Game Center: World of Tanks, Warships e Warplanes.

    O `preferences.xml` do WGC lista o caminho de cada jogo instalado. O nome
    sai da pasta, que segue o padrao `World_of_Tanks_EU`.
    """
    p = Path(prefs_path) if prefs_path else (
        Path(_env("PROGRAMDATA", r"C:\ProgramData")) / "Wargaming.net"
        / "GameCenter" / "preferences.xml"
    )
    if not p.exists():
        return []
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    seen = set()
    for raw in re.findall(r"<path>(.*?)</path>", text, re.S):
        folder = Path(raw.strip())
        if not raw.strip() or folder.name in seen:
            continue
        seen.add(folder.name)
        out.append(_make(folder.name.replace("_", " "), "wargaming", folder))
    return out


# ---------------------------------------------------------- Minecraft/Roblox


def scan_minecraft(dot_minecraft=None) -> list:
    """O launcher oficial nao mantem inventario: ou o `.minecraft` existe, ou
    o jogo nao esta instalado. Uma linha, e e a informacao toda."""
    p = Path(dot_minecraft) if dot_minecraft else (
        Path(_env("APPDATA", "")) / ".minecraft" if os.name == "nt"
        else Path.home() / ".minecraft"
    )
    if not p.exists():
        return []
    return [_make("Minecraft (Java)", "minecraft", p)]


def scan_roblox(versions_dir=None) -> list:
    p = Path(versions_dir) if versions_dir else (
        Path(_env("LOCALAPPDATA", "")) / "Roblox" / "Versions"
    )
    if not p.exists():
        return []
    try:
        has_player = any(child.joinpath("RobloxPlayerBeta.exe").exists()
                         for child in p.iterdir() if child.is_dir())
    except OSError:
        return []
    return [_make("Roblox", "roblox", p)] if has_player else []


# ------------------------------------------------- atalhos nao-Steam (VDF b)


def parse_binary_vdf(data: bytes) -> dict:
    """Parser do VDF binario, que e o formato do `shortcuts.vdf`.

    Nada a ver com o KeyValues em texto: aqui cada campo comeca com um byte de
    tipo (0 = objeto, 1 = string, 2 = int32), seguido de chave terminada em
    zero. Sao 30 linhas porque o formato e simples, nao porque seja seguro:
    qualquer arquivo truncado cai no `except` e vira dicionario vazio.
    """
    pos = 0
    end = len(data)

    def read_cstr() -> str:
        nonlocal pos
        stop = data.index(b"\x00", pos)
        text = data[pos:stop].decode("utf-8", errors="replace")
        pos = stop + 1
        return text

    def read_map() -> dict:
        nonlocal pos
        node: dict = {}
        while pos < end:
            kind = data[pos]
            pos += 1
            if kind == 8:            # fim do bloco
                return node
            key = read_cstr()
            if kind == 0:
                node[key] = read_map()
            elif kind == 1:
                node[key] = read_cstr()
            elif kind == 2:
                node[key] = int.from_bytes(data[pos:pos + 4], "little")
                pos += 4
            else:                    # tipo desconhecido: nao da para seguir
                return node
        return node

    try:
        return read_map()
    except (ValueError, IndexError):
        return {}


def scan_steam_shortcuts(steam_dir=None) -> list:
    """Jogos NAO-Steam adicionados a biblioteca da Steam.

    E por aqui que aparecem Riot, FiveM e emuladores na maquina de quem
    centraliza tudo na Steam - e sem isto eles sumiriam da varredura.
    """
    root = Path(steam_dir) if steam_dir else steam_install_dir()
    if not root:
        return []
    userdata = Path(root) / "userdata"
    if not userdata.exists():
        return []
    out = []
    seen = set()
    for shortcut in sorted(userdata.glob("*/config/shortcuts.vdf")):
        try:
            blob = shortcut.read_bytes()
        except OSError:
            continue
        for entry in (parse_binary_vdf(blob).get("shortcuts") or {}).values():
            if not isinstance(entry, dict):
                continue
            name = entry.get("AppName") or entry.get("appname") or ""
            if not name or name in seen:
                continue
            seen.add(name)
            target = (entry.get("StartDir") or entry.get("Exe") or "").strip('"')
            out.append(_make(name, "steam:atalho", target))
    return out


# -------------------------------------------------------------- Linux extra


def scan_flatpak(roots=None) -> list:
    dirs = [Path(p) for p in (roots or (
        "/var/lib/flatpak/app", str(Path.home() / ".local/share/flatpak/app")))]
    out = []
    for root in dirs:
        if not root.exists():
            continue
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
        for child in children:
            # o id e reverso (com.valvesoftware.Steam); o ultimo pedaco e o
            # nome mais proximo de humano que existe sem ler o appstream
            out.append(_make(child.name.split(".")[-1], "flatpak", child,
                             child.name))
    return out


def scan_snap(root=None) -> list:
    base = Path(root or "/snap")
    if not base.exists():
        return []
    try:
        children = sorted(p for p in base.iterdir()
                          if p.is_dir() and p.name != "bin")
    except OSError:
        return []
    return [_make(c.name, "snap", c) for c in children]


# -------------------------------------------------------------------- geral


SCANNERS = {
    "steam": lambda o: scan_steam(o.get("steam_dir")),
    "epic": lambda o: scan_epic(o.get("epic_dir")),
    "legendary": lambda o: scan_legendary(o.get("legendary_path")),
    "gog": lambda o: scan_gog(o.get("gog_db")),
    "xbox": lambda o: scan_xbox(o.get("xbox_roots")),
    "battlenet": lambda o: scan_battlenet(o.get("battlenet_config")),
    "riot": lambda o: scan_riot(o.get("riot_installs")),
    "ea": lambda o: scan_ea(o.get("origin_dir"), o.get("ea_dir")),
    "ubisoft": lambda o: scan_ubisoft(),
    "rockstar": lambda o: scan_rockstar(),
    "amazon": lambda o: scan_amazon(o.get("amazon_db")),
    "itch": lambda o: scan_itch(o.get("itch_db")),
    "lutris": lambda o: scan_lutris(o.get("lutris_db")),
    "heroic": lambda o: scan_heroic(o.get("heroic_dir")),
    "oculus": lambda o: scan_oculus(o.get("oculus_manifests"),
                                    o.get("oculus_software")),
    "wargaming": lambda o: scan_wargaming(o.get("wargaming_prefs")),
    "minecraft": lambda o: scan_minecraft(o.get("dot_minecraft")),
    "roblox": lambda o: scan_roblox(o.get("roblox_versions")),
    "steam_shortcuts": lambda o: scan_steam_shortcuts(o.get("steam_dir")),
    "flatpak": lambda o: scan_flatpak(o.get("flatpak_roots")),
    "snap": lambda o: scan_snap(o.get("snap_root")),
    "known_dirs": lambda o: scan_known_dirs(o.get("known_dirs")),
    "registry": lambda o: scan_registry(),
}


def scan_all(only=None, skip=(), **options) -> list:
    """Roda todos os scanners e deduplica.

    Um scanner que exploda nao pode derrubar a varredura inteira: launcher
    quebrado, banco travado e permissao negada sao rotina.
    """
    games = []
    errors = {}
    for name, fn in SCANNERS.items():
        if only and name not in only:
            continue
        if name in skip:
            continue
        try:
            games.extend(fn(options))
        except Exception as exc:      # noqa: BLE001 - varredura best-effort
            errors[name] = str(exc)
    result = dedupe(games)
    scan_all.last_errors = errors
    return result


scan_all.last_errors = {}


def dedupe(games: list) -> list:
    """Um mesmo jogo aparece em varios lugares; fica o registro mais rico.

    A chave inclui o launcher de proposito: saber que o Rocket League esta na
    Epic E na Steam importa, porque os replays ficam em lugares diferentes.
    """
    best: dict = {}
    for g in games:
        key = (_norm(g.name), g.launcher)
        current = best.get(key)
        if current is None or _richness(g) > _richness(current):
            best[key] = g
    return sorted(best.values(), key=lambda g: (_support_rank(g), g.name.lower()))


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _richness(g: InstalledGame) -> int:
    return ((2 if g.profile else 0) + (1 if g.install_dir else 0)
            + (1 if g.size_bytes else 0))


def _support_rank(g: InstalledGame) -> int:
    order = ["completo", "posicional", "eventos", "conta", "nenhum"]
    try:
        return order.index(str(g.support))
    except ValueError:
        return len(order)


def find_artifacts(game: InstalledGame, extra_roots=()) -> list:
    """Replays/demos do jogo que existam de fato no disco."""
    if not game.profile:
        return []
    hits = []
    roots = [Path(game.install_dir)] if game.install_dir else []
    roots += [Path(r) for r in extra_roots]
    for root in roots:
        if not root.exists():
            continue
        for pattern in game.profile.patterns:
            try:
                hits.extend(str(p) for p in root.glob(pattern))
            except OSError:
                continue
    # padroes absolutos declarados em artifacts (%USERPROFILE%\...)
    for raw in game.profile.artifacts:
        if "%" not in raw and not raw[1:3] == ":\\":
            continue
        expanded = os.path.expandvars(raw)
        if "*" not in expanded:
            continue
        base = Path(expanded).anchor or "."
        try:
            pattern = str(Path(expanded).relative_to(base))
            hits.extend(str(p) for p in Path(base).glob(pattern))
        except (OSError, ValueError):
            continue
    return sorted(set(hits))
