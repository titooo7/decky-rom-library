"""Game systems, tile names and Steam launch options, as ES-DE defines them."""
import collections
import json
import os
import re
import shutil
import subprocess
import time

from . import arcade, info, names, wii
from .apps import EMULATOR, FRONTEND, classify
from . import dsu
from .dolphin import Dolphin
from .eden import Eden
from .esde import EsDe, Unresolved, unquote, words
from .jsonfile import read_json, write_json
from .rdb import Facts
from .retroarch import RetroArch

# Armada's launch wrapper applies its per-game settings. Elsewhere the command runs as is.
WRAPPER = "/usr/libexec/armada/armada-game-launch"
SHELL_SAFE = re.compile(r"[\w@%+=:,./-]+")
TAGS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# An AppImage built on uruntime keeps its image mounted 5 s after the app exits, for a relaunch.
# Steam waits for the mount's FUSE process: the game ends 6 s late in Steam. 0 unmounts at exit.
UNMOUNT_AT_EXIT = "REUSE_CHECK_DELAY=0"
# The shortcut fields the plugin sets and keeps up to date.
FIELDS = ("name", "exe", "start_dir", "launch_options")
# The app hub of the SteamOS ARM port, which Loadout drives. Its launchers sit in the user's
# ~/.local/share/steamos-arm/hub/bin, one per app, named by the app's id in the hub.
HUB = ["/usr/bin/python3", "/usr/lib/steamos-arm/hub/hub.py"]
HUB_TIMEOUT = 300
# The pseudo system that holds the shortcuts which are not ROM tiles. Their paths are "app:<appid>".
APPS = "apps"
# The lines of a .cue and a .gdi that name a track file.
CUE_FILE = re.compile(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))', re.IGNORECASE)
GDI_TRACK = re.compile(r'^\s*\d+\s+\d+\s+\d+\s+\d+\s+(?:"([^"]+)"|(\S+))')


