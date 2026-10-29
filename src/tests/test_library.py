"""Tests for the systems, games and shortcuts built from ES-DE. Run with: python3 -I tests/test_library.py"""
import json
import logging
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.path.insert(0, HERE)

from rom_library import esde, library  # noqa: E402
from test_esde import ARM_FILES, TUNIC, Home, touch  # noqa: E402

DEFAULTS = os.path.join(HERE, "..", "backend", "defaults")


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.h = Home()
        self.settings = os.path.join(self.h.root, "settings")
        os.makedirs(self.settings)
        library.WRAPPER = "/nonexistent/armada-game-launch"
        library.dsu.running = lambda: False
        self.lib = self.make()

    def make(self):
        lib = library.Library(DEFAULTS, self.settings, self.h.home, os.path.join(self.h.root, "runtime"),
                              logging.getLogger())
        lib.esde.bundled = lambda: ARM_FILES
        return lib

    def tearDown(self):
        self.h.cleanup()

    def test_system_list(self):
        systems = {s["id"]: s for s in self.lib.system_list()}
        self.assertEqual(sorted(systems), ["c64", "fbneo", "n64", "psp", "psx", "switch", "wii"])
        self.assertEqual(systems["fbneo"]["name"], "Arcade (FinalBurn Neo)")
        self.assertEqual(systems["fbneo"]["collection"], "Arcade")
        self.assertEqual(systems["n64"]["collection"], "Nintendo 64")
        self.assertTrue(systems["switch"]["available"])
        self.assertEqual(systems["wii"]["reason"], None)

    def test_games_use_dat_titles(self):
        games = self.lib.games("fbneo")
        # .hidden.zip is no FBNeo set.
        self.assertEqual([(g["name"], g["title"]) for g in games], [("Marble Madness", "Marble Madness (set 1)")])
        self.assertEqual([g["name"] for g in self.lib.games("n64")], ["Super Mario 64"])

    def test_shortcut(self):
        rom = self.h.rom(f"switch/{TUNIC}")
        got = self.lib.shortcut("switch", rom)
        self.assertEqual(got["name"], "TUNIC")
        self.assertEqual(got["exe"], self.h.home + "/Applications/Eden.AppImage")
        self.assertEqual(got["start_dir"], self.h.home + "/Applications/")
        self.assertEqual(got["launch_options"], "REUSE_CHECK_DELAY=0 %command% -f -g " + esde.escape(rom))
        self.assertEqual(got["collection"], "Nintendo Switch")

    def test_switch_games_stop_eden_without_a_question(self):
        ini = os.path.join(self.h.home, ".config", "eden", "qt-config.ini")
        touch(ini, "[UI]\nconfirmStop\\default=true\nconfirmStop=0\n")
        os.remove(self.h.rom("switch/updates/noload.txt"))
        self.lib.shortcut("switch", self.h.rom(f"switch/{TUNIC}"))
        with open(ini) as f:
            text = f.read()
        self.assertIn("confirmStop\\default=false\nconfirmStop=2\n", text)
        # Eden reads the updates beside the games, and ES-DE skips them.
        self.assertIn(f"Paths\\external_content_dirs\\1\\path={self.h.rom('switch/updates')}\n", text)
        self.assertTrue(os.path.exists(self.h.rom("switch/updates/noload.txt")))

    def test_wrapper_keeps_command(self):
        library.WRAPPER = os.path.join(self.h.bin, "bash")
        rom = self.h.rom(f"switch/{TUNIC}")
        got = self.lib.shortcut("switch", rom)
        self.assertEqual(got["launch_options"],
                         f"REUSE_CHECK_DELAY=0 {library.WRAPPER} %command% -f -g " + esde.escape(rom))

    def test_forget_keeps_a_newer_tile(self):
        rom = self.h.rom(f"switch/{TUNIC}")
        self.lib.record(rom, 2, "switch", {k: "" for k in library.FIELDS})
        self.lib.forget(rom, 1)
        self.assertEqual(self.lib.tiles[rom]["appid"], 2)
        self.lib.forget(rom, 2)
        self.assertNotIn(rom, self.lib.tiles)

    def test_sync_unmounts_appimages_of_older_tiles_at_exit(self):
        rom = self.h.rom(f"switch/{TUNIC}")
        old = {"name": "TUNIC", "exe": self.h.home + "/Applications/Eden.AppImage",
               "start_dir": self.h.home + "/Applications/", "launch_options": "-f -g " + esde.escape(rom)}
        self.lib.record(rom, 4177257837, "switch", old)
        item = next(i for i in self.lib.sync_plan() if i["path"] == rom)
        self.assertEqual(item["changed"], ["launch_options"])
        self.assertEqual(item["want"]["launch_options"], "REUSE_CHECK_DELAY=0 %command% -f -g " + esde.escape(rom))

    def test_env_goes_before_wrapper(self):
        library.WRAPPER = os.path.join(self.h.bin, "bash")
        rom = "wii/Luigi's Mansion (Europe).rvz"
        touch(self.h.rom("wii/Luigi's Mansion (Europe).esprefix"), "WINEDEBUG=-all")
        got = self.lib.shortcut("wii", self.h.rom(rom))
        self.assertTrue(got["launch_options"].startswith(
            f"REUSE_CHECK_DELAY=0 WINEDEBUG=-all {library.WRAPPER} %command% -b -e "))

    def test_sync_migrates_old_tiles(self):
        marble, tunic = self.h.rom("fbneo/marble.zip"), self.h.rom(f"switch/{TUNIC}")
        gone = self.h.rom("fbneo/1942.zip")
        with open(os.path.join(self.settings, "tiles.json"), "w") as f:
            json.dump({marble: 3666588897, tunic: 3201098150, gone: 1}, f)
        lib = self.make()
        plan = {item["path"]: item for item in lib.sync_plan()}
        self.assertEqual(plan[marble]["changed"], ["exe", "start_dir", "launch_options"])
        self.assertEqual(plan[marble]["collections"], ["Arcade"])
        self.assertEqual(plan[tunic]["system"], "switch")
        self.assertTrue(plan[gone]["missing"])
        lib.set_applied(marble, plan[marble]["want"])
        again = {item["path"]: item for item in self.make().sync_plan()}
        self.assertEqual(again[marble]["changed"], [])
        # A tile whose ROM is gone stays listed as missing.
        self.assertEqual([g["path"] for g in self.make().games("fbneo") if g["missing"]], [gone])

    def test_user_systems_override(self):
        with open(os.path.join(self.settings, "systems.json"), "w") as f:
            json.dump({"systems": {"n64": {"collection": "N64"}}}, f)
        systems = {s["id"]: s for s in self.lib.system_list()}
        self.assertEqual(systems["n64"]["collection"], "N64")

    def test_media_name_keeps_subfolder(self):
        game = [g for g in self.lib.games("c64")][0]
        media = self.lib.media("c64", game["path"])
        self.assertEqual(media["stem"], "defender/Side A")
        self.assertEqual(media["libretro"], "Commodore - 64")

    def test_files_follow_m3u_and_cue(self):
        m3u = self.h.rom("psx/Vagrant Story.m3u")
        touch(m3u, "#EXTM3U\nVagrant Story (Disc 1).cue\n../n64/Super Mario 64 (Europe) (En,Fr,De).zip\n")
        cue = self.h.rom("psx/Vagrant Story (Disc 1).cue")
        touch(cue, 'FILE "Vagrant Story (Disc 1).bin" BINARY\n')
        touch(self.h.rom("psx/Vagrant Story (Disc 1).bin"), "x" * 2000)
        got = self.lib.files("psx", m3u)
        # The game outside the system's folder stays.
        self.assertEqual(got["files"], ["Vagrant Story.m3u", "Vagrant Story (Disc 1).cue", "Vagrant Story (Disc 1).bin"])
        self.assertEqual(got["size"], os.path.getsize(m3u) + os.path.getsize(cue) + 2000)

    def test_uninstall_deletes_a_folder_game(self):
        game = self.h.rom("psx/Final Fantasy VII.m3u")
        touch(os.path.join(game, "Final Fantasy VII (Disc 1).chd"), "x" * 300)
        self.assertEqual(self.lib.files("psx", game)["size"], 300)
        self.lib.choose(game, "portrait", "file:/tmp/ff7.png")
        self.lib.uninstall("psx", game)
        self.assertFalse(os.path.exists(game))
        self.assertTrue(os.path.isdir(self.h.rom("psx")))
        self.assertIsNone(self.lib.choice(game, "portrait"))
        self.assertEqual([g["name"] for g in self.lib.games("psx")], [])

    def test_games_on_a_card_are_listed_and_deleted_there(self):
        card = os.path.join(self.h.root, "run/media/steamos/SDCard/Emulation/roms")
        touch(os.path.join(card, "psp/Patapon (Europe).iso"), "x" * 500)
        touch(os.path.join(card, "gba/Metroid Fusion (Europe).gba"), "x")
        self.addCleanup(setattr, esde, "CARD_ROM_DIRS", esde.CARD_ROM_DIRS)
        esde.CARD_ROM_DIRS = (os.path.join(self.h.root, "run/media/*/*/Emulation/roms"),)
        systems = self.lib.systems()
        # A system the card alone has appears, and a system both have lists both folders.
        self.assertEqual(systems["gba"]["roots"], [card + "/gba"])
        self.assertEqual(systems["psp"]["roots"], [self.h.rom("psp"), card + "/psp"])
        self.assertEqual(sorted(g["name"] for g in self.lib.games("psp")), ["LittleBigPlanet", "Patapon"])
        self.assertEqual([g["name"] for g in self.lib.games("gba")], ["Metroid Fusion"])
        game = card + "/psp/Patapon (Europe).iso"
        self.assertEqual(self.lib.system_of(game, systems), "psp")
        self.assertEqual(self.lib.files("psp", game), {"files": ["Patapon (Europe).iso"], "size": 500})
        first = self.lib.stamp("psp")
        touch(os.path.join(card, "psp/Sub/Other.iso"))
        self.assertNotEqual(self.lib.stamp("psp"), first)
        self.lib.uninstall("psp", game)
        self.assertFalse(os.path.exists(game))
        self.assertEqual([g["name"] for g in self.lib.games("psp")], ["LittleBigPlanet", "Other"])

    def test_system_list_names_emulator_and_extensions(self):
        systems = {s["id"]: s for s in self.lib.system_list()}
        self.assertEqual(systems["switch"]["emulator"], self.lib.esde.label(self.lib.systems()["switch"]["esde"]))
        self.assertTrue(systems["switch"]["emulator"])
        self.assertIn("nsp", systems["switch"]["extensions"])
        self.assertNotIn(".nsp", systems["switch"]["extensions"])

    def test_wii_games_keep_their_settings_in_dolphin(self):
        rom = self.h.rom("wii/New Super Mario Bros. Wii (Europe).iso")
        with open(rom, "wb") as f:
            f.write(b"SMNP01".ljust(0x18, b"\0") + struct.pack(">I", 0x5D1C9EA3) + b"\0" * 4
                    + b"NEW SUPER MARIO BROS. Wii".ljust(0x60, b"\0"))
        psp = self.h.rom("psp/LittleBigPlanet (Europe) (PSP) (PSN).iso")
        self.assertIsNone(self.lib.emulator_settings("psp", psp))
        self.assertEqual(self.lib.emulator_settings("wii", rom)["values"]["wiimote"], None)
        self.assertEqual(self.lib.emulator_settings("wii", rom)["console"], "wii")
        # A tile or the launcher asks for the shortcut: the game gets its sideways layout.
        self.lib.shortcut("wii", rom)
        self.assertEqual(self.lib.emulator_settings("wii", rom)["values"]["wiimote"], "Sideways Wii Remote")
        self.lib.set_emulator_setting("wii", rom, "aspect", "1")
        with open(os.path.join(self.h.home, ".local/share/dolphin-emu/GameSettings/SMNP01.ini")) as f:
            self.assertEqual(f.read(), "[Controls]\nWiimoteProfile1 = Sideways Wii Remote\n"
                                       "[Video_Settings]\nAspectRatio = 1\n")

    def test_switch_games_keep_their_gyroscope_in_eden(self):
        rom = self.h.rom("switch/The Legend of Zelda Breath of the Wild.nsp")
        touch(rom, "x")
        self.lib.decoded.facts = lambda sid, path: {"serial": "01007EF00011E000"}
        self.assertIsNone(self.lib.emulator_settings("switch", rom)["values"]["gyro"])
        self.assertIs(self.lib.emulator_settings("switch", rom)["dsu"], False)
        self.lib.set_emulator_setting("switch", rom, "gyro", "false")
        self.assertEqual(self.lib.emulator_settings("switch", rom)["values"]["gyro"], "false")
        self.lib.decoded.facts = lambda sid, path: {}
        self.assertIsNone(self.lib.emulator_settings("switch", rom))
        with self.assertRaises(ValueError):
            self.lib.set_emulator_setting("switch", rom, "gyro", "true")

    def test_stamp_changes_when_a_game_comes_or_goes(self):
        first = self.lib.stamp("fbneo")
        self.assertEqual(self.lib.stamp("fbneo"), first)
        os.remove(self.h.rom("fbneo/marble.zip"))
        second = self.lib.stamp("c64")
        self.assertNotEqual(self.lib.stamp("fbneo"), first)
        touch(self.h.rom("c64/defender/Side C.d64"))
        self.assertNotEqual(self.lib.stamp("c64"), second)

    def test_arcade_games_keep_their_settings_in_retroarch(self):
        config = os.path.join(self.h.home, ".var/app/org.libretro.RetroArch/config/retroarch")
        touch(os.path.join(config, "retroarch.cfg"), f'libretro_directory = "{config}/cores"\n')
        rom = self.h.rom("fbneo/marble.zip")
        self.assertIsNone(self.lib.emulator_settings("fbneo", rom))
        touch(os.path.join(config, "cores/fbneo_libretro.info"), 'corename = "FinalBurn Neo"\n')
        got = self.lib.emulator_settings("fbneo", rom)
        self.assertEqual((got["emulator"], got["globals"]["bezel"]), ("RetroArch", "true"))
        self.lib.set_emulator_setting("fbneo", rom, "bezel", "false")
        with open(os.path.join(config, "config/FinalBurn Neo/marble.cfg")) as f:
            self.assertEqual(f.read(), 'input_overlay_enable = "false"\n')

    def test_import_moves_a_cue_and_its_tracks(self):
        downloads = os.path.join(self.h.home, "Downloads")
        cue = os.path.join(downloads, "Vagrant Story (Disc 1).cue")
        touch(cue, 'FILE "Vagrant Story (Disc 1).bin" BINARY\n')
        touch(os.path.join(downloads, "Vagrant Story (Disc 1).bin"), "x" * 2000)
        touch(os.path.join(downloads, "notes.txt"), "keep")
        got = self.lib.import_game("psx", cue)
        self.assertEqual(got, self.h.rom("psx/Vagrant Story (Disc 1).cue"))
        self.assertTrue(os.path.isfile(self.h.rom("psx/Vagrant Story (Disc 1).bin")))
        self.assertEqual(os.listdir(downloads), ["notes.txt"])
        self.assertIn(got, [g["path"] for g in self.lib.games("psx")])

    def test_import_keeps_a_game_already_there(self):
        rom = os.path.join(self.h.home, "Downloads", "Super Mario 64 (Europe) (En,Fr,De).zip")
        touch(rom, "new")
        touch(self.h.rom("n64/Super Mario 64 (Europe) (En,Fr,De).zip"), "old")
        with self.assertRaises(FileExistsError):
            self.lib.import_game("n64", rom)
        self.assertTrue(os.path.isfile(rom))
        with self.assertRaises(ValueError):
            self.lib.import_game("n64", os.path.join(self.h.home, "Downloads", "notes.txt"))


if __name__ == "__main__":
    unittest.main()
