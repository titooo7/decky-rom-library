"""Tests for ES-DE parsing and launch commands. Run with: python3 -I tests/test_esde.py"""
import logging
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from rom_library import esde, markup  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures", "esde")
# The plugin's copy of ES-DE's files, and the one for Linux on ARM that the tests read.
COPY = os.path.join(HERE, "..", "backend", "defaults", "esde")
ARM_FILES = os.path.join(COPY, "linuxarm")
TUNIC = "TUNIC [0100DA801624E000][v0][US](nsw2u.in).nsp"


def touch(path, text="", mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


class Home:
    """A home folder laid out like the Odin's, with ES-DE's real files."""

    def __init__(self):
        self.root = tempfile.mkdtemp()
        self.home = os.path.join(self.root, "home")
        self.bin = os.path.join(self.root, "bin")
        es = os.path.join(self.home, "ES-DE")
        shutil.copytree(os.path.join(FIXTURES, "custom_systems"), os.path.join(es, "custom_systems"))
        shutil.copytree(os.path.join(FIXTURES, "gamelists"), os.path.join(es, "gamelists"))
        shutil.copytree(os.path.join(FIXTURES, "settings"), os.path.join(es, "settings"))
        for name in ("org.libretro.RetroArch", "org.ppsspp.PPSSPP", "org.flycast.Flycast",
                     "org.DolphinEmu.dolphin-emu", "bash"):
            touch(os.path.join(self.bin, name), mode=0o755)
        touch(os.path.join(self.home, "Applications", "Eden.AppImage"), mode=0o755)
        cores = os.path.join(self.home, ".var/app/org.libretro.RetroArch/config/retroarch/cores")
        for core in ("fbneo_libretro.so", "parallel_n64_libretro.so", "vice_x64sc_libretro.so",
                     "mednafen_psx_libretro.so"):
            touch(os.path.join(cores, core))
        self.roms = os.path.join(self.home, "ROMs")
        for rel in ("fbneo/marble.zip", "fbneo/.hidden.zip", "fbneo/readme.txt", f"switch/{TUNIC}",
                    "switch/updates/zelda-update.nsp", "switch/updates/noload.txt",
                    "c64/defender/Side A.d64", "c64/defender/Side B.d64",
                    "psp/LittleBigPlanet (Europe) (PSP) (PSN).iso",
                    "wii/Luigi's Mansion (Europe).rvz",
                    "psx/Final Fantasy VII.m3u/Final Fantasy VII.m3u",
                    "psx/Final Fantasy VII.m3u/Final Fantasy VII (Disc 1).chd",
                    "n64/Super Mario 64 (Europe) (En,Fr,De).zip"):
            touch(os.path.join(self.roms, rel))
        os.environ["PATH"] = self.bin
        self.es = esde.EsDe(self.home, "~/ES-DE", os.path.join(self.root, "cache"), COPY, logging.getLogger())
        self.es.bundled = lambda: ARM_FILES

    def rom(self, rel):
        return os.path.join(self.roms, rel)

    def cleanup(self):
        shutil.rmtree(self.root)


class MarkupTest(unittest.TestCase):
    def test_elements_skip_longer_tags_and_comments(self):
        text = markup.clean("<systemList><!-- <system><name>x</name></system> -->"
                            "<system><name>a &amp; b</name></system><system/></systemList>")
        self.assertEqual([markup.text(inner, "name") for _, inner in markup.elements(text, "system")],
                         ["a & b", None])

    def test_attributes(self):
        attrs, _ = next(markup.elements('<string name="ROMDirectory" value="~/R &amp; M" />', "string"))
        self.assertEqual(attrs, {"name": "ROMDirectory", "value": "~/R & M"})


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.h = Home()

    def tearDown(self):
        self.h.cleanup()

    def test_systems_put_custom_entries_first(self):
        systems = self.h.es.systems()
        self.assertGreater(len(systems), 190)
        self.assertEqual(systems["ps2"]["commands"][0][0], "ARMSX2")
        self.assertEqual(systems["fbneo"]["commands"],
                         [("FinalBurn Neo", "%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/fbneo_libretro.so %ROM%")])
        self.assertIn(".nsp", systems["switch"]["extensions"])

    def test_rules_put_custom_entries_first(self):
        emulators, cores = self.h.es.rules()
        self.assertIn("~/Applications/Cemu*.AppImage", emulators["CEMU"]["staticpath"])
        self.assertIn("~/.var/app/org.libretro.RetroArch/config/retroarch/cores", cores["RETROARCH"])

    def test_settings(self):
        self.assertEqual(self.h.es.rom_dir(), self.h.home + "/ROMs/")
        self.assertEqual(self.h.es.media_dir(), self.h.home + "/ES-DE/downloaded_media")

    def test_rom_dir_without_esde(self):
        shutil.rmtree(self.h.roms)
        os.remove(os.path.join(self.h.home, "ES-DE/settings/es_settings.xml"))
        self.assertEqual(self.h.es.rom_dir(), self.h.home + "/Emulation/roms/")
        os.makedirs(self.h.roms)
        self.assertEqual(self.h.es.rom_dir(), self.h.home + "/ROMs/")

    def with_card(self, *games):
        """Make a card's ROM folder with games and return it, with the plugin searching for cards there."""
        card = os.path.join(self.h.root, "run/media/steamos/MicroSD/Emulation/roms")
        for rel in games:
            touch(os.path.join(card, rel))
        self.addCleanup(setattr, esde, "CARD_ROM_DIRS", esde.CARD_ROM_DIRS)
        esde.CARD_ROM_DIRS = (os.path.join(self.h.root, "run/media/*/*/Emulation/roms"),)
        return card

    def test_cards_add_their_rom_folders(self):
        card = self.with_card("fbneo/pacman.zip", "psp/Other (Europe).iso")
        self.assertEqual(self.h.es.rom_dirs(), [self.h.home + "/ROMs/", card + "/"])
        # ES-DE's setting stays the first, where new games go.
        self.assertEqual(self.h.es.rom_dir(), self.h.home + "/ROMs/")
        fbneo = self.h.es.systems()["fbneo"]
        self.assertEqual(self.h.es.system_dirs(fbneo), [self.h.roms + "/fbneo", card + "/fbneo"])
        found = self.h.es.scan(fbneo)
        self.assertIn(self.h.rom("fbneo/marble.zip"), found)
        self.assertIn(card + "/fbneo/pacman.zip", found)
        self.assertEqual(self.h.es.media_name(fbneo, card + "/fbneo/pacman.zip"), "pacman")

    def test_a_card_alone_holds_the_games(self):
        shutil.rmtree(self.h.roms)
        card = self.with_card("psp/Other (Europe).iso")
        psp = self.h.es.systems()["psp"]
        self.assertEqual(self.h.es.system_dirs(psp), [card + "/psp"])
        self.assertEqual(self.h.es.scan(psp), [card + "/psp/Other (Europe).iso"])

    def test_a_card_folder_that_is_a_link_counts_once(self):
        card = self.with_card()
        os.makedirs(os.path.dirname(card))
        os.symlink(self.h.roms, card)
        self.assertEqual(self.h.es.rom_dirs(), [self.h.home + "/ROMs/"])
        fbneo = self.h.es.systems()["fbneo"]
        self.assertEqual(self.h.es.scan(fbneo).count(self.h.rom("fbneo/marble.zip")), 1)

    def test_launch_gives_the_rom_directory_of_the_game(self):
        card = self.with_card("fbneo/pacman.zip")
        fbneo = self.h.es.systems()["fbneo"]
        fbneo = {**fbneo, "commands": [("t", "%EMULATOR_RETROARCH% -rompath %ROMPATH% %ROM%")]}
        got = self.h.es.launch(fbneo, card + "/fbneo/pacman.zip", "t")
        self.assertEqual(got["args"], f"-rompath {card}/ {card}/fbneo/pacman.zip")

    def native(self, machine):
        sh = os.path.join(self.h.root, "sh")
        with open(sh, "wb") as f:
            f.write(b"\x7fELF" + bytes(14) + machine.to_bytes(2, "little"))
        esde.NATIVE_BINARY = sh

    def test_without_esde_the_plugin_reads_its_copy_for_the_machine(self):
        es = esde.EsDe(self.h.home, "~/ES-DE", os.path.join(self.h.root, "cache"), COPY, logging.getLogger())
        try:
            for machine, platform in ((esde.ELF_AARCH64, "linuxarm"), (62, "linux")):
                self.native(machine)
                self.assertEqual(es.bundled(), os.path.join(COPY, platform))
                self.assertIn("switch", es.systems())
                self.assertIn("EDEN", es.rules()[0])
            # An AppImage that does not extract leaves the copy.
            touch(os.path.join(self.h.home, "Applications", "ES-DE_x64.AppImage"), "#!/bin/sh\nexit 1\n", 0o755)
            self.assertEqual(es.bundled(), os.path.join(COPY, "linux"))
        finally:
            esde.NATIVE_BINARY = "/bin/sh"

    def test_gamelist(self):
        systems = self.h.es.systems()
        alternative, games = self.h.es.gamelist(systems["psp"])
        self.assertEqual(alternative, "PPSSPP (Standalone)")
        self.assertIn(self.h.rom("psp/LittleBigPlanet (Europe) (PSP) (PSN).iso"), games)
        self.assertEqual(self.h.es.gamelist(systems["wii"]), ("Dolphin (Standalone)", {}))

    def test_collections(self):
        touch(os.path.join(self.h.home, "ES-DE/collections/custom-Shooters.cfg"),
              "%ROMPATH%/fbneo/marble.zip\r\n\n")
        self.assertEqual(self.h.es.collections(), {"Shooters": {self.h.rom("fbneo/marble.zip")}})

    def test_scan(self):
        systems = self.h.es.systems()
        self.assertEqual(self.h.es.scan(systems["switch"]), [self.h.rom(f"switch/{TUNIC}")])
        self.assertEqual(len(self.h.es.scan(systems["c64"])), 2)
        self.assertEqual(self.h.es.scan(systems["psx"]), [self.h.rom("psx/Final Fantasy VII.m3u")])
        # ShowHiddenFiles is on in the Odin's settings.
        self.assertEqual(len(self.h.es.scan(systems["fbneo"])), 2)


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self.h = Home()
        self.systems = self.h.es.systems()

    def tearDown(self):
        self.h.cleanup()

    def launch(self, system, rel, label=None):
        s = self.systems[system]
        return self.h.es.launch(s, self.h.rom(rel), label or self.h.es.label(s))

    def test_escape_matches_esde_log(self):
        self.assertEqual(esde.escape("/var/home/armada/ROMs/switch/" + TUNIC),
                         r"/var/home/armada/ROMs/switch/TUNIC\ \[0100DA801624E000\]\[v0\]\[US\]\(nsw2u.in\).nsp")

    def test_retroarch_core(self):
        got = self.launch("fbneo", "fbneo/marble.zip")
        cores = self.h.home + "/.var/app/org.libretro.RetroArch/config/retroarch/cores"
        self.assertEqual(got, {
            "exe": self.h.bin + "/org.libretro.RetroArch",
            "args": f"-L {cores}/fbneo_libretro.so {self.h.rom('fbneo/marble.zip')}",
            "prefix": "",
            "start_dir": None,
        })

    def test_appimage_with_escaped_rom(self):
        got = self.launch("switch", f"switch/{TUNIC}")
        self.assertEqual(got["exe"], self.h.home + "/Applications/Eden.AppImage")
        self.assertEqual(got["args"], "-f -g " + esde.escape(self.h.rom(f"switch/{TUNIC}")))

    def test_system_alternative_emulator(self):
        got = self.launch("psp", "psp/LittleBigPlanet (Europe) (PSP) (PSN).iso")
        self.assertEqual(got["exe"], self.h.bin + "/org.ppsspp.PPSSPP")
        self.assertEqual(self.h.es.label(self.systems["dreamcast"]), "Flycast (Standalone)")

    def test_inject_before_emulator(self):
        rom = "wii/Luigi's Mansion (Europe).rvz"
        self.assertEqual(self.launch("wii", rom)["prefix"], "")
        touch(self.h.rom("wii/Luigi's Mansion (Europe).esprefix"), "WINEDEBUG=-all\n")
        got = self.launch("wii", rom)
        self.assertEqual(got["prefix"], "WINEDEBUG=-all")
        self.assertEqual(got["args"], "-b -e " + esde.escape(self.h.rom(rom)))

    def test_missing_core(self):
        with self.assertRaisesRegex(esde.Unresolved, "flycast_libretro.so is not installed"):
            self.launch("dreamcast", "fbneo/marble.zip", "Flycast")
        with self.assertRaisesRegex(esde.Unresolved, "MUPEN64PLUS is not installed"):
            self.launch("n64", "n64/Super Mario 64 (Europe) (En,Fr,De).zip", "Mupen64Plus (Standalone)")

    def test_start_dir_and_rom_path(self):
        touch(os.path.join(self.h.bin, "mame"), mode=0o755)
        got = self.launch("arcade", "fbneo/marble.zip", "MAME (Standalone)")
        self.assertEqual(got["start_dir"], self.h.home + "/.mame")
        self.assertEqual(got["args"], f"-rompath {self.h.roms}/fbneo\\;{self.h.roms}//arcade marble")

    def test_directory_as_file(self):
        got = self.launch("psx", "psx/Final Fantasy VII.m3u", "Beetle PSX")
        self.assertTrue(got["args"].endswith(esde.escape(self.h.rom("psx/Final Fantasy VII.m3u/Final Fantasy VII.m3u"))))

    def test_desktop_shortcut(self):
        touch(self.h.rom("ags/Game.desktop"),
              "[Desktop Entry]\nExec=/opt/game/run.sh --full %U\nPath=/opt/game\n")
        got = self.launch("ags", "ags/Game.desktop")
        self.assertEqual(got["exe"], "/opt/game/run.sh")
        self.assertEqual(got["args"], "--full")
        # The command's %STARTDIR% takes precedence over the Path key.
        self.assertEqual(got["start_dir"], self.h.roms + "/ags")

    def test_shell_script(self):
        touch(self.h.rom("ags/Game.sh"), "#!/bin/sh\n")
        got = self.launch("ags", "ags/Game.sh")
        self.assertEqual(got["exe"], self.h.bin + "/bash")
        self.assertEqual(got["args"], self.h.rom("ags/Game.sh"))

    def test_every_bundled_command_expands(self):
        # Every command either expands or names what is missing, in the copies for ARM and x86.
        self.expand_all()
        self.h.es.bundled = lambda: os.path.join(COPY, "linux")
        self.expand_all()

    def expand_all(self):
        for system in self.h.es.systems().values():
            for label, _ in system["commands"]:
                try:
                    got = self.h.es.launch(system, self.h.rom("fbneo/marble.zip"), label)
                except esde.Unresolved as e:
                    self.assertTrue(str(e))
                    continue
                self.assertNotIn("\x00", got["args"] + got["prefix"])
                self.assertNotRegex(got["args"], r"%(ROM|BASENAME|EMULATOR_|CORE_|STARTDIR|INJECT)")


if __name__ == "__main__":
    unittest.main()