def references(path):
    """Return the files a .cue, .gdi or .m3u names, relative to its folder."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".cue", ".gdi", ".m3u") or not os.path.isfile(path):
        return []
    try:
        with open(path, errors="replace") as f:
            lines = f.read(1 << 16).splitlines()
    except OSError:
        return []
    if ext == ".m3u":
        names = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    else:
        pattern = CUE_FILE if ext == ".cue" else GDI_TRACK
        names = [m.group(1) or m.group(2) for m in map(pattern.match, lines) if m]
    return [os.path.join(os.path.dirname(path), n) for n in names]


def disk_size(paths):
    total = 0
    for p in paths:
        if os.path.isdir(p) and not os.path.islink(p):
            for folder, _, files in os.walk(p):
                total += sum(os.lstat(os.path.join(folder, f)).st_size for f in files)
        elif os.path.exists(p):
            total += os.stat(p).st_size
    return total


def quote(arg):
    # Steam runs launch options through sh -c.
    if SHELL_SAFE.fullmatch(arg):
        return arg
    return "'" + arg.replace("'", "'\"'\"'") + "'"


class Library:
    def __init__(self, plugin_dir, settings_dir, home, runtime_dir, log):
        self.plugin_dir = plugin_dir
        self.home = home
        self.log = log
        self.user_file = os.path.join(settings_dir, "systems.json")
        self.tiles_file = os.path.join(settings_dir, "tiles.json")
        self.choices_file = os.path.join(settings_dir, "art.json")
        self.settings_file = os.path.join(settings_dir, "settings.json")
        # Each app shortcut by app id: its name, its kind, whether it got artwork once and its id in the hub.
        self.apps_file = os.path.join(settings_dir, "apps.json")
        self.hub = HUB
        self.hub_bin = os.path.join(home, ".local", "share", "steamos-arm", "hub", "bin")
        self.defaults = read_json(os.path.join(plugin_dir, "systems.json"), {})
        user = read_json(self.user_file, {})
        esde_home = user.get("esde_home", self.defaults.get("esde_home", "~/ES-DE"))
        self.esde = EsDe(home, esde_home, os.path.join(runtime_dir, "esde"), os.path.join(plugin_dir, "esde"), log)
        self.facts_db = Facts(home, log)
        self.decoded = info.Files(os.path.join(runtime_dir, "files.json"), home, log)
        self.arcade = arcade.Arcade(os.path.join(plugin_dir, "facts", "fbneo.json"),
                                    os.path.join(runtime_dir, "history"), log)
        self.dolphin = Dolphin(home)
        self.eden = Eden(home)
        self.retroarch = RetroArch(home)
        self.titles = {}
        # Each tile by ROM path: its app id, its system and the shortcut fields last set.
        # An older tiles.json holds app ids alone.
        self.tiles = {path: rec if isinstance(rec, dict) else {"appid": rec}
                      for path, rec in read_json(self.tiles_file, {}).items()}
        # The image the user picked for each kind of a game's art, by ROM path.
        self.choices = read_json(self.choices_file, {})

    def expand(self, path):
        return self.home + path[1:] if path.startswith("~") else path

    def save_tiles(self):
        write_json(self.tiles_file, self.tiles)

    # Systems

    def user_systems(self):
        """Return the systems of the user's systems.json, read again when it changes."""
        try:
            return (self.esde.parsed.get(self.user_file, json.loads) or {}).get("systems", {})
        except ValueError as e:
            self.log.warning(f"{self.user_file}: {e}")
            return {}

    def extras(self, sid, user):
        return {**self.defaults.get("systems", {}).get(sid, {}), **user.get(sid, {})}

    def systems(self):
        """Return {id: system} for ES-DE's systems that have a ROM folder, then the manual ones."""
        user = self.user_systems()
        out = {}
        try:
            found = self.esde.systems()
        except (OSError, Unresolved) as e:
            self.log.warning(f"ES-DE: {e}")
            found = {}
        for sid, s in sorted(found.items(), key=lambda kv: kv[1]["fullname"].lower()):
            extras = self.extras(sid, user)
            roots = self.esde.system_dirs(s)
            root = roots[0]
            if "exe" in extras or not os.path.isdir(root):
                continue
            out[sid] = {
                "id": sid,
                "esde": s,
                "extras": extras,
                "name": extras.get("name") or s["fullname"],
                "collection": extras.get("collection") or s["fullname"],
                # The folder new games go to, and every folder that holds games, such as the
                # system's folder on an SD card.
                "root": root,
                "roots": roots,
            }
        # A system with "exe" in systems.json runs that command and ignores ES-DE's.
        for sid in sorted({*self.defaults.get("systems", {}), *user}):
            extras = self.extras(sid, user)
            if "exe" in extras and "roms" in extras:
                out[sid] = {
                    "id": sid,
                    "esde": None,
                    "extras": extras,
                    "name": extras.get("name", sid),
                    "collection": extras.get("collection"),
                    "root": os.path.normpath(self.expand(extras["roms"])),
                    "roots": [os.path.normpath(self.expand(extras["roms"]))],
                }
        return out

    @staticmethod
    def root_of(system, path):
        """Return the folder of a system's games that holds a game, else the first one."""
        return next((r for r in system["roots"] if path.startswith(r + "/")), system["root"])

    def unavailable(self, system):
        """Return why a system's games cannot start, or None."""
        s = system["esde"]
        try:
            if s:
                self.esde.check(self.esde.command(s, self.esde.label(s)))
            else:
                exe = self.expand(system["extras"]["exe"])
                if not os.path.exists(exe):
                    return f"{os.path.basename(exe)} is not installed"
        except Unresolved as e:
            return str(e)
        return None

    def system_list(self):
        out = []
        for sid, system in self.systems().items():
            games = self.games(sid, system)
            if not games:
                continue
            reason = self.unavailable(system)
            out.append({
                "id": sid,
                "name": system["name"],
                "collection": system["collection"],
                "arcade": system["extras"].get("titles") == "fbneo",
                "emulator": self.emulator(system),
                "extensions": self.extensions(system),
                "available": reason is None,
                "reason": reason,
                "games": sum(not g["missing"] for g in games),
                "tiles": sum(g["appid"] is not None for g in games),
                "missing": sum(g["missing"] for g in games),
            })
        return sorted(out, key=lambda s: s["name"].lower())

    def emulator(self, system):
        """Return the name of the emulator a system's games start in: ES-DE's label of the command,
        else the emulator its command names, else the file the system runs."""
        s = system["esde"]
        if not s:
            return os.path.basename(self.expand(system["extras"]["exe"]))
        label = self.esde.label(s)
        if label:
            return label
        first = words(self.esde.command(s, ""))[:1]
        m = re.match(r"%EMULATOR_(\w+)%", first[0]) if first else None
        return m[1].replace("_", " ").title() if m else (os.path.basename(first[0]) if first else "")

    def runs(self, system, emulator):
        """Tell whether a system's games start in an emulator of ES-DE's find rules, such as DOLPHIN for
        ES-DE's Wii and GameCube."""
        s = system["esde"]
        command = [w for w in words(self.esde.command(s, self.esde.label(s)))
                   if not w.startswith("%INJECT%")] if s else []
        return command[:1] == [f"%EMULATOR_{emulator}%"]

    def core(self, system):
        """Return the RetroArch core a system's games start with, such as fbneo_libretro.so, else None."""
        s = system["esde"]
        m = s and re.search(r"%CORE_RETROARCH%[/\\]([^\s\"]+)", self.esde.command(s, self.esde.label(s)))
        return m[1] if m and self.runs(system, "RETROARCH") else None

    def emulator_settings(self, sid, path):
        """Return the game's settings that the plugin changes in its emulator, with the global values
        and whether the DSU server runs, else None. Dolphin's come with the disc's console: "wii" or
        "gc". Eden's need the game's id, read from its file. RetroArch's need the core's .info file."""
        system = self.systems()[sid]
        if self.runs(system, "DOLPHIN"):
            game_id, console = self.disc(sid, path)
            return {**self.dolphin.settings(game_id), "emulator": "Dolphin", "console": console,
                    "dsu": dsu.running()}
        if self.runs(system, "EDEN"):
            game_id = self.switch_id(sid, path)
            return game_id and {**self.eden.settings(game_id), "emulator": "Eden", "dsu": dsu.running()}
        core = self.core(system)
        if core:
            got = self.retroarch.settings(core, self.game(sid, path)["path"])
            return got and {**got, "emulator": "RetroArch"}
        return None

    def set_emulator_setting(self, sid, path, key, value):
        """Set one of the game's settings in its emulator, or bring back the global value with None."""
        system = self.systems()[sid]
        if self.runs(system, "DOLPHIN"):
            self.dolphin.set(self.disc(sid, path)[0], key, value)
        elif self.runs(system, "EDEN") and self.switch_id(sid, path):
            self.eden.set(self.switch_id(sid, path), key, value)
        elif self.core(system):
            self.retroarch.set(self.core(system), self.game(sid, path)["path"], key, value)
        else:
            raise ValueError(f"no settings for {os.path.basename(path)}")

    def switch_id(self, sid, path):
        """Return a Switch game's title id, such as 01007EF00011E000, else None."""
        return self.decoded.facts(sid, self.game(sid, path)["path"]).get("serial")

    def updates(self, system):
        """Return the folder of a Switch system's updates and DLC, made when missing, else None. ES-DE
        skips a folder that holds a noload.txt."""
        folder = system["esde"] and self.esde.system_dir(system["esde"])
        if not folder or not os.path.isdir(folder):
            return None
        updates = os.path.join(folder, "updates")
        os.makedirs(updates, exist_ok=True)
        if not os.path.exists(os.path.join(updates, "noload.txt")):
            open(os.path.join(updates, "noload.txt"), "w").close()
        return updates

    def setup(self):
        """Give the installed emulators the handheld's defaults at the plugin's start, for the tiles
        made before. Only Eden's for now. A failure leaves the emulator as it is."""
        try:
            switch = next((s for s in self.systems().values() if self.runs(s, "EDEN")), None)
            for path in self.eden.setup(switch and self.updates(switch)):
                self.log.info(f"Eden's handheld defaults: {path}")
        except OSError as e:
            self.log.warning(f"Eden's defaults: {e}")

    def prepare(self, system, sid, path):
        """Give a game's emulator the handheld's defaults before the game runs: Dolphin's and Eden's,
        with the game's swapped buttons. A failure leaves the emulator as it is."""
        try:
            if self.runs(system, "DOLPHIN"):
                self.dolphin.prepare(self.disc(sid, path)[0])
            elif self.runs(system, "EDEN"):
                self.eden.prepare(self.switch_id(sid, path), self.updates(system))
        except (OSError, ValueError, wii.Unreadable) as e:
            self.log.warning(f"The emulator's defaults for {os.path.basename(path)}: {e}")

    def disc(self, sid, path):
        """Return the id on a Wii or GameCube disc, such as SMNP01, and its console: "wii" or "gc"."""
        head = wii.header(self.game(sid, path)["path"])
        serial = wii.parse(head).get("serial")
        if not serial:
            raise ValueError(f"{os.path.basename(path)} has no game id")
        return serial, wii.console(head)

    def extensions(self, system):
        """Return the extensions of a system's games, lowercase and without the dot."""
        found = system["esde"]["extensions"] if system["esde"] else system["extras"].get("extensions", [])
        return sorted({e.strip().lstrip(".").lower() for e in found if e.strip().lstrip(".")})

    def import_game(self, sid, path):
        """Move a game's file or folder into its system's folder, with the files its .cue, .gdi or
        .m3u names beside it. Return the game's path there."""
        system = self.systems()[sid]
        path = os.path.normpath(path)
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        if not os.path.isdir(path) and ext not in self.extensions(system):
            raise ValueError(f"{system['name']} takes no .{ext} file")
        source = os.path.dirname(path)
        found, todo = [], [path]
        while todo:
            p = os.path.normpath(todo.pop(0))
            if p in found or not os.path.lexists(p) or not p.startswith(source + os.sep):
                continue
            found.append(p)
            todo.extend(references(p))
        if not found:
            raise FileNotFoundError(f"{path} is gone")
        moves = [(p, os.path.join(system["root"], os.path.relpath(p, source))) for p in found]
        taken = next((t for _, t in moves if os.path.lexists(t)), None)
        if taken:
            raise FileExistsError(f"{os.path.relpath(taken, system['root'])} is already in the {system['name']} folder")
        for p, t in moves:
            os.makedirs(os.path.dirname(t), exist_ok=True)
            shutil.move(p, t)
        return moves[0][1]

    def system_of(self, path, systems):
        return next((sid for sid, s in systems.items() if any(path.startswith(r + "/") for r in s["roots"])), None)

    # Games

    def titles_for(self, system):
        name = system["extras"].get("titles")
        if not name:
            return None
        if name not in self.titles:
            self.titles[name] = read_json(os.path.join(self.plugin_dir, "titles", name + ".json"), {})
        return self.titles[name]

    def scan(self, system):
        if system["esde"]:
            return self.esde.scan(system["esde"])
        extensions = tuple(e.lower() for e in system["extras"].get("extensions", []))
        try:
            names = sorted(os.listdir(system["root"]))
        except OSError:
            return []
        return [os.path.join(system["root"], f) for f in names
                if not f.startswith(".") and os.path.isfile(os.path.join(system["root"], f))
                and (not extensions or f.lower().endswith(extensions))]

    def stamp(self, sid):
        """Return a value that changes when a game comes into or leaves a system's ROM folder: the
        number of its folders and their latest modification time."""
        times = []
        for root in self.systems()[sid]["roots"]:
            for folder, _, _ in os.walk(root):
                try:
                    times.append(os.stat(folder).st_mtime_ns)
                except OSError:
                    pass
        return f"{len(times)}:{max(times, default=0)}"

    def metadata(self, system):
        return self.esde.gamelist(system["esde"])[1] if system["esde"] else {}

    def entry(self, system, path, meta, titles):
        """Return a game's record, or None for a BIOS or a set its DAT does not list."""
        file = os.path.basename(path)
        set_name = os.path.splitext(file)[0]
        title = None
        if titles is not None:
            title = titles.get(set_name)
            # BIOS sets map to null. The FBNeo core runs no set missing from its DAT.
            if title is None and (set_name in titles or system["extras"].get("dat_only")):
                return None
        # ES-DE lists a game it has not scraped under its file name.
        name = meta.get("name")
        if not name or name == set_name:
            name = TAGS.sub("", title or set_name).strip() or set_name
        tile = self.tiles.get(path)
        return {
            "path": path,
            "file": file,
            "stem": set_name,
            "title": title or set_name,
            "name": name,
            "appid": tile["appid"] if tile else None,
            "favorite": bool(meta.get("favorite")),
            "meta": meta,
            "missing": False,
        }

    def games(self, sid, system=None):
        system = system or self.systems()[sid]
        meta = self.metadata(system)
        titles = self.titles_for(system)
        found = []
        for path in self.scan(system):
            m = meta.get(path, {})
            # A hidden game stays listed while it has a tile, so the tile can go.
            if m.get("hidden") and path not in self.tiles:
                continue
            g = self.entry(system, path, m, titles)
            if g:
                found.append(g)
        counts = collections.Counter(g["name"] for g in found)
        for g in found:
            if counts[g["name"]] > 1:
                g["name"] = g["title"]
        known = {g["path"] for g in found}
        for path, tile in self.tiles.items():
            if path in known or (tile.get("system") or self.system_of(path, {sid: system})) != sid:
                continue
            name = tile.get("applied", {}).get("name") or os.path.splitext(os.path.basename(path))[0]
            found.append({"path": path, "file": os.path.basename(path), "stem": "", "title": name,
                          "name": name, "appid": tile["appid"], "favorite": False, "meta": {},
                          "missing": True})
        return sorted(found, key=lambda g: g["name"].lower())

    def game(self, sid, path, systems=None):
        system = (systems or self.systems())[sid]
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        g = self.entry(system, path, self.metadata(system).get(path, {}), self.titles_for(system))
        if not g:
            raise ValueError(f"{os.path.basename(path)} is not a game")
        return g

    def rom_files(self, system, path):
        """Return a game's file or folder, then the files its .cue, .gdi or .m3u names in the system's folder."""
        root = os.path.realpath(self.root_of(system, path))
        found, todo = [], [path]
        while todo:
            p = os.path.normpath(todo.pop(0))
            # A symlink counts by where it sits: deleting it leaves its target.
            folder = os.path.realpath(os.path.dirname(p))
            if p in found or not os.path.lexists(p) or not (folder + os.sep).startswith(root + os.sep):
                continue
            found.append(p)
            todo.extend(references(p))
        return found

    def files(self, sid, path):
        """Return a game's files relative to its system's folder, and their size in bytes."""
        system = self.systems()[sid]
        found = self.rom_files(system, self.game(sid, path)["path"])
        root = self.root_of(system, path)
        return {"files": [os.path.relpath(p, root) for p in found], "size": disk_size(found)}

    def uninstall(self, sid, path):
        """Delete a game's files and forget the images picked for its tile."""
        system = self.systems()[sid]
        for p in self.rom_files(system, self.game(sid, path)["path"]):
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            elif os.path.lexists(p):
                os.remove(p)
        if self.choices.pop(path, None) is not None:
            write_json(self.choices_file, self.choices)

    # Steam shortcuts

    def command(self, system, game):
        """Return the shortcut fields that start a game: exe, start_dir and launch_options."""
        if system["esde"]:
            s = system["esde"]
            got = self.esde.launch(s, game["path"], self.esde.label(s, game["meta"]))
            exe, args, prefix, start = got["exe"], got["args"], got["prefix"], got["start_dir"]
        else:
            extras = system["extras"]
            exe = self.expand(extras["exe"])
            args = " ".join(quote(self.expand(a.replace("{rom}", game["path"]))) for a in extras.get("args", []))
            prefix, start = "", extras.get("start_dir") and self.expand(extras["start_dir"])
        if start and not os.path.isdir(start):
            # ES-DE creates a missing start folder on launch.
            try:
                os.makedirs(start, exist_ok=True)
            except OSError as e:
                self.log.warning(f"start folder {start}: {e}")
        # Environment assignments go first, ahead of Armada's wrapper. Steam puts the words before
        # %command% ahead of its own launch wrapper, as on the SteamOS ARM port.
        prefix_words = words(prefix)
        env = [UNMOUNT_AT_EXIT]
        while prefix_words and ENV_ASSIGNMENT.match(prefix_words[0]):
            env.append(prefix_words.pop(0))
        wrapper = [WRAPPER] if os.path.exists(WRAPPER) else []
        parts = env + wrapper + prefix_words + ["%command%", args]
        return {
            "exe": exe,
            "start_dir": start or os.path.dirname(exe) + "/",
            "launch_options": " ".join(p for p in parts if p),
        }

    def shortcut(self, sid, path):
        """Return the fields of a game's Steam shortcut, for a new tile or the hidden launcher. The
        game's emulator gets its defaults first."""
        systems = self.systems()
        system = systems[sid]
        game = self.game(sid, path, systems)
        self.prepare(system, sid, game["path"])
        return {
            "name": game["name"],
            **self.command(system, game),
            "collection": system["collection"],
            "favorite": game["favorite"],
            "collections": self.collections_of(path),
        }

    def collections_of(self, path):
        return sorted(name for name, paths in self.esde.collections().items() if path in paths)

    def sync_plan(self):
        """Compare each tile with its game and return what to change in Steam."""
        systems = self.systems()
        plan = []
        dirty = False
        for path, tile in self.tiles.items():
            sid = tile.get("system") or self.system_of(path, systems)
            if sid and tile.get("system") != sid:
                tile["system"] = sid
                dirty = True
            item = {"path": path, "appid": tile["appid"], "system": sid, "missing": False,
                    "want": None, "changed": [], "favorite": False, "collections": []}
            plan.append(item)
            system = systems.get(sid)
            if not system or not os.path.exists(path):
                item["missing"] = True
                continue
            try:
                game = self.game(sid, path, systems)
                want = {"name": game["name"], **self.command(system, game)}
            except (OSError, ValueError, Unresolved) as e:
                item["error"] = str(e)
                continue
            if "applied" not in tile:
                # Keep the name of a tile made before ES-DE sync: the user may have renamed it.
                tile["applied"] = {"name": want["name"]}
                dirty = True
            item["want"] = want
            item["changed"] = [k for k in FIELDS if tile["applied"].get(k) != want[k]]
            item["favorite"] = game["favorite"]
            item["collections"] = [c for c in [system["collection"], *self.collections_of(path)] if c]
        if dirty:
            self.save_tiles()
        return plan

    def record(self, path, appid, system, applied):
        self.tiles[path] = {"appid": appid, "system": system, "applied": {k: applied[k] for k in FIELDS}}
        self.save_tiles()

    def set_applied(self, path, applied):
        if path in self.tiles:
            self.tiles[path]["applied"] = {k: applied[k] for k in FIELDS}
            self.save_tiles()

    def forget(self, path, appid=None):
        # A list read before a tile was added names no app id or an older one: keep the new tile.
        tile = self.tiles.get(path)
        if tile and (appid is None or tile["appid"] == int(appid)):
            del self.tiles[path]
            self.save_tiles()

    # Shortcuts that are not ROM tiles

    def settings(self):
        return read_json(self.settings_file, {})

    # The hidden shortcut that runs a game without a tile.

    def launcher(self):
        return self.settings().get("launcher")

    def set_launcher(self, appid):
        settings = self.settings()
        settings["launcher"] = int(appid)
        write_json(self.settings_file, settings)

    def apps_plan(self, shortcuts):
        """Classify the shortcuts Steam lists, {appid, name, exe, launch_options, art}, minus the tiles.

        Return {apps: [{appid, name, kind, art}]}: art says whether an app still needs artwork."""
        tiles = {t["appid"] for t in self.tiles.values()} | {self.launcher()}
        try:
            binaries, flatpaks = self.esde.emulator_binaries()
        except (OSError, Unresolved) as e:
            self.log.warning(f"ES-DE rules: {e}")
            binaries, flatpaks = {}, {}
        frontend = self.esde.binary()
        known = read_json(self.apps_file, {})
        apps, out = {}, []
        for shortcut in shortcuts:
            appid = int(shortcut["appid"])
            if appid in tiles:
                continue
            kind = classify(shortcut, binaries, flatpaks, frontend)
            had_art = known.get(str(appid), {}).get("art", False) or bool(shortcut.get("art"))
            apps[str(appid)] = {"name": shortcut["name"], "kind": kind, "art": had_art,
                                "hub": self.hub_app(shortcut.get("exe") or "")}
            out.append({
                "appid": appid,
                "name": shortcut["name"],
                "kind": kind,
                "art": not had_art,
            })
        write_json(self.apps_file, apps)
        return {"apps": out}

    def app_art_done(self, appid):
        apps = read_json(self.apps_file, {})
        if str(appid) in apps:
            apps[str(appid)]["art"] = True
            write_json(self.apps_file, apps)

    def app_list(self):
        """Return the emulators and the front-ends, which know how to show in Steam: other apps stay out."""
        apps = read_json(self.apps_file, {})
        order = {EMULATOR: 0, FRONTEND: 1}
        out = [{"path": f"app:{appid}", "file": a["kind"], "name": a["name"], "appid": int(appid),
                "kind": a["kind"], "hub": a.get("hub"), "thumb": None, "favorite": False, "missing": False}
               for appid, a in apps.items() if a["kind"] in order]
        return sorted(out, key=lambda a: (order[a["kind"]], a["name"].lower()))

    def describe(self, appid):
        """Return what the plugin knows of a Steam app: {tile, system} for a tile, {kind, hub} for an
        app it lists, {kind: "launcher"} for the hidden launcher, else None."""
        appid = int(appid)
        tile = next(((p, t) for p, t in self.tiles.items() if t["appid"] == appid), None)
        if tile:
            return {"tile": tile[0], "system": tile[1].get("system")}
        if appid == self.launcher():
            return {"kind": "launcher"}
        app = read_json(self.apps_file, {}).get(str(appid))
        return {"kind": app["kind"], "hub": app.get("hub")} if app else None

    def hub_app(self, exe):
        """Return the hub's id for an app whose shortcut starts one of the hub's launchers, else None."""
        exe = unquote(exe)
        if os.path.dirname(exe) != self.hub_bin or not os.path.isfile(exe) or not os.path.isfile(self.hub[-1]):
            return None
        return os.path.basename(exe)

    def run_hub(self, *args):
        """Run a hub command as Loadout does and return its JSON answer."""
        env = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH")}
        env["HOME"] = self.home
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        r = subprocess.run([*self.hub, *args], capture_output=True, text=True, timeout=60, env=env)
        out = json.loads(r.stdout or "{}")
        if r.returncode or "error" in out:
            raise RuntimeError(out.get("error") or r.stderr.strip() or f"hub {args[0]} failed")
        return out

    def uninstall_app(self, appid):
        """Remove an app the hub installed and the hub's record of its Steam shortcut.

        The hub's remove job runs in its own process: this waits for it. Steam's shortcut stays for
        the caller to remove."""
        apps = read_json(self.apps_file, {})
        hub = apps.get(str(appid), {}).get("hub")
        if not hub:
            raise ValueError(f"{appid} is not an app of the hub")
        job = self.run_hub("remove", hub)
        deadline = time.monotonic() + HUB_TIMEOUT
        while True:
            done = next((j for j in self.run_hub("jobs")["jobs"] if j["id"] == job["id"]), None)
            if not done or done["state"] != "running":
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f"the hub still removes {hub}")
            time.sleep(1)
        if not done or done["state"] != "done":
            raise RuntimeError((done or {}).get("error") or f"the hub did not remove {hub}")
        self.run_hub("steam-gone", hub)
        apps.pop(str(appid), None)
        write_json(self.apps_file, apps)

    def app_media(self, path):
        app = read_json(self.apps_file, {}).get(path.partition(":")[2])
        if not app:
            raise KeyError(path)
        name = app["name"]
        # SteamGridDB names emulators and programs with a suffix.
        names = ([f"{name} (Emulator)"] if app["kind"] == EMULATOR else []) + [f"{name} (Program)", name]
        return {"stem": "", "title": name, "name": name, "art": ["sgdb"], "libretro": None,
                "esde_media": None, "sgdb_names": names}

    # Artwork

    def media(self, sid, path):
        if sid == APPS:
            return self.app_media(path)
        systems = self.systems()
        return self.media_for(systems[sid], self.game(sid, path, systems))

    def media_for(self, system, game):
        extras = system["extras"]
        libretro = extras.get("libretro")
        art = extras.get("art") or (["esde", "libretro", "sgdb"] if libretro else ["esde", "sgdb"])
        esde = system["esde"]
        return {
            "stem": self.esde.media_name(esde, game["path"]) if esde else game["stem"],
            "title": game["title"],
            "name": game["name"],
            "art": art,
            "libretro": libretro,
            "esde_media": os.path.join(self.esde.media_dir(), system["id"]) if esde else None,
        }

    def info(self, sid, path):
        """Return what the game's page shows of a game, and the source of each fact: its file, its
        file name's tags, ES-DE's scraped facts, then FinalBurn Neo and history.xml, or libretro's
        database."""
        systems = self.systems()
        system = systems[sid]
        game = self.game(sid, path, systems)
        own = dict(self.decoded.facts(sid, game["path"]))
        if sid == "switch" and own.get("serial") and own.get("version"):
            update = info.switch_update(self.root_of(system, game["path"]), own["serial"])
            version = update and self.decoded.facts(sid, update).get("version")
            if version and version != own["version"]:
                own["version"] = f"{own['version']}, update {version}"
        layers = [("file", own), ("name", names.from_file_name(game["file"])), ("esde", info.scraped(game["meta"]))]
        if system["extras"].get("titles") == "fbneo":
            layers += self.arcade.layers(game["stem"])
        elif system["extras"].get("libretro"):
            found = self.facts_db.lookup(system["extras"]["libretro"], game["file"], [game["title"], game["name"]])
            if found.get("year"):
                found["released"] = str(found.pop("year"))
            layers.append(("libretro", found))
        out, sources = info.merge(layers)
        out["sources"] = sources
        return out

    def refresh_info(self):
        """Decode the files that changed since the last start and check history.xml for a new version.
        Return a summary for the log."""
        systems = self.systems()
        games, arcade_games, scraped = [], 0, 0
        for sid, system in systems.items():
            for g in self.games(sid, system):
                if g["missing"]:
                    continue
                if info.decodable(sid, g["path"]):
                    games.append((sid, g["path"]))
                if system["extras"].get("titles") == "fbneo":
                    arcade_games += 1
                scraped += bool(info.scraped(g["meta"]))
        start = time.monotonic()
        decoded, failed = self.decoded.refresh(games)
        files_s = time.monotonic() - start
        history = self.arcade.history.version()
        if arcade_games:
            try:
                self.arcade.history.check()
                history = self.arcade.history.version()
            except (OSError, ValueError) as e:
                self.log.warning(f"history.xml: {e}")
        return {"decoded": decoded, "failed": failed, "scraped": scraped, "history": history,
                "files_s": round(files_s, 2), "history_s": round(time.monotonic() - start - files_s, 2)}

    def choice(self, path, kind):
        return self.choices.get(path, {}).get(kind)

    def choose(self, path, kind, image_id):
        self.choices.setdefault(path, {})[kind] = image_id
        write_json(self.choices_file, self.choices)
