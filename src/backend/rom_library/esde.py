"""Game systems, emulators and launch commands as ES-DE defines them, read from the installed
ES-DE's files, else from the plugin's copy of ES-DE 3.5.0's.

The launch logic follows FileData::launchGame() in ES-DE 3.5, under the MIT license:
Copyright (c) 2024-2026 Northwestern Software AB, (c) 2020-2024 Leon Styhre, (c) 2014 Alec Lofquist."""
import glob
import html
import os
import re
import shutil
import subprocess
import tempfile

from . import markup

# The bundled files sit in the AppImage and in native installs under this folder,
# with one subfolder per platform.
RESOURCES = "usr/share/es-de/resources/systems"
NATIVE_PREFIXES = ("/usr", "/usr/local", "/app")
APPIMAGES = (
    "~/Applications/ES-DE*.AppImage",
    "~/AppImages/ES-DE*.AppImage",
    "~/.local/bin/ES-DE*.AppImage",
    "~/bin/ES-DE*.AppImage",
)
NATIVE_BINARIES = ("/usr/bin/es-de", "/usr/local/bin/es-de")
# Decky's Python may run under box64 or FEX: a native binary tells the machine.
NATIVE_BINARY = "/bin/sh"
# The ROM folders without ES-DE's setting, first found first: ES-DE's default, then the one of
# Loadout and EmuDeck. A new device gets the last.
ROM_DIRS = ("~/ROMs", "~/Emulation/roms")
# The ROM folders of mounted SD cards and drives, searched in addition to the one above: SteamOS
# mounts a card at /run/media/<user>/<label>, such as /run/media/steamos/MicroSD, and older
# versions at /run/media/<device>, such as /run/media/mmcblk0p1.
CARD_ROM_DIRS = ("/run/media/*/*/Emulation/roms", "/run/media/*/Emulation/roms")
# Directories a desktop session puts on PATH, for the systempath rules.
SESSION_PATH = (
    "~/.local/bin", "~/bin", "/usr/local/bin", "/usr/bin", "/bin",
    "/var/lib/flatpak/exports/bin", "~/.local/share/flatpak/exports/bin",
)
ELF_AARCH64 = 183
# Characters getEscapedPath() escapes with a backslash, in its order.
ESCAPED = "\\ '\"!$^&*(){}[]?;<>"
# Field codes a .desktop Exec key may hold.
FIELD_CODES = re.compile(r"(?<!%)%[fFuUdDnNickvm]")
ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
FLATPAK_ID = re.compile(r"[A-Za-z][\w-]*(\.[\w-]+){2,}")
# The gamelist fields a scraper fills.
SCRAPED = ("desc", "developer", "publisher", "releasedate", "genre", "players", "rating")


class Unresolved(Exception):
    """A launch command whose emulator or core cannot be found."""


def escape(path):
    """Escape a path for the shell the way ES-DE's getEscapedPath() does."""
    for ch in ESCAPED:
        start = 0
        while (offset := path.find(ch, start)) != -1:
            start = offset + 1
            if offset == 0 or path[offset - 1] != "\\":
                path = path[:offset] + "\\" + path[offset:]
                start += 1
    return path


def unescape(path):
    return re.sub(r"\\(.)", r"\1", path)


def extension(name):
    dot = name.rfind(".")
    return name[dot:] if dot != -1 else "."


def stem(path):
    name = os.path.basename(path.rstrip("/"))
    if os.path.isdir(path):
        return name
    dot = name.rfind(".")
    return name[:dot] if dot != -1 else name


def words(command):
    """Split a shell command into its words, keeping quotes and escapes in each word."""
    out, word, quote, i = [], "", None, 0
    while i < len(command):
        c = command[i]
        if quote:
            word += c
            if c == "\\" and quote == '"' and i + 1 < len(command):
                word += command[i + 1]
                i += 1
            elif c == quote:
                quote = None
        elif c == "\\" and i + 1 < len(command):
            word += command[i:i + 2]
            i += 1
        elif c in "'\"":
            word += c
            quote = c
        elif c.isspace():
            if word:
                out.append(word)
            word = ""
        else:
            word += c
        i += 1
    if word:
        out.append(word)
    return out


def unquote(word):
    """Return the string a shell makes of one word."""
    out, quote, i = "", None, 0
    while i < len(word):
        c = word[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                out += c
        elif quote == '"':
            if c == '"':
                quote = None
            elif c == "\\" and i + 1 < len(word) and word[i + 1] in '$`"\\':
                out += word[i + 1]
                i += 1
            else:
                out += c
        elif c in "'\"":
            quote = c
        elif c == "\\" and i + 1 < len(word):
            out += word[i + 1]
            i += 1
        else:
            out += c
        i += 1
    return out


def elf_machine(path):
    try:
        with open(path, "rb") as f:
            header = f.read(20)
    except OSError:
        return None
    if header[:4] != b"\x7fELF":
        return None
    return int.from_bytes(header[18:20], "little")


class Parsed:
    """Parsed files, reparsed when their size or time changes."""

    def __init__(self):
        self.entries = {}

    def get(self, path, parse):
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (st.st_mtime_ns, st.st_size)
        hit = self.entries.get((path, parse))
        if hit and hit[0] == key:
            return hit[1]
        with open(path, encoding="utf-8", errors="replace") as f:
            value = parse(f.read())
        self.entries[(path, parse)] = (key, value)
        return value


def parse_settings(text):
    out = {}
    for tag in ("string", "bool", "int", "float"):
        for attrs, _ in markup.elements(text, tag):
            if "name" in attrs:
                out[attrs["name"]] = attrs.get("value", "")
    return out


def parse_systems(text):
    """Return ({name: system}, load_exclusive) from an es_systems.xml file."""
    text = markup.clean(text)
    systems = {}
    for _, inner in markup.elements(text, "system"):
        name = markup.text(inner, "name", "")
        if not name or name in systems:
            continue
        commands = []
        for attrs, body in markup.elements(inner, "command"):
            label = attrs.get("label", "")
            # ES-DE needs a label on every command once there are several.
            if commands and (not label or not commands[0][0]):
                break
            if label and any(label == known for known, _ in commands):
                continue
            commands.append((label, html.unescape(body).strip()))
        systems[name] = {
            "name": name,
            "fullname": markup.text(inner, "fullname", ""),
            "path": markup.text(inner, "path", ""),
            "extensions": re.split(r"[\s,]+", markup.text(inner, "extension", "").strip()),
            "commands": commands,
            "platform": markup.text(inner, "platform", ""),
        }
    return systems, bool(re.search(r"<loadExclusive\s*/?>", text))


def parse_rules(text):
    """Return ({emulator: {systempath, staticpath}}, {core: [corepath]}) from an es_find_rules.xml file."""
    text = markup.clean(text)
    emulators, cores = {}, {}
    for attrs, inner in markup.elements(text, "emulator"):
        name = attrs.get("name")
        if not name or name in emulators:
            continue
        rules = {"systempath": [], "staticpath": []}
        for rule, body in markup.elements(inner, "rule"):
            if rule.get("type") in rules:
                rules[rule["type"]] += markup.texts(body, "entry")
        emulators[name] = rules
    for attrs, inner in markup.elements(text, "core"):
        name = attrs.get("name")
        if not name or name in cores:
            continue
        cores[name] = [entry for rule, body in markup.elements(inner, "rule")
                       if rule.get("type") == "corepath" for entry in markup.texts(body, "entry")]
    return emulators, cores


def parse_lines(text):
    return [line.replace("\r", "") for line in text.splitlines() if line.strip()]


def parse_gamelist(text):
    """Return (system alternative emulator, {path as written: metadata}) from a gamelist.xml file."""
    text = markup.clean(text)
    alternative = None
    for _, inner in markup.elements(text, "alternativeEmulator"):
        alternative = markup.text(inner, "label") or None
    games = {}
    for _, inner in markup.elements(text, "game"):
        path = markup.text(inner, "path")
        if path:
            games[path] = {
                "name": markup.text(inner, "name"),
                "hidden": markup.text(inner, "hidden") == "true",
                "favorite": markup.text(inner, "favorite") == "true",
                "altemulator": markup.text(inner, "altemulator") or None,
                # Scraped facts, empty until ES-DE scrapes the game.
                **{k: markup.text(inner, k) or None for k in SCRAPED},
            }
    return alternative, games


class EsDe:
    def __init__(self, home, esde_home, cache_dir, copy_dir, log):
        self.home = home
        self.esde_home = self.expand(esde_home)
        self.cache_dir = cache_dir
        # The plugin's copy of ES-DE's files, one folder per platform.
        self.copy_dir = copy_dir
        self.log = log
        self.parsed = Parsed()

    def expand(self, path):
        return self.home + path[1:] if path.startswith("~") else path

    # Configuration files

    def settings(self):
        return self.parsed.get(os.path.join(self.esde_home, "settings", "es_settings.xml"), parse_settings) or {}

    def setting(self, name, default=""):
        return self.settings().get(name, default)

    def rom_dir(self):
        """Return the ROM directory with a trailing slash: ES-DE's setting, as getROMDirectory()
        reads it, else the first of ROM_DIRS found."""
        value = self.setting("ROMDirectory")
        if not value:
            dirs = [self.expand(d) for d in ROM_DIRS]
            return next((d for d in dirs if os.path.isdir(d)), dirs[-1]) + "/"
        value = self.expand(value)
        return value if value.endswith("/") else value + "/"

    def rom_dirs(self):
        """Return every ROM directory with a trailing slash: rom_dir() first, then the ROM folder of
        each mounted card. A folder that is another one's symlink, as EmuDeck makes of ~/Emulation
        when it keeps the games on the card, counts once."""
        found, real = [self.rom_dir()], set()
        real.add(os.path.realpath(found[0]))
        for pattern in CARD_ROM_DIRS:
            for folder in sorted(glob.glob(pattern)):
                if os.path.isdir(folder) and os.path.realpath(folder) not in real:
                    real.add(os.path.realpath(folder))
                    found.append(folder + "/")
        return found

    def media_dir(self):
        value = self.setting("MediaDirectory")
        return self.expand(value) if value else os.path.join(self.esde_home, "downloaded_media")

    def binary(self):
        """Return the ES-DE AppImage or binary, or None."""
        for pattern in APPIMAGES:
            found = sorted(glob.glob(self.expand(pattern)))
            if found:
                return found[0]
        return next((b for b in NATIVE_BINARIES if os.path.exists(b)), None)

    def platform(self, binary):
        return "linuxarm" if elf_machine(binary) == ELF_AARCH64 else "linux"

    def bundled(self):
        """Return the folder that holds es_systems.xml and es_find_rules.xml: the installed ES-DE's,
        else the plugin's copy for this machine."""
        binary = self.binary()
        try:
            folder = binary and self.installed(binary)
        except (OSError, Unresolved) as e:
            self.log.warning(f"ES-DE: {e}")
            folder = None
        return folder or os.path.join(self.copy_dir, self.platform(NATIVE_BINARY))

    def installed(self, binary):
        """Return the folder of an installed ES-DE's files, extracted from its AppImage, else None."""
        platform = self.platform(binary)
        if not binary.endswith(".AppImage"):
            for prefix in NATIVE_PREFIXES:
                folder = os.path.join(prefix, RESOURCES[4:], platform)
                if os.path.isfile(os.path.join(folder, "es_systems.xml")):
                    return folder
            return None
        st = os.stat(binary)
        folder = os.path.join(self.cache_dir, f"esde-{int(st.st_mtime)}-{st.st_size}", platform)
        if not os.path.isfile(os.path.join(folder, "es_find_rules.xml")):
            self.extract(binary, platform, folder)
        return folder

    def extract(self, appimage, platform, folder):
        # The AppImage runtime extracts matching files without starting ES-DE.
        os.makedirs(self.cache_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.cache_dir) as tmp:
            env = {k: v for k, v in os.environ.items() if not k.startswith(("LD_", "PYTHON"))}
            subprocess.run([appimage, "--appimage-extract", f"{RESOURCES}/{platform}/es_*.xml"],
                           cwd=tmp, env=env, capture_output=True, timeout=60, check=False)
            found = glob.glob(os.path.join(tmp, "*", RESOURCES, platform))
            if not found or not os.path.isfile(os.path.join(found[0], "es_systems.xml")):
                raise Unresolved(f"Could not read the systems inside {os.path.basename(appimage)}")
            os.makedirs(os.path.dirname(folder), exist_ok=True)
            shutil.rmtree(folder, ignore_errors=True)
            shutil.copytree(found[0], folder)

    def config_files(self, name):
        """Return the custom file, then the bundled one; ES-DE takes the first definition."""
        files = [os.path.join(self.esde_home, "custom_systems", name), os.path.join(self.bundled(), name)]
        return [f for f in files if os.path.isfile(f)]

    def systems(self):
        """Return {name: system} as ES-DE loads them, custom entries first."""
        out = {}
        for i, path in enumerate(self.config_files("es_systems.xml")):
            systems, exclusive = self.parsed.get(path, parse_systems)
            for name, system in systems.items():
                out.setdefault(name, system)
            if i == 0 and exclusive and path.startswith(self.esde_home):
                break
        return out

    def rules(self):
        emulators, cores = {}, {}
        for path in self.config_files("es_find_rules.xml"):
            found_emulators, found_cores = self.parsed.get(path, parse_rules)
            for name, rules in found_emulators.items():
                emulators.setdefault(name, rules)
            for name, paths in found_cores.items():
                cores.setdefault(name, paths)
        return emulators, cores

    def system_dirs(self, system):
        """Return the folders of a system's games, one in each ROM directory that has it, or the
        first ROM directory's when none does."""
        dirs = [os.path.normpath(self.expand(system["path"].replace("%ROMPATH%", root).replace("//", "/")))
                for root in self.rom_dirs()]
        found, real = [], set()
        for folder in dirs:
            if os.path.isdir(folder) and os.path.realpath(folder) not in real:
                real.add(os.path.realpath(folder))
                found.append(folder)
        return found or dirs[:1]

    def system_dir(self, system):
        """Return the first folder of a system's games."""
        return self.system_dirs(system)[0]

    def system_dir_of(self, system, path):
        """Return the folder of a system's games that holds a game, else the first one."""
        folders = self.system_dirs(system)
        return next((f for f in folders if path.startswith(f + "/")), folders[0])

    def rom_dir_of(self, path):
        """Return the ROM directory that holds a game, else rom_dir()."""
        dirs = self.rom_dirs()
        return next((d for d in dirs if path.startswith(d)), dirs[0])

    def gamelist(self, system):
        """Return (system alternative emulator, {absolute path: metadata})."""
        path = os.path.join(self.esde_home, "gamelists", system["name"], "gamelist.xml")
        parsed = self.parsed.get(path, parse_gamelist)
        if not parsed:
            return None, {}
        alternative, games = parsed
        roots = self.system_dirs(system)
        out = {}
        for written, meta in games.items():
            if written.startswith("./"):
                # A path relative to the system's folder can name a game in any of its folders.
                for root in roots:
                    out.setdefault(os.path.normpath(root + written[1:]), meta)
            elif written.startswith("/"):
                out[os.path.normpath(written)] = meta
        return alternative, out

    def collections(self):
        """Return {name: set of absolute paths} for ES-DE's custom collections."""
        folder = os.path.join(self.esde_home, "collections")
        out = {}
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return out
        rom_dirs = self.rom_dirs()
        for f in names:
            if not (f.startswith("custom-") and f.endswith(".cfg")) or f == "custom-.cfg":
                continue
            lines = self.parsed.get(os.path.join(folder, f), parse_lines) or []
            out[f[7:-4]] = {os.path.normpath(line.replace("%ROMPATH%", rom_dir))
                            for line in lines for rom_dir in rom_dirs}
        return out

    # Games

    def scan(self, system):
        """Return the game paths of a system the way ES-DE's populateFolder() finds them."""
        roots = [r for r in self.system_dirs(system)
                 if os.path.isdir(r) and not os.path.exists(os.path.join(r, "noload.txt"))]
        extensions = set(system["extensions"])
        show_hidden = self.setting("ShowHiddenFiles", "true") == "true"
        found, seen = [], set()

        def walk(folder, depth):
            try:
                real = os.path.realpath(folder)
                if real in seen or depth > 20:
                    return
                seen.add(real)
                entries = sorted(os.scandir(folder), key=lambda e: e.name)
            except OSError:
                return
            for entry in entries:
                if not show_hidden and entry.name.startswith("."):
                    continue
                is_dir = entry.is_dir()
                ext = extension(entry.name)
                if ext in extensions and not (is_dir and ext == "."):
                    found.append(entry.path)
                elif is_dir and not os.path.exists(os.path.join(entry.path, "noload.txt")):
                    walk(entry.path, depth + 1)

        # The folders share seen, so a folder that is another's symlink or inside it lists once.
        for root in roots:
            walk(root, 0)
        return found

    def media_name(self, system, path):
        """Return a game's media file name without extension, under its kind's folder."""
        sub = os.path.relpath(os.path.dirname(path), self.system_dir_of(system, path))
        return stem(path) if sub == "." else os.path.join(sub, stem(path))

    # Launch commands

    def command(self, system, label):
        """Return the command of a label, or the default one."""
        if label:
            for known, command in system["commands"]:
                if known == label:
                    return command
        return system["commands"][0][1] if system["commands"] else ""

    def label(self, system, meta=None):
        """Return the emulator label ES-DE uses for a game: its own, the system's, or the default."""
        alternative, _ = self.gamelist(system)
        labels = [known for known, _ in system["commands"]]
        per_game = self.setting("AlternativeEmulatorPerGame", "true") == "true"
        if per_game and meta and meta.get("altemulator") in labels:
            return meta["altemulator"]
        if alternative in labels:
            return alternative
        return labels[0] if labels else ""

    def search_path(self):
        dirs = os.environ.get("PATH", "").split(":") + [self.expand(d) for d in SESSION_PATH]
        return [d for i, d in enumerate(dirs) if d and d not in dirs[:i]]

    def which(self, name):
        for d in self.search_path():
            path = os.path.join(d, name)
            if os.path.isfile(path) or os.path.islink(path):
                return path
        return None

    def find_emulator(self, name):
        """Return (binary, explicit command or None) for an emulator's find rules."""
        emulators, _ = self.rules()
        rules = emulators.get(name)
        if not rules or not (rules["systempath"] or rules["staticpath"]):
            raise Unresolved(f"ES-DE has no find rule for {name}")
        for entry in rules["systempath"]:
            found = self.which(entry)
            if found:
                return found, None
        for entry in rules["staticpath"]:
            path, _, explicit = entry.partition("|")
            path = self.expand(path).replace("%ROMPATH%", self.rom_dir())
            if "*" in path:
                matches = sorted(glob.glob(path))
                path = matches[0] if matches else path
            if os.path.isfile(path) or os.path.islink(path):
                return path, explicit or None
        raise Unresolved(f"{name} is not installed")

    def emulator_binaries(self):
        """Return ({path: emulator}, {Flatpak id: emulator}) for every binary the find rules can name."""
        emulators, _ = self.rules()
        paths, flatpaks = {}, {}
        for name, rules in emulators.items():
            for entry in rules["systempath"]:
                if FLATPAK_ID.fullmatch(entry):
                    flatpaks.setdefault(entry, name)
                found = self.which(entry)
                if found:
                    paths.setdefault(found, name)
            for entry in rules["staticpath"]:
                path = self.expand(entry.partition("|")[0]).replace("%ROMPATH%", self.rom_dir())
                base = os.path.basename(path)
                if "/exports/bin/" in path and FLATPAK_ID.fullmatch(base):
                    flatpaks.setdefault(base, name)
                for found in sorted(glob.glob(path)) if "*" in path else [path]:
                    if os.path.isfile(found) or os.path.islink(found):
                        paths.setdefault(found, name)
        # A shortcut may name a link's target.
        for path, name in list(paths.items()):
            paths.setdefault(os.path.realpath(path), name)
        return paths, flatpaks

    def check(self, command):
        """Raise Unresolved when the emulator or the core of a command cannot be found."""
        m = re.search(r"%EMULATOR_([^%]+)%", command)
        binary = self.find_emulator(m.group(1))[0] if m else None
        self.find_core(command, binary)

    def find_core(self, command, binary):
        m = re.search(r"%CORE_([^%]+)%[/\\]([^\s\"]+)", command)
        if not m:
            return None
        _, cores = self.rules()
        paths = cores.get(m.group(1))
        if not paths:
            raise Unresolved(f"ES-DE has no core rule for {m.group(1)}")
        for folder in paths:
            path = self.expand(os.path.join(folder, m.group(2)))
            if binary:
                path = path.replace("%EMUPATH%", os.path.dirname(binary))
            if os.path.isfile(path) or os.path.islink(path):
                return path
        raise Unresolved(f"{m.group(2)} is not installed")

    def launch(self, system, rom, label):
        """Expand the command ES-DE runs for a game into a Steam shortcut.

        Return {exe, args, prefix, start_dir}: Steam runs "prefix %command% args"
        through sh with %command% standing for the quoted exe."""
        command = self.command(system, label)
        rom_path = escape(rom)
        base = stem(rom)
        rom_raw = rom
        rom_file = rom
        # A directory with a game extension launches the file of the same name inside it.
        if os.path.isdir(rom):
            inner = os.path.join(rom, os.path.basename(rom))
            if os.path.isfile(inner) or os.path.islink(inner):
                rom_path, rom_file = escape(inner), inner
                base = base.split(".")[0]
        file_name = base + extension(os.path.basename(rom_path))

        for flag in ("%RUNINBACKGROUND%", "%HIDEWINDOW%", "%ESCAPESPECIALS%"):
            command = command.replace(flag, "").lstrip()
        shortcut = False
        if "%ENABLESHORTCUTS%" in command:
            shortcut = extension(rom_raw) == ".desktop"
            command = command.replace("%ENABLESHORTCUTS%", "").lstrip()
        quoted_core = '"%CORE_' in command
        command = command.replace('"%CORE_', "%CORE_")
        if command.startswith("~"):
            command = self.home + command[1:]

        # Resolve the pre-command in place and mark the emulator's position.
        m = re.search(r"%PRECOMMAND_([^%]+)%", command)
        if m:
            binary, explicit = self.find_emulator(m.group(1))
            command = command[:m.start()] + (explicit or escape(binary)) + command[m.end():]
        mark = "\x00"
        m = re.search(r"%EMULATOR_([^%]+)%", command)
        explicit = None
        if m:
            binary, explicit = self.find_emulator(m.group(1))
            command = command[:m.start()] + mark + command[m.end():]
        else:
            # A command that names its binary: the first word, with a wildcard or a PATH lookup.
            first = words(command)[0] if command.strip() else ""
            binary = unquote(first)
            if "*" in binary:
                matches = sorted(glob.glob(binary))
                binary = matches[0] if matches else binary
            if not (os.path.isfile(binary) or os.path.islink(binary)):
                binary = self.which(binary) if "/" not in binary else None
            if not binary:
                raise Unresolved(f"{unquote(first) or 'The emulator'} is not installed")
            command = mark + command.lstrip()[len(first):]
        emu_dir = os.path.dirname(binary)

        command = command.replace("%EMUPATH%", escape(emu_dir))
        core = self.find_core(command.replace(mark, ""), binary)
        if core:
            m = re.search(r"%CORE_[^%]+%[/\\][^\s\"]+\"?" if quoted_core else r"%CORE_[^%]+%[/\\]\S+", command)
            command = command[:m.start()] + (escape(core) if " " in core else core) + command[m.end():]

        start_dir = None
        m = re.search(r'%STARTDIR%=(?:"([^"]*)"|(\S*))\s?', command)
        if m:
            start_dir = self.expand(m.group(1) if m.group(1) is not None else m.group(2))
            start_dir = (start_dir.replace("%EMUDIR%", emu_dir)
                         .replace("%GAMEDIR%", os.path.dirname(rom_file))
                         .replace("%GAMEENTRYDIR%", rom))
            command = command[:m.start()] + command[m.end():]

        while (m := re.search(r'%INJECT%=(?:"([^"]*)"|(\S*))\s?', command)):
            name = (m.group(1) if m.group(1) is not None else m.group(2)).replace("%BASENAME%", base)
            if name == "%ROM%":
                name = rom_raw
            elif name.startswith("~"):
                name = self.expand(name)
            elif not name.startswith("/"):
                name = os.path.join(os.path.dirname(rom_file), name)
            injected = ""
            try:
                if os.path.getsize(name) <= 4096:
                    with open(name, encoding="utf-8", errors="replace") as f:
                        injected = "".join(line.rstrip("\r\n") for line in f)
            except OSError:
                pass
            command = command[:m.start()] + (injected + " " if injected else "") + command[m.end():]

        if shortcut:
            exec_line, path_key = self.desktop_entry(rom_raw)
            if exec_line is None:
                raise Unresolved(f"{os.path.basename(rom_raw)} has no Exec key")
            if path_key and not start_dir:
                start_dir = path_key
            command = command.replace(mark, "").lstrip()
            rom_path = exec_line

        command = (command.replace("%ROM%", rom_path)
                   .replace("%BASENAME%", base)
                   .replace("%FILENAME%", file_name)
                   .replace("%ROMRAW%", rom_raw)
                   .replace("%ROMRAWWIN%", rom_raw.replace("/", "\\"))
                   .replace("%ROMPATH%", escape(self.rom_dir_of(rom_raw)))
                   .replace("%ESPATH%", os.path.dirname(self.binary() or ""))
                   .replace("%EMUDIR%", escape(emu_dir))
                   .replace("%GAMEDIRRAW%", os.path.dirname(rom_file))
                   .replace("%GAMEDIR%", escape(os.path.dirname(rom_file))))

        if mark in command:
            prefix, _, args = command.partition(mark)
            exe = binary
            if explicit:
                first, *rest = words(explicit)
                exe = unquote(first)
                if "/" not in exe:
                    exe = self.which(exe) or exe
                args = " ".join(rest) + args
        else:
            # A .desktop file brings its own binary.
            all_words = words(command)
            if not all_words:
                raise Unresolved(f"{os.path.basename(rom_raw)} has an empty command")
            prefix, exe = "", unquote(all_words[0])
            if "/" not in exe:
                exe = self.which(exe) or exe
            args = command.lstrip()[len(all_words[0]):]
        return {
            "exe": exe,
            "args": " ".join(words(args)),
            "prefix": " ".join(words(prefix)),
            "start_dir": start_dir,
        }

    @staticmethod
    def desktop_entry(path):
        """Return (Exec command, Path) from a .desktop file, as ES-DE parses it."""
        exec_line, path_key, valid = None, None, False
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            return None, None
        for line in lines:
            line = line.strip()
            if line.startswith("#!"):
                continue
            if "[Desktop Entry]" in line:
                valid = True
            elif line.startswith("Path=") and line[5:]:
                path_key = line[5:]
            elif line.startswith("Exec="):
                exec_line = FIELD_CODES.sub("", line[5:]).replace("%%", "%").strip()
        return (exec_line, path_key) if valid else (None, None)
